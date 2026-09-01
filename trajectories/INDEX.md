# Agent decision traces (deliverable 04)

27 traces across 5 evaluation arms.

Each file records one decision end to end: the drop, the decision cutoff, every
enrichment tool call with its raw output and timing, the triage verdict with its
reasons and cited evidence, the verifier's audit, the action taken, and the
ground-truth outcome.

> `outcome` is shown for post-hoc scoring ONLY. It is never visible to the agent
> at decision time — `ReplayContext` raises `LeakageError` on any attempt to read
> it, and `tests/test_no_leakage.py` plants a post-cutoff record to prove the
> filter works.

## The interesting ones

The five `mintscout_*` traces are **all verdict-changing verifier vetoes** — the
verifier overruling the triage agent. Two of them are failures where it destroyed
a correct MINT; those are the subject of CHANGELOG "Removed experiment #3".

| File | Arm | Triage → Final | Vetoed | Ground truth | Tools |
|---|---|---|---|---|---|
| `baseline_mint_all_01_0xed74a202.json` | baseline_mint_all | MINT → MINT | no | high_value | 8 |
| `baseline_mint_all_02_0xba8f6099.json` | baseline_mint_all | MINT → MINT | no | high_value | 8 |
| `baseline_mint_all_03_0xa21ff64d.json` | baseline_mint_all | MINT → MINT | no | high_value | 8 |
| `baseline_mint_all_04_0xe1a65a7a.json` | baseline_mint_all | MINT → MINT | no | low_value | 8 |
| `baseline_mint_all_05_0x39c3388c.json` | baseline_mint_all | MINT → MINT | no | low_value | 8 |
| `baseline_mint_all_ink_01_0xfc5cdcd7.json` | baseline_mint_all | MINT → MINT | no | high_value | 8 |
| `baseline_mint_all_ink_02_0xd1f7bac9.json` | baseline_mint_all | MINT → MINT | no | high_value | 8 |
| `baseline_mint_all_ink_03_0x12d3908f.json` | baseline_mint_all | MINT → MINT | no | low_value | 8 |
| `deterministic_01_0xed74a202.json` | deterministic | MINT → MINT | no | high_value | 8 |
| `deterministic_02_0xba8f6099.json` | deterministic | MINT → MINT | no | high_value | 8 |
| `deterministic_03_0xa0f40bfc.json` | deterministic | MINT → MINT | no | high_value | 8 |
| `deterministic_04_0xe1a65a7a.json` | deterministic | MINT → MINT | no | low_value | 8 |
| `deterministic_05_0x57f2527c.json` | deterministic | MINT → MINT | no | low_value | 8 |
| `deterministic_ink_01_0x8877888f.json` | deterministic | MINT → MINT | no | high_value | 8 |
| `deterministic_ink_02_0x2100812f.json` | deterministic | MINT → MINT | no | low_value | 8 |
| `mintscout_01_0xe1a65a7a.json` | mintscout | MINT → WATCH | **yes** | low_value | 8 |
| `mintscout_02_0xa0f40bfc.json` | mintscout | MINT → WATCH | **yes** | high_value | 8 |
| `mintscout_03_0xeb18b8c6.json` | mintscout | MINT → SKIP | **yes** | low_value | 8 |
| `mintscout_04_0xa766deb0.json` | mintscout | MINT → WATCH | **yes** | low_value | 8 |
| `mintscout_05_0x10aacd50.json` | mintscout | MINT → WATCH | **yes** | high_value | 8 |
| `mintscout_no_verifier_01_0xed74a202.json` | mintscout_no_verifier | SKIP → SKIP | no | high_value | 8 |
| `mintscout_no_verifier_02_0xba8f6099.json` | mintscout_no_verifier | MINT → MINT | no | high_value | 8 |
| `mintscout_no_verifier_03_0xa0f40bfc.json` | mintscout_no_verifier | MINT → MINT | no | high_value | 8 |
| `mintscout_no_verifier_04_0xe1a65a7a.json` | mintscout_no_verifier | MINT → MINT | no | low_value | 8 |
| `mintscout_no_verifier_05_0xeb18b8c6.json` | mintscout_no_verifier | MINT → MINT | no | low_value | 8 |
| `single_prompt_llm_01_0xed74a202.json` | single_prompt_llm | SKIP → SKIP | no | high_value | 8 |
| `single_prompt_llm_02_0x178004c8.json` | single_prompt_llm | MINT → MINT | no | low_value | 8 |

## Arms

| Arm | What it isolates |
|---|---|
| `baseline_mint_all` | Mint every free drop — the fair naive baseline |
| `deterministic` | Hand-written rubric, no LLM |
| `single_prompt_llm` | One LLM call, no tools/memory/verifier |
| `mintscout_no_verifier` | Enrichment tools + LLM triage |
| `mintscout` | Triage + adversarial verifier |
| `*_ink` | Same arms on Ink — a chain the rubric was never tuned against |

Headline numbers and full methodology: `results/comparison.md`,
`results/cross_chain.md`, and `CHANGELOG.md` in the repo.

