"""Hard spend limits for live execution.

This is the component that stands between a bug and an emptied wallet, so it is
deliberately boring and fails closed:

* Every limit must be set explicitly. A missing or unparseable limit is treated
  as zero, not as unlimited.
* Spend is committed to disk BEFORE the transaction is broadcast, never after.
  A crash mid-send must over-count, never under-count -- the failure mode of
  under-counting is spending past the cap.
* The guard is the only thing that authorises a send; the runner cannot bypass it.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from dataclasses import dataclass, asdict

STATE = pathlib.Path(os.environ.get("MINTSCOUT_STATE_DIR", "data")) / "spend_state.json"


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        # An unparseable limit is a configuration error. Fail CLOSED.
        return 0


@dataclass
class Limits:
    max_total_spend_wei: int = 0      # total value + gas across the whole run
    max_spend_per_mint_wei: int = 0   # per-transaction ceiling
    max_mints_total: int = 0          # hard count cap
    max_mints_per_hour: int = 0
    # These two carry SAFE defaults rather than 0. A zero default here would
    # mean "no ceiling" / "no reserve", so a caller constructing Limits()
    # directly would silently get weaker protection than one using from_env().
    # Safety defaults must not depend on which constructor you happened to use.
    max_gas_price_wei: int = 5_000_000_000
    # Gas reserve defaults to 0 = NOT enforced.
    # Reserving ~0.0008 ETH per wallet costs ~0.015 ETH idle across a 20-wallet
    # fleet, which the operator judged too capital-intensive for the benefit.
    # The trade-off being accepted: a wallet can now be spent down to empty, and
    # an empty wallet cannot pay to transfer its own NFTs out, so a sweep may
    # need topping the wallet up first. Set GAS_RESERVE_WEI to re-enable.
    gas_reserve_wei: int = 0

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            max_total_spend_wei=_env_int("MAX_TOTAL_SPEND_WEI"),
            max_spend_per_mint_wei=_env_int("MAX_SPEND_PER_MINT_WEI"),
            max_mints_total=_env_int("MAX_MINTS_TOTAL"),
            max_mints_per_hour=_env_int("MAX_MINTS_PER_HOUR"),
            max_gas_price_wei=_env_int("MAX_GAS_PRICE_WEI", 5_000_000_000),
            gas_reserve_wei=_env_int("GAS_RESERVE_WEI", 0),
        )

    def as_dict(self) -> dict:
        return asdict(self)


class Denied(Exception):
    """Raised when a spend is refused. Carries the human-readable reason."""


class SpendGuard:
    def __init__(self, limits: Limits | None = None, state_path=STATE):
        self.limits = limits or Limits.from_env()
        self.state_path = pathlib.Path(state_path)
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except ValueError:
                pass
        return {"spent_wei": 0, "mints": 0, "recent": [], "started_at": int(time.time())}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(self.state_path)

    # ------------------------------------------------------------------ checks
    def check(self, cost_wei: int, gas_price_wei: int,
              wallet_balance_wei: int | None = None) -> None:
        """Raise Denied if this spend is not permitted. Does not commit."""
        L = self.limits
        with self._lock:
            if L.max_total_spend_wei <= 0:
                raise Denied("MAX_TOTAL_SPEND_WEI is not set (or is 0). Live "
                             "execution refuses to run without an explicit cap.")
            if L.max_mints_total <= 0:
                raise Denied("MAX_MINTS_TOTAL is not set (or is 0).")
            if cost_wei > L.max_spend_per_mint_wei > 0:
                raise Denied(f"per-mint cost {cost_wei} exceeds "
                             f"MAX_SPEND_PER_MINT_WEI {L.max_spend_per_mint_wei}")
            if self._state["spent_wei"] + cost_wei > L.max_total_spend_wei:
                raise Denied(f"would exceed MAX_TOTAL_SPEND_WEI: spent "
                             f"{self._state['spent_wei']} + {cost_wei} > "
                             f"{L.max_total_spend_wei}")
            if self._state["mints"] + 1 > L.max_mints_total:
                raise Denied(f"MAX_MINTS_TOTAL reached ({L.max_mints_total})")
            if gas_price_wei > L.max_gas_price_wei > 0:
                raise Denied(f"gas price {gas_price_wei} exceeds "
                             f"MAX_GAS_PRICE_WEI {L.max_gas_price_wei}")
            if L.max_mints_per_hour > 0:
                cutoff = time.time() - 3600
                recent = [t for t in self._state["recent"] if t > cutoff]
                if len(recent) >= L.max_mints_per_hour:
                    raise Denied(f"MAX_MINTS_PER_HOUR reached "
                                 f"({L.max_mints_per_hour} in the last hour)")
            if wallet_balance_wei is not None:
                if wallet_balance_wei - cost_wei < L.gas_reserve_wei:
                    raise Denied(
                        f"would leave wallet below GAS_RESERVE_WEI "
                        f"({L.gas_reserve_wei}); balance {wallet_balance_wei}, "
                        f"cost {cost_wei}. Reserve exists so the wallet can "
                        f"always pay for its own sweep.")

    def commit(self, cost_wei: int) -> None:
        """Record a spend. Called BEFORE broadcasting, so a crash over-counts."""
        with self._lock:
            self._state["spent_wei"] += cost_wei
            self._state["mints"] += 1
            self._state["recent"] = ([t for t in self._state["recent"]
                                      if t > time.time() - 3600] + [time.time()])
            self._save()

    def refund(self, cost_wei: int) -> None:
        """Give budget back when a send provably never happened."""
        with self._lock:
            self._state["spent_wei"] = max(0, self._state["spent_wei"] - cost_wei)
            self._state["mints"] = max(0, self._state["mints"] - 1)
            self._save()

    @property
    def summary(self) -> dict:
        L = self.limits
        return {
            "spent_wei": self._state["spent_wei"],
            "spent_eth": round(self._state["spent_wei"] / 1e18, 8),
            "budget_wei": L.max_total_spend_wei,
            "budget_eth": round(L.max_total_spend_wei / 1e18, 8),
            "remaining_eth": round(
                max(0, L.max_total_spend_wei - self._state["spent_wei"]) / 1e18, 8),
            "mints": self._state["mints"],
            "max_mints": L.max_mints_total,
        }


def volume_status() -> dict:
    """Read-only view of the volume marker. Safe to call on every request.

    Kept separate from volume_check() because that one INCREMENTS the boot
    counter -- wiring the incrementing version into the /status endpoint would
    have added a "boot" per HTTP request and made the number meaningless.
    """
    d = pathlib.Path(os.environ.get("MINTSCOUT_STATE_DIR", "data"))
    marker = d / ".volume_check.json"
    out = {"state_dir": str(d), "writable": os.access(d, os.W_OK),
           "boots": 0, "persisted": False, "first_boot": None}
    try:
        if marker.exists():
            prev = json.loads(marker.read_text())
            out["boots"] = int(prev.get("boots", 0))
            out["first_boot"] = prev.get("first_boot")
            out["last_boot"] = prev.get("last_boot")
            out["persisted"] = out["boots"] > 1
    except Exception:
        pass
    return out


def volume_check() -> dict:
    """Prove whether the state directory actually persists across restarts.

    Checking that /data is *writable* is not enough -- an unmounted container
    path is writable too, it just evaporates on restart. The only definitive
    test is a boot counter: if it survives a redeploy and increments, the volume
    is real. If it reads 1 on every boot, you are writing to ephemeral container
    storage and MAX_TOTAL_SPEND_WEI stops meaning anything across a crash-loop.
    """
    d = pathlib.Path(os.environ.get("MINTSCOUT_STATE_DIR", "data"))
    marker = d / ".volume_check.json"
    out = {"state_dir": str(d), "writable": False, "boots": 0,
           "persisted": False, "first_boot": None, "detail": ""}
    try:
        d.mkdir(parents=True, exist_ok=True)
        prev = {}
        if marker.exists():
            try:
                prev = json.loads(marker.read_text())
            except ValueError:
                prev = {}
        out["boots"] = int(prev.get("boots", 0)) + 1
        out["first_boot"] = prev.get("first_boot") or int(time.time())
        if prev.get("last_boot"):
            out["seconds_since_last_boot"] = max(0, int(time.time())
                                                 - int(prev["last_boot"]))
        out["persisted"] = out["boots"] > 1
        marker.write_text(json.dumps({"boots": out["boots"],
                                      "first_boot": out["first_boot"],
                                      "last_boot": int(time.time())}, indent=1))
        out["writable"] = True
        if out["persisted"]:
            out["detail"] = (f"state survived {out['boots'] - 1} restart(s) — "
                             f"volume is mounted and persisting")
        else:
            out["detail"] = ("first boot at this path. Restart once and re-check: "
                             "if boots stays 1, the volume is NOT mounted")
    except Exception as e:
        out["detail"] = f"NOT WRITABLE ({type(e).__name__}: {e})"
    return out


def is_live() -> bool:
    """Live execution requires BOTH switches. Default is always dry-run."""
    dry = (os.environ.get("DRY_RUN", "true") or "true").strip().lower()
    live = (os.environ.get("LIVE_EXECUTION", "false") or "false").strip().lower()
    return dry in ("false", "0", "no") and live in ("true", "1", "yes")
