"""Operator-set decision rules (2026-09-01), each tied to a real observation.

1. Social approval is mandatory -- the rubric alone may not spend.
   Cause: "Rat - The Brokers" scored 88 with NO_SOCIAL and was 6.9% filled after
   67 hours.
2. One X account, one mint. A handle reused across drops is a mill.
   Cause: @TheMerchantt_ appeared on a second drop after selling out on a first.
3. Skip already-open drops that are not selling: open >= 24h and < 50% filled.
"""
from __future__ import annotations

import time
import types

import pytest


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("MINTSCOUT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CHAINS", "robinhood")
    monkeypatch.setenv("SOCIAL_ENABLED", "false")
    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("MAX_WALLETS", "2")
    monkeypatch.setenv("MAX_TOTAL_SPEND_WEI", "10000000000000000")
    monkeypatch.setenv("MAX_MINTS_TOTAL", "50")
    monkeypatch.delenv("MINT_SEED", raising=False)
    from mintscout.live import Runner
    return Runner()


def _cand(start_offset=-100_000, cap=1):
    now = int(time.time())
    return types.SimpleNamespace(
        chain="robinhood", collection="0x" + "ab" * 20,
        mint_price=0, start_time=now + start_offset, end_time=now + 86_400,
        max_per_wallet=cap, fee_bps=1000, restrict_fee_recipients=True,
        drop_uri=None)


# ------------------------------------------------ rule 1: social is mandatory
def test_require_social_is_on_by_default(runner):
    assert runner.require_social is True


def test_repeat_handles_skipped_by_default(runner):
    assert runner.skip_repeat_handles is True


def test_stale_thresholds_match_operator_policy(runner):
    assert runner.stale_hours == 24.0
    assert runner.stale_min_fill == 0.5


# ------------------------------------------------ rule 2: handle memory
def test_minted_handle_persists_across_restarts(runner, tmp_path, monkeypatch):
    runner._remember_handle("TheMerchantt_")
    monkeypatch.setenv("MINTSCOUT_STATE_DIR", str(tmp_path))
    from mintscout.live import Runner
    fresh = Runner()
    assert "themerchantt_" in fresh.minted_handles, (
        "a handle we already minted must survive a restart")


def test_handle_matching_is_case_insensitive(runner):
    runner._remember_handle("PinieNFT")
    assert "pinienft" in runner.minted_handles


def test_remember_handle_tolerates_none(runner):
    runner._remember_handle(None)
    assert runner.minted_handles == set()


# ------------------------------------------------ rule 3: stale drop fill
@pytest.mark.parametrize("open_hours,fill,should_skip", [
    (67, 0.069, True),    # "Rat - The Brokers", the drop that prompted the rule
    (25, 0.10, True),
    (24, 0.499, True),    # boundary: at 24h, just under 50%
    (24, 0.50, False),    # at 24h and exactly 50% -> allowed
    (23, 0.01, False),    # too young to judge, even at 1% filled
    (100, 0.90, False),   # old but selling well
])
def test_stale_rule_matches_policy(runner, open_hours, fill, should_skip):
    """Pure check of the documented rule: open >= 24h AND fill < 50%."""
    is_stale = open_hours >= runner.stale_hours and fill < runner.stale_min_fill
    assert is_stale is should_skip


def test_fill_ratio_handles_missing_supply(runner, monkeypatch):
    """A collection with no maxSupply must not be treated as 0% filled."""
    rpc = runner.rpcs["robinhood"]
    monkeypatch.setattr(rpc, "try_call", lambda *a, **k: None)
    ratio, tot, mx = runner.fill_ratio("robinhood", "0x" + "cd" * 20)
    assert ratio is None, "unknown supply must be None, never a false 0%"


def test_fill_ratio_computes_correctly(runner, monkeypatch):
    rpc = runner.rpcs["robinhood"]

    def fake(addr, data, *a, **k):
        from mintscout import constants as C
        if data.startswith(C.SEL_TOTAL_SUPPLY):
            return "0x" + f"{154:064x}"
        return "0x" + f"{2222:064x}"

    monkeypatch.setattr(rpc, "try_call", fake)
    ratio, tot, mx = runner.fill_ratio("robinhood", "0x" + "cd" * 20)
    assert (tot, mx) == (154, 2222)
    assert abs(ratio - 0.0693) < 0.001, "the Rat - The Brokers case"
