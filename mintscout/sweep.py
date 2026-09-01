"""Sweep minted tokens from the fleet to a vault address.

One transaction per wallet, each an EIP-7702 batch of `transferFrom` calls.
Dry-run by default like everything else.

Two warnings this deliberately surfaces in the confirmation output:
  * Transferring an ERC-6551 token also transfers whatever its token-bound
    account holds. That can be worth more than the NFT.
  * Every wallet must retain gas for its own sweep. The planner refuses to
    spend a wallet below its reserve, because stranded NFTs in an ungassed
    wallet is the obvious failure mode of a multi-wallet fleet.
"""
from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode as abi_encode

from . import constants as C


@dataclass
class TransferCall:
    collection: str
    token_id: int
    frm: str
    to: str

    def calldata(self) -> str:
        body = abi_encode(["address", "address", "uint256"],
                          [self.frm, self.to, self.token_id])
        return C.SEL_TRANSFER_FROM + body.hex()


def plan_sweep(holdings: dict[str, list[int]], wallet: str, vault: str,
               erc6551_collections: set[str] | None = None) -> dict:
    """Build the per-wallet sweep plan. Pure function -- no keys, no network."""
    erc6551_collections = erc6551_collections or set()
    calls = [TransferCall(col, tid, wallet, vault)
             for col, ids in holdings.items() for tid in ids]
    warnings = []
    tba = sorted(set(holdings) & erc6551_collections)
    if tba:
        warnings.append(
            "ERC-6551: transferring these tokens also transfers whatever their "
            "token-bound accounts hold: " + ", ".join(tba))
    return {"wallet": wallet, "vault": vault, "n_calls": len(calls),
            "collections": sorted(holdings),
            "calls": [{"collection": c.collection, "token_id": c.token_id,
                       "calldata": c.calldata()} for c in calls],
            "warnings": warnings}


def redact(obj):
    """Strip anything key-shaped before a plan or trajectory is serialised."""
    BAD = ("private_key", "_key", "key", "seed", "mnemonic", "secret")
    if isinstance(obj, dict):
        return {k: ("<redacted>" if any(b in k.lower() for b in BAD)
                    else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# --------------------------------------------------------------- discovery
TRANSFER_GAS_LIMIT = 120_000


def find_fleet_holdings(rpc, wallets: list[str], collections: list[str],
                        lookback_blocks: int = 3_000_000
                        ) -> dict[str, dict[str, list[int]]]:
    """All holdings for the whole fleet in ONE log scan.

    The per-wallet, per-collection version issued
    len(wallets) x len(collections) chunked scans -- ~500 requests for a
    5-wallet, 4-collection fleet, which took minutes. eth_getLogs accepts an
    address ARRAY and an OR-array on an indexed topic, so every collection and
    every recipient can be queried together and split client-side.

    Returns {wallet_lower: {collection: [token_ids]}}.
    """
    from . import constants as C
    padded = ["0x" + "0" * 24 + w[2:].lower() for w in wallets]
    tip = rpc.block_number()
    frm = max(1, tip - lookback_blocks)
    try:
        logs = rpc.get_logs_chunked(list(collections),
                                    [C.TOPIC_TRANSFER, None, padded], frm, tip)
    except Exception:
        return {}

    seen: dict[str, dict[str, set]] = {}
    for l in logs:
        if len(l.get("topics", [])) != 4:
            continue
        to = "0x" + l["topics"][2][-40:]
        col = l["address"].lower()
        seen.setdefault(to.lower(), {}).setdefault(col, set()).add(
            int(l["topics"][3], 16))

    # Confirm current ownership: a token received and later moved on must not be
    # offered for sweeping.
    out: dict[str, dict[str, list[int]]] = {}
    for w, cols in seen.items():
        for col, ids in cols.items():
            held = []
            for tid in sorted(ids):
                owner = rpc.try_call(col, C.SEL_OWNER_OF + f"{tid:064x}")
                if owner and ("0x" + owner[-40:]).lower() == w:
                    held.append(tid)
            if held:
                out.setdefault(w, {})[col] = held
    return out


def find_holdings(rpc, wallet: str, collections: list[str],
                  lookback_blocks: int = 3_000_000) -> dict[str, list[int]]:
    """Token IDs currently held by `wallet`, per collection.

    Derived from Transfer logs rather than ERC721Enumerable: `tokenOfOwnerByIndex`
    is optional and most of these collections are minimal proxies that do not
    implement it. Every candidate id is then confirmed with `ownerOf`, so a token
    that was received and later moved on is not offered for sweeping.
    """
    from . import constants as C
    padded = "0x" + "0" * 24 + wallet[2:].lower()
    tip = rpc.block_number()
    frm = max(1, tip - lookback_blocks)
    out: dict[str, list[int]] = {}
    for col in collections:
        try:
            logs = rpc.get_logs_chunked(col, [C.TOPIC_TRANSFER, None, padded],
                                        frm, tip)
        except Exception:
            continue
        ids = sorted({int(l["topics"][3], 16) for l in logs
                      if len(l.get("topics", [])) == 4})
        held = []
        for tid in ids:
            owner = rpc.try_call(col, C.SEL_OWNER_OF + f"{tid:064x}")
            if owner and ("0x" + owner[-40:]).lower() == wallet.lower():
                held.append(tid)
        if held:
            out[col] = held
    return out


def build_transfer_tx(rpc, wallet: str, collection: str, token_id: int,
                      to: str, nonce: int, gas_price: int) -> dict:
    from . import constants as C
    from eth_abi import encode as abi_encode
    data = C.SEL_TRANSFER_FROM + abi_encode(
        ["address", "address", "uint256"], [wallet, to, token_id]).hex()
    return {"type": 2, "chainId": rpc.chain_id, "to": collection,
            "from": wallet, "value": 0, "data": data, "nonce": nonce,
            "gas": TRANSFER_GAS_LIMIT, "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": min(gas_price, 1_000_000_000)}
