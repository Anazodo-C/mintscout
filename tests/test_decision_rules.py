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
    # Neutralise the shipped seed: these tests assert on an empty ledger, and a
    # committed seed file must not silently populate it.
    from mintscout import state as _state
    monkeypatch.setattr(_state.State, "_SEED", tmp_path / "no-such-seed.json")
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
    """Pushing to main auto-deploys, so state MUST outlive a restart -- otherwise
    the one-account-one-mint rule silently resets to allowing everything."""
    runner.state.record_mint("robinhood", "0x" + "ab" * 20,
                             handle="TheMerchantt_", name="X", tokens=5,
                             txs=["0xdead"])
    monkeypatch.setenv("MINTSCOUT_STATE_DIR", str(tmp_path))
    from mintscout.live import Runner
    fresh = Runner()
    assert fresh.state.has_handle("TheMerchantt_")
    assert fresh.state.summary()["tokens_held"] == 5


def test_handle_matching_is_case_insensitive_and_strips_at(runner):
    runner.state.seed_handle("PinieNFT")
    assert runner.state.has_handle("pinienft")
    assert runner.state.has_handle("@PINIENFT")


def test_seed_handle_tolerates_none(runner):
    assert runner.state.seed_handle("") is False
    assert runner.state.handles == set()


def test_seeding_twice_is_idempotent(runner):
    assert runner.state.seed_handle("dup") is True
    assert runner.state.seed_handle("dup") is False
    assert len(runner.state.handles) == 1


# ------------------------------------------------ terminal revert caching
def test_sold_out_is_terminal_but_not_active_is_not():
    from mintscout.state import is_terminal
    assert is_terminal("SOLD OUT — max supply reached  [MintQuantityExceedsMaxSupply(2501, 2500)]")
    assert is_terminal("WALLET CAP already reached  [MintQuantityExceedsMaxMintedPerWallet(4, 2)]")
    # a closed phase can open later -- caching it would lose real mints
    assert not is_terminal("phase NOT OPEN at this timestamp  [NotActive(1, 2, 3)]")
    assert not is_terminal("fee recipient rejected by this drop")
    assert not is_terminal(None)


def test_terminal_mark_round_trips_and_can_be_cleared(runner):
    col = "0x" + "ef" * 20
    assert runner.state.terminal_reason("robinhood", col) is None
    runner.state.mark_terminal("robinhood", col, "SOLD OUT")
    assert runner.state.terminal_reason("robinhood", col) == "SOLD OUT"
    runner.state.forget("robinhood", col)
    assert runner.state.terminal_reason("robinhood", col) is None


def test_mint_ledger_accumulates_tokens_and_txs(runner):
    col = "0x" + "12" * 20
    runner.state.record_mint("robinhood", col, handle="h", name="N",
                             tokens=5, txs=["0xa"])
    runner.state.record_mint("robinhood", col, handle="h", name="N",
                             tokens=10, txs=["0xb"])
    rec = runner.state.recent(1)[0]
    assert rec["tokens"] == 15
    assert rec["txs"] == ["0xa", "0xb"]


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
