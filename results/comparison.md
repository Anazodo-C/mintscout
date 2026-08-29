# MintScout — results

Dataset: `drops_robinhood.jsonl` · **201 drops** · 6 high-value (base rate **3.0%**)

Ground truth (measured strictly after each mint window closed): a drop is high-value if it filled to ≥80% of max supply, drew ≥100 unique minters, and saw ≥1 non-mint transfer within 48h of close.

**Resource parity:** every arm sees the identical dossier, the identical 20-wallet fleet and budget, and the identical executor. The only thing that differs is the triage decision.


## Headline

| Arm | Precision@K | Recall | Lift vs base rate | Chose | Wallet slots wasted | Wasted gas (ETH) |
|---|---|---|---|---|---|---|
| Baseline A — mint every free drop | **0.030** | 1.000 | 1.0× | 201/201 | 3065 | 0.052375 |
| Deterministic rubric (no LLM) | **1.000** | 0.167 | 33.5× | 1/201 | 0 | 0.000000 |

## Full metrics

| Metric | Baseline A — mint every free drop | Deterministic rubric (no LLM) |
|---|---|---|
| Drops chosen to mint | 201 | 1 |
| True positives | 6 | 1 |
| False positives | 195 | 0 |
| Precision@K | 0.0299 | 1.0 |
| Recall of high-value | 1.0 | 0.1667 |
| F1 | 0.058 | 0.2857 |
| Fill efficiency (achieved/achievable) | 1.0 | 0.1053 |
| Raw fill vs 100/collection | 0.516 | 0.4 |
| Tokens acquired from high-value drops | 380 | 40 |
| Wallet slots spent | 3155 | 20 |
| Wallet slots wasted on false positives | 3065 | 0 |
| Verifier vetoes | 0 | 0 |
| Median decision latency (ms) | 0.0 | 0.0 |
| Arm errors | 0 | 0 |
| Wasted gas on false positives (ETH) | 0.052375 | 0.000000 |

## Honest reading of these numbers

- The baseline has **perfect recall by construction** — it mints everything, so it cannot miss a good drop. Its precision (0.030) is exactly the base rate, which is also a correctness check on the harness.
- MintScout trades recall for precision: it finds 17% of the high-value drops while being 33.5× more precise. **The recall loss is real and is stated, not hidden.**
- `fill_efficiency` is graded against what was *achievable* given each drop's per-wallet cap, not against a flat 100. 30% of Robinhood free drops are cap=1 and hard-ceiling at 20 tokens with a 20-wallet fleet; grading against 100 would mostly measure the cap, not the agent.
- **The evaluation is counterfactual.** Taking 100 tokens of a 1,105-supply collection would itself perturb the sellout and unique-minter signals the labels are built from. These numbers say "the agent picked the drops that did well", not "the agent would have obtained exactly this many tokens".
- Coverage is partial and measured, not claimed: SeaDrop `PublicDropUpdated` misses both non-SeaDrop launches (StonkBrokers) and non-public SeaDrop phases (DUNLAPS's allowlist mints). See CHANGELOG.
