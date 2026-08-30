"""Fetch and pin off-chain metadata so the eval runs offline.

Separate from static reads on purpose: this hits IPFS gateways, not the chain,
so it has completely different failure modes and rate limits.
"""
import json, pathlib, sys, collections
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor
from mintscout.dataset import load
from mintscout.metadata import resolve, is_on_chain, PIN_DIR

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/drops_robinhood.jsonl"))
    a = ap.parse_args()
    recs = load(a.data)
    print(f"dataset: {a.data}  n={len(recs)}")
    uris = collections.Counter()
    for r in recs:
        for d in r["features_at_cutoff"]["drop_uris_before_cutoff"]:
            u = (d.get("uri") or "").strip()
            if u and not is_on_chain(u):
                uris[u] += 1
        cu = (r["features_at_cutoff"]["static"].get("contract_uri") or "").strip()
        if cu and not is_on_chain(cu):
            uris[cu] += 1
    todo = [u for u in uris if u.startswith(("ipfs://", "http"))]
    print(f"{len(todo)} distinct off-chain metadata URIs to pin")
    res = collections.Counter()

    def one(u):
        m, prov = resolve(u, allow_network=True, timeout=15)
        res[prov if m else "unavailable"] += 1
        n = sum(res.values())
        if n % 25 == 0:
            print(f"  {n}/{len(todo)}  {dict(res)}", flush=True)

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(one, todo))
    print(f"done: {dict(res)}")
    print(f"pinned files: {len(list(PIN_DIR.glob('*.json')))} in {PIN_DIR}")


if __name__ == "__main__":
    main()
