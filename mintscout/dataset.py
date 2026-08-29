"""Replay dataset builder.

Produces one JSONL record per drop. The schema is deliberately split so that
label leakage is a *structural* impossibility rather than a convention:

    features_at_cutoff  -- everything the agent may see. Every item carries a
                           timestamp <= decision_cutoff_ts.
    outcome             -- everything measurable only after the window closed.
                           ReplayContext never exposes this to the agent.

Because the public RPCs serve no archive state, nothing here is reconstructed by
simulating a past block. Every historical fact is derived from logs, plus a
small set of deploy-time-stable contract reads taken at `latest` (flagged with
their provenance -- see `_STATIC_PROVENANCE`).
"""
from __future__ import annotations

import json
import pathlib
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from . import constants as C
from .blockclock import BlockClock
from .decode import (decode_drop_uri_updated, decode_public_drop_updated,
                     decode_seadrop_mint, decode_transfer)
from .rpc import RpcClient, client

ROOT = pathlib.Path(__file__).resolve().parents[1]
SECONDARY_WINDOW_S = 48 * 3600

_STATIC_PROVENANCE = (
    "Read via eth_call at 'latest'. The public RPCs have no archive state, so a "
    "read as-of the drop's start block is impossible. These fields (name, symbol, "
    "maxSupply, owner, tokenURI shape) are set at deploy time and are stable in "
    "practice; totalSupply is NOT stable and is therefore stored under 'outcome', "
    "never under features."
)


def _pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def _chunks(xs: list, n: int) -> Iterable[list]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# ---------------------------------------------------------------- static reads
def _read_static(rpc: RpcClient, col: str) -> dict:
    """Deploy-time-stable contract facts. See _STATIC_PROVENANCE.

    Read failures are recorded explicitly in `_read_errors` rather than being
    collapsed into None. A missing value must be distinguishable from a value we
    failed to fetch, otherwise infrastructure noise becomes a model feature.
    """
    errors: list[str] = []

    def s(sel: str, arg: str = "") -> str | None:
        try:
            return rpc.try_call(col, sel + arg)
        except Exception as e:
            errors.append(f"{sel}: {type(e).__name__}")
            return None

    def as_int(h: str | None) -> int | None:
        if not h or h == "0x":
            return None
        try:
            return int(h[:66], 16)
        except ValueError:
            return None

    def as_str(h: str | None) -> str | None:
        if not h or h == "0x":
            return None
        try:
            raw = h[2:]
            off = int(raw[0:64], 16) * 2
            ln = int(raw[off:off + 64], 16)
            return bytes.fromhex(raw[off + 64: off + 64 + ln * 2]).decode("utf8", "replace")
        except Exception:
            return None

    word = lambda a: "0" * 24 + a[2:].lower()
    iface = lambda i: rpc.try_call(col, C.SEL_SUPPORTS_INTERFACE + i[2:] + "0" * 56)

    out: dict = {}
    out["name"] = as_str(s(C.SEL_NAME))
    out["symbol"] = as_str(s(C.SEL_SYMBOL))
    out["max_supply"] = as_int(s(C.SEL_MAX_SUPPLY))
    ow = s(C.SEL_OWNER)
    out["owner"] = ("0x" + ow[-40:]) if ow else None
    out["contract_uri"] = as_str(s(C.SEL_CONTRACT_URI))
    out["base_uri"] = as_str(s(C.selector("baseURI()"))) or as_str(s(C.selector("tokenBaseURI()")))
    # Captured for completeness/inspection but EXCLUDED from features: tokenURI(1)
    # reverts for never-minted tokens, so its availability leaks the outcome.
    # See enrich.metadata_fetch for the full reasoning.
    out["token_uri_1_EXCLUDED_LEAKY"] = as_str(s(C.SEL_TOKEN_URI, "0" * 63 + "1"))
    i721, i1155 = iface(C.ERC165["erc721"]), iface(C.ERC165["erc1155"])
    out["is_erc721"] = bool(i721 and int(i721, 16))
    out["is_erc1155"] = bool(i1155 and int(i1155, 16))
    if out["is_erc721"]:
        out["standard"] = "erc721"
    elif out["is_erc1155"]:
        out["standard"] = "erc1155"
    else:
        # A contract answering BOTH ownerOf and balanceOf but neither interface
        # id is where ERC-404 support would attach. Recorded, not decoded --
        # ERC-404 has no ratified interface id. See README "Cut list".
        has_owner_of = s(C.SEL_OWNER_OF, "0" * 63 + "1") is not None
        has_balance = rpc.try_call(col, C.SEL_BALANCE_OF + word(C.PROBE_ADDRESS)) is not None
        out["standard"] = "unknown"
        out["answers_ownerof_and_balanceof"] = bool(has_owner_of and has_balance)
    try:
        out["code_size"] = len(rpc.get_code(col))
    except Exception:
        out["code_size"] = None
    out["_provenance"] = _STATIC_PROVENANCE
    out["_read_errors"] = errors
    out["_read_ok"] = not errors
    return out


def _read_total_supply(rpc: RpcClient, col: str) -> int | None:
    h = rpc.try_call(col, C.SEL_TOTAL_SUPPLY)
    try:
        return int(h[:66], 16) if h else None
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ main build
# Regression fixtures (BUILD.md §11). These are recent drops whose 48h
# post-window observation period has not fully elapsed, so they are force-included
# and their truncated observation window is recorded on the record rather than
# quietly ignored.
REGRESSION_FIXTURES = {
    "0xed74a2029ff2633f21260e097e451358c26a507d": "UNDEADLINES",
    "0xe801b3399193ad1af4e0bbcad72a45c2ff819a8f": "DUNLAPS",
    "0x539cdd042c2f3d93ebc5be7dfff0c79f3b4fabf0": "StonkBrokers",
}


def build(chains: list[str], days: float, out_path: str, sample: int = 200,
          seed: int = 1337, dust: int = C.DUST_THRESHOLD_WEI,
          force_include: dict[str, str] | None = None) -> dict:
    now = int(time.time())
    window_start = now - int(days * 86400)
    force_include = REGRESSION_FIXTURES if force_include is None else force_include
    records: list[dict] = []
    meta: dict = {"chains": {}, "built_at": now, "days": days,
                  "sample_target": sample, "seed": seed, "dust_threshold_wei": dust}

    for chain in chains:
        print(f"\n=== {chain}: scanning {days} days ===", flush=True)
        rpc = client(chain)
        clock = BlockClock(rpc)
        err = clock.calibrate()
        tip = rpc.block_number()
        from_block = max(1, clock.block_of(window_start))
        print(f"  blocks {from_block}..{tip} ({tip - from_block} blocks), "
              f"clock max err {err:.1f}s", flush=True)

        pdu = rpc.get_logs_chunked(C.SEADROP, [C.TOPIC_PUBLIC_DROP_UPDATED],
                                   from_block, tip, progress=True, label="PublicDropUpdated")
        duri = rpc.get_logs_chunked(C.SEADROP, [C.TOPIC_DROP_URI_UPDATED],
                                    from_block, tip, progress=True, label="DropURIUpdated")
        print(f"  PublicDropUpdated={len(pdu)}  DropURIUpdated={len(duri)}", flush=True)

        # --- group config revisions by collection --------------------------
        revs: dict[str, list] = defaultdict(list)
        for l in pdu:
            p = decode_public_drop_updated(l, chain)
            p.block_timestamp = int(clock.ts_of(p.block_number))
            revs[p.collection].append(p)
        uris: dict[str, list] = defaultdict(list)
        for l in duri:
            col, uri = decode_drop_uri_updated(l)
            uris[col].append({"uri": uri, "block_number": int(l["blockNumber"], 16),
                              "block_timestamp": int(clock.ts_of(int(l["blockNumber"], 16)))})

        # --- eligibility ----------------------------------------------------
        # A drop enters the dataset when:
        #   * some config revision is free (<= dust), and
        #   * that free phase's window closed >= 48h ago, so the ground-truth
        #     label (which needs 48h of post-window secondary activity) exists.
        eligible, forced = [], []
        for col, rl in revs.items():
            rl.sort(key=lambda p: (p.block_number, p.log_index))
            free = next((p for p in rl if p.mint_price <= dust), None)
            is_fixture = col.lower() in force_include
            if free is None or free.start_time == 0:
                continue
            if free.end_time <= free.start_time:
                continue
            if free.start_time < window_start:
                continue                       # window began before our scan
            if free.end_time + SECONDARY_WINDOW_S > now:
                # Label observation period not fully elapsed. Regression fixtures
                # are kept anyway (with the truncation recorded); everything else
                # is dropped so the main set's labels are all measured over the
                # full 48h.
                if is_fixture and free.end_time < now:
                    forced.append((col, rl, free))
                continue
            (forced if is_fixture else eligible).append((col, rl, free))
        print(f"  collections with a free config: "
              f"{sum(1 for c, r in revs.items() if any(p.mint_price <= dust for p in r))}"
              f"   label-eligible (closed >48h ago): {len(eligible)}"
              f"   regression fixtures: {len(forced)}", flush=True)
        meta["chains"][chain] = {
            "from_block": from_block, "to_block": tip,
            "public_drop_updated": len(pdu), "drop_uri_updated": len(duri),
            "distinct_collections": len(revs), "eligible": len(eligible),
            "clock_max_error_s": round(err, 1),
        }
        if not eligible:
            continue

        # --- sample ----------------------------------------------------------
        rng = random.Random(seed)
        per_chain = sample if len(chains) == 1 else max(1, sample // len(chains))
        if len(eligible) > per_chain:
            eligible = rng.sample(eligible, per_chain)
        # fixtures are always present and never double-counted
        have = {c for c, _, _ in eligible}
        eligible += [f for f in forced if f[0] not in have]
        cols = [c for c, _, _ in eligible]
        print(f"  sampled {len(cols)} collections", flush=True)

        # --- mints (topic1 OR-array, batched) --------------------------------
        # Chunk by TIME LOCALITY, not arbitrary order: collections whose drop
        # windows are close together share a narrow block range, so each query
        # fetches only the blocks that can possibly contain their events. Without
        # this, every chunk spans the entire scan window and the fetched volume
        # is ~10x larger for no extra information.
        by_start = sorted(eligible, key=lambda e: e[2].start_time)
        mints_by_col: dict[str, list] = defaultdict(list)
        for i, grp in enumerate(_chunks(by_start, 40)):
            g_lo = min(f.start_time for _, _, f in grp)
            g_hi = max(f.end_time for _, _, f in grp)
            blo = max(1, clock.block_of(g_lo) - 10)
            bhi = min(tip, clock.block_of(g_hi) + 10)
            logs = rpc.get_logs_chunked(
                C.SEADROP, [C.TOPIC_SEADROP_MINT, [_pad(a) for a, _, _ in grp]],
                blo, bhi, progress=True, label=f"mints {i + 1}")
            for l in logs:
                m = decode_seadrop_mint(l, chain)
                m.block_timestamp = int(clock.ts_of(m.block_number))
                mints_by_col[m.collection].append(m)
        print(f"  mint events: {sum(len(v) for v in mints_by_col.values())}", flush=True)

        # --- transfers (address-array, batched) ------------------------------
        # Secondary activity is only ever read in the 48h AFTER each window
        # closes, so query exactly that, grouped by end time.
        by_end = sorted(eligible, key=lambda e: e[2].end_time)
        xfer_by_col: dict[str, list] = defaultdict(list)
        for i, grp in enumerate(_chunks(by_end, 25)):
            g_lo = min(f.end_time for _, _, f in grp)
            g_hi = max(f.end_time for _, _, f in grp) + SECONDARY_WINDOW_S
            blo = max(1, clock.block_of(g_lo) - 10)
            bhi = min(tip, clock.block_of(g_hi) + 10)
            logs = rpc.get_logs_chunked([a for a, _, _ in grp], [C.TOPIC_TRANSFER],
                                        blo, bhi, progress=True,
                                        label=f"transfers {i + 1}")
            for l in logs:
                t = decode_transfer(l)
                if t is None:
                    continue
                t["block_timestamp"] = int(clock.ts_of(t["block_number"]))
                xfer_by_col[t["contract"]].append(t)
        print(f"  transfer events: {sum(len(v) for v in xfer_by_col.values())}", flush=True)

        # --- static reads -----------------------------------------------------
        statics: dict[str, dict] = {}
        supplies: dict[str, int | None] = {}

        def load(col: str) -> None:
            statics[col] = _read_static(rpc, col)
            supplies[col] = _read_total_supply(rpc, col)

        with ThreadPoolExecutor(max_workers=rpc.cfg["max_concurrency"]) as ex:
            list(ex.map(load, cols))
        print(f"  static reads complete for {len(statics)} collections", flush=True)

        # --- assemble ---------------------------------------------------------
        for col, rl, free in eligible:
            cutoff = free.start_time
            st = statics.get(col, {})
            mints = sorted(mints_by_col.get(col, []), key=lambda m: m.block_number)
            xf = xfer_by_col.get(col, [])
            rec = {
                "chain": chain,
                "collection": col,
                "decision_cutoff_ts": cutoff,
                "features_at_cutoff": {
                    "public_drop": {
                        "mint_price_wei": free.mint_price,
                        "start_time": free.start_time,
                        "end_time": free.end_time,
                        "duration_s": free.duration_s,
                        "max_per_wallet": free.max_per_wallet,
                        "fee_bps": free.fee_bps,
                        "restrict_fee_recipients": free.restrict_fee_recipients,
                        "config_block_timestamp": free.block_timestamp,
                    },
                    "config_revisions_before_cutoff": [
                        p.as_dict() for p in rl if (p.block_timestamp or 0) <= cutoff],
                    "drop_uris_before_cutoff": [
                        u for u in uris.get(col, []) if u["block_timestamp"] <= cutoff],
                    "static": st,
                },
                "config_revisions_all": [p.as_dict() for p in rl],
                "outcome": {
                    "total_minted": sum(m.quantity for m in mints),
                    "mint_txs": len({m.tx_hash for m in mints}),
                    "unique_minters": len({m.minter for m in mints}),
                    "max_supply": st.get("max_supply"),
                    "total_supply_now": supplies.get(col),
                    "first_mint_ts": mints[0].block_timestamp if mints else None,
                    "last_mint_ts": mints[-1].block_timestamp if mints else None,
                    "secondary_transfers_48h": sum(
                        1 for t in xf
                        if t["from"] != C.ZERO_ADDRESS
                        and free.end_time <= t["block_timestamp"] <= free.end_time + SECONDARY_WINDOW_S),
                    # How much of the 48h observation window had actually elapsed
                    # when the dataset was built. < 48h only for regression
                    # fixtures; surfaced so a truncated label is never mistaken
                    # for a complete one.
                    "secondary_window_elapsed_s": max(0, min(SECONDARY_WINDOW_S,
                                                             now - free.end_time)),
                    "secondary_window_truncated": (now - free.end_time) < SECONDARY_WINDOW_S,
                    "is_regression_fixture": col.lower() in force_include,
                    "fixture_name": force_include.get(col.lower()),
                    "repriced_after_cutoff": any(
                        p.mint_price > dust and (p.block_timestamp or 0) > cutoff for p in rl),
                    "mints_after_cutoff": sum(
                        1 for m in mints if (m.block_timestamp or 0) > cutoff),
                    "mints_before_cutoff": sum(
                        1 for m in mints if (m.block_timestamp or 0) <= cutoff),
                },
            }
            records.append(rec)

        meta["chains"][chain]["sampled"] = len(cols)

    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    (out.parent / (out.stem + "_meta.json")).write_text(json.dumps(meta, indent=2) + "\n")
    print(f"\nwrote {len(records)} drops -> {out}")
    return meta


def load(path: str | pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in pathlib.Path(path).read_text().splitlines() if l.strip()]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="mintscout.dataset")
    ap.add_argument("--chains", default="robinhood")
    ap.add_argument("--days", type=float, default=6.0)
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--out", default=str(ROOT / "data/drops.jsonl"))
    a = ap.parse_args()
    build(a.chains.split(","), a.days, a.out, a.sample, a.seed)
