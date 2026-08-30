"""Which decision-time features actually separate high-value from low-value?

Used to calibrate the deterministic rubric HONESTLY: the rubric is tuned on the
calibration split only, and every arm is scored on the held-out test split.
A hand-tuned rubric scored on the data it was tuned on is not a baseline, it is
a memorised answer.
"""
import json, pathlib, statistics, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from mintscout.dataset import load
from mintscout.eval.labels import label
from mintscout.eval.replay import ReplayContext
from mintscout.enrich import build_dossier
from mintscout.eval.split import split

ROOT = pathlib.Path(__file__).resolve().parents[1]


def rate(rows, pred):
    n = len(rows)
    return (sum(1 for r in rows if pred(r)) / n) if n else 0.0


def main():
    recs = load(ROOT / "data/drops_robinhood.jsonl")
    calib, test = split(recs)
    print(f"total={len(recs)}  calib={len(calib)}  test={len(test)}")
    for nm, rs in (("CALIB", calib), ("TEST", test)):
        hv = sum(1 for r in rs if label(r) == "high_value")
        print(f"  {nm}: n={len(rs)} high_value={hv} base_rate={hv/len(rs):.3%}")

    rows = []
    for r in calib:
        d = build_dossier(ReplayContext(r), allow_network=False)
        rows.append((label(r) == "high_value", d, r))
    hi = [x for x in rows if x[0]]
    lo = [x for x in rows if not x[0]]
    print(f"\ncalibration: {len(hi)} high / {len(lo)} low\n")

    feats = {
        "metadata present":      lambda d, r: bool(d["metadata"].get("present")),
        "has drop_uri":          lambda d, r: bool(d["metadata"].get("drop_uri")),
        "on-chain metadata":     lambda d, r: bool(d["metadata"].get("on_chain_metadata")),
        "erc6551":               lambda d, r: bool(d["erc6551"].get("token_bound_account")),
        ">=3 attributes":        lambda d, r: (d["metadata"].get("n_attributes") or 0) >= 3,
        "max_supply sane":       lambda d, r: bool(d["collection"].get("max_supply_sane")),
        "supply 200..20000":     lambda d, r: 200 <= (d["collection"].get("max_supply") or 0) <= 20000,
        "cap == 1":              lambda d, r: d["economics"]["max_per_wallet"] == 1,
        "cap >= 5":              lambda d, r: d["economics"]["max_per_wallet"] >= 5,
        "window <= 6h":          lambda d, r: (d["economics"]["duration_hours"] or 0) <= 6,
        "window >= 24h":         lambda d, r: (d["economics"]["duration_hours"] or 0) >= 24,
        ">=2 price flips":       lambda d, r: (d["config_history"].get("price_flips") or 0) >= 2,
        ">=3 revisions":         lambda d, r: (d["config_history"].get("n_revisions_before_cutoff") or 0) >= 3,
        "restrict fee recips":   lambda d, r: bool(d["economics"]["restrict_fee_recipients"]),
        "code_size > 6000":      lambda d, r: (d["collection"].get("code_size") or 0) > 6000,
        "code_size > 12000":     lambda d, r: (d["collection"].get("code_size") or 0) > 12000,
        "has symbol":            lambda d, r: bool(d["collection"].get("symbol")),
        "name len >= 4":         lambda d, r: len((d["collection"].get("name") or "").strip()) >= 4,
    }
    print(f"{'feature':26} {'P(f|high)':>10} {'P(f|low)':>10} {'lift':>7}")
    print("-" * 58)
    scored = []
    for nm, fn in feats.items():
        ph = rate(hi, lambda x: fn(x[1], x[2]))
        pl = rate(lo, lambda x: fn(x[1], x[2]))
        lift = (ph / pl) if pl > 0 else (float("inf") if ph > 0 else 0.0)
        scored.append((abs(ph - pl), nm, ph, pl, lift))
    for _, nm, ph, pl, lift in sorted(scored, reverse=True):
        ls = "inf" if lift == float("inf") else f"{lift:.2f}"
        print(f"{nm:26} {ph:10.3f} {pl:10.3f} {ls:>7}")

    cs_hi = [x[1]["collection"].get("code_size") or 0 for x in hi]
    cs_lo = [x[1]["collection"].get("code_size") or 0 for x in lo]
    if cs_hi and cs_lo:
        print(f"\ncode_size  median high={statistics.median(cs_hi):,.0f}  "
              f"low={statistics.median(cs_lo):,.0f}")
    ms_hi = [x[1]["collection"].get("max_supply") or 0 for x in hi]
    ms_lo = [x[1]["collection"].get("max_supply") or 0 for x in lo]
    if ms_hi and ms_lo:
        print(f"max_supply median high={statistics.median(ms_hi):,.0f}  "
              f"low={statistics.median(ms_lo):,.0f}")


if __name__ == "__main__":
    main()
