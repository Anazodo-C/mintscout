"""Triage agent: evidence dossier -> strict-JSON verdict."""
from __future__ import annotations

import json
import pathlib

from . import llm

PROMPTS = pathlib.Path(__file__).parent / "prompts"
SYSTEM = (PROMPTS / "triage.md").read_text()
BASELINE_SYSTEM = (PROMPTS / "single_prompt_baseline.md").read_text()

VALID = {"MINT", "WATCH", "SKIP"}


def _prune(d):
    """Drop null/empty leaves. A field that is null carries no evidence, and
    every one of them costs input tokens on every call."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            v = _prune(v)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(d, list):
        return [_prune(v) for v in d]
    return d


def _clean(d):
    """Strip internal keys at every level so the model sees only evidence.

    Recursive on purpose: a note nested inside `metadata` is just as much
    commentary-in-the-payload as a top-level one, and it costs tokens on every
    call while telling the model nothing it can weigh.
    """
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if not str(k).startswith("_")}
    if isinstance(d, list):
        return [_clean(v) for v in d]
    return d


def triage(dossier: dict, *, use_cache: bool = True,
           model: str = llm.DEFAULT_MODEL) -> dict:
    user = ("Evidence dossier:\n\n```json\n"
            + json.dumps(_prune(_clean(dossier)), sort_keys=True,
                         separators=(",", ":"))
            + "\n```\n\nReturn your verdict as JSON.")
    raw = llm.complete(SYSTEM, user, model=model, use_cache=use_cache,
                       max_tokens=700)
    try:
        out = llm.parse_json(raw)
    except ValueError:
        return {"verdict": "SKIP", "score": 0,
                "reasons": ["triage returned unparseable output"],
                "evidence": [], "risk_flags": ["parse_error"], "_raw": raw[:500]}
    v = str(out.get("verdict", "")).upper()
    out["verdict"] = v if v in VALID else "SKIP"
    out.setdefault("score", 0)
    out.setdefault("reasons", [])
    out.setdefault("evidence", [])
    out.setdefault("risk_flags", [])
    return out


def single_prompt_baseline(rec_features: dict, *, use_cache: bool = True,
                           model: str = llm.DEFAULT_MODEL) -> dict:
    """Baseline B: one prompt, raw config, no tools, no memory, no verifier.

    This isolates how much of the gain comes from agent ENGINEERING versus from
    the model itself -- which is exactly what the scoring rubric asks.
    """
    user = ("Drop configuration:\n\n```json\n"
            + json.dumps(_prune(rec_features), sort_keys=True,
                         separators=(",", ":"))
            + "\n```\n\nReturn JSON.")
    raw = llm.complete(BASELINE_SYSTEM, user, model=model, use_cache=use_cache,
                       max_tokens=500)
    try:
        out = llm.parse_json(raw)
    except ValueError:
        return {"verdict": "SKIP", "score": 0, "reasons": ["unparseable"]}
    v = str(out.get("verdict", "")).upper()
    out["verdict"] = v if v in ("MINT", "SKIP") else "SKIP"
    return out
