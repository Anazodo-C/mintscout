# MintScout — Improvement Changelog

Maintained from Phase 1 onward, not retrofitted. Every "Evidence" cell is a
number this repo can reproduce (`scripts/`, `results/`, or `python -m
mintscout.verify`).

**Pre-existing work disclosure (ground rule 02).** The `minipengs-bot`
(TypeScript, one hardcoded collection, five wallets, fixed quantity) and its
build spec existed before the competition. **No file from it is used here.**
MintScout is a new codebase written from scratch in this repository. The
relationship is conceptual only: Minipengs is the manual, single-collection
version of the problem MintScout generalises — it cannot answer *which*
collection to mint, which is the entire question here.

---

## Stage table

| # | Stage | What I tried and why | Evidence | Decision / learning |
|---|---|---|---|---|
| 0 | Verify the brief before building on it | Both source documents assert constants (bytecode size, topic hashes, RPC limits). Indexers fail silently on stale constants, so I asserted every one against mainnet before writing a line of pipeline code. | `python -m mintscout.verify` → 22/22 checks | **Four spec errors found.** See "Corrections" below. Building on the documented values would have hard-failed at startup (size assert) or silently returned zero logs (topic hash). |
| 1 | Derive every topic/selector with keccak at import, assert against a real log | The guide itself supplies `PublicDropUpdated = 0x1c4c8b9b…` "compute at build time, do not hardcode". | Derived `0x3e30d8e1f739…`; matched topic0 of a real log at block 49450834. The documented value is wrong. | Kept. `data/fixtures/topics.json` pins a real log per event; `tests/test_constants.py` fails if any drifts. |
| 2 | Adaptive per-chain `getLogs` windowing | Spec says "10,000 block cap on both chains". Measured: that is true only for Ink. | Ink: `-32602 block range greater than 10000 max` at exactly 10,001. Robinhood: 200k blocks fine, 1M → `-32000 log query timed out`, and a *separate* result cap of 10,000 logs. | Kept. Robinhood windows raised 10k→120k, cutting a 9-day backfill from ~1,300 requests to 65. Errors are classified as range-reducible (halve the window) vs transient (retry), because retrying an oversized query can never succeed. |
| 3 | Time-localised chunking in the dataset builder | First build fetched every collection's logs across the whole scan window. | Transfer volume for 201 collections: 192,841 logs. | Kept. Sorting collections by drop window before chunking means each query covers only the blocks that can contain its events. ~10x less data fetched for identical information. |
| 4 | Baseline A — mint every free drop | The fair baseline: what a naive script, and the Minipengs bot generalised, does. | Precision@K = **0.050**, recall = **1.000** on 360 held-out drops | Established. Precision equals the base rate exactly, as it must — a useful correctness check on the harness itself. |
| 5 | Ground-truth labels from logs only | No archive state on the public RPCs, so labels must be computable from logs, transactions and receipts alone. | 3 criteria (≥80% fill, ≥100 unique minters, ≥1 secondary transfer in 48h). UNDEADLINES: fill 1.00, **2,734 unique minters** — matching the research doc exactly. | Kept. Labels are a pure function of committed data, so the eval runs offline. |
| 5b | **Held-out calibration/test split** | The deterministic rubric is hand-tuned. Scoring it on the drops it was tuned against measures memorisation, not skill. | Stable hash split on collection address: calib n=341 (3.8% base rate), test n=360 (5.0%) | Kept. Rubric tuned on `calib` only; **every arm reported on `test`**. The gap this exposed is real and is published: precision **0.571 on calibration → 0.333 on held-out**. Without the split I would have reported the 0.571. |
| 6 | Deterministic rubric (no LLM) | Before adding a model, find out how far hand-written rules get. Reporting this honestly is worth more than a bigger headline number. | Held-out precision **0.333** vs baseline **0.050** — **6.7× lift**, recall 0.278 | Kept as its own arm — it isolates how much of any gain actually needs a language model. |
| 6b | Pinned metadata, then *measured* which rubric signals actually exist | The published rubric leans on on-chain art, ERC-6551 and trait counts. I pinned all 553 off-chain metadata documents and measured those signals. | **546/553 DropURIs resolve to OpenSea drop-stage config, not token art metadata.** On-chain-art, ERC-6551 and ≥3-attributes each measured **0.000 for BOTH classes** | **Removed those three signals from the deterministic rubric.** They are unobservable at decision time on this dataset, so scoring them is scoring noise. What replaced them: presence of a resolvable drop config (P=1.00 high vs 0.45 low), `code_size > 6000` (25× lift — most spam is a 45-byte ERC-1167 minimal proxy), and ≥3 config revisions (5× lift). |
| 6c | Cross-run deployer memory, replayed in time order | Memory is only honest if a deployer's record contains solely drops that had *closed* before the drop being judged opened. | 137/360 held-out drops have a prior deployer record | Kept. Outcomes are replayed in `end_time` order and interleaved with decisions in `cutoff` order, exactly as they would have arrived live — a naive backfill would let a deployer's record include the very drop being judged. |
| 7 | LLM triage + adversarial verifier | Two separate calls: the verifier sees the dossier and the finished verdict and is instructed to find the reason it is wrong. Separate call, not a second turn, so it cannot be anchored by triage's reasoning. | See `results/comparison.md`, `trajectories/` | Verifier disagreements are the most interesting artefact the system produces and are surfaced, not buried. |

---

## Removed experiments

The brief asks for these explicitly. Both are real removals with measurements attached.

### ❌ Removed #1 — the gas-savings framing for EIP-7702

**What I tried.** The original plan justified EIP-7702 batching as a gas saving
across multiple mints.

**Evidence that killed it.** `scripts/measure_gas.py`, n=**60** live SeaDrop mint
receipts on Robinhood Chain:

```
median gasUsed              100,254
median effective gas price  0.170 gwei
median fee per mint         0.0000175 ETH
intrinsic overhead saved per extra mint in a batch   0.0000036 ETH
batching 5 mints instead of 5 transactions saves     0.0000143 ETH
```

Batching five mints saves roughly **four cents' worth of ETH at any plausible
price**. A judge with a calculator would end the submission, and *Measured
Improvement* explicitly invites them to check.

**Decision. Removed the gas metric entirely.** EIP-7702 is reframed around three
properties that are real and measurable:

1. **One nonce, one inclusion.** Five sequential mints from one EOA are five
   nonces; if mint #2 reverts (sold out, phase closed, cap hit), #3–5 stall
   behind the nonce gap. A type-4 batch is one nonce.
2. **Atomic mint-and-sweep.** `mintPublic` + `transferFrom → vault` in one
   transaction, so the NFT is never idle in a hot wallet between two inclusions.
   That is a *custody* property, not an economic one.
3. **Batching across collections, not quantity.** Per-wallet caps make
   quantity-batching impossible anyway — 30% of Robinhood free drops are cap=1.
   Batching only makes sense *across* concurrently-open collections, which is
   exactly what 7702 provides and a multi-quantity `mint()` cannot.

**Learning: on cheap L2s, batching is a reliability primitive, not an economic
one.** The one genuine gas number is *wasted gas avoided* by pre-flight
simulation dropping doomed calls — reported separately, because that claim
survives arithmetic.

### ❌ Removed #2 — `tokenURI(1)` as a metadata feature

Two separate problems, found by refusing to accept a suspicious number at face
value. The first was a bug in my own code; the second is a real leak.

**The symptom.** On the first 201-drop build, **181 of 201 collections recorded
no metadata at all**. That is implausible for a launchpad where collections pay
to configure a drop, so I checked it instead of shipping it.

**Problem A — an exception-swallowing helper fabricated a feature.**
`rpc.try_call()` was written as "eth_call that returns None instead of raising",
catching *every* exception:

```python
try:    return self.call(to, data) or None
except Exception:  return None          # <-- the bug
```

Reading `tokenURI(1)` on UNDEADLINES directly returns a **13,321-character
on-chain base64 data URI** — the call works perfectly. The 181 "missing" values
were rate-limit and timeout failures during a static-read pass that ran at full
fan-out *while a 9-day log backfill was saturating the same RPC*. A transient
infrastructure failure had been silently converted into the model feature
"this collection has no metadata", and it would have been fed to the agent as
evidence.

**Fix.** `try_call` now returns `None` only for a genuine revert and re-raises
transport failures; `_read_static` records `_read_errors`/`_read_ok` per
collection so a *failed* read is distinguishable from an *absent* value. Static
reads moved to their own low-concurrency pass (`scripts/refresh_statics.py`),
separate from the log backfill, so a static-read bug never costs a refetch of
9 days of logs.

**Learning: a helper that turns every failure into a default value will quietly
manufacture evidence.** In an evaluated system that is worse than crashing,
because the number it produces still looks like a result.

**Problem B — `tokenURI` is genuinely leaky, independent of the bug.**
Even with the bug fixed, `tokenURI` cannot be used as a feature:

- With no archive state it can only be read at `latest`.
- It **reverts for a token that has never been minted** — confirmed directly:
  `tokenURI(0)` reverts on UNDEADLINES while `tokenURI(1)` succeeds.
- So for a drop that never sold, `tokenURI(1)` reverts. Its availability encodes
  *whether the drop minted at all* — precisely the outcome being predicted.

**Decision. `tokenURI` stays out of the feature set** (retained in the dataset as
`token_uri_1_EXCLUDED_LEAKY` for inspection, never read by a tool). Metadata now
comes only from sources that cannot leak:

| Source | Why it is safe |
|---|---|
| `DropURIUpdated` | Log-derived, carries a block timestamp the `ReplayContext` has already filtered to `<= cutoff` |
| `contractURI()` | Collection-level, set at deploy, independent of whether any token was minted |
| `code_size` | Deploy-time constant. Used as the leak-free proxy for "fully on-chain art", which is otherwise only visible through `tokenURI` — generating SVG on-chain requires substantially more bytecode |

**Learning: with no archive state, every `latest` read is a leak until proven
otherwise.** The admissibility rule now applied throughout: a feature qualifies
only if it is log-derived with a timestamp, or provably fixed at deploy time.
`tests/test_no_leakage.py` plants a post-cutoff record and asserts it never
comes back.

| 8 | Pre-flight: stop retrying reverts | The live pre-flight demo took over two minutes for twelve simulations. | An `execution reverted` (code 3) was being retried 5× with backoff | **Fixed.** A revert is a deterministic answer, not a transient failure — it will revert identically every time. Raising immediately took the same demo from **2m+ to 13.6s**. Three distinct failure classes are now handled distinctly: range-reducible (shrink the window), transient (retry), deterministic (raise at once). |

---

## The pre-flight number, measured live

`python -m mintscout.cli preflight --chain robinhood --n 12` — read-only, nothing signed:

```
kept 0 / 12
wasted gas avoided: 1,203,048 gas = 0.00020506 ETH
```

Of twelve drops that were **free when configured**, pre-flight dropped all twelve:

- **9** would have reverted (phase not open, sold out, cap hit) — caught by `eth_call`.
- **3 had been repriced between configuration and mint** — caught by re-reading
  `getPublicDrop()` immediately before signing: **0.0016, 0.01 and 0.025 ETH**.

That third category is the entire justification for treating `PublicDropUpdated`
as upsert and re-reading the config at signing time rather than trusting the
queued value. It is not a hypothetical: it happened three times in a single
60-minute window.

---

## Corrections to the source research

Found by measurement; each would have caused a real failure.

| Claim in `guide.md` / `BUILD.md` | Measured reality | Consequence if trusted |
|---|---|---|
| SeaDrop code size **21,082** | **21,081** on both chains | `verify.py`'s startup assert fails immediately |
| Bytecode **"byte-for-byte identical"** across chains | **34 bytes differ**, in exactly 2 runs: the 2-byte chain id at offset 14462 and the 32-byte cached EIP-712 domain separator at 14645 (which derives from it) | A naive hash-equality assert fails. The corrected claim is *stronger*: same implementation, differing only in deploy-time immutables — and the baked-in chain id is now asserted to equal the chain actually reached, proving the deployment belongs to that chain |
| `PublicDropUpdated` topic0 ≈ `0x1c4c8b9b…` | `0x3e30d8e1f739ea4795c481b21c23f905e938b80339305f3508e43c558e5dead3` | Watcher silently returns zero logs forever — the #1 indexer failure mode, which the guide itself warns about |
| `eth_getLogs` capped at 10,000 blocks **on both chains** | Ink: hard 10,000-block cap. Robinhood: **no block cap**, but a 10,000-*result* cap and a query timeout | 12x more requests than necessary on Robinhood; and the result cap is missed entirely, so wide unfiltered queries fail in a way that block-splitting alone does not fix |
| **DUNLAPS** "ran a free phase then repriced to 0.002 ETH" | **No free public phase exists.** All **14** `PublicDropUpdated` revisions are priced: 0.005 → 0.0035 → 0.002 ETH. The observed 0.0-value mints came from an allowlist/signed phase, which `PublicDropUpdated` does not cover | DUNLAPS is legitimately *not* a free-drop candidate. More importantly this exposes a **second discovery gap** (see coverage below) |
| Public RPCs are openly queryable | Both **403 any request without a `User-Agent`** (curl's default passes, python-urllib's does not) | Every request fails with an error that looks like a network problem |

---

## Measured coverage limits (stated, not hidden)

SeaDrop `PublicDropUpdated` discovery is **incomplete**, in two distinct ways,
both found by measurement:

1. **Non-SeaDrop launches.** `StonkBrokers`
   (`0x539cdd042c2f3d93ebc5be7dfff0c79f3b4fabf0`) is a 4,444-supply, fully
   on-chain, ERC-6551 collection that minted via its own contract.
   `getPublicDrop()` returns all zeros and it emits **0** `PublicDropUpdated`
   events. It is invisible to this system by construction.
2. **Non-public SeaDrop phases.** DUNLAPS's free mints happened through an
   allowlist/signed phase, not a public drop. Free mints can therefore exist on
   SeaDrop itself without ever appearing in `PublicDropUpdated`.

The README reports *measured* coverage rather than claiming completeness. The
honest framing: MintScout covers the public-drop surface of one launchpad, which
is where ~2,700 configurations/day on Robinhood actually appear, and the
extension point for the other two surfaces is named.

---

## Scale of the problem (measured, not estimated)

| Measurement | Value | How |
|---|---|---|
| `PublicDropUpdated` on Robinhood | **24,587 in 9 days ≈ 2,732/day** | 65 windows over 5.1M blocks |
| Fraction of configs that are free | **616 / 1,170 ≈ 53%** in an 11h sample | topic-filtered scan |
| `payer == minter` on live mints | **94,872 / 96,278 = 98.5%** | confirms every wallet must sign and fund its own mint, so 7702 cannot batch *across* wallets |
| Base rate of high-value free drops | **~3%** | 6 / 201 on the first full build |

That last number is the problem statement in one figure: **97% of free drops are
not worth minting**, and there are ~2,700 new configurations a day.
