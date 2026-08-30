"""Deterministic calibration / test split.

Why this exists: the deterministic rubric is hand-tuned. Scoring a hand-tuned
rubric on the same drops it was tuned against measures memorisation, not skill.
So the rubric is calibrated on `calib` and EVERY arm is reported on `test`.

The split is a stable hash of the collection address, not a shuffle, so it does
not move when the dataset is rebuilt or reordered.
"""
from __future__ import annotations

import hashlib


def bucket(collection: str) -> int:
    return int(hashlib.sha256(collection.lower().encode()).hexdigest()[:8], 16) % 100


def split(recs: list[dict], test_pct: int = 50) -> tuple[list[dict], list[dict]]:
    calib = [r for r in recs if bucket(r["collection"]) >= test_pct]
    test = [r for r in recs if bucket(r["collection"]) < test_pct]
    return calib, test
