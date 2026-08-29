"""The decision-cutoff leakage controls, asserted rather than assumed.

The integrity of every number in results/ rests on the agent never seeing
post-cutoff data. These tests run over the real committed dataset.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from mintscout.dataset import load
from mintscout.enrich import build_dossier
from mintscout.eval.replay import LeakageError, ReplayContext

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data/drops_robinhood.jsonl"

pytestmark = pytest.mark.skipif(not DATA.exists(), reason="dataset not built")


@pytest.fixture(scope="module")
def recs():
    return load(DATA)


def test_no_item_after_cutoff(recs):
    """Every timestamped item reachable through the context is <= cutoff."""
    for rec in recs:
        ctx = ReplayContext(rec)
        for item in ctx.logs("all"):
            assert (item.get("block_timestamp") or 0) <= ctx.cutoff, (
                f"{rec['collection']}: item at {item.get('block_timestamp')} "
                f"exceeds cutoff {ctx.cutoff}")


def test_outcome_is_unreachable(recs):
    """The outcome block must not be readable through the context at all."""
    ctx = ReplayContext(recs[0])
    for key in ("outcome", "config_revisions_all"):
        with pytest.raises(LeakageError):
            ctx[key]
        with pytest.raises(LeakageError):
            getattr(ctx, key)


def test_injected_post_cutoff_log_is_filtered(recs):
    """Directly plant a post-cutoff record and assert it never comes back.

    This is the test that actually proves the filter works, rather than proving
    the dataset happens to be clean.
    """
    rec = json.loads(json.dumps(recs[0]))
    cutoff = rec["decision_cutoff_ts"]
    poison = {"uri": "ipfs://POISON", "block_number": 10 ** 9,
              "block_timestamp": cutoff + 10_000}
    rec["features_at_cutoff"]["drop_uris_before_cutoff"].append(poison)
    ctx = ReplayContext(rec)
    assert all(u["uri"] != "ipfs://POISON" for u in ctx.drop_uris())
    assert all(i.get("block_timestamp", 0) <= cutoff for i in ctx.logs("all"))


def test_dossier_contains_no_outcome_fields(recs):
    """The payload handed to the LLM must not mention outcome quantities."""
    banned = ("total_minted", "unique_minters", "secondary_transfers",
              "total_supply_now", "is_regression_fixture", "label")
    for rec in recs[:60]:
        ctx = ReplayContext(rec)
        blob = json.dumps(build_dossier(ctx, allow_network=False)).lower()
        for b in banned:
            assert b not in blob, f"{rec['collection']}: dossier leaks {b!r}"


def test_mint_velocity_is_empty_at_decision_point(recs):
    """Velocity is an outcome signal; it must be unavailable pre-start."""
    from mintscout.enrich import mint_velocity
    for rec in recs[:60]:
        ctx = ReplayContext(rec)
        mv = mint_velocity(ctx)
        assert mv["available"] is False
        assert mv["mints_per_min"] is None
