"""Fleet funding: top-up-to-target semantics.

The property that matters most is idempotency. Funding runs repeatedly as
wallets drain, so a second run must send only the new deficit -- never the full
target again.
"""
from __future__ import annotations

import pytest

from mintscout.funding import TRANSFER_GAS, derive_fleet, plan_funding

SEED = "test test test test test test test test test test test junk"
GAS = 500_000_000
ETH = 10 ** 18


class FakeRpc:
    """Minimal RpcClient stand-in: fixed gas price and scripted balances."""

    chain = "robinhood"
    chain_id = 4663

    def __init__(self, balances_by_address: dict[str, int]):
        self._bal = {k.lower(): v for k, v in balances_by_address.items()}
        self.calls = []

    def raw(self, method, params, **kw):
        self.calls.append(method)
        if method == "eth_gasPrice":
            return hex(GAS)
        if method == "eth_getBalance":
            return hex(self._bal.get(params[0].lower(), 0))
        raise AssertionError(f"unexpected RPC call: {method}")


def _fleet_addrs(n=4):
    return [a for _, a, _ in derive_fleet(SEED, n)]


def test_derivation_is_deterministic():
    a = derive_fleet(SEED, 3)
    b = derive_fleet(SEED, 3)
    assert [x[1] for x in a] == [x[1] for x in b]
    assert len({x[1] for x in a}) == 3, "each index must give a distinct address"


def test_sends_only_the_deficit():
    addrs = _fleet_addrs(3)
    rpc = FakeRpc({addrs[0]: 10 * ETH,
                   addrs[1]: 0,                 # needs the full target
                   addrs[2]: ETH // 2})         # needs half
    plan = plan_funding(rpc, SEED, target_wei=ETH, n_wallets=3)
    by_idx = {t["index"]: t["deficit"] for t in plan.transfers}
    assert by_idx[1] == ETH
    assert by_idx[2] == ETH // 2, "must top up, not re-send the whole target"


def test_wallets_at_or_above_target_are_skipped():
    """This is what makes re-running the command safe."""
    addrs = _fleet_addrs(4)
    rpc = FakeRpc({addrs[0]: 10 * ETH, addrs[1]: ETH,          # exactly at target
                   addrs[2]: 3 * ETH,                          # above target
                   addrs[3]: 0})                               # below
    plan = plan_funding(rpc, SEED, target_wei=ETH, n_wallets=4)
    assert [t["index"] for t in plan.transfers] == [3]


def test_second_run_after_funding_is_a_no_op():
    addrs = _fleet_addrs(3)
    funded = {addrs[0]: 10 * ETH, addrs[1]: ETH, addrs[2]: ETH}
    plan = plan_funding(FakeRpc(funded), SEED, target_wei=ETH, n_wallets=3)
    assert plan.transfers == []
    assert plan.total_cost == 0


def test_funder_is_never_funded_by_itself():
    addrs = _fleet_addrs(3)
    rpc = FakeRpc({addrs[0]: 0, addrs[1]: 0, addrs[2]: 0})
    plan = plan_funding(rpc, SEED, target_wei=ETH, n_wallets=3, funder_index=0)
    assert all(t["index"] != 0 for t in plan.transfers)


def test_gas_is_budgeted_at_the_fee_ceiling():
    addrs = _fleet_addrs(3)
    rpc = FakeRpc({addrs[0]: 10 * ETH, addrs[1]: 0, addrs[2]: 0})
    plan = plan_funding(rpc, SEED, target_wei=ETH, n_wallets=3)
    # maxFeePerGas is set to 2x base at send time, so the plan must budget 2x.
    assert plan.total_gas_cost == 2 * TRANSFER_GAS * GAS * len(plan.transfers)


def test_unaffordable_plan_is_flagged_with_the_shortfall():
    addrs = _fleet_addrs(3)
    rpc = FakeRpc({addrs[0]: ETH // 2, addrs[1]: 0, addrs[2]: 0})
    plan = plan_funding(rpc, SEED, target_wei=ETH, n_wallets=3)
    assert plan.affordable is False
    assert plan.shortfall == plan.total_cost - plan.funder_balance


def test_affordable_when_funder_covers_value_plus_gas():
    addrs = _fleet_addrs(3)
    need = 2 * ETH + 2 * TRANSFER_GAS * GAS * 2
    rpc = FakeRpc({addrs[0]: need, addrs[1]: 0, addrs[2]: 0})
    plan = plan_funding(rpc, SEED, target_wei=ETH, n_wallets=3)
    assert plan.affordable is True


def test_bad_funder_index_raises():
    addrs = _fleet_addrs(2)
    rpc = FakeRpc({a: 0 for a in addrs})
    with pytest.raises(ValueError, match="funder index"):
        plan_funding(rpc, SEED, target_wei=ETH, n_wallets=2, funder_index=9)
