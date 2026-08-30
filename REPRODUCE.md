# REPRODUCE.md

Deliverable 02. Target: a clean environment reaches the headline numbers in
**under 10 minutes**, with **no API key** and **no network** for the eval itself.

---

## 0. What you need

| | |
|---|---|
| Python | 3.10+ (developed on 3.12.7) |
| Network | Only for `verify` and rebuilding the dataset. **The eval runs fully offline.** |
| API key | **Not required.** The LLM cache is committed, so the agent arms replay byte-identically. Set `ANTHROPIC_API_KEY` only to re-run live with `--no-cache`. |
| Node/npm | **Not required.** EIP-7702 signing is pure Python via `eth-account`. |

```bash
cd mintscout
pip install -r requirements.txt      # or: uv pip install -r requirements.txt
cp .env.example .env                 # nothing needs filling in to reproduce
```

---

## 1. Verify the environment (~20s, needs network)

Asserts every load-bearing constant against **live mainnet** rather than trusting
this repo's own literals.

```bash
python -m mintscout.verify
```

Expected: **22/22 checks pass**, covering

- both chain ids (Robinhood 4663, Ink 57073);
- SeaDrop code size **21,081 bytes** on both chains;
- the two deployments are the **same implementation** — identical after masking
  the 2-byte chain id at offset 14462 and the 32-byte EIP-712 domain separator
  at 14645, the only 34 bytes that differ;
- the **chain id baked into the bytecode equals the chain actually reached**;
- every event topic derived by keccak matches **topic0 of a real committed log**
  (`data/fixtures/topics.json`);
- `getPublicDrop(UNDEADLINES)` still reads `mintPrice = 0`, `cap = 2`.

> If this fails, do not trust anything downstream — that is the point of it.

---

## 2. Reproduce the headline numbers (~2 min, offline, no key)

```bash
python -m mintscout.eval.run --arms baseline_mint_all,deterministic
python -m mintscout.report
cat results/comparison.md
```

Reads only the committed `data/drops_robinhood.jsonl`. No network, no API key,
no flakiness, no drift.

## 3. Reproduce the agent arms (~5 min, offline via the committed cache)

```bash
python -m mintscout.eval.run \
  --arms baseline_mint_all,deterministic,single_prompt_llm,mintscout_no_verifier,mintscout
python -m mintscout.report
```

Every prompt is keyed by `sha256(model, temperature, system, messages)`, so a
cache hit is **byte-identical** to the original live response. `temperature=0`
throughout.

To re-run live instead (needs `ANTHROPIC_API_KEY`):

```bash
python -m mintscout.eval.run --arms mintscout --no-cache
```

## 4. Verify the leakage controls (~5s)

The integrity of every number above depends on the agent never seeing
post-cutoff data. This is asserted, not assumed:

```bash
python -m pytest tests/ -q
```

`tests/test_no_leakage.py` **plants a post-cutoff record and asserts it never
comes back**, asserts `outcome` is unreachable through `ReplayContext`, asserts
the dossier handed to the LLM contains no outcome field names, and asserts
`mint_velocity` is empty at every pre-start decision point.

---

## 5. Optional: live, read-only demos (needs network, no keys, spends nothing)

```bash
# what the watcher sees right now
python -m mintscout.cli watch --chain robinhood --minutes 45

# pre-flight simulation: the one defensible gas number
python -m mintscout.cli preflight --chain robinhood --n 10

# the wallet fill planner (pure function, no network)
python -m mintscout.cli plan --cap 2 --supply 6969
```

`preflight` is `eth_call` only — nothing is signed and nothing is sent.

---

## 6. Optional: rebuild the dataset from chain (~35–50 min, network-heavy)

Not needed to reproduce anything; included so the dataset is not a black box.

```bash
python -m mintscout.cli dataset --chains robinhood --days 9 --sample 700 \
       --out data/drops_robinhood.jsonl
python scripts/refresh_statics.py --workers 4 --pin
python scripts/measure_gas.py
python scripts/build_fixtures.py
```

Note it will **not** reproduce byte-identically: it re-samples from a moving
9-day window against a live chain. The committed JSONL is the frozen artifact
the eval is defined over.

---

## Runtime and cost

| Step | Time | Network | Key |
|---|---|---|---|
| `verify` | ~20s | yes | no |
| eval, deterministic arms | ~2s | **no** | no |
| eval, all five arms (cached) | ~1–2 min | **no** | no |
| eval, all five arms (`--no-cache`) | ~6 min | yes | yes |
| `pytest` | ~5s | **no** | no |
| dataset rebuild | 35–50 min | yes | no |

Token cost for a full live re-run is reported in `results/metrics.json` under
`llm`, and in `results/comparison.md`.

## Determinism

- `temperature=0` on every LLM call.
- Dataset sampling seeded (`--seed 1337`).
- Every derived constant re-asserted at startup against committed fixtures.
- Labels are a pure function of committed data.
- Timestamp interpolation error is **measured**, not assumed: `BlockClock.calibrate()`
  reports max error against held-out real blocks (Robinhood ~25s, Ink 0s) — negligible
  against a 48h label window, and printed during every dataset build.
