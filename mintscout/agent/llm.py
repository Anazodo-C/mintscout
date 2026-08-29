"""LLM client with a committed on-disk cache.

Why the cache is committed: a judge without an API key must still be able to
reproduce the headline number. Every response is keyed by a hash of
(model, temperature, system, messages), so the cache is exact -- a cache hit is
byte-identical to what the live call returned. `--no-cache` re-runs live.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data/llm_cache"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

_lock = threading.Lock()


class LLMUnavailable(RuntimeError):
    pass


class Stats:
    def __init__(self) -> None:
        self.calls = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.seconds = 0.0
        self._l = threading.Lock()

    def bump(self, **kw):
        with self._l:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def as_dict(self):
        return {"calls": self.calls, "cache_hits": self.cache_hits,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "seconds": round(self.seconds, 1)}


STATS = Stats()


def _key(model: str, system: str, messages: list, temperature: float) -> str:
    blob = json.dumps({"model": model, "system": system, "messages": messages,
                       "temperature": temperature}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def complete(system: str, user: str, *, model: str = DEFAULT_MODEL,
             temperature: float = 0.0, max_tokens: int = 1200,
             use_cache: bool = True) -> str:
    messages = [{"role": "user", "content": user}]
    k = _key(model, system, messages, temperature)
    path = CACHE_DIR / f"{k}.json"
    if use_cache and path.exists():
        STATS.bump(cache_hits=1)
        return json.loads(path.read_text())["text"]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMUnavailable(
            "ANTHROPIC_API_KEY is not set and this prompt is not in the committed "
            "cache. Set the key, or run an arm that does not require an LLM.")
    import anthropic
    client = anthropic.Anthropic()
    t0 = time.perf_counter()
    resp = client.messages.create(model=model, max_tokens=max_tokens,
                                  temperature=temperature, system=system,
                                  messages=messages)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    STATS.bump(calls=1, seconds=time.perf_counter() - t0,
               input_tokens=resp.usage.input_tokens,
               output_tokens=resp.usage.output_tokens)
    with _lock:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "model": model, "temperature": temperature, "system": system,
            "messages": messages, "text": text,
            "usage": {"input_tokens": resp.usage.input_tokens,
                      "output_tokens": resp.usage.output_tokens}}, indent=1))
    return text


_JSON = re.compile(r"\{.*\}", re.S)


def parse_json(text: str) -> dict:
    """Models occasionally wrap JSON in a fence or a sentence. Recover it."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = _JSON.search(text)
        if not m:
            raise
        return json.loads(m.group(0))
