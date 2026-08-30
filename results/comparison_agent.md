# MintScout — results

Dataset: `drops_robinhood.jsonl` · **150 drops** · 18 high-value (base rate **12.0%**)

Ground truth (measured strictly after each mint window closed): a drop is high-value if it filled to ≥80% of max supply, drew ≥100 unique minters, and saw ≥1 non-mint transfer within 48h of close.

**Resource parity:** every arm sees the identical dossier, the identical 20-wallet fleet and budget, and the identical executor. The only thing that differs is the triage decision.


## Headline

| Arm | Precision@K | Precision (pop-corrected) | Recall | F1 | Chose | Vetoes |
|---|---|---|---|---|---|---|
| Baseline A — mint every free drop | 0.120 | **0.050** | 1.000 | **0.095** | 150/150 | 0 |
| Deterministic rubric (no LLM) | 0.625 | **0.392** | 0.278 | **0.325** | 8/150 | 0 |
| Baseline B — single-prompt LLM | 0.000 | **0.000** | 0.000 | **0.000** | 1/150 | 0 |
| MintScout (triage only, no verifier) | 0.500 | **0.279** | 0.278 | **0.278** | 10/150 | 0 |
| **MintScout (triage + verifier)** | 0.667 | **0.436** | 0.111 | **0.177** | 3/150 | 114 |

## Full metrics

| Metric | Baseline A — mint every free drop | Deterministic rubric (no LLM) | Baseline B — single-prompt LLM | MintScout (triage only, no verifier) | **MintScout (triage + verifier)** |
|---|---|---|---|---|---|
| Drops chosen to mint | 150 | 8 | 1 | 10 | 3 |
| True positives | 18 | 5 | 0 | 5 | 2 |
| False positives | 132 | 3 | 1 | 5 | 1 |
| Precision@K | 0.12 | 0.625 | 0.0 | 0.5 | 0.6667 |
| Recall of high-value | 1.0 | 0.2778 | 0.0 | 0.2778 | 0.1111 |
| F1 | 0.2143 | 0.3846 | 0.0 | 0.3571 | 0.1905 |
| Fill efficiency (achieved/achievable) | 1.0 | 0.2727 | 0.0 | 0.2727 | 0.1091 |
| Raw fill vs 100/collection | 0.5141 | 0.5875 | 0.2 | 0.51 | 0.7333 |
| Tokens acquired from high-value drops | 1100 | 300 | 0 | 300 | 120 |
| Wallet slots spent | 2262 | 107 | 20 | 147 | 28 |
| Wallet slots wasted on false positives | 1956 | 37 | 20 | 77 | 7 |
| Verifier vetoes | 0 | 0 | 0 | 0 | 114 |
| Median decision latency (ms) | 0.0 | 0.0 | 6813.3 | 6719.7 | 9391.2 |
| Arm errors | 0 | 0 | 0 | 0 | 0 |
| Wasted gas on false positives (ETH) | 0.033425 | 0.000632 | 0.000342 | 0.001316 | 0.000120 |

## Cost per decision

- LLM calls: **412** live, **188** served from the committed cache
- Tokens: 646,080 in / 108,137 out
- A judge without an API key reproduces these numbers from `data/llm_cache/`; `--no-cache` re-runs live.

## Honest reading of these numbers

- The baseline has **perfect recall by construction** — it mints everything, so it cannot miss a good drop. Its precision (0.120) is exactly the base rate, which is also a correctness check on the harness.
- MintScout trades recall for precision: it finds 11% of the high-value drops while being 5.56× more precise. **The recall loss is real and is stated, not hidden.**
- `fill_efficiency` is graded against what was *achievable* given each drop's per-wallet cap, not against a flat 100. 39.4% of the free drops in this dataset are cap=1 and hard-ceiling at 20 tokens with a 20-wallet fleet; grading against 100 would mostly measure the cap, not the agent.
- **The evaluation is counterfactual.** Taking 100 tokens of a 1,105-supply collection would itself perturb the sellout and unique-minter signals the labels are built from. These numbers say "the agent picked the drops that did well", not "the agent would have obtained exactly this many tokens".
- Coverage is partial and measured, not claimed: SeaDrop `PublicDropUpdated` misses both non-SeaDrop launches (StonkBrokers) and non-public SeaDrop phases (DUNLAPS's allowlist mints). See CHANGELOG.

## Note on the evaluation set

The LLM arms were run on a **stratified subset of 150 drops** drawn from the 360 held-out drops, under a fixed API budget. **Every one of the 18 high-value drops is kept**; negatives are sampled at 0.386. A random subset would have held ~7 positives, and recall measured on 7 positives is noise.

Precision on that subset is therefore **inflated**, so both numbers are reported: `Precision@K` as observed, and `population-corrected` reweighted back to the true 5.0% prevalence. Recall is unaffected by the reweighting.

**The correction validates itself:** the mint-everything baseline scores its subset base rate and corrects to exactly the population base rate. All arms saw the identical subset, so parity between them holds.
