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
