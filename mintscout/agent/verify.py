"""Adversarial verifier: a second, separate LLM call that can veto triage.

Kept as its own call (not a second turn) so it cannot be anchored by the triage
agent's chain of thought -- it sees only the dossier and the finished verdict.
"""
from __future__ import annotations

import json
import pathlib

from . import llm
from .triage import _clean

PROMPTS = pathlib.Path(__file__).parent / "prompts"
SYSTEM = (PROMPTS / "verifier.md").read_text()

VALID = {"MINT", "WATCH", "SKIP"}


def verify(dossier: dict, verdict: dict, *, use_cache: bool = True,
           model: str = llm.DEFAULT_MODEL) -> dict:
    user = ("Evidence dossier:\n\n```json\n"
            + json.dumps(_clean(dossier), indent=2, sort_keys=True)
            + "\n```\n\nTriage verdict to audit:\n\n```json\n"
            + json.dumps({k: v for k, v in verdict.items() if not k.startswith("_")},
                         indent=2, sort_keys=True)
            + "\n```\n\nReturn your audit as JSON.")
    raw = llm.complete(SYSTEM, user, model=model, use_cache=use_cache)
    try:
        out = llm.parse_json(raw)
    except ValueError:
        # A verifier that cannot be parsed must not silently pass the verdict.
        return {"agreed": True, "objections": [],
                "final_verdict": verdict.get("verdict", "SKIP"),
                "confidence": 0, "_parse_error": True, "_raw": raw[:400]}
    fv = str(out.get("final_verdict", "")).upper()
    if fv not in VALID:
        fv = verdict.get("verdict", "SKIP")
    out["final_verdict"] = fv
    out["agreed"] = bool(out.get("agreed", True))
    out.setdefault("objections", [])
    out.setdefault("confidence", 0)
    # Record the veto explicitly -- these are the most interesting artefacts the
    # system produces and the report surfaces them.
    out["vetoed"] = (not out["agreed"]) or (fv != verdict.get("verdict"))
    return out
