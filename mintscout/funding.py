"""Top up the mint fleet from a funder wallet.

Top-up-to-target, not send-a-flat-amount. Every run computes
`deficit = target - balance` per wallet and sends only the difference, so:

  * re-running it is idempotent -- wallets already at target are skipped, and
    running it twice does not double-spend;
  * it is safe to run on a schedule as wallets drain, which matters now that
    GAS_RESERVE_WEI defaults to 0 and wallets are allowed to reach empty.

`payer == minter` is a SeaDrop constraint on MINTING, not on funding. Moving ETH
between your own wallets is an ordinary transfer and is unaffected by it. What
the constraint does mean is that funding is *necessary*: wallet 0 cannot mint on
behalf of wallets 1..N, so each one must hold its own gas and sign for itself.

Dry-run by default. Nothing is broadcast without an explicit live flag.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .rpc import RpcClient

TRANSFER_GAS = 21_000


@dataclass
class FundingPlan:
    chain: str
    funder: str
    funder_balance: int
    target_wei: int
    transfers: list[dict]          # [{index, address, balance, deficit}]
    gas_price: int
    total_value: int
    total_gas_cost: int
    affordable: bool
    shortfall: int

    @property
    def total_cost(self) -> int:
        return self.total_value + self.total_gas_cost


def derive_fleet(seed: str, n: int, start: int = 0) -> list[tuple[int, str, str]]:
    """(index, address, private_key) for wallets [start, start+n)."""
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    out = []
    for i in range(start, start + n):
        a = Account.from_mnemonic(seed, account_path=f"m/44'/60'/0'/0/{i}")
        out.append((i, a.address, a.key.hex()))
    return out


def plan_funding(rpc: RpcClient, seed: str, target_wei: int, n_wallets: int,
                 funder_index: int = 0) -> FundingPlan:
    """Read balances and compute per-wallet deficits. Read-only."""
    fleet = derive_fleet(seed, n_wallets)
    funder = next((w for w in fleet if w[0] == funder_index), None)
    if funder is None:
        raise ValueError(f"funder index {funder_index} is outside the fleet "
                         f"(0..{n_wallets - 1})")

    gas_price = int(rpc.raw("eth_gasPrice", []), 16)
    balances: dict[int, int] = {}
    for idx, addr, _ in fleet:
        try:
            balances[idx] = int(rpc.raw("eth_getBalance", [addr, "latest"]), 16)
        except Exception:
            balances[idx] = -1          # unknown; excluded from the plan below

    transfers = []
    for idx, addr, _ in fleet:
        if idx == funder_index:
            continue
        bal = balances[idx]
        if bal < 0:
            continue
        deficit = target_wei - bal
        if deficit <= 0:
            continue                     # already at or above target -- skip
        transfers.append({"index": idx, "address": addr, "balance": bal,
                          "deficit": deficit})

    total_value = sum(t["deficit"] for t in transfers)
    # maxFeePerGas is set to 2x base when sending, so budget for the ceiling.
    total_gas = len(transfers) * TRANSFER_GAS * gas_price * 2
    funder_balance = balances.get(funder_index, 0)
    need = total_value + total_gas
    return FundingPlan(
        chain=rpc.chain, funder=funder[1], funder_balance=funder_balance,
        target_wei=target_wei, transfers=transfers, gas_price=gas_price,
        total_value=total_value, total_gas_cost=total_gas,
        affordable=funder_balance >= need,
        shortfall=max(0, need - funder_balance),
    )


def execute_funding(rpc: RpcClient, seed: str, plan: FundingPlan,
                    funder_index: int = 0, log=print) -> dict:
    """Broadcast the plan. Callers must have confirmed `plan.affordable`.

    Nonces are assigned sequentially from the funder's pending count rather than
    re-read per transfer: re-reading would race against transactions still in
    the mempool and reuse a nonce, which silently replaces the previous transfer
    instead of adding to it.
    """
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    funder = Account.from_mnemonic(
        seed, account_path=f"m/44'/60'/0'/0/{funder_index}")

    nonce = int(rpc.raw("eth_getTransactionCount",
                        [funder.address, "pending"]), 16)
    sent, failed = [], []
    for i, t in enumerate(plan.transfers):
        tx = {
            "type": 2, "chainId": rpc.chain_id, "to": t["address"],
            "value": t["deficit"], "nonce": nonce + i, "gas": TRANSFER_GAS,
            "maxFeePerGas": plan.gas_price * 2,
            "maxPriorityFeePerGas": min(plan.gas_price, 1_000_000_000),
            "data": "0x",
        }
        try:
            signed = funder.sign_transaction(tx)
            raw = signed.raw_transaction.hex()
            h = rpc.raw("eth_sendRawTransaction",
                        [raw if raw.startswith("0x") else "0x" + raw])
            sent.append({"index": t["index"], "address": t["address"],
                         "value": t["deficit"], "tx": h})
            log(f"  [{t['index']:>2}] {t['address']}  "
                f"+{t['deficit'] / 1e18:.6f} ETH  tx={h}")
        except Exception as e:
            failed.append({"index": t["index"], "address": t["address"],
                           "error": f"{type(e).__name__}: {str(e)[:120]}"})
            log(f"  [{t['index']:>2}] {t['address']}  FAILED: "
                f"{type(e).__name__}: {str(e)[:100]}")
    return {"sent": sent, "failed": failed,
            "total_value": sum(s["value"] for s in sent)}


def wait_for_funding(rpc: RpcClient, result: dict, timeout_s: float = 120.0,
                     log=print) -> dict:
    """Poll receipts so the operator sees confirmation, not just submission."""
    pending = {s["tx"]: s for s in result["sent"]}
    confirmed, reverted = [], []
    deadline = time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        for h in list(pending):
            try:
                r = rpc.get_receipt(h)
            except Exception:
                r = None
            if not r:
                continue
            ok = int(r.get("status", "0x0"), 16) == 1
            (confirmed if ok else reverted).append(pending.pop(h))
        if pending:
            time.sleep(2)
    log(f"  confirmed={len(confirmed)}  reverted={len(reverted)}  "
        f"still_pending={len(pending)}")
    return {"confirmed": confirmed, "reverted": reverted,
            "pending": list(pending.values())}
