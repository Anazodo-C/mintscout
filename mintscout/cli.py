"""MintScout CLI.  python -m mintscout.cli --help"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import constants as C


def cmd_verify(a):
    from .verify import main as vmain
    return vmain([])


def cmd_dataset(a):
    from .dataset import build
    build(a.chains.split(","), a.days, a.out, a.sample, a.seed)
    return 0


def cmd_eval(a):
    from .eval.run import main as emain
    argv = ["--data", a.data, "--arms", a.arms, "--trajectories", str(a.trajectories)]
    if a.limit:
        argv += ["--limit", str(a.limit)]
    if a.no_cache:
        argv += ["--no-cache"]
    return emain(argv)


def cmd_report(a):
    from .report import main as rmain
    return rmain()


def cmd_watch(a):
    """Tail SeaDrop for free drops. Deterministic; no LLM in this path."""
    from .decode import decode_public_drop_updated
    from .rpc import client
    rpc = client(a.chain)
    tip = rpc.block_number()
    frm = tip - int(a.minutes * 60 / rpc.cfg["block_time"])
    logs = rpc.get_logs_chunked(C.SEADROP, [C.TOPIC_PUBLIC_DROP_UPDATED], frm, tip)
    now = int(time.time())
    free = []
    for l in logs:
        p = decode_public_drop_updated(l, a.chain)
        if p.mint_price <= a.dust:
            free.append(p)
    print(f"{a.chain}: {len(logs)} drop configs in the last {a.minutes} min, "
          f"{len(free)} free (<= {a.dust} wei)")
    upcoming = sorted((p for p in free if p.end_time > now), key=lambda p: p.start_time)
    print(f"{len(upcoming)} still open or upcoming:\n")
    for p in upcoming[:a.limit]:
        when = p.start_time - now
        state = "OPEN" if p.start_time <= now else f"in {when//60}m{when%60}s"
        print(f"  {p.collection}  cap={p.max_per_wallet:<5} "
              f"window={(p.end_time-p.start_time)/3600:5.1f}h  {state}")
    return 0


def cmd_plan(a):
    """Show the fill plan for a cap/supply -- no keys, no network."""
    from .wallets import plan_fill
    p = plan_fill(a.cap, a.supply, a.wallets, a.target)
    print(json.dumps(p, indent=2))
    return 0


def cmd_preflight(a):
    """Live pre-flight demo: simulate a batch and report wasted gas avoided.

    This is the defensible gas number -- gas NOT burned on mints that were
    certain to revert. Read-only: eth_call only, nothing is signed or sent.
    """
    from .decode import decode_public_drop_updated
    from .executor import MintCall, preflight, recent_fee_recipient
    from .rpc import client
    rpc = client(a.chain)
    tip = rpc.block_number()
    frm = tip - int(a.minutes * 60 / rpc.cfg["block_time"])
    logs = rpc.get_logs_chunked(C.SEADROP, [C.TOPIC_PUBLIC_DROP_UPDATED], frm, tip)
    now = int(time.time())
    seen, cands = set(), []
    for l in reversed(logs):
        p = decode_public_drop_updated(l, a.chain)
        if p.collection in seen or p.mint_price > 0:
            continue
        seen.add(p.collection)
        cands.append(p)
        if len(cands) >= a.n:
            break
    fee = a.fee_recipient or recent_fee_recipient(rpc)
    minter = a.minter or C.PROBE_ADDRESS
    print(f"pre-flight over {len(cands)} recently-configured free drops on {a.chain}")
    print(f"  fee recipient (read from a recent SeaDropMint log): {fee}")
    print(f"  simulated minter: {minter}\n")
    calls = [MintCall(a.chain, p.collection, fee, minter, 1) for p in cands]
    res = preflight(rpc, calls)
    for d in res.dropped[:a.n]:
        print(f"  DROP {d['collection']}  [{d.get('check','')}] {d['reason'][:96]}")
    print(f"\n  kept {len(res.kept)} / {len(calls)}")
    print(f"  wasted gas avoided: {res.wasted_gas_avoided:,} gas "
          f"= {res.wasted_fee_avoided_wei/1e18:.8f} ETH")
    print("\n  (read-only: eth_call only. Nothing signed, nothing sent.)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mintscout")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify", help="assert chain ids, SeaDrop bytecode, topics").set_defaults(fn=cmd_verify)

    d = sub.add_parser("dataset", help="build the replay dataset")
    d.add_argument("--chains", default="robinhood"); d.add_argument("--days", type=float, default=9.0)
    d.add_argument("--sample", type=int, default=700); d.add_argument("--seed", type=int, default=1337)
    d.add_argument("--out", default="data/drops_robinhood.jsonl"); d.set_defaults(fn=cmd_dataset)

    e = sub.add_parser("eval", help="run evaluation arms")
    e.add_argument("--data", default="data/drops_robinhood.jsonl")
    e.add_argument("--arms", default="baseline_mint_all,deterministic")
    e.add_argument("--limit", type=int, default=0); e.add_argument("--no-cache", action="store_true")
    e.add_argument("--trajectories", type=int, default=5); e.set_defaults(fn=cmd_eval)

    sub.add_parser("report", help="write results/comparison.md").set_defaults(fn=cmd_report)

    w = sub.add_parser("watch", help="tail SeaDrop for free drops")
    w.add_argument("--chain", default="robinhood"); w.add_argument("--minutes", type=float, default=30)
    w.add_argument("--dust", type=int, default=0); w.add_argument("--limit", type=int, default=25)
    w.set_defaults(fn=cmd_watch)

    p = sub.add_parser("plan", help="wallet fill plan")
    p.add_argument("--cap", type=int, required=True); p.add_argument("--supply", type=int, default=0)
    p.add_argument("--wallets", type=int, default=C.MAX_WALLETS_DEFAULT)
    p.add_argument("--target", type=int, default=C.TARGET_PER_COLLECTION); p.set_defaults(fn=cmd_plan)

    f = sub.add_parser("preflight", help="live pre-flight demo (read-only)")
    f.add_argument("--chain", default="robinhood"); f.add_argument("--minutes", type=float, default=45)
    f.add_argument("--n", type=int, default=10); f.add_argument("--fee-recipient", default=None)
    f.add_argument("--minter", default=None); f.set_defaults(fn=cmd_preflight)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
