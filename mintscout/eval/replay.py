"""ReplayContext -- the decision cutoff, enforced in code.

The integrity of the entire evaluation rests on the agent never seeing anything
that happened at or after the moment it would have had to decide. That is
enforced here structurally: ReplayContext holds the raw record privately and
exposes only a filtered view. There is no code path from a ReplayContext to the
`outcome` block.

`tests/test_no_leakage.py` asserts this against every record in the dataset.
"""
from __future__ import annotations

from typing import Any


class LeakageError(AssertionError):
    """Raised when something tries to read post-cutoff data through the context."""


class ReplayContext:
    """A drop, as it looked at `decision_cutoff_ts` and not one second later."""

    # Keys that exist in the record but must never be reachable from here.
    _FORBIDDEN = ("outcome", "config_revisions_all")

    def __init__(self, rec: dict, cutoff_ts: int | None = None):
        self._rec = rec
        self._cutoff = int(cutoff_ts if cutoff_ts is not None else rec["decision_cutoff_ts"])

    # -------------------------------------------------------------- identity
    @property
    def chain(self) -> str:
        return self._rec["chain"]

    @property
    def collection(self) -> str:
        return self._rec["collection"]

    @property
    def cutoff(self) -> int:
        return self._cutoff

    # -------------------------------------------------------------- features
    @property
    def public_drop(self) -> dict:
        return dict(self._rec["features_at_cutoff"]["public_drop"])

    @property
    def static(self) -> dict:
        return dict(self._rec["features_at_cutoff"].get("static") or {})

    def config_revisions(self) -> list[dict]:
        """Config revisions visible at the cutoff. A drop repriced *after* the
        cutoff (DUNLAPS) must look, from here, exactly like a free drop -- which
        is the point of the challenging case."""
        return [r for r in self._rec["features_at_cutoff"]["config_revisions_before_cutoff"]
                if (r.get("block_timestamp") or 0) <= self._cutoff]

    def drop_uris(self) -> list[dict]:
        return [u for u in self._rec["features_at_cutoff"]["drop_uris_before_cutoff"]
                if (u.get("block_timestamp") or 0) <= self._cutoff]

    def logs(self, kind: str = "all") -> list[dict]:
        """Any timestamped item, hard-filtered to <= cutoff.

        Mint velocity is deliberately unavailable here: at the pre-start decision
        point no mint has happened yet, so this returns an empty list for
        `kind="mints"` by construction rather than by accident.
        """
        items: list[dict] = []
        if kind in ("all", "configs"):
            items += self.config_revisions()
        if kind in ("all", "uris"):
            items += self.drop_uris()
        bad = [i for i in items if (i.get("block_timestamp") or 0) > self._cutoff]
        if bad:
            raise LeakageError(
                f"{self.collection}: {len(bad)} item(s) with timestamp > cutoff "
                f"{self._cutoff} escaped the filter")
        return items

    # ------------------------------------------------------------- guardrails
    def __getitem__(self, key: str) -> Any:
        if key in self._FORBIDDEN:
            raise LeakageError(
                f"{key!r} is outcome data and is not readable at decision time. "
                "This is the leakage control described in the README.")
        return self._rec[key]

    def __getattr__(self, name: str) -> Any:
        if name in self._FORBIDDEN:
            raise LeakageError(
                f"{name!r} is outcome data and is not readable at decision time.")
        raise AttributeError(name)

    def dossier(self) -> dict:
        """The exact payload handed to the agent. Nothing else reaches it."""
        return {
            "chain": self.chain,
            "collection": self.collection,
            "decision_cutoff_ts": self._cutoff,
            "public_drop": self.public_drop,
            "config_revisions_visible": self.config_revisions(),
            "drop_uris_visible": self.drop_uris(),
            "static": self.static,
        }
