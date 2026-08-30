# MintScout

**Agentic free-mint discovery for Robinhood Chain and Ink.**
micro1 Frontier Engineering Challenge 2026 · single-entrant submission

> Every number in this README was measured against mainnet by code in this
> repository. `python -m mintscout.verify` re-checks the load-bearing ones in
> about 20 seconds. Where my measurements contradict the project brief, the
> measurement wins and the discrepancy is documented in [CHANGELOG.md](CHANGELOG.md).

---

## 1. The problem

**Who has it.** A collector minting free NFT drops on Robinhood Chain — a
chain that launched July 1 2026 and has almost no tooling.

**The bottleneck, measured.** On the SeaDrop launchpad on Robinhood Chain alone:

| Measurement | Value | Source |
|---|---|---|
| New public drop configurations | **24,587 in 9 days ≈ 2,732/day** | `data/drops_robinhood_meta.json` |
| Of those, free (price = 0) | **~53%** (616/1,170 in an 11h sample) | topic-filtered scan |
| Free drops that turn out to be worth minting | **~3%** | ground-truth labels, §4 |

That is the problem in one line: **roughly 1,400 free drops a day, and about 97%
of them are not worth minting.** Triage today means watching Discord, clicking
through a block explorer, and eyeballing the art — and the good ones close fast
(UNDEADLINES sold 6,969 tokens in 4.2 hours). The collector either mints
indiscriminately and accumulates a wallet full of spam, or misses the ones that
mattered.

**Why it is worth solving.** The scarce resource is *not gas* — a mint costs
0.0000175 ETH, a rounding error (§6). It is **attention**. Reducing ~1,400 daily
candidates to a short, evidence-backed shortlist converts an impossible manual
task into a two-minute review.

**Ethics (ground rules 04–06).** MintScout mints publicly-offered, permissionless
drops with the operator's own funds. No exploitation, no front-running of other
users, no private data. `DRY_RUN=true` is the repo default — what a judge runs is
a simulation. See §7.

---

## 2. The insight the architecture is built on

SeaDrop publishes the entire drop configuration on-chain **before the mint
opens**, as a readable struct:

```solidity
getPublicDrop(address) returns (
  uint80 mintPrice,   // 0 == free. This IS the free-mint filter.
  uint48 startTime,   // known BEFORE the mint opens
  uint48 endTime, uint16 maxTotalMintableByWallet, uint16 feeBps, bool restrictFeeRecipients )
```

Two consequences:

1. **"Is it free?" is a deterministic `eth_call`, not a heuristic.** No LLM, no
   false positives.
2. **This is a scheduling problem, not a latency race.** `startTime` is in the
   future when the config is published, which is what makes an agent viable at
   all — see the hot take in §9.

And the same SeaDrop contract is deployed at the **same address on both chains**:

```
0x00005EA00Ac477B1030CE78506496e8C2dE24bf5
```

`verify.py` proves this is the same implementation rather than asserting it: the
two deployments differ in exactly **34 bytes**, which are the 2-byte chain id at
offset 14462 and the 32-byte cached EIP-712 domain separator at 14645 that
derives from it. Masking those, the bytecode is identical. The baked-in chain id
is then asserted to equal the chain actually reached — proof the deployment
belongs to that chain and is not a copy from another network.

---

## 3. Architecture

```
WATCHER (deterministic, no LLM)
  poll SeaDrop for PublicDropUpdated / DropURIUpdated / SeaDropMint
  per-chain adaptive log windows, cursor persisted
        │  mintPrice <= DUST → candidate
ENRICHER (8 deterministic tools, all cached, all recorded as trajectory steps)
  erc165_probe · collection_stats · metadata_fetch · erc6551_probe
  economics · drop_config_history · mint_velocity · deployer_history
        │
TRIAGE AGENT (LLM, temperature 0, strict JSON)
  verdict MINT|WATCH|SKIP + score + reasons + evidence + risk_flags
        │
VERIFIER (LLM, SEPARATE call — adversarial, can veto)
  audits each evidence claim against the dossier; catches fabrication,
  outcome leakage, and MINT verdicts justified only by absence of negatives
        │
SCHEDULER  → priority queue on startTime; groups OVERLAPPING windows into
             one per-wallet 7702 batch; enforces budget + wallet caps
        │
APPROVAL GATE (human)  DRY_RUN default; live needs --live + explicit caps
        │
EXECUTOR (deterministic, fast)
  re-read getPublicDrop → eth_call each mint → drop the doomed ones →
  sign ONE EIP-7702 type-4 transaction
```

**The governing principle: no language model is ever in the latency path.** The
agent's output is a cached verdict written minutes-to-hours early; the executor
at `startTime` reads it in microseconds.

---

## 4. Evaluation

### The methodological problem

You cannot evaluate a mint sniper live: runs are non-deterministic, cost money,
and a judge cannot re-run them. Worse, **the public RPCs serve no archive
state** — `eth_call` at a historical block returns `metadata is not found` — so
you cannot simulate against a past block either. `rpc.py` refuses historical
`eth_call`/`eth_getCode` outright so this can never be done by accident.

**Solution: a log-derived replay harness.** Everything needed exists in
`eth_getLogs` output, which *is* served historically. The dataset is committed;
the eval is a pure function over it and runs offline.

### Ground truth

Computed strictly after each mint window closed, from logs alone. High-value iff:

1. `total_minted / max_supply >= 0.80`, **and**
2. `unique_minters >= 100`, **and**
3. ≥1 non-mint `Transfer` within 48h of `endTime` (someone actually traded it).

Sanity check: UNDEADLINES labels high-value with **2,734 unique minters** —
matching the independent research figure exactly.

### The decision cutoff is enforced in code

`ReplayContext` holds the record privately and exposes only a filtered view.
There is no code path from it to the `outcome` block; both `outcome` and
`config_revisions_all` raise `LeakageError` on access.

`tests/test_no_leakage.py` does not merely check that the dataset is clean — it
**plants a post-cutoff record and asserts it never comes back**, asserts the
dossier handed to the LLM contains no outcome field names, and asserts
`mint_velocity` is empty at every pre-start decision point.

> The hardest-won lesson of this build is in CHANGELOG "Removed #2": with no
> archive state, **every `latest` read is a leak until proven otherwise.**
> `tokenURI(1)` reverts for never-minted tokens, so its availability encodes
> whether the drop sold. It is excluded from the feature set for that reason.

### Arms (resource parity is mandatory)

Every arm sees the identical dossier, the identical 20-wallet fleet and budget,
and the identical executor. **Only the triage decision differs.**

| Arm | What it isolates |
|---|---|
| **A — mint every free drop** | The fair baseline: what a naive script, and the Minipengs bot generalised, does |
| **Deterministic rubric** | How far hand-written rules get *without* a model |
| **B — single-prompt LLM** | Model contribution with no tools, no memory, no verifier |
| **MintScout (no verifier)** | Contribution of enrichment tools |
| **MintScout** | Contribution of the adversarial verifier |

Including the deterministic arm is deliberate: it answers the question a judge
should ask — *how much of this actually needs an LLM?*

### Cross-chain generalisation (the strongest test in here)

The deterministic rubric's weights were chosen by looking at the **Robinhood
calibration split only**. **Ink was never used to tune anything**, so the entire
Ink dataset is out-of-distribution held-out data — a different chain, a different
rollup stack (OP Stack vs Arbitrum Orbit), a 10× different block time, and a
different drop population.

If precision survives that, the signals describe *how a serious operator behaves*
rather than one chain's quirks. If it collapses, that is worth knowing — and it is
reported either way, without retuning.

📊 **Results: [`results/comparison.md`](results/comparison.md)** ·
**[`results/cross_chain.md`](results/cross_chain.md)** ·
trajectories in [`trajectories/`](trajectories/) — the five `mintscout_*` files are
all **verdict-changing verifier vetoes**, including two where the verifier
destroyed a correct MINT. Those failures are the point, not an oversight; see
CHANGELOG "Removed experiment #3".

---

## 5. Measured coverage limits

SeaDrop `PublicDropUpdated` discovery is **incomplete in two distinct ways**,
both found by measurement and neither hidden:

1. **Non-SeaDrop launches.** `StonkBrokers` — 4,444 supply, fully on-chain,
   ERC-6551 — minted through its own contract. `getPublicDrop()` returns all
   zeros; it emits **0** `PublicDropUpdated` events. Invisible by construction.
2. **Non-public SeaDrop phases.** The brief states DUNLAPS "ran a free phase then
   repriced to 0.002 ETH". It did not, as a *public* drop: all **14** config
   revisions are priced (0.005 → 0.0035 → 0.002 ETH). Its free mints came from an
   allowlist/signed phase, which `PublicDropUpdated` does not cover.

MintScout covers the public-drop surface of one launchpad — which is where
~2,700 configurations/day on Robinhood actually appear. The other two surfaces
are named as extension points rather than claimed as covered.

---

## 6. EIP-7702: what it is actually worth here

I measured the gas argument and **it does not survive contact with the numbers**
(`scripts/measure_gas.py`, n=60 live receipts):

```
median gasUsed 100,254 · median fee 0.0000175 ETH
batching 5 mints instead of 5 transactions saves 0.0000143 ETH  (~4 cents)
```

**So the gas-savings framing was removed.** What 7702 is genuinely worth:

1. **One nonce, one inclusion.** Five sequential mints are five nonces; a revert
   on #2 stalls #3–5 behind the nonce gap. With drops selling out in hours, a
   stalled nonce is a missed drop.
2. **Atomic mint-and-sweep.** `mintPublic` + `transferFrom → vault` in one
   transaction — a *custody* property, since the hot wallet holds keys on a server.
3. **Batching across collections, not quantity.** 30% of Robinhood free drops are
   cap=1, so quantity-batching is impossible; only cross-collection batching helps,
   which is exactly what 7702 gives and a multi-quantity `mint()` cannot.

Measured constraint: **`payer == minter` in 94,872 of 96,278 live mints (98.5%)**,
so every wallet must fund and sign its own transaction — 7702 cannot batch
*across* wallets, only within one.

**The one honest gas number** is *wasted gas avoided* by pre-flight simulation
dropping certain-to-revert calls. Try it live, read-only:

```bash
python -m mintscout.cli preflight --chain robinhood --n 10
```

---

## 7. Safety model (ground rules 04–08)

- **`DRY_RUN=true` is the repo default.** What a judge runs is a simulation.
- **Live mode requires `--live` plus `MAX_TOTAL_SPEND_WEI` and `MAX_WALLETS` set
  in advance.** Approval is of a *bounded policy envelope*, not of each
  transaction — that is what unattended operation across overnight drops
  requires, and saying so plainly is more honest than implying a human approves
  every mint.
- **Keys.** `MINT_SEED` is a throwaway BIP-39 mnemonic, env-only, never written
  to disk or logged. `.env` is gitignored and `scripts/scrub_secrets.py` blocks
  mnemonics and private keys at pre-commit. Trajectories redact key material.
- **Gas reserve.** Every wallet retains enough for its own sweep transaction;
  the planner refuses to spend below it. Stranded NFTs in an ungassed wallet is
  the obvious failure mode of a fleet, and it is designed out.
- **Multi-wallet policy, disclosed plainly.** Default **20** wallets, user-editable.
  Why 20: wallets 21+ only help the cap≤4 band, while each adds a key to manage,
  a funding transaction and a sweep transaction. **39.4%** of the free drops in this
  dataset are cap=1 and hard-ceiling at 20 tokens regardless of fleet size.
- Only publicly-offered permissionless mints, operator's own funds, no private
  data, no front-running of other users.

---

## 8. Scope: what was cut, and why

- **ERC-404 — cut.** No ratified interface id exists, so it cannot be probed via
  ERC-165, and the reference example is on Ethereum L1, not a target chain. The
  enricher records `standard: "unknown"` plus
  `answers_ownerof_and_balanceof` for contracts answering both — which is exactly
  where 404 support would attach. A stated non-implementation beats a
  half-working detector.
- **ERC-1155 — probe only.** Three lines of interface check; no separate
  execution path, since none appeared in the dataset — **701/701 were ERC-721**.
- **Non-SeaDrop launchpads — cut**, extension point named (§5).
- **Base (8453) — excluded**, documented: its public RPC rate-limits far too
  aggressively for a reproducible offline dataset build.
- **TypeScript/viem signer — cut.** The brief recommends a thin TS executor
  because hand-rolling EIP-7702 authorization RLP in Python is risky. That is no
  longer true: `eth-account` 0.13.7 ships `sign_authorization`,
  `SignedSetCodeAuthorization` and `SetCodeTransaction`. Dropping it removes an
  entire toolchain from the reproduction steps.

**Pre-existing work (ground rule 02).** The `minipengs-bot` (TypeScript, one
hardcoded collection, five wallets, fixed quantity) and its build spec predate
this competition. **No file from it is used here.** MintScout is new code. The
relationship is conceptual: Minipengs is the manual, single-collection version of
the problem MintScout generalises — it cannot answer *which* collection to mint.

---

## 9. Hot take

> The instinct is to put the model in the hot path: see the drop, ask the LLM,
> mint. That cannot work — the competitive window at a mint open is seconds and
> an LLM round-trip is several of them. The subtler failure is worse: trigger on
> `SeaDropMint` and you only notice a drop once other people are already minting it.
>
> The fix was not a faster model. It was **reading the schedule.** SeaDrop
> publishes `startTime` in `PublicDropUpdated` before the mint opens, which turns
> a latency race into a scheduling problem — and scheduling problems are where
> agents are strong. You get minutes of slack to run tools, cross-check evidence,
> and let a verifier argue with the triage agent, then hand a cached verdict to a
> dumb, fast executor.
>
> **Before adding an agent to any real-time system, find the part of the domain
> that is announced in advance.** If nothing is, the agent belongs offline
> building the priors the fast path consumes — not in the request path.

---

## 10. Reproduce

See **[REPRODUCE.md](REPRODUCE.md)**. Short version — no API key needed for the
headline numbers, because the LLM cache is committed:

```bash
python -m mintscout.verify        # ~20s, asserts chain ids + SeaDrop bytecode + topics
python -m mintscout.eval.run --arms baseline_mint_all,deterministic
python -m mintscout.report        # writes results/comparison.md
```

## Documents

| File | What |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | Deliverable 01 — iterations, **two removed experiments**, six corrections to the brief |
| [REPRODUCE.md](REPRODUCE.md) | Deliverable 02 |
| [results/comparison.md](results/comparison.md) | The comparison table (Robinhood, held-out) |
| [results/cross_chain.md](results/cross_chain.md) | Robinhood-calibrated rubric applied to Ink with zero retuning |
| [results/preflight_demo.txt](results/preflight_demo.txt) | Live read-only pre-flight: 3 drops caught mid-reprice |
| [trajectories/](trajectories/) | Deliverable 04 — including verifier vetoes |
