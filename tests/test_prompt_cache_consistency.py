"""The committed prompts must match the committed LLM cache.

Without this, a prompt can be edited after a run and the repo will quietly stop
reproducing its own published numbers -- the cache keys no longer match, so a
re-run silently makes live calls and produces different results.

That happened during this build: a `git checkout` of verifier.md reverted it
further back than intended, and the offline reproduction dropped MintScout's
precision from 0.436 to 0.000 before this test existed. The prompt was recovered
from the cache (each entry stores its system prompt) and pinned here.
"""
import hashlib
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/llm_cache"
PROMPTS = ROOT / "mintscout/agent/prompts"

pytestmark = pytest.mark.skipif(not CACHE.exists() or not any(CACHE.glob("*.json")),
                                reason="no committed LLM cache")


def _cached_systems():
    out = []
    for f in CACHE.glob("*.json"):
        try:
            out.append(json.load(f.open()).get("system", ""))
        except (ValueError, OSError):
            continue
    return out


@pytest.mark.parametrize("name,marker", [
    ("triage.md", "triage stage"),
    ("verifier.md", "verifier stage"),
])
def test_committed_prompt_is_present_in_cache(name, marker):
    """The on-disk prompt must be byte-identical to one actually used.

    If this fails, the committed results were produced by a different prompt and
    the repo no longer reproduces them offline.
    """
    live = (PROMPTS / name).read_text()
    systems = [s for s in _cached_systems() if marker in s]
    if not systems:
        pytest.skip(f"no cached calls for {name}")
    digests = {hashlib.sha256(s.encode()).hexdigest() for s in systems}
    assert hashlib.sha256(live.encode()).hexdigest() in digests, (
        f"{name} does not match any prompt in the committed cache. The published "
        f"numbers were produced by a different version of this prompt, so a "
        f"re-run will make live calls and may not reproduce them.")


def test_dominant_prompt_is_the_committed_one():
    """The committed prompt should be the one used for the largest run.

    Guards against pinning a prompt that only a small probe run used.
    """
    import collections
    for name, marker in (("triage.md", "triage stage"), ("verifier.md", "verifier stage")):
        systems = [s for s in _cached_systems() if marker in s]
        if not systems:
            continue
        counts = collections.Counter(hashlib.sha256(s.encode()).hexdigest()
                                     for s in systems)
        dominant, n = counts.most_common(1)[0]
        live = hashlib.sha256((PROMPTS / name).read_text().encode()).hexdigest()
        assert live == dominant, (
            f"{name} is not the prompt used for the largest cached run "
            f"({n} calls). The published metrics came from that run.")
