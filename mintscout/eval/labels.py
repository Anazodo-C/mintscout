"""Ground-truth labelling.

A drop is HIGH VALUE if, measured strictly after its mint window closed:
    1. total_minted / max_supply >= 0.80        (it actually sold)
    2. unique_minters >= 100                    (broad distribution, not one bot)
    3. >=1 non-mint Transfer within 48h of endTime  (someone traded it)

All three are computable from logs alone -- no archive state required, which is
what makes the whole eval reproducible offline.

Every threshold is a knob on `LabelPolicy` so the sensitivity of the headline
number to the label definition can be reported rather than hidden.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LabelPolicy:
    min_fill_ratio: float = 0.80
    min_unique_minters: int = 100
    require_secondary: bool = True
    secondary_window_s: int = 48 * 3600


DEFAULT = LabelPolicy()


def fill_ratio(rec: dict) -> float | None:
    o = rec["outcome"]
    ms = o.get("max_supply") or 0
    if not ms:
        return None
    return (o.get("total_minted") or 0) / ms


def label(rec: dict, policy: LabelPolicy = DEFAULT) -> str:
    return "high_value" if is_high_value(rec, policy) else "low_value"


def is_high_value(rec: dict, policy: LabelPolicy = DEFAULT) -> bool:
    o = rec["outcome"]
    fr = fill_ratio(rec)
    if fr is None or fr < policy.min_fill_ratio:
        return False
    if (o.get("unique_minters") or 0) < policy.min_unique_minters:
        return False
    if policy.require_secondary and (o.get("secondary_transfers_48h") or 0) < 1:
        return False
    return True


def explain(rec: dict, policy: LabelPolicy = DEFAULT) -> dict:
    """Per-criterion breakdown -- used in the report so a reader can see exactly
    why any single drop was labelled the way it was."""
    o = rec["outcome"]
    fr = fill_ratio(rec)
    return {
        "fill_ratio": None if fr is None else round(fr, 4),
        "fill_ok": fr is not None and fr >= policy.min_fill_ratio,
        "unique_minters": o.get("unique_minters"),
        "minters_ok": (o.get("unique_minters") or 0) >= policy.min_unique_minters,
        "secondary_transfers_48h": o.get("secondary_transfers_48h"),
        "secondary_ok": (not policy.require_secondary
                         or (o.get("secondary_transfers_48h") or 0) >= 1),
        "label": label(rec, policy),
    }


def summarize(recs: list[dict], policy: LabelPolicy = DEFAULT) -> dict:
    labs = [label(r, policy) for r in recs]
    n_hi = sum(1 for l in labs if l == "high_value")
    return {"n": len(recs), "high_value": n_hi, "low_value": len(recs) - n_hi,
            "base_rate": round(n_hi / len(recs), 4) if recs else 0.0}
