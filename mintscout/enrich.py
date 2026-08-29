"""Enrichment tools.

Each tool is deterministic, individually cached, and returns a structured result
that is recorded verbatim as a trajectory step. Tools run against a
ReplayContext during evaluation (offline, cutoff-enforced) and against live RPC
during operation -- the same code path, so what the eval measures is what runs.

The critical rule, enforced here rather than documented: signals that only exist
*after* the mint (velocity, final minter count, secondary activity) are outcomes,
not features. `mint_velocity` therefore returns an explicit empty result at a
pre-start decision point instead of silently leaking.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import constants as C
from .metadata import is_on_chain, resolve, summarize


class ToolRecorder:
    """Collects tool calls in trajectory form (deliverable 04)."""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def record(self, tool: str, inp: dict, out: Any, ms: float) -> Any:
        self.steps.append({"tool": tool, "input": inp, "output": out,
                           "ms": round(ms, 1)})
        return out


def _timed(fn: Callable, *a, **kw):
    t0 = time.perf_counter()
    out = fn(*a, **kw)
    return out, (time.perf_counter() - t0) * 1000


# --------------------------------------------------------------------- tools
def erc165_probe(ctx) -> dict:
    s = ctx.static
    std = s.get("standard") or "unknown"
    return {"standard": std,
            "is_erc721": bool(s.get("is_erc721")),
            "is_erc1155": bool(s.get("is_erc1155")),
            # ERC-404 has no ratified interface id, so it cannot be probed via
            # ERC-165. A contract answering both ownerOf and balanceOf is where
            # support would attach. Recorded, deliberately not inferred.
            "answers_ownerof_and_balanceof": bool(s.get("answers_ownerof_and_balanceof")),
            "note": ("ERC-404 detection intentionally not implemented: no "
                     "ratified interface id. See README cut list.")
            if std == "unknown" else None}


def collection_stats(ctx) -> dict:
    s = ctx.static
    ms = s.get("max_supply")
    # Supply sanity: vanity numbers (4444, 6969, 9999) are normal; zero or
    # absurd values are a negative signal.
    sane = ms is not None and 0 < ms <= 250_000
    return {"max_supply": ms, "owner": s.get("owner"), "name": s.get("name"),
            "symbol": s.get("symbol"), "code_size": s.get("code_size"),
            "max_supply_sane": sane,
            "note": "total_supply is an OUTCOME and is not available at decision time."}


def metadata_fetch(ctx, allow_network: bool = False) -> dict:
    """Resolve collection metadata from decision-time-safe sources only.

    IMPORTANT -- why tokenURI(1) is NOT used as a feature.
    tokenURI(1) can only be read at `latest` (no archive state), and it REVERTS
    when token 1 has never been minted. Its mere availability therefore encodes
    whether the drop minted at all, which is precisely the outcome being
    predicted. Measured on the first dataset build: 181 of 201 collections
    returned no tokenURI, concentrated in the drops that never sold. Using it
    would have silently leaked the label.

    The sources used instead are both safe:
      * DropURIUpdated  -- comes from a log with a block timestamp that the
                           ReplayContext has already filtered to <= cutoff.
                           This is the primary source.
      * contractURI()   -- collection-level, set at deploy time, independent of
                           whether any token was ever minted.
    """
    s = ctx.static
    uris = ctx.drop_uris()
    drop_uri = uris[-1]["uri"] if uris else None
    contract_uri = s.get("contract_uri")

    meta, prov, source = None, "absent", None
    if drop_uri:
        meta, prov = resolve(drop_uri, allow_network=allow_network)
        source = "drop_uri"
    if meta is None and contract_uri:
        meta, prov = resolve(contract_uri, allow_network=allow_network)
        source = "contract_uri"

    out = summarize(meta)
    out.update({
        "drop_uri": drop_uri,
        "contract_uri": (contract_uri[:120] + "...")
                        if contract_uri and len(contract_uri) > 120 else contract_uri,
        "source": source,
        # On-chain metadata is a costly signal, so it is checked on the safe
        # sources rather than on tokenURI.
        "on_chain_metadata": is_on_chain(drop_uri) or is_on_chain(contract_uri),
        "provenance": prov,
        "contract_uri_present": bool(contract_uri),
        "_tokenuri_excluded": ("tokenURI(1) deliberately excluded: it reverts "
                               "for never-minted tokens, so its availability "
                               "leaks the outcome label."),
    })
    return out


def erc6551_probe(ctx, meta: dict | None = None) -> dict:
    m = meta if meta is not None else metadata_fetch(ctx)
    return {"token_bound_account": bool(m.get("token_bound_account")),
            "source": "metadata field"}


def drop_config_history(ctx) -> dict:
    """Config revisions visible at the cutoff.

    Repeated price flips are themselves a risk signal, so the *shape* of the
    revision history is surfaced to the agent, not just the final values.
    """
    revs = ctx.config_revisions()
    prices = [r["mint_price"] for r in revs]
    return {"n_revisions_before_cutoff": len(revs),
            "distinct_prices": sorted(set(prices)),
            "price_flips": sum(1 for a, b in zip(prices, prices[1:]) if a != b),
            "revisions": [{"mint_price": r["mint_price"], "start_time": r["start_time"],
                           "end_time": r["end_time"], "max_per_wallet": r["max_per_wallet"],
                           "block_timestamp": r.get("block_timestamp")} for r in revs[-6:]]}


def mint_velocity(ctx) -> dict:
    """Mints/min since startTime.

    At the pre-start decision point this is EMPTY BY CONSTRUCTION -- the drop has
    not opened, so no mint can exist at or before the cutoff. Returning a real
    number here would be leakage; returning an explicit sentinel keeps the tool
    in the trajectory (showing it was consulted) without inventing a signal.
    """
    pd = ctx.public_drop
    if ctx.cutoff <= pd["start_time"]:
        return {"available": False, "mints_per_min": None,
                "reason": "decision point is at/before startTime; no mint has "
                          "occurred yet. Outcome signal, not a feature."}
    return {"available": False, "mints_per_min": None,
            "reason": "replay context exposes no post-cutoff mint logs."}


def economics(ctx) -> dict:
    pd = ctx.public_drop
    return {"mint_price_wei": pd["mint_price_wei"], "is_free": pd["mint_price_wei"] == 0,
            "max_per_wallet": pd["max_per_wallet"], "fee_bps": pd["fee_bps"],
            "restrict_fee_recipients": pd["restrict_fee_recipients"],
            "duration_hours": round(pd["duration_s"] / 3600, 2),
            "starts_in_s": pd["start_time"] - (pd.get("config_block_timestamp") or pd["start_time"]),
            # Advance notice is what makes the whole architecture work: the
            # config is published before the mint opens.
            "advance_notice_s": max(0, pd["start_time"] - (pd.get("config_block_timestamp") or 0))
            if pd.get("config_block_timestamp") else None}


def deployer_history(ctx, memory=None) -> dict:
    """Prior collections by this deployer and how they performed.

    Reads the memory store written after previous runs' outcomes were known.
    This is what makes run N+1 better than run N.
    """
    owner = (ctx.static or {}).get("owner")
    if memory is None or not owner:
        return {"deployer": owner, "known": False,
                "prior_collections": 0, "prior_high_value": 0,
                "note": "no memory store attached" if memory is None
                        else "deployer unknown"}
    return memory.deployer_stats(ctx.chain, owner)


# ------------------------------------------------------------------- dossier
def build_dossier(ctx, memory=None, allow_network: bool = False,
                  recorder: ToolRecorder | None = None) -> dict:
    """Run every tool and assemble the payload the agent reasons over."""
    rec = recorder or ToolRecorder()
    out: dict = {"chain": ctx.chain, "collection": ctx.collection,
                 "decision_cutoff_ts": ctx.cutoff}

    r, ms = _timed(erc165_probe, ctx)
    out["standard"] = rec.record("erc165_probe", {"collection": ctx.collection}, r, ms)

    r, ms = _timed(collection_stats, ctx)
    out["collection"] = rec.record("collection_stats", {"collection": ctx.collection}, r, ms)

    r, ms = _timed(metadata_fetch, ctx, allow_network)
    out["metadata"] = rec.record("metadata_fetch",
                                 {"allow_network": allow_network}, r, ms)

    r, ms = _timed(erc6551_probe, ctx, out["metadata"])
    out["erc6551"] = rec.record("erc6551_probe", {}, r, ms)

    r, ms = _timed(economics, ctx)
    out["economics"] = rec.record("economics", {}, r, ms)

    r, ms = _timed(drop_config_history, ctx)
    out["config_history"] = rec.record("drop_config_history", {}, r, ms)

    r, ms = _timed(mint_velocity, ctx)
    out["mint_velocity"] = rec.record("mint_velocity", {}, r, ms)

    r, ms = _timed(deployer_history, ctx, memory)
    out["deployer"] = rec.record("deployer_history", {}, r, ms)

    out["_trajectory"] = rec.steps
    return out
