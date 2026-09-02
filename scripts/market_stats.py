"""Collect market outcomes (volume, sales, floor, owners) for labelled drops.

Uses OpenSea's collection stats endpoint, which -- unlike /collections/{slug} --
answers WITHOUT an API key:

    GET /api/v2/collections/{slug}/stats
    -> total: {volume, sales, num_owners, floor_price, floor_price_symbol}
       intervals: one_day / seven_day / thirty_day {volume, sales}

Top offer is NOT available. Offers are signed off-chain Seaport orders and never
touch the chain until accepted, and the offers endpoint requires a key. Floor is
a listing (an ask), whereas volume and sales are realised. Where the two
disagree, trust volume.

This replaces fill-rate as the outcome measure. A collection can mint out 100%
and never trade -- observed repeatedly, e.g. Heads Have Better Pixels sold out
2,021/2,021 and its realised price fell 94.5%.
"""
import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor

from mintscout.social import _http_get, opensea_profile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "https://api.opensea.io/api/v2"


def collection_stats(slug: str) -> dict:
    """Realised market stats for a collection slug. Never raises."""
    out = {"stats_ok": False}
    if not slug:
        return out
    try:
        code, body = _http_get(f"{BASE}/collections/{slug}/stats")
        if code != 200 or not body:
            return out
        d = json.loads(body)
        t = d.get("total") or {}
        out.update({
            "stats_ok": True,
            "volume": t.get("volume"),
            "sales": t.get("sales"),
            "num_owners": t.get("num_owners"),
            "floor_price": t.get("floor_price"),
            "floor_symbol": t.get("floor_price_symbol"),
        })
        for iv in d.get("intervals") or []:
            k = iv.get("interval")
            if k:
                out[f"vol_{k}"] = iv.get("volume")
                out[f"sales_{k}"] = iv.get("sales")
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default=str(ROOT / "data/social_research.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "data/market_research.jsonl"))
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args()

    rows = [json.loads(l) for l in pathlib.Path(a.infile).read_text().splitlines()
            if l.strip()]
    out_path = pathlib.Path(a.out)
    done = set()
    if out_path.exists():
        for l in out_path.read_text().splitlines():
            try:
                done.add(json.loads(l)["collection"])
            except Exception:
                pass
    todo = [r for r in rows if r["collection"] not in done]
    print(f"{len(rows)} rows, {len(done)} already done -> fetching {len(todo)}",
          flush=True)

    fh = out_path.open("a")
    n = [0]

    def one(r):
        slug = r.get("slug")
        if not slug:
            # slug may be missing if the earlier pass only got the handle
            p = opensea_profile(r["chain"], r["collection"])
            slug = p.get("slug")
            r["slug"] = slug
        r.update(collection_stats(slug))
        fh.write(json.dumps(r) + "\n")
        fh.flush()
        n[0] += 1
        if n[0] % 20 == 0:
            print(f"  {n[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, todo))
    fh.close()
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
