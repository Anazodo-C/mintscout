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


def test_funding_and_runner_derive_identical_addresses(monkeypatch):
    """The funder must top up exactly the wallets the runner mints from.

    Both derive from MINT_SEED at m/44'/60'/0'/0/{i}, but in different modules.
    If those paths ever drift, funding would credit addresses the runner never
    uses -- the ETH would be sound but the fleet would sit unarmed, and the only
    symptom would be '0/20 armed' with a funded-looking wallet somewhere else.
    """
    monkeypatch.setenv("MINT_SEED", SEED)
    monkeypatch.setenv("MAX_WALLETS", "6")
    monkeypatch.setenv("WALLET_INDEX", "0")
    monkeypatch.setenv("CHAINS", "robinhood")
    monkeypatch.setenv("SOCIAL_ENABLED", "false")

    from mintscout.live import Runner

    r = Runner()
    # Avoid network: stub the balance/gas reads the fleet loader performs.
    for rpc in r.rpcs.values():
        monkeypatch.setattr(rpc, "raw",
                            lambda m, p=None, **k: hex(10 ** 16))
    r.load_wallet()

    runner_addrs = [w["address"] for w in r.fleet]
    funding_addrs = [a for _, a, _ in derive_fleet(SEED, 6)]
    assert runner_addrs == funding_addrs, (
        "funding and the runner must derive the same fleet")


# ------------------------------------------------------------------ sweep
def test_transfer_tx_is_well_formed():
    """transferFrom(from,to,tokenId) on the COLLECTION, not on SeaDrop."""
    from mintscout import constants as C
    from mintscout.sweep import TRANSFER_GAS_LIMIT, build_transfer_tx

    rpc = FakeRpc({})
    w = "0x" + "11" * 20
    col = "0x" + "22" * 20
    to = "0x" + "33" * 20
    tx = build_transfer_tx(rpc, w, col, 42, to, nonce=7, gas_price=GAS)
    assert tx["to"] == col, "must call the collection, not SeaDrop"
    assert tx["from"] == w, "only the owner can move its own token"
    assert tx["data"].startswith(C.SEL_TRANSFER_FROM)
    assert f"{42:064x}" in tx["data"], "tokenId must be encoded"
    assert to[2:].lower() in tx["data"].lower()
    assert tx["value"] == 0
    assert tx["gas"] == TRANSFER_GAS_LIMIT
    assert tx["nonce"] == 7


def test_holdings_confirm_ownership_before_offering_to_sweep(monkeypatch):
    """A token received and later moved on must NOT be offered for sweeping."""
    from mintscout import constants as C
    from mintscout.sweep import find_holdings

    wallet = "0x" + "11" * 20
    other = "0x" + "99" * 20
    col = "0x" + "22" * 20

    class R(FakeRpc):
        def block_number(self):
            return 1_000

        def get_logs_chunked(self, address, topics, a, b, **kw):
            # two tokens were received at some point
            return [{"topics": [C.TOPIC_TRANSFER, "0x0", "0x0", f"0x{i:064x}"]}
                    for i in (1, 2)]

        def try_call(self, to, data, *a, **k):
            # token 1 still ours, token 2 has moved on
            tid = int(data[-64:], 16)
            owner = wallet if tid == 1 else other
            return "0x" + "0" * 24 + owner[2:]

    held = find_holdings(R({}), wallet, [col])
    assert held == {col: [1]}, "only still-owned tokens may be swept"
