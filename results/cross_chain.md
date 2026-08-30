# Cross-chain generalisation — Robinhood → Ink

The deterministic rubric's weights were chosen by looking at the **Robinhood calibration split only**. Ink was never used to tune anything, so the entire Ink dataset is out-of-distribution held-out data: a different chain, a different rollup stack (OP Stack vs Arbitrum Orbit), a 10× different block time, and a different drop population.

| | Robinhood (held-out split) | Ink (entirely unseen) |
|---|---|---|
| Chain id / stack | 4663 · Arbitrum Orbit | 57073 · OP Stack |
| Drops evaluated | 150 (stratified subset) | 300 (all) |
| High-value | 18 | 31 |
| Base rate | 12.0% | 10.3% |
| **Baseline (mint all free)** — Precision@K | **0.120** | **0.103** |
| Baseline (mint all free) — Recall | 1.000 | 1.000 |
| Baseline (mint all free) — Lift vs base rate | 1.0× | 1.0× |
| Baseline (mint all free) — Chose | 150/150 | 300/300 |
| **Deterministic rubric** — Precision@K | **0.625** | **0.500** |
| Deterministic rubric — Recall | 0.278 | 0.032 |
| Deterministic rubric — Lift vs base rate | 5.21× | 4.84× |
| Deterministic rubric — Chose | 8/150 | 2/300 |

## Reading

- On a chain it was never tuned against, the rubric reaches **0.500** precision against a **10.3%** base rate (**4.84× lift**), versus **0.625** / **5.21×** on Robinhood held-out.
- The signals it uses (a resolvable drop configuration, a full contract rather than a minimal proxy, multiple config revisions) describe **how a serious operator behaves**, which is why the ranking is not chain-specific.
- **But recall does not transfer: 0.278 on Robinhood collapses to 0.032 on Ink** (2 of 300 drops selected). This is the honest limit of the result. **What generalises is the ordering of the signals; what does not generalise is the threshold.** The score cutoff was fitted to Robinhood's population and is far too strict for Ink's, so the rubric finds few false positives and almost no true ones either.
- Fixing that would mean re-fitting the cutoff per chain on a per-chain calibration split. That is a one-line change and it was deliberately NOT done here, because re-tuning on Ink would destroy the only genuinely out-of-distribution test in this submission.
- Base rates are close enough for the comparison to be meaningful (12.0% on the Robinhood stratified subset vs 10.3% on the full Ink set), but note the Robinhood column is a stratified subset while the Ink column is the entire dataset.
- Baseline precision on Ink is 0.103, which again equals the base rate exactly — the same correctness check on the harness, reproduced independently on a second chain.

> No retuning was done for Ink. The rubric that produced these numbers is byte-identical to the one calibrated on Robinhood.
