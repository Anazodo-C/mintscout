"""Decision arms under evaluation.

Resource parity is mandatory: every arm sees the identical ReplayContext, the
identical dossier, the identical wallet fleet and budget, and its output goes
through the identical executor. The ONLY thing that differs between arms is the
triage decision. Anything else would make the comparison meaningless.
"""
from __future__ import annotations

import re

from ..enrich import build_dossier

PLACEHOLDER = re.compile(
    r"^(test+|aaa+|asdf|qwer|untitled|unnamed|new ?collection|collection|nft|"
    r"my ?nft|sample|demo|foo|bar|xxx+|\.+|-+|\s*)$", re.I)


# ---------------------------------------------------------------- Baseline A
def mint_everything_free(dossier: dict) -> dict:
    """Baseline A -- what a naive script (and the Minipengs-style bot,
    generalised) does: every free drop is a mint. Perfect recall by construction,
    precision equal to the base rate."""
    return {"verdict": "MINT", "score": 100,
            "reasons": ["drop is free; baseline mints every free drop"],
            "evidence": [], "risk_flags": []}


# ------------------------------------------------------- Deterministic filter
def deterministic_rubric(dossier: dict) -> dict:
    """Iteration 1 -- hand-written filters, no LLM.

    Included as its own arm because it answers the question a judge should ask:
    how much of the gain actually needs a language model? Reporting this honestly
    is worth more than a bigger headline number.
    """
    col = dossier.get("collection", {}) or {}
    meta = dossier.get("metadata", {}) or {}
    econ = dossier.get("economics", {}) or {}
    hist = dossier.get("config_history", {}) or {}
    tba = (dossier.get("erc6551") or {}).get("token_bound_account")

    score, reasons, flags = 50, [], []

    ms = col.get("max_supply")
    if not col.get("max_supply_sane"):
        score -= 30
        flags.append(f"max_supply not sane ({ms})")
    elif 200 <= (ms or 0) <= 20_000:
        score += 10
        reasons.append(f"supply {ms} in the plausible band")

    if not meta.get("present"):
        score -= 30
        flags.append(f"no resolvable metadata (provenance={meta.get('provenance')})")
    else:
        if meta.get("on_chain_metadata"):
            score += 25
            reasons.append("fully on-chain metadata (costly signal)")
        n = meta.get("n_attributes") or 0
        if n >= 3:
            score += 12
            reasons.append(f"{n} structured attributes")
        elif n == 0:
            score -= 8
            flags.append("no attributes")
        if meta.get("has_image"):
            score += 3
        nm = (meta.get("name") or col.get("name") or "")
        if PLACEHOLDER.match(nm.strip()):
            score -= 25
            flags.append(f"placeholder-looking name {nm!r}")

    if tba:
        score += 15
        reasons.append("ERC-6551 token-bound account")

    flips = hist.get("price_flips") or 0
    if flips >= 2:
        score -= 10
        flags.append(f"{flips} price flips before cutoff")

    if (econ.get("duration_hours") or 0) <= 0:
        score -= 10
        flags.append("non-positive mint window")

    score = max(0, min(100, score))
    verdict = "MINT" if score >= 70 else ("WATCH" if score >= 55 else "SKIP")
    return {"verdict": verdict, "score": score, "reasons": reasons,
            "evidence": [], "risk_flags": flags}


# ---------------------------------------------------------------- Baseline B
def single_prompt_llm(dossier: dict, *, use_cache: bool = True, model=None) -> dict:
    """Baseline B -- one LLM call on the raw config. No tools, no memory, no
    verifier. Isolates model contribution from agent engineering."""
    from ..agent.triage import single_prompt_baseline
    from ..agent import llm as _llm
    raw = {"chain": dossier.get("chain"), "collection": dossier.get("collection"),
           "public_drop": (dossier.get("economics") or {}),
           "name": (dossier.get("collection") or {}).get("name"),
           "max_supply": (dossier.get("collection") or {}).get("max_supply")}
    out = single_prompt_baseline(raw, use_cache=use_cache,
                                 model=model or _llm.DEFAULT_MODEL)
    out.setdefault("evidence", [])
    out.setdefault("risk_flags", [])
    return out


# ------------------------------------------------------------ Full MintScout
def mintscout(dossier: dict, *, use_cache: bool = True, model=None,
              with_verifier: bool = True) -> dict:
    from ..agent import llm as _llm
    from ..agent.triage import triage
    from ..agent.verify import verify
    m = model or _llm.DEFAULT_MODEL
    t = triage(dossier, use_cache=use_cache, model=m)
    if not with_verifier:
        t["_verifier"] = None
        return t
    v = verify(dossier, t, use_cache=use_cache, model=m)
    final = dict(t)
    final["verdict"] = v["final_verdict"]
    final["_triage_verdict"] = t["verdict"]
    final["_verifier"] = v
    return final


ARMS = {
    "baseline_mint_all": mint_everything_free,
    "deterministic": deterministic_rubric,
    "single_prompt_llm": single_prompt_llm,
    "mintscout_no_verifier": lambda d, **kw: mintscout(d, with_verifier=False, **kw),
    "mintscout": mintscout,
}

NEEDS_LLM = {"single_prompt_llm", "mintscout_no_verifier", "mintscout"}
