"""Stratified evaluation subset, for running LLM arms under a fixed budget.

Running every arm over all 360 held-out drops costs more than the budget allows.
A plain random 150 would be the wrong economy: at a 5% base rate it would contain
about 7 high-value drops, and recall measured on 7 positives is noise.

So the subset keeps **every** positive and samples the negatives, which spends
the budget on the drops that actually carry information. Precision measured on
this subset is inflated relative to the real population, so `correct_precision`
reweights it back to the true prevalence and BOTH numbers are reported.

All arms see the identical subset, so the comparison between them stays fair --
resource parity is preserved.
"""
from __future__ import annotations

import random

from .labels import label


def stratified(recs: list[dict], n_target: int, seed: int = 1337) -> tuple[list[dict], dict]:
    hi = [r for r in recs if label(r) == "high_value"]
    lo = [r for r in recs if label(r) != "high_value"]
    n_lo = max(0, n_target - len(hi))
    rng = random.Random(seed)
    lo_s = rng.sample(lo, n_lo) if len(lo) > n_lo else lo
    sub = hi + lo_s
    rng.shuffle(sub)
    meta = {
        "stratified": True,
        "population_n": len(recs),
        "population_high_value": len(hi),
        "population_base_rate": round(len(hi) / len(recs), 4) if recs else 0,
        "subset_n": len(sub),
        "subset_high_value": len(hi),
        "subset_base_rate": round(len(hi) / len(sub), 4) if sub else 0,
        # every positive is kept, so negatives are the only downsampled class
        "negative_sampling_rate": round(len(lo_s) / len(lo), 4) if lo else 1.0,
        "note": ("All positives kept; negatives downsampled. Precision on this "
                 "subset is inflated -- use corrected_precision for the "
                 "population estimate. Recall is unaffected by the reweighting."),
    }
    return sub, meta


def correct_precision(tp: int, fp: int, negative_sampling_rate: float) -> float:
    """Reweight subset precision back to the true population prevalence.

    Every positive was kept, so TP needs no correction. Negatives were sampled at
    rate r, so each observed false positive stands for 1/r of them.
    """
    if negative_sampling_rate <= 0:
        return 0.0
    fp_pop = fp / negative_sampling_rate
    denom = tp + fp_pop
    return round(tp / denom, 4) if denom else 0.0
