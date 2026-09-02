"""Floor price, offers and owner counts from OpenSea, using an API key.

The endpoints behind a key that matter for judging a drop's value:

  GET /api/v2/collections/{slug}/stats
      total: {volume, sales, num_owners, floor_price, market_cap}
      intervals: one_day / seven_day / thirty_day {volume, sales, average_price}

  GET /api/v2/offers/collection/{slug}          collection-wide bids
  GET /api/v2/listings/collection/{slug}/best   cheapest asks (the real floor)

Why these are worth having on top of the on-chain volume already collected:
floor and top offer are FORWARD-looking -- what you could sell into right now --
whereas realised volume is what already happened. A collection can have healthy
past volume and no live bid, which is exactly the state you cannot detect from
chain data alone because offers are signed off-chain and never land on it.

Key source: POST /api/v2/auth/keys issues an anonymous 7-day agent key with no
account, no email and no signup. Set OPENSEA_API_KEY to use a longer-lived one.
"""
import argparse, json, os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor

from mintscout.social import _http_get

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "https://api.opensea.io/api/v2"


def _key() -> str:
    k = (os.environ.get("OPENSEA_API_KEY") or "").strip()
    if k:
        return k
    p = pathlib.Path("/tmp/os_key.txt")
    return p.read_text().strip() if p.exists() else ""


def fetch(slug: str, key: str) -> dict:
    """Stats + best listing + best offer for one slug. Never raises."""
    h = {"x-api-key": key, "accept": "application/json"}
    out = {"os_ok": False}
    try:
        code, body = _http_get(f"{BASE}/collections/{slug}/stats", headers=h)
        if code != 200:
            out["os_error"] = f"stats {code}"
            return out
        d = json.loads(body)
        t = d.get("total") or {}
        out.update({
            "os_ok": True,
            "os_volume": t.get("volume"),
            "os_sales": t.get("sales"),
            "os_owners": t.get("num_owners"),
            "os_floor": t.get("floor_price"),
            "os_market_cap": t.get("market_cap"),
        })
        for iv in d.get("intervals") or []:
            k = iv.get("interval")
            if k:
                out[f"os_vol_{k}"] = iv.get("volume")
                out[f"os_sales_{k}"] = iv.get("sales")
                out[f"os_avg_{k}"] = iv.get("average_price")
    except Exception as e:
        out["os_error"] = f"{type(e).__name__}"
        return out

    # Best collection-wide offer: what you could sell into RIGHT NOW.
    try:
        code, body = _http_get(f"{BASE}/offers/collection/{slug}", headers=h)
        if code == 200:
            offers = (json.loads(body) or {}).get("offers") or []
            best = 0.0
            for o in offers[:50]:
                try:
                    params = (o.get("protocol_data") or {}).get("parameters") or {}
                    qty = 0
                    for c in params.get("consideration") or []:
                        qty = max(qty, int(c.get("startAmount") or 1))
                    for off in params.get("offer") or []:
                        amt = int(off.get("startAmount") or 0)
                        if amt:
                            best = max(best, amt / 1e18 / max(1, qty))
                except Exception:
                    continue
            out["os_top_offer"] = round(best, 8) if best else 0.0
            out["os_n_offers"] = len(offers)
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default=str(ROOT / "data/market_research.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "data/opensea_market.jsonl"))
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()

    key = _key()
    if not key:
        print("no API key: set OPENSEA_API_KEY or write one to /tmp/os_key.txt")
        return 2
    rows = [json.loads(l) for l in pathlib.Path(a.infile).read_text().splitlines()
            if l.strip()]
    rows = [r for r in rows if r.get("slug")]
    out_path = pathlib.Path(a.out)
    done = set()
    if out_path.exists():
        for l in out_path.read_text().splitlines():
            try:
                done.add(json.loads(l)["collection"])
            except Exception:
                pass
    todo = [r for r in rows if r["collection"] not in done]
    print(f"{len(rows)} with slugs, {len(done)} done -> fetching {len(todo)}",
          flush=True)

    fh = out_path.open("a")
    n = [0]

    def one(r):
        r.update(fetch(r["slug"], key))
        fh.write(json.dumps(r) + "\n")
        fh.flush()
        n[0] += 1
        if n[0] % 20 == 0:
            ok = "ok" if r.get("os_ok") else r.get("os_error")
            print(f"  {n[0]}/{len(todo)}  last={ok}", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, todo))
    fh.close()
    got = sum(1 for l in out_path.read_text().splitlines()
              if json.loads(l).get("os_ok"))
    print(f"wrote {out_path} ({got} with stats)", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
