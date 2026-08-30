# Video script (≤5 min) — deliverable 03

Target 4:40. Nothing is minted on camera; a real mainnet receipt hash goes in the
README appendix instead. Every number shown is reproducible by the command shown.

---

## 0:00–0:40 — The problem, in one screen

> "This is the SeaDrop launchpad on Robinhood Chain. Watch what it does in
> forty-five minutes."

```bash
python -m mintscout.cli watch --chain robinhood --minutes 45
```

Say over the output:
- **~2,730 new drop configurations a day** on this chain alone. Measured, 9 days.
- **About half are free.**
- **About 4% are worth minting.**

> "So: roughly 1,400 free drops a day, and 96% of them are junk. You cannot do
> this by hand, and the good ones sell out in hours. That's the problem."

## 0:40–1:20 — The insight

Show `getPublicDrop` returning a struct with `startTime` in the **future**.

> "The instinct is to put a model in the hot path — see the drop, ask the LLM,
> mint. That can't work; the window at a mint open is seconds and an LLM
> round-trip is several of them.
>
> But SeaDrop publishes the whole drop config *before* the mint opens. That turns
> a latency race into a **scheduling problem** — and scheduling problems are
> where agents are strong. Triage runs hours early. The executor at startTime
> just reads a cached verdict."

## 1:20–1:50 — Trust the code, not the brief

```bash
python -m mintscout.verify
```

> "Twenty-two checks against live mainnet. It re-derives every event topic with
> keccak and matches it against a real committed log — the brief's topic hash was
> wrong, and a wrong topic means an indexer that silently returns nothing forever.
>
> It also proves the same SeaDrop implementation is on both chains: exactly 34
> bytes differ, and they're the chain id and the EIP-712 domain separator derived
> from it. And it asserts the chain id *baked into the bytecode* matches the chain
> we actually reached."

## 1:50–2:40 — The evaluation, and why it's honest

> "You can't evaluate a mint sniper live, and these RPCs serve no archive state —
> so you can't simulate a past block either. Everything is rebuilt from logs."

Show `results/comparison.md`. Walk the table. Then the honest part:

> "The baseline mints everything, so it has **perfect recall** and precision equal
> to the base rate. MintScout trades recall for precision. **That recall loss is
> real and it's in the table** — hiding it would read worse than stating it."

Then show the leakage control:

```bash
python -m pytest tests/ -q
```

> "This doesn't just check the dataset is clean. It **plants a post-cutoff record
> and asserts it never comes back.**"

## 2:40–3:20 — The removed experiment that mattered most

> "The most important thing I found was a bug in my own code."

Show CHANGELOG "Removed #2".

> "181 of 201 collections came back with no metadata. That's implausible, so I
> checked. My `try_call` helper caught *every* exception and returned None — so a
> rate-limited read was indistinguishable from 'this collection has no metadata'.
> A transient RPC failure had become a model feature.
>
> Worse, the fix revealed a second problem: `tokenURI` **reverts for tokens that
> were never minted**. So its availability encodes whether the drop sold — the
> exact thing we're predicting. It's excluded from the feature set entirely.
>
> The rule I'd take anywhere: **with no archive state, every `latest` read is a
> leak until proven otherwise.**"

## 3:20–4:00 — The other removed experiment: gas

```bash
python scripts/measure_gas.py
```

> "I planned to sell EIP-7702 as a gas saving. Then I measured 60 live receipts:
> a mint costs **0.0000175 ETH**, and batching five saves **0.0000143 ETH** —
> about four cents. If I'd claimed 'saves gas' as the headline, a judge with a
> calculator would have ended the submission.
>
> So I removed the gas metric and reframed 7702 around what it actually buys:
> **one nonce, one inclusion.** Five sequential mints are five nonces, and a
> revert on number two stalls three through five behind the nonce gap. With drops
> selling out in hours, a stalled nonce is a missed drop."

## 4:00–4:30 — The one gas number that survives

```bash
python -m mintscout.cli preflight --chain robinhood --n 10
```

> "Pre-flight re-reads the drop config — configs are mutable and get edited
> mid-flight — then simulates every call and drops the ones that would revert.
> That's **wasted gas avoided**, and unlike 'gas saved by batching' it survives
> arithmetic. Read-only: nothing signed, nothing sent."

## 4:30–4:40 — Close

> "Default is dry-run. Live needs an explicit flag and a spend cap set in advance.
> Coverage is measured, not claimed — SeaDrop public drops miss non-SeaDrop
> launches and allowlist phases, and both are documented with the collection that
> proves it.
>
> The thing I'd carry to the next system: before you put an agent in a real-time
> loop, find the part of the domain that's **announced in advance**. If there
> isn't one, the agent belongs offline building the priors the fast path reads."

---

### Shot list

| # | Command | Shows |
|---|---|---|
| 1 | `mintscout.cli watch` | scale of the problem, live |
| 2 | `mintscout.verify` | 22/22 against mainnet |
| 3 | `results/comparison.md` | the comparison table |
| 4 | `pytest tests/ -q` | leakage controls |
| 5 | `CHANGELOG.md` "Removed #2" | the self-caught bug |
| 6 | `scripts/measure_gas.py` | 60 receipts |
| 7 | `mintscout.cli preflight` | wasted gas avoided |
| 8 | `trajectories/*.json` | a verifier veto |
