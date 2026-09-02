"""Collect social profiles for LABELLED drops, so metrics can be scored against
real outcomes rather than intuition.

Uses the committed replay dataset, where every drop already has a ground-truth
label (>=80% filled, >=100 unique minters, >=1 secondary transfer within 48h).
For each sampled collection it resolves the OpenSea profile -> X handle -> full
profile metrics, and writes one JSONL row per collection.

IMPORTANT CAVEAT, recorded on every row as `followers_are_post_hoc: true`:
follower and post counts are read TODAY, after these drops resolved. An account
grows *because* its mint succeeded, so `followers` is contaminated for
prediction. Metrics that are structurally less time-dependent -- following
count, verified status, bio shape, and RATIOS between them -- are the ones worth
trusting. The analysis reports both and says which is which.
"""
import argparse, json, pathlib, random, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor

from mintscout.dataset import load
from mintscout.eval.labels import label, explain
from mintscout.social import opensea_profile, x_profile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/drops_robinhood.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "data/social_research.jsonl"))
    ap.add_argument("--negatives", type=int, default=150)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    recs = load(a.data)
    hi = [r for r in recs if label(r) == "high_value"]
    lo = [r for r in recs if label(r) != "high_value"]
    random.Random(a.seed).shuffle(lo)
    sample = hi + lo[:a.negatives]
    print(f"{len(recs)} drops -> sampling {len(hi)} high-value + "
          f"{min(a.negatives, len(lo))} low-value = {len(sample)}", flush=True)

    out_path = pathlib.Path(a.out)
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            try:
                done.add(json.loads(line)["collection"])
            except Exception:
                pass
        print(f"resuming: {len(done)} already collected", flush=True)

    fh = out_path.open("a")
    n = [0]

    def one(rec):
        col = rec["collection"]
        if col in done:
            return
        st = rec["features_at_cutoff"].get("static") or {}
        pd = rec["features_at_cutoff"]["public_drop"]
        o = rec["outcome"]
        row = {
            "collection": col,
            "chain": rec["chain"],
            "name": st.get("name"),
            "label": label(rec),
            "label_detail": explain(rec),
            # outcome facts
            "max_supply": o.get("max_supply"),
            "total_minted": o.get("total_minted"),
            "unique_minters": o.get("unique_minters"),
            "secondary_48h": o.get("secondary_transfers_48h"),
            "sellout_hours": (
                round((o["last_mint_ts"] - o["first_mint_ts"]) / 3600, 2)
                if o.get("last_mint_ts") and o.get("first_mint_ts") else None),
            # decision-time on-chain facts
            "code_size": st.get("code_size"),
            "cap": pd.get("max_per_wallet"),
            "duration_h": round((pd.get("duration_s") or 0) / 3600, 2),
            "start_time": pd.get("start_time"),
            "followers_are_post_hoc": True,
        }
        try:
            osp = opensea_profile(rec["chain"], col)
            row["os_available"] = bool(osp.get("available"))
            row["handle"] = osp.get("twitter_username")
            row["slug"] = osp.get("slug")
            if osp.get("twitter_username"):
                x = x_profile(osp["twitter_username"])
                row.update({
                    "x_available": bool(x.get("available")),
                    "followers": x.get("followers"),
                    "following": x.get("following"),
                    "posts": x.get("posts"),
                    "verified": x.get("verified"),
                    "bio": (x.get("bio") or "")[:200],
                    "x_name": x.get("name"),
                    "fetched_at": x.get("fetched_at"),
                })
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        fh.write(json.dumps(row) + "\n")
        fh.flush()
        n[0] += 1
        if n[0] % 10 == 0:
            print(f"  {n[0]}/{len(sample) - len(done)}", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, sample))
    fh.close()
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
