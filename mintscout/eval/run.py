"""Evaluation runner.

Runs every arm over the identical dataset with identical resources and writes
results/metrics.json plus trajectories. The only difference between arms is the
triage decision -- see baselines.py.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

from .. import constants as C
from ..dataset import load
from ..enrich import ToolRecorder, build_dossier
from . import baselines as B
from .labels import DEFAULT as LABEL_POLICY
from .labels import explain, label, summarize
from .replay import ReplayContext

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"
TRAJ = ROOT / "trajectories"

GAS_FIXTURE = ROOT / "data/fixtures/gas_measurement.json"


def _gas() -> tuple[int, int]:
    if GAS_FIXTURE.exists():
        g = json.loads(GAS_FIXTURE.read_text())
        return g["median_gas_used"], g["median_effective_gas_price_wei"]
    return 100_254, 170_449_000


# ------------------------------------------------------------- fill modelling
def fill_plan(cap: int, max_supply: int | None, max_wallets: int = C.MAX_WALLETS_DEFAULT,
              target: int = C.TARGET_PER_COLLECTION) -> dict:
    """How many tokens this fleet could actually acquire.

    Grading against a flat 100 would mostly measure the drop's per-wallet cap
    (30% of Robinhood free drops are cap=1, hard-ceiling 20 with 20 wallets),
    punishing the agent for something outside its control. `fill_efficiency` is
    therefore graded against what was ACHIEVABLE, with raw fill reported too.
    """
    cap = max(0, int(cap or 0))
    supply = max_supply or 0
    if cap == 0:
        return {"wallets_used": 0, "achievable": 0, "capped_by": "cap=0"}
    want = min(target, supply) if supply else target
    wallets_needed = math.ceil(want / cap)
    wallets_used = min(wallets_needed, max_wallets)
    achievable = min(target, wallets_used * cap, supply or target)
    return {"wallets_used": wallets_used, "achievable": achievable,
            "capped_by": "wallets" if wallets_needed > max_wallets else "supply/target"}


# ------------------------------------------------------------------ evaluate
def build_memory_timeline(all_recs: list[dict], eval_recs: list[dict]):
    """Deployer memory that is correct in TIME, not just in content.

    A deployer's track record may only contain drops that had already CLOSED
    when the drop under judgement opened. Backfilling naively -- writing every
    outcome and then evaluating -- would let a deployer's record include the very
    drop being judged, or drops that had not happened yet. Either leaks the label.

    So outcomes are replayed in end_time order and interleaved with decisions in
    cutoff order, exactly as they would have arrived in a live run.
    """
    from ..memory import Memory
    import tempfile
    from ..eval.labels import label as _label

    db = pathlib.Path(tempfile.mkdtemp()) / "eval_memory.sqlite"
    mem = Memory(db)
    closed = sorted(all_recs,
                    key=lambda r: r["features_at_cutoff"]["public_drop"]["end_time"])
    order = sorted(eval_recs, key=lambda r: r["decision_cutoff_ts"])
    snapshots: dict[str, dict] = {}
    i = 0
    for rec in order:
        cutoff = rec["decision_cutoff_ts"]
        while i < len(closed) and \
                closed[i]["features_at_cutoff"]["public_drop"]["end_time"] < cutoff:
            c = closed[i]
            owner = (c["features_at_cutoff"].get("static") or {}).get("owner")
            mem.record_outcome(c["chain"], c["collection"], owner, _label(c),
                               c["features_at_cutoff"]["public_drop"]["end_time"])
            i += 1
        owner = (rec["features_at_cutoff"].get("static") or {}).get("owner")
        snapshots[rec["collection"]] = mem.deployer_stats(rec["chain"], owner or "")
    return snapshots


class _FrozenMemory:
    """Serves the per-drop memory snapshot captured at that drop's cutoff."""

    def __init__(self, snapshots: dict[str, dict]):
        self._s = snapshots
        self._cur: str | None = None

    def for_drop(self, collection: str):
        m = _FrozenMemory(self._s)
        m._cur = collection
        return m

    def deployer_stats(self, chain: str, address: str) -> dict:
        return self._s.get(self._cur or "", {"deployer": address, "known": False,
                                             "prior_collections": 0,
                                             "prior_high_value": 0})


def evaluate_arm(name: str, recs: list[dict], *, use_cache: bool = True,
                 model: str | None = None, workers: int = 6,
                 collect_trajectories: int = 0, memory=None) -> dict:
    fn = B.ARMS[name]
    needs_llm = name in B.NEEDS_LLM
    gas_used, gas_price = _gas()
    rows: list[dict] = []
    trajectories: list[dict] = []

    def one(rec: dict) -> dict:
        ctx = ReplayContext(rec)
        recorder = ToolRecorder()
        t0 = time.perf_counter()
        mem = memory.for_drop(rec["collection"]) if memory is not None else None
        dossier = build_dossier(ctx, memory=mem, allow_network=False,
                                recorder=recorder)
        kw = {}
        if needs_llm:
            kw = {"use_cache": use_cache, "model": model} if model else {"use_cache": use_cache}
        try:
            out = fn(dossier, **kw)
            err = None
        except Exception as e:                       # never let one drop kill a run
            out = {"verdict": "SKIP", "score": 0,
                   "reasons": [f"arm error: {type(e).__name__}"],
                   "risk_flags": ["arm_error"]}
            err = f"{type(e).__name__}: {e}"
        ms = (time.perf_counter() - t0) * 1000

        pd = rec["features_at_cutoff"]["public_drop"]
        st = rec["features_at_cutoff"]["static"]
        plan = fill_plan(pd["max_per_wallet"], st.get("max_supply"))
        lab = label(rec, LABEL_POLICY)
        chose = out["verdict"] == "MINT"
        return {
            "collection": rec["collection"], "chain": rec["chain"],
            "verdict": out["verdict"], "score": out.get("score", 0),
            "label": lab, "chose": chose,
            "wallets_used": plan["wallets_used"] if chose else 0,
            "achievable": plan["achievable"] if chose else 0,
            "gas_wei": (plan["wallets_used"] * gas_used * gas_price) if chose else 0,
            "spend_wei": (plan["achievable"] * pd["mint_price_wei"]) if chose else 0,
            "ms": ms, "error": err,
            "reasons": out.get("reasons", [])[:4],
            "risk_flags": out.get("risk_flags", [])[:4],
            "triage_verdict": out.get("_triage_verdict"),
            "verifier": out.get("_verifier"),
            "_dossier": dossier, "_trajectory": recorder.steps, "_rec": rec,
        }

    with ThreadPoolExecutor(max_workers=workers if needs_llm else 1) as ex:
        rows = list(ex.map(one, recs))

    # ---- metrics
    n = len(rows)
    hv = [r for r in rows if r["label"] == "high_value"]
    chosen = [r for r in rows if r["chose"]]
    tp = [r for r in chosen if r["label"] == "high_value"]
    fp = [r for r in chosen if r["label"] != "high_value"]
    precision = len(tp) / len(chosen) if chosen else 0.0
    recall = len(tp) / len(hv) if hv else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    achievable_hv = sum(r["achievable"] for r in tp)
    achievable_all = sum(
        fill_plan(r["_rec"]["features_at_cutoff"]["public_drop"]["max_per_wallet"],
                  r["_rec"]["features_at_cutoff"]["static"].get("max_supply"))["achievable"]
        for r in hv)
    vetoes = [r for r in rows if r.get("verifier") and r["verifier"].get("vetoed")]

    metrics = {
        "arm": name, "n": n,
        "n_high_value": len(hv), "base_rate": round(len(hv) / n, 4) if n else 0,
        "n_chosen": len(chosen),
        "precision_at_k": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "lift_over_base_rate": round(precision / (len(hv) / n), 2) if hv and chosen and n else 0,
        "false_positives": len(fp),
        "true_positives": len(tp),
        "tokens_acquired_from_high_value": achievable_hv,
        "tokens_acquirable_from_all_high_value": achievable_all,
        "fill_efficiency": round(achievable_hv / achievable_all, 4) if achievable_all else 0.0,
        "raw_fill_vs_100_per_collection": round(
            sum(r["achievable"] for r in chosen) / (100 * len(chosen)), 4) if chosen else 0.0,
        "total_gas_wei": sum(r["gas_wei"] for r in rows),
        "wasted_gas_wei_on_false_positives": sum(r["gas_wei"] for r in fp),
        "wasted_spend_wei_on_false_positives": sum(r["spend_wei"] for r in fp),
        "wallet_slots_spent": sum(r["wallets_used"] for r in rows),
        "wallet_slots_wasted": sum(r["wallets_used"] for r in fp),
        "verifier_vetoes": len(vetoes),
        "arm_errors": sum(1 for r in rows if r["error"]),
        "median_decision_ms": round(sorted(r["ms"] for r in rows)[n // 2], 1) if n else 0,
    }

    # ---- trajectories (deliverable 04): prefer the interesting ones
    if collect_trajectories:
        interesting = (vetoes
                       + [r for r in tp][:2]
                       + [r for r in fp][:2]
                       + [r for r in rows if r["_rec"]["outcome"].get("is_regression_fixture")])
        seen, picked = set(), []
        for r in interesting:
            if r["collection"] in seen:
                continue
            seen.add(r["collection"])
            picked.append(r)
            if len(picked) >= collect_trajectories:
                break
        for r in picked:
            rec = r["_rec"]
            pd = rec["features_at_cutoff"]["public_drop"]
            trajectories.append({
                "arm": name,
                "drop": {"chain": rec["chain"], "collection": rec["collection"],
                         "start_time": pd["start_time"], "cap": pd["max_per_wallet"],
                         "mint_price_wei": pd["mint_price_wei"],
                         "fixture_name": rec["outcome"].get("fixture_name")},
                "decision_cutoff_ts": rec["decision_cutoff_ts"],
                "tool_calls": r["_trajectory"],
                "triage": {"verdict": r.get("triage_verdict") or r["verdict"],
                           "score": r["score"], "reasons": r["reasons"],
                           "risk_flags": r["risk_flags"]},
                "verifier": r.get("verifier"),
                "action": {"mode": "dry_run", "wallets": r["wallets_used"],
                           "achievable": r["achievable"],
                           "gas_wei": r["gas_wei"]},
                "outcome": {"label": r["label"],
                            "label_detail": explain(rec, LABEL_POLICY),
                            "note": "outcome is NEVER visible to the agent; shown "
                                    "here only for post-hoc scoring."},
            })

    for r in rows:
        r.pop("_dossier", None); r.pop("_rec", None); r.pop("_trajectory", None)
    return {"metrics": metrics, "rows": rows, "trajectories": trajectories}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mintscout.eval")
    ap.add_argument("--data", default=str(ROOT / "data/drops_robinhood.jsonl"))
    ap.add_argument("--arms", default="baseline_mint_all,deterministic")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--trajectories", type=int, default=5)
    ap.add_argument("--memory", action="store_true",
                    help="enable cross-run deployer memory (time-ordered replay)")
    ap.add_argument("--split", default="test", choices=["test", "calib", "all"],
                    help="which split to score on. Default 'test': the "
                         "deterministic rubric is hand-tuned on 'calib', so "
                         "scoring every arm on held-out 'test' keeps the "
                         "comparison fair.")
    a = ap.parse_args(argv)

    from .split import split as _split
    allrecs = load(a.data)
    calib, test = _split(allrecs)
    recs = {"test": test, "calib": calib, "all": allrecs}[a.split]
    if a.limit:
        recs = recs[:a.limit]
    print(f"dataset: {a.data}")
    print(f"  full={len(allrecs)}  calib={len(calib)}  test={len(test)}  "
          f"-> scoring on '{a.split}' (n={len(recs)})")
    print(f"  {summarize(recs)}")
    RESULTS.mkdir(exist_ok=True); TRAJ.mkdir(exist_ok=True)

    memory = None
    if a.memory:
        snaps = build_memory_timeline(allrecs, recs)
        known = sum(1 for v in snaps.values() if v.get("known"))
        print(f"  memory: {known}/{len(snaps)} drops have a prior deployer record")
        memory = _FrozenMemory(snaps)

    all_metrics, all_traj = [], []
    for arm in a.arms.split(","):
        arm = arm.strip()
        if not arm:
            continue
        print(f"\n--- arm: {arm}")
        t0 = time.time()
        res = evaluate_arm(arm, recs, use_cache=not a.no_cache, model=a.model,
                           workers=a.workers, collect_trajectories=a.trajectories,
                           memory=memory)
        m = res["metrics"]
        m["wall_seconds"] = round(time.time() - t0, 1)
        all_metrics.append(m)
        all_traj += res["trajectories"]
        print(f"    precision@K={m['precision_at_k']:.3f} recall={m['recall']:.3f} "
              f"chosen={m['n_chosen']}/{m['n']} lift={m['lift_over_base_rate']}x "
              f"vetoes={m['verifier_vetoes']} ({m['wall_seconds']}s)")
        (RESULTS / f"rows_{arm}.json").write_text(json.dumps(res["rows"], indent=1))

    from ..agent import llm as _llm
    payload = {"dataset": a.data, "n": len(recs), "split": a.split,
               "split_sizes": {"full": len(allrecs), "calib": len(calib),
                               "test": len(test)},
               "label_policy": LABEL_POLICY.__dict__,
               "labels": summarize(recs),
               "llm": _llm.STATS.as_dict(),
               "arms": all_metrics}
    (RESULTS / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n")
    for i, t in enumerate(all_traj[:12]):
        (TRAJ / f"{t['arm']}_{i:02d}_{t['drop']['collection'][:10]}.json").write_text(
            json.dumps(t, indent=2) + "\n")
    print(f"\nwrote results/metrics.json and {min(len(all_traj), 12)} trajectories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
