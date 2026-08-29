"""Deterministic wallet fleet (BIP-44) with a gas reserve.

`MINT_SEED` is env-only, never written to disk, never logged, and redacted from
trajectories. `.env` is gitignored and scripts/scrub_secrets.py blocks mnemonics
and private keys at pre-commit.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

from eth_account import Account

from . import constants as C

Account.enable_unaudited_hdwallet_features()
DERIVATION = "m/44'/60'/0'/0/{i}"


@dataclass
class Wallet:
    index: int
    address: str
    _key: str          # never serialised -- see redact()

    def redact(self) -> dict:
        return {"index": self.index, "address": self.address}


def derive(n: int = C.MAX_WALLETS_DEFAULT, seed: str | None = None) -> list[Wallet]:
    seed = seed if seed is not None else os.environ.get("MINT_SEED", "")
    if not seed.strip():
        raise RuntimeError(
            "MINT_SEED is not set. It is env-only by design; see .env.example. "
            "Dry-run planning does not need it -- use plan_fill() instead.")
    out = []
    for i in range(n):
        a = Account.from_mnemonic(seed, account_path=DERIVATION.format(i=i))
        out.append(Wallet(i, a.address, a.key.hex()))
    return out


def plan_fill(cap: int, remaining_supply: int | None,
              max_wallets: int = C.MAX_WALLETS_DEFAULT,
              target: int = C.TARGET_PER_COLLECTION) -> dict:
    """How many wallets to use and what is actually achievable.

    Why the default fleet is 20 and not more (disclosed plainly in the README):
    wallets 21+ only help the cap<=4 band, while each additional wallet costs a
    key to manage, a funding transaction and a sweep transaction. 30% of
    Robinhood free drops are cap=1 and hard-ceiling at 20 tokens regardless.
    """
    cap = max(0, int(cap or 0))
    supply = remaining_supply if remaining_supply else 0
    if cap == 0:
        return {"wallets_needed": 0, "wallets_used": 0, "achievable": 0,
                "limited_by": "cap=0"}
    want = min(target, supply) if supply else target
    needed = math.ceil(want / cap)
    used = min(needed, max_wallets)
    achievable = min(target, used * cap, supply or target)
    return {"wallets_needed": needed, "wallets_used": used,
            "achievable": achievable,
            "limited_by": "wallet_count" if needed > max_wallets else "supply_or_target"}


def gas_reserve_ok(balance_wei: int, gas_price_wei: int,
                   reserve_gas: int = 200_000) -> bool:
    """Every wallet must retain enough to pay for its own sweep after minting.

    Stranded NFTs in an ungassed wallet is the obvious failure mode of a
    multi-wallet fleet; refusing to spend below the reserve is the fix.
    """
    return balance_wei >= reserve_gas * gas_price_wei * 2
