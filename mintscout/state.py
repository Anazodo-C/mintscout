"""Durable operating state: what we minted, and what is permanently unmintable.

Lives on the Railway volume at $MINTSCOUT_STATE_DIR (/data in production), so it
survives redeploys. That matters more than it sounds: pushing to main
auto-deploys, so without persistence every code change wiped the record of what
had already been minted -- and the "one X account, one mint" rule silently reset
to allowing everything.

Three things are kept:

* `minted` -- every collection we actually minted, with the handle, token count
  and transaction hashes. This is the operator's record and the source of truth
  for the repeat-handle rule.
* `handles` -- the set of X accounts already minted for. Derived from `minted`,
  plus any manually seeded.
* `terminal` -- collections that can never be minted again, with the reason.
  Only genuinely irreversible failures go here.

Writes are atomic (temp file + rename) so a crash mid-write cannot leave a
truncated file that loses the whole history.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time

# Reverts that can NEVER resolve. Everything else is retried.
#   MintQuantityExceedsMaxSupply           -- supply never goes back up.
#   MintQuantityExceedsMaxMintedPerWallet  -- terminal for THIS fleet; if the
#       wallet set ever changes, clear the entry (see `forget`).
# Deliberately NOT terminal:
#   NotActive          -- a phase can open later, which is the normal case for
#                         a queued drop firing at startTime.
#   FeeRecipient*      -- drop configuration is mutable and often corrected.
TERMINAL_REVERTS = (
    "MintQuantityExceedsMaxSupply",
    "MintQuantityExceedsMaxMintedPerWallet",
)


def is_terminal(reason: str | None) -> bool:
    return bool(reason) and any(t in reason for t in TERMINAL_REVERTS)


class State:
    def __init__(self, path: str | pathlib.Path | None = None):
        base = pathlib.Path(path or os.environ.get("MINTSCOUT_STATE_DIR", "data"))
        self.path = base / "mint_state.json"
        self._lock = threading.Lock()
        self._d = self._load()

    # ------------------------------------------------------------- storage
    _SEED = pathlib.Path(__file__).resolve().parents[1] / "data/seed_state.json"

    def _load(self) -> dict:
        if not self.path.exists() and self._SEED.exists():
            # First boot on a fresh volume. Import the committed seed so
            # decisions made before persistence existed are not forgotten --
            # otherwise the one-account-one-mint rule starts from nothing.
            try:
                d = json.loads(self._SEED.read_text())
                d.setdefault("minted", {}); d.setdefault("handles", [])
                d.setdefault("terminal", {})
                self._d = d
                self._save()
                return d
            except Exception:
                pass
        try:
            d = json.loads(self.path.read_text())
            d.setdefault("minted", {})
            d.setdefault("handles", [])
            d.setdefault("terminal", {})
            return d
        except Exception:
            return {"minted": {}, "handles": [], "terminal": {}}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._d, indent=1, sort_keys=True))
            tmp.replace(self.path)          # atomic; a crash cannot truncate
        except OSError:
            pass

    # --------------------------------------------------------------- mints
    def record_mint(self, chain: str, collection: str, *, handle: str | None,
                    name: str | None, tokens: int, txs: list[str]) -> None:
        key = f"{chain}:{collection.lower()}"
        with self._lock:
            prev = self._d["minted"].get(key, {})
            self._d["minted"][key] = {
                "chain": chain, "collection": collection.lower(),
                "name": name or prev.get("name"),
                "handle": handle or prev.get("handle"),
                "tokens": int(prev.get("tokens", 0)) + int(tokens),
                "txs": (prev.get("txs") or []) + list(txs),
                "first_minted_at": prev.get("first_minted_at") or int(time.time()),
                "last_minted_at": int(time.time()),
            }
            if handle:
                h = handle.lower().lstrip("@")
                if h not in self._d["handles"]:
                    self._d["handles"].append(h)
            self._save()

    def seed_handle(self, handle: str, note: str = "") -> bool:
        """Record a handle as already-minted without an associated mint.

        Used for drops that were minted before the rule existed, and for
        accounts the operator has decided never to mint again.
        """
        h = (handle or "").lower().lstrip("@")
        if not h:
            return False
        with self._lock:
            if h in self._d["handles"]:
                return False
            self._d["handles"].append(h)
            self._d.setdefault("seeded", {})[h] = {
                "note": note, "at": int(time.time())}
            self._save()
            return True

    def has_handle(self, handle: str | None) -> bool:
        if not handle:
            return False
        return handle.lower().lstrip("@") in set(self._d.get("handles") or [])

    # ------------------------------------------------------------ terminal
    def mark_terminal(self, chain: str, collection: str, reason: str) -> None:
        key = f"{chain}:{collection.lower()}"
        with self._lock:
            if key in self._d["terminal"]:
                return
            self._d["terminal"][key] = {"reason": reason, "at": int(time.time())}
            self._save()

    def terminal_reason(self, chain: str, collection: str) -> str | None:
        e = self._d.get("terminal", {}).get(f"{chain}:{collection.lower()}")
        return e.get("reason") if e else None

    def forget(self, chain: str, collection: str) -> None:
        """Clear a terminal mark -- e.g. after changing the wallet fleet, which
        invalidates a per-wallet cap verdict."""
        with self._lock:
            self._d.get("terminal", {}).pop(f"{chain}:{collection.lower()}", None)
            self._save()

    # ------------------------------------------------------------- summary
    @property
    def handles(self) -> set:
        return {h for h in (self._d.get("handles") or [])}

    def summary(self) -> dict:
        m = self._d.get("minted", {})
        return {
            "collections_minted": len(m),
            "tokens_held": sum(int(v.get("tokens", 0)) for v in m.values()),
            "handles_used": len(self._d.get("handles") or []),
            "terminal_cached": len(self._d.get("terminal") or {}),
            "path": str(self.path),
        }

    def recent(self, n: int = 10) -> list[dict]:
        m = list(self._d.get("minted", {}).values())
        return sorted(m, key=lambda v: v.get("last_minted_at", 0), reverse=True)[:n]
