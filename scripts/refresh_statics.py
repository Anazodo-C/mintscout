"""Re-read static contract facts for a built dataset, and pin metadata offline.

Run after `mintscout.dataset build`. Kept as a separate pass because static reads
are cheap and idempotent, while the log backfill is expensive -- so a static-read
bug is recoverable without refetching 9 days of logs.

Uses low concurrency deliberately: the first build ran these at full fan-out
during a heavy backfill and rate-limited reads were being recorded as missing
values. See CHANGELOG "Removed #2".
"""
import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor

from mintscout.dataset import _read_static, _read_total_supply
from mintscout.metadata import resolve, is_on_chain
from mintscout.rpc import client

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/drops_robinhood.jsonl"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pin", action="store_true", help="fetch+pin IPFS metadata")
    a = ap.parse_args()

    path = pathlib.Path(a.data)
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    by_chain = {}
    for r in recs:
        by_chain.setdefault(r["chain"], []).append(r)

    for chain, rs in by_chain.items():
        rpc = client(chain, rps=14.0)
        print(f"{chain}: refreshing statics for {len(rs)} collections "
              f"(workers={a.workers})")
        done = [0]

        def one(r):
            # Keep the BEST partial result rather than discarding everything when
            # a single selector read fails. An earlier version replaced the whole
            # record with {"_read_ok": False}, throwing away name/max_supply/owner
            # that had actually been read successfully.
            st = {"_read_ok": False, "_read_errors": ["not attempted"]}
            for attempt in range(2):
                try:
                    cand = _read_static(rpc, r["collection"])
                    if len(cand.get("_read_errors") or []) < len(st.get("_read_errors") or [99]):
                        st = cand
                    if cand.get("_read_ok"):
                        break
                except Exception as e:
                    st.setdefault("_read_errors", []).append(type(e).__name__)
                time.sleep(0.8 * (attempt + 1))
            # preserve provenance note, drop the leaky field from features
            st.pop("token_uri_1", None)
            r["features_at_cutoff"]["static"] = st
            try:
                r["outcome"]["total_supply_now"] = _read_total_supply(rpc, r["collection"])
            except Exception:
                pass
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"    {done[0]}/{len(rs)}", flush=True)

        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(one, rs))
        ok = sum(1 for r in rs if r["features_at_cutoff"]["static"].get("_read_ok"))
        print(f"  static reads OK: {ok}/{len(rs)}")

    if a.pin:
        uris = set()
        for r in recs:
            st = r["features_at_cutoff"]["static"]
            for u in (st.get("contract_uri"), st.get("base_uri")):
                if u and not is_on_chain(u):
                    uris.add(u)
            for d in r["features_at_cutoff"]["drop_uris_before_cutoff"]:
                if d["uri"] and not is_on_chain(d["uri"]):
                    uris.add(d["uri"])
        uris = [u for u in uris if u.startswith(("ipfs://", "http"))]
        print(f"pinning {len(uris)} off-chain metadata URIs for offline eval")
        hits = [0, 0]

        def pin(u):
            m, prov = resolve(u, allow_network=True, timeout=10)
            hits[0 if m else 1] += 1

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(pin, uris))
        print(f"  pinned={hits[0]} unreachable={hits[1]}")

    with path.open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    print(f"rewrote {path}")


if __name__ == "__main__":
    main()
