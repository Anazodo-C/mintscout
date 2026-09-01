# DEPLOY.md — running MintScout live on Railway

> **Read §0 before setting `LIVE_EXECUTION=true`.** This process spends real
> funds from a wallet you control. It is designed to fail closed, but the caps
> only protect you if you set them.

---

## 0. What actually runs, and what does not

**No contract deployment is required for this mode.** That was the original
plan, and measuring the live chain showed it is unnecessary:

Real Robinhood minters send a **plain type-2 transaction** calling
`mintPublic(nftContract, feeRecipient, minterIfNotPayer, quantity)` on the
SeaDrop contract, with `minterIfNotPayer` set to their **own address** and
~205k gas. Verified by decoding live mints. Single-collection minting needs no
custom contract at all.

EIP-7702 is only needed to batch **several collections into one transaction**.
The triage-only configuration you are deploying mints one collection at a time,
so it does not touch that path. `contracts/BatchExecutor.sol` is included for
when you want batching later; it is **not deployed and not used**.

### The honest performance expectation

From the held-out evaluation, so you know what you are buying:

| | |
|---|---|
| Precision (population-corrected) | **0.279** — roughly 1 in 3.6 picks is genuinely good |
| Recall | **0.278** — it will **miss about 7 of every 10** good drops |
| Base rate without the agent | 0.050 |

It is a **~5.6× improvement on picking**, not an oracle. It is conservative by
design: it would rather skip ten mediocre drops than mint them. Judge it on
"of the things it made me mint, how many were worth it", not on coverage.

---

## 1. Railway setup

### 1.1 Create the service

1. Railway → **New Project** → **Deploy from GitHub repo** → `Anazodo-C/mintscout`.
2. Railway detects `Dockerfile` and `railway.json` automatically.
3. **Add a Volume** — Settings → Volumes → mount path **`/data`**.

> The volume is not optional. Spend state lives in `/data/spend_state.json`. If
> the container restarts without it, the "spent so far" counter resets to zero
> and your total-spend cap silently stops meaning anything. A crash-loop could
> then spend your cap repeatedly.

#### Confirming the volume is actually mounted

Checking that `/data` is *writable* proves nothing — an unmounted container path
is writable too, it just evaporates on restart. So the runner keeps a boot
counter at `/data/.volume_check.json` and prints it at startup:

```
state volume    : OK /data  boots=4  writable=True
                  state survived 3 restart(s) — volume is mounted and persisting
```

**How to read it:**

| Startup line | Meaning |
|---|---|
| `boots=1` … *first boot at this path* | Expected on a genuinely first deploy. **Restart once and look again.** |
| `boots` increments across restarts | ✅ The volume is real and persisting. |
| `boots` stays at **1** after a restart | ❌ **Not mounted.** You are writing to ephemeral storage — the spend cap will reset on every restart. Fix before going live. |
| `!!` … `NOT WRITABLE` | ❌ The path cannot be written at all; spend state is never saved. |

The same data is on `GET /status` under `volume` (read-only there — it does not
increment the counter).

So the full check is: **deploy, note `boots`, hit Restart in Railway, and confirm
`boots` went up.** In the Railway UI it should also appear under
Service → Settings → Volumes with mount path `/data`.

### 1.2 Variables

Set these in Railway → Variables. **Start in dry-run** (the defaults) and only
flip the two live switches once the logs look right.

#### Required to run at all

| Variable | Example | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-…` | Needed only if `USE_LLM=true` |
| `CHAINS` | `robinhood` | Comma-separated. `robinhood,ink` for both |

#### Required to spend (all of them, or it refuses to start)

| Variable | Suggested first run | What it does |
|---|---|---|
| `DRY_RUN` | `false` | Switch 1 of 2 |
| `LIVE_EXECUTION` | `true` | Switch 2 of 2 — **both** required |
| `MINT_SEED` | *your BIP-39 phrase* | Env-only. Never logged, never written to disk |
| `MAX_TOTAL_SPEND_WEI` | `3000000000000000` | 0.003 ETH total, all-in. **No default — unset means refuse** |
| `MAX_SPEND_PER_MINT_WEI` | `500000000000000` | 0.0005 ETH per transaction |
| `MAX_MINTS_TOTAL` | `5` | Hard count cap. **No default — unset means refuse** |
| `MAX_MINTS_PER_HOUR` | `3` | Rate cap. **Set it — unset means unlimited per hour** |

#### Tuning (sensible defaults, override to taste)

| Variable | Default | What it does |
|---|---|---|
| `USE_LLM` | `true` | `false` runs the deterministic rubric only, and costs nothing |
| `ON_LLM_FAILURE` | `deterministic` | On an LLM outage: fall back to the rubric, or `skip` to halt spending |
| `PREFILTER_MIN_SCORE` | `45` | Rubric score needed before a drop reaches the LLM |
| `MAX_QUANTITY_PER_MINT` | `2` | Capped again by the drop's own per-wallet limit |
| `MAX_GAS_PRICE_WEI` | `5000000000` | Refuse to mint into a gas spike (5 gwei) |
| `GAS_RESERVE_WEI` | `800000000000000` | Never spend the wallet below this |
| `POLL_INTERVAL_S` | `60` | Seconds between scans |
| `LOOKBACK_MINUTES` | `20` | First-scan window; afterwards it resumes from its cursor |
| `MAX_LEAD_SECONDS` | `7200` | Ignore drops opening more than 2h out |
| `WALLET_INDEX` | `0` | BIP-44 index, `m/44'/60'/0'/0/{i}` |

### 1.3 Fund the wallet

Deploy first with `DRY_RUN=true`. The startup banner prints the derived address
and its balance:

```
[wallet] 0xAbC…123  (derivation index 0)
[wallet] robinhood: 0.000000 ETH
[wallet] WARNING robinhood balance is zero — cannot mint
```

Send a **small** amount of Robinhood-chain ETH to that address. A mint costs
~0.00002 ETH all-in, so 0.005 ETH covers well over a hundred mints plus the gas
reserve. Use a throwaway seed holding only the mint budget.

---

## 2. Going live

1. Deploy with defaults. Watch the logs for a few cycles.
2. Confirm the verdicts look sane — you should see junk being skipped by name
   and by contract size.
3. Fund the wallet, confirm the balance line.
4. Set `DRY_RUN=false` **and** `LIVE_EXECUTION=true`. Redeploy.
5. The banner will show `*** LIVE MODE — this process will spend real funds ***`.

**To stop it instantly:** set `LIVE_EXECUTION=false` and redeploy, or pause the
service. It finishes the current cycle and stops cleanly on SIGTERM.

---

## 3. Reading the logs

Every drop the agent considers prints a block like this:

```
──────────────────────────────────────────────────────────────
[14:41:56] CANDIDATE  0xdd888c15824b5a538f383fbc85a5480723a41ba9
  chain=robinhood  price=FREE  cap=3  opens in 12m  window 24.0h
  NAME   'DADAYA RH'  symbol='DADAYA RH'  supply=10000
  TOOLS  standard=erc721  code_size=19658  metadata=drop_stage_config (1 stages)
         stages: Public stage
  FILTER deterministic score=88 verdict=MINT
         + drop metadata resolves (configured through the standard flow)
         + contract is a full implementation (19,658 bytes), not a 45-byte minimal proxy
         + supply 10,000 in the plausible band
  TRIAGE MINT  score=78  (2140ms)
         + large contract implementation, not minimal-proxy spam
         ! unknown deployer with no track record
         evidence: collection.code_size = 19658 [positive]
  EXECUTE quantity=2  value=FREE  max_cost=0.000136 ETH  gas=260000
         pre-flight simulation: OK
  DECISION SENT  tx=0x…
         CONFIRMED  gasUsed=101,204  actual_cost=0.000017 ETH  block=49…
```

**How to read each line**

| Line | Meaning |
|---|---|
| `CANDIDATE` | A free drop config was seen on SeaDrop, before the mint opens |
| `TOOLS` | Deterministic enrichment. `code_size=45` means an ERC-1167 minimal proxy — the strongest spam signal |
| `FILTER` | Free rubric pass. Below `PREFILTER_MIN_SCORE` it is dropped **without an LLM call** |
| `TRIAGE` | The model's verdict, with `+` reasons, `!` risk flags, and the dossier fields it cited |
| `EXECUTE` | Config re-read, cost computed, transaction simulated |
| `DECISION` | The outcome: `SENT` / `SKIP` / `ABORT` / `DEFER` / `BLOCKED by spend guard` / `DRY RUN` |

`HEARTBEAT` prints every 5 minutes with running counters and budget remaining.

**Where to intervene, by symptom**

| You see | Change |
|---|---|
| Good drops skipped at the filter | Lower `PREFILTER_MIN_SCORE` (more LLM cost) |
| Junk reaching the LLM | Raise `PREFILTER_MIN_SCORE` |
| `ABORT — repriced since queueing` | Working as intended: the drop stopped being free |
| `DEFER — window not open` | Working as intended: it will retry next cycle |
| `BLOCKED by spend guard` | A cap was hit. Read the reason before raising it |
| Everything is `code_size=45` | Normal. Most free drops genuinely are proxy spam |

`GET /status` returns the same counters as JSON, plus budget and limits. No key
material is ever exposed there.

---

## 4. Cost

| | |
|---|---|
| Gas per mint | ~0.000017 ETH measured (n=60 receipts) |
| LLM | ~$0.012 per drop that clears the pre-filter |
| Expected LLM spend | ~$1.40/day on Robinhood — the pre-filter removes ~90% |

Set `USE_LLM=false` to run the rubric alone at zero LLM cost. Given it beat the
LLM arm on F1, that is a legitimate configuration rather than a downgrade.

---

## 5. Safety model

- **Fails closed.** Both `DRY_RUN=false` and `LIVE_EXECUTION=true` are required.
  An unset or unparseable cap is treated as **zero**, not as unlimited.
- **Budget is committed before broadcast.** A crash mid-send over-counts rather
  than under-counts; over-estimates are refunded once the receipt confirms.
- **Config is re-read immediately before signing.** Drop configs are mutable and
  are edited mid-flight — a live pre-flight run caught three drops repriced from
  free to 0.0016 / 0.01 / 0.025 ETH between configuration and mint.
- **Every mint is simulated** with `eth_call` first; anything that would revert
  is dropped before it costs gas.
- **Gas reserve** stops the wallet being drained below what it needs to move
  tokens out later.
- **Keys never leave the process.** `MINT_SEED` is env-only. The private key is
  never logged, never serialised, and never returned by `/status`.

### Residual risks — read these

1. **A funded hot wallet on a server can be stolen if the host is compromised.**
   Use a throwaway seed holding only the mint budget. Never your main wallet.
2. **It will mint worthless NFTs.** Precision is 0.279 — roughly 2 in 3 picks
   will not be worth it. The caps bound the loss; they do not prevent it.
3. **No sweep is automated.** Minted tokens stay in the hot wallet until you run
   `mintscout sweep --to 0xVAULT` yourself.
4. **The eval measured decision quality, not execution.** Live execution is
   newer code than the evaluated path. Start with `MAX_MINTS_TOTAL=3`.
