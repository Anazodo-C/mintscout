"""Spend guard behaviour, including the crash-loop regression.

The bug: reaching MAX_MINTS_TOTAL made preflight_config() return False, which
exited the process non-zero, which Railway's ON_FAILURE policy restarted, which
hit the same condition and exited again -- forever. Spending your allowance is
the most ordinary event in the system and must never look like a crash.
"""
from __future__ import annotations

import json

import pytest

from mintscout.budget import Denied, Limits, SpendGuard


def _guard(tmp_path, state=None, **limits):
    p = tmp_path / "spend.json"
    if state:
        p.write_text(json.dumps(state))
    return SpendGuard(Limits(**limits), state_path=p)


def test_exhausted_mint_count_still_denies(tmp_path):
    g = _guard(tmp_path, {"spent_wei": 0, "mints": 5, "recent": [], "started_at": 0},
               max_total_spend_wei=10 ** 15, max_mints_total=5)
    with pytest.raises(Denied, match="MAX_MINTS_TOTAL"):
        g.check(0, 0)


def test_unset_caps_fail_closed(tmp_path):
    g = _guard(tmp_path)
    with pytest.raises(Denied, match="MAX_TOTAL_SPEND_WEI"):
        g.check(1, 1)


def test_gas_reserve_disabled_by_default(tmp_path):
    """Reserve is now 0 by default -- a wallet may be spent to empty."""
    assert Limits().gas_reserve_wei == 0
    g = _guard(tmp_path, max_total_spend_wei=10 ** 15, max_mints_total=3)
    g.check(10 ** 13, 10 ** 9, wallet_balance_wei=10 ** 13)      # must not raise


def test_gas_reserve_still_enforced_when_set(tmp_path):
    g = _guard(tmp_path, max_total_spend_wei=10 ** 15, max_mints_total=3,
               gas_reserve_wei=8 * 10 ** 14)
    with pytest.raises(Denied, match="GAS_RESERVE_WEI"):
        g.check(10 ** 13, 10 ** 9, wallet_balance_wei=10 ** 13)


def test_spend_is_committed_before_broadcast(tmp_path):
    """commit() must persist immediately so a crash over-counts, never under."""
    g = _guard(tmp_path, max_total_spend_wei=10 ** 15, max_mints_total=3)
    g.commit(10 ** 13)
    reloaded = _guard(tmp_path, max_total_spend_wei=10 ** 15, max_mints_total=3)
    assert reloaded.summary["spent_wei"] == 10 ** 13
    assert reloaded.summary["mints"] == 1


# ---------------------------------------------------------------- revert decoding
def test_decode_sold_out():
    from mintscout.executor import decode_revert
    # MintQuantityExceedsMaxSupply(2501, 2500) as returned by a live SeaDrop call
    data = ("0xe12d2314"
            + f"{2501:064x}" + f"{2500:064x}")
    out = decode_revert(data)
    assert "SOLD OUT" in out
    assert "2501" in out


def test_decode_standard_error_string():
    from mintscout.executor import decode_revert
    msg = b"boom"
    data = ("0x08c379a0" + f"{32:064x}" + f"{len(msg):064x}"
            + msg.hex() + "00" * 28)
    assert decode_revert(data) == "boom"


def test_decode_returns_none_for_unknown():
    from mintscout.executor import decode_revert
    assert decode_revert(None) is None
    assert decode_revert("0x") is None
    assert decode_revert("0xdeadbeef" + "00" * 32) is None


def test_rpc_error_carries_revert_data():
    """RpcError must preserve error['data'] or the reason is unrecoverable."""
    from mintscout.rpc import RpcError
    e = RpcError(3, "execution reverted", "0xe12d2314")
    assert e.data == "0xe12d2314"
    assert RpcError(3, "x").data is None


# ------------------------------------------------- expected vs worst-case cost
class _GasRpc:
    chain = "robinhood"
    chain_id = 4663

    def __init__(self, est=None):
        self._est = est

    def raw(self, method, params, **kw):
        if method == "eth_estimateGas":
            if self._est is None:
                raise RuntimeError("estimateGas unsupported")
            return hex(self._est)
        raise AssertionError(method)


def _tx(gas_limit=260_000, max_fee=10 ** 9, value=0):
    return {"to": "0x" + "11" * 20, "from": "0x" + "22" * 20, "data": "0x",
            "value": value, "gas": gas_limit, "maxFeePerGas": max_fee}


def test_expected_cost_is_far_below_the_gas_ceiling():
    """The bug that blocked a live mint: charging the LIMIT, not the usage."""
    from mintscout.executor import estimate_cost_wei, expected_cost_wei
    tx = _tx()
    worst = estimate_cost_wei(tx)
    expected = expected_cost_wei(_GasRpc(est=100_254), tx)
    assert expected < worst
    assert worst / expected > 1.5, "ceiling should be materially above expected"


def test_expected_cost_falls_back_to_measured_median():
    from mintscout.executor import MEASURED_MINT_GAS, expected_cost_wei
    tx = _tx()
    got = expected_cost_wei(_GasRpc(est=None), tx)   # estimateGas unavailable
    assert got == int(MEASURED_MINT_GAS * 1.5) * tx["maxFeePerGas"]


def test_expected_cost_never_exceeds_the_gas_limit():
    """A wild estimate must not let the budget exceed what the tx can spend."""
    from mintscout.executor import expected_cost_wei
    tx = _tx(gas_limit=120_000)
    got = expected_cost_wei(_GasRpc(est=10 ** 7), tx)
    assert got <= int(tx["gas"] * 1.5) * tx["maxFeePerGas"]


def test_expected_cost_includes_mint_value():
    from mintscout.executor import expected_cost_wei
    tx = _tx(value=5 * 10 ** 14)
    assert expected_cost_wei(_GasRpc(est=100_000), tx) > 5 * 10 ** 14


def test_armed_threshold_matches_node_requirement(monkeypatch, tmp_path):
    """A wallet marked armed must actually be able to send.

    Nodes check `balance >= gas_limit * maxFeePerGas + value` up front. Using
    expected gas here (rather than the limit) marked four wallets armed that the
    node then rejected with 'insufficient funds', losing four live mints.
    """
    monkeypatch.setenv("MINTSCOUT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CHAINS", "robinhood")
    monkeypatch.setenv("SOCIAL_ENABLED", "false")
    monkeypatch.delenv("MINT_SEED", raising=False)
    from mintscout import state as _state
    monkeypatch.setattr(_state.State, "_SEED", tmp_path / "none.json")
    from mintscout.executor import DEFAULT_MINT_GAS
    from mintscout.live import Runner

    r = Runner()
    gp = 600_000_000
    for rpc in r.rpcs.values():
        monkeypatch.setattr(rpc, "raw", lambda m, p=None, **k: hex(gp))
    assert r._min_balance_to_mint() == DEFAULT_MINT_GAS * gp * 2
