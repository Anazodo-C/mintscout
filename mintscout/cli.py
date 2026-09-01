"""MintScout CLI.  python -m mintscout.cli --help"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

from . import constants as C


def _load_dotenv() -> None:
    """Read .env so commands do not need shell gymnastics to pass secrets.

    MINT_SEED is a 12-word mnemonic, so extracting it with grep/cut and
    re-quoting it through the shell is fragile -- and getting it wrong means a
    confusing failure while holding a funded wallet. Existing environment
    variables always win, so `MINT_SEED=... python -m mintscout.cli` still works.
    """
    f = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


_load_dotenv()


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


def cmd_fund(a):
    """Top up the fleet to a target balance. Dry-run unless --live is passed."""
    import os
    from .funding import execute_funding, plan_funding, wait_for_funding
    from .rpc import client

    seed = (os.environ.get("MINT_SEED") or "").strip()
    if not seed:
        print("MINT_SEED is not set. It is env-only by design; export it or put "
              "it in .env (which is gitignored).")
        return 2

    rpc = client(a.chain)
    if a.split_even:
        from .funding import even_split_target
        target = even_split_target(rpc, seed, a.wallets, a.funder_index)
        print(f"even split across {a.wallets} wallets "
              f"-> {target / 1e18:.8f} ETH each (gas reserved)")
    else:
        if not a.target_eth:
            print("pass --target-eth, or --split-even to divide the funder's "
                  "balance evenly across the fleet")
            return 2
        target = int(a.target_eth * 1e18)
    print(f"funding plan — {a.chain}")
    plan = plan_funding(rpc, seed, target, a.wallets, a.funder_index)

    print(f"  funder      [{a.funder_index}] {plan.funder}")
    print(f"  balance     {plan.funder_balance / 1e18:.6f} ETH")
    print(f"  gas price   {plan.gas_price / 1e9:.4f} gwei")
    print(f"  fleet       {a.wallets} wallets "
          f"(indices 0..{a.wallets - 1})")
    print()
    if not plan.transfers:
        print("  nothing to do — every wallet is already at or above target.")
        return 0
    for t in plan.transfers:
        print(f"  [{t['index']:>2}] {t['address']}  "
              f"has {t['balance'] / 1e18:.6f}  ->  send "
              f"{t['deficit'] / 1e18:.6f} ETH")
    print()
    print(f"  transfers   {len(plan.transfers)}")
    print(f"  value       {plan.total_value / 1e18:.6f} ETH")
    print(f"  gas         {plan.total_gas_cost / 1e18:.6f} ETH")
    print(f"  TOTAL       {plan.total_cost / 1e18:.6f} ETH")
    if not plan.affordable:
        print(f"\n  INSUFFICIENT FUNDS — funder is short "
              f"{plan.shortfall / 1e18:.6f} ETH. Nothing sent.")
        return 1
    print(f"  remaining   {(plan.funder_balance - plan.total_cost) / 1e18:.6f} "
          f"ETH in funder afterwards")

    if a.max_total_eth and plan.total_cost > a.max_total_eth * 1e18:
        print(f"\n  BLOCKED — total {plan.total_cost / 1e18:.6f} ETH exceeds "
              f"--max-total {a.max_total_eth} ETH. Nothing sent.")
        return 1

    if not a.live:
        print("\n  DRY RUN — nothing broadcast. Re-run with --live to send.")
        return 0

    print("\n  LIVE — broadcasting…")
    res = execute_funding(rpc, seed, plan, a.funder_index)
    if res["sent"]:
        print("\n  waiting for receipts…")
        wait_for_funding(rpc, res)
    print(f"\n  sent={len(res['sent'])} failed={len(res['failed'])} "
          f"value={res['total_value'] / 1e18:.6f} ETH")
    return 0 if not res["failed"] else 1


def cmd_sweep(a):
    """Move minted NFTs out of the hot fleet into a vault you control.

    Dry-run unless --live. Each wallet signs its own transfers: an NFT can only
    be moved by its owner, so this is one transaction per token per wallet.
    """
    import os
    from .funding import derive_fleet
    from .rpc import client
    from .state import State
    from .sweep import (TRANSFER_GAS_LIMIT, build_transfer_tx,
                        find_fleet_holdings, plan_sweep)

    seed = (os.environ.get("MINT_SEED") or "").strip()
    if not seed:
        print("MINT_SEED is not set (env-only by design).")
        return 2
    if not a.to or not a.to.startswith("0x") or len(a.to) != 42:
        print("--to must be a 0x-prefixed 20-byte address")
        return 2

    rpc = client(a.chain)
    gas_price = int(rpc.raw("eth_gasPrice", []), 16)

    # Which collections to look in: the mint ledger, unless overridden.
    if a.collection:
        cols = [a.collection.lower()]
    else:
        st = State()
        cols = sorted({v["collection"] for v in st.recent(200)
                       if v.get("chain") == a.chain})
    if not cols:
        print("no minted collections on record; pass --collection to sweep one "
              "explicitly")
        return 1
    print(f"sweeping {a.chain} -> {a.to}")
    print(f"  collections: {len(cols)}")

    fleet = derive_fleet(seed, a.wallets)
    print(f"  scanning {len(fleet)} wallet(s) x {len(cols)} collection(s) "
          f"in one pass…")
    all_held = find_fleet_holdings(rpc, [w[1] for w in fleet], cols)

    total_tokens = 0
    plans = []
    skipped_self = 0
    for idx, addr, key in fleet:
        holdings = all_held.get(addr.lower(), {})
        n = sum(len(v) for v in holdings.values())
        if addr.lower() == a.to.lower():
            # Consolidating INTO one of the fleet wallets: its tokens are
            # already at the destination. A self-transfer burns gas and most
            # ERC-721s revert on from == to.
            skipped_self = n
            print(f"  [{idx:>2}] {addr[:12]}… is the DESTINATION — "
                  f"{n} token(s) already here, skipped")
            continue
        if not n:
            continue
        total_tokens += n
        plans.append((idx, addr, key, holdings))
        for col, ids in holdings.items():
            print(f"  [{idx:>2}] {addr[:12]}…  {col[:12]}…  "
                  f"{len(ids)} token(s): {ids[:8]}{'…' if len(ids) > 8 else ''}")

    if not total_tokens:
        print(f"  nothing to move — no tokens outside the destination"
              + (f" ({skipped_self} already there)" if skipped_self else "") + ".")
        return 0
    cost = total_tokens * TRANSFER_GAS_LIMIT * gas_price * 2
    print(f"\n  {total_tokens} token(s) across {len(plans)} wallet(s)")
    print(f"  gas at {gas_price / 1e9:.3f} gwei: ~{cost / 1e18:.6f} ETH total")
    print("\n  NOTE: transferring an ERC-6551 token also transfers whatever its")
    print("  token-bound account holds. Check before sweeping such collections.")

    if not a.live:
        print("\n  DRY RUN — nothing broadcast. Re-run with --live to send.")
        return 0

    print("\n  LIVE — broadcasting…")
    sent = failed = 0
    for idx, addr, key, holdings in plans:
        try:
            nonce = int(rpc.raw("eth_getTransactionCount", [addr, "pending"]), 16)
        except Exception as e:
            print(f"  [{idx:>2}] nonce read failed: {type(e).__name__}")
            continue
        i = 0
        for col, ids in holdings.items():
            for tid in ids:
                tx = build_transfer_tx(rpc, addr, col, tid, a.to, nonce + i,
                                       gas_price)
                i += 1
                try:
                    from eth_account import Account
                    acct = Account.from_key(key)
                    payload = {k: v for k, v in tx.items() if k != "from"}
                    signed = acct.sign_transaction(payload)
                    raw = signed.raw_transaction.hex()
                    h = rpc.raw("eth_sendRawTransaction",
                                [raw if raw.startswith("0x") else "0x" + raw])
                    sent += 1
                    print(f"  [{idx:>2}] token {tid} -> {a.to[:10]}…  tx={h}")
                except Exception as e:
                    failed += 1
                    print(f"  [{idx:>2}] token {tid} FAILED: "
                          f"{type(e).__name__}: {str(e)[:90]}")
    print(f"\n  sent={sent} failed={failed}")
    return 0 if not failed else 1


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

    fu = sub.add_parser("fund", help="top up the wallet fleet to a target balance")
    fu.add_argument("--chain", default="robinhood")
    fu.add_argument("--target-eth", type=float, default=0.0,
                    help="desired balance PER WALLET; only the deficit is sent")
    fu.add_argument("--split-even", action="store_true",
                    help="split the funder's current balance evenly across the "
                         "fleet (reserves transfer gas first)")
    fu.add_argument("--wallets", type=int, default=C.MAX_WALLETS_DEFAULT)
    fu.add_argument("--funder-index", type=int, default=0)
    fu.add_argument("--max-total-eth", type=float, default=0.0,
                    dest="max_total_eth", help="refuse if the plan exceeds this")
    fu.add_argument("--live", action="store_true",
                    help="actually broadcast (default is dry-run)")
    fu.set_defaults(fn=cmd_fund)

    sw = sub.add_parser("sweep", help="move minted NFTs to a vault address")
    sw.add_argument("--to", required=True, help="destination vault address")
    sw.add_argument("--chain", default="robinhood")
    sw.add_argument("--wallets", type=int, default=C.MAX_WALLETS_DEFAULT)
    sw.add_argument("--collection", default=None,
                    help="sweep only this collection (default: all in the ledger)")
    sw.add_argument("--live", action="store_true",
                    help="actually broadcast (default is dry-run)")
    sw.set_defaults(fn=cmd_sweep)

    f = sub.add_parser("preflight", help="live pre-flight demo (read-only)")
    f.add_argument("--chain", default="robinhood"); f.add_argument("--minutes", type=float, default=45)
    f.add_argument("--n", type=int, default=10); f.add_argument("--fee-recipient", default=None)
    f.add_argument("--minter", default=None); f.set_defaults(fn=cmd_preflight)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
