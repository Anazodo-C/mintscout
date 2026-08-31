"""Social resolution: contract -> OpenSea profile -> X account stats.

Deliberately a separate module rather than an addition to `enrich.py`. The eight
enrichment tools feed the backtested arms, and every number in `results/` was
produced by them; adding a ninth would change that surface. Nothing here is
reachable from the evaluation path.

## Why this is NOT in the backtest

Follower counts are only readable in the present tense. No source returns "how
many followers did this account have on 14 August". Worse, an account grows
*because* its mint succeeded, so scoring it against a historical outcome would
predict the outcome from a number that outcome caused. That is the same leak as
`tokenURI` (CHANGELOG "Removed #2"), in a new costume.

So this signal is forward-validated in live mode only, and every record carries
`fetched_at` to show the reading was taken at decision time.

## Endpoint reality (measured 2026-08-31, corrects the spec)

* `GET /api/v2/chain/{chain}/contract/{address}` -- works with NO key. Returns
  the collection slug.
* `GET /api/v2/collections/{slug}` -- the spec says this is unauthenticated.
  **It is not.** It returns HTTP 401 `"Missing an API Key, which is required for
  this request."` That is the call the spec relies on for `twitter_username`.
* Workaround, verified on both chains: the public collection page embeds
  `"twitterUsername":"..."` in its JSON payload, and is reachable with no
  credentials. Used as the default path; the v2 API is used instead when
  `OPENSEA_API_KEY` is set, since it is cheaper and more stable than HTML.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE_ROOT = pathlib.Path(os.environ.get("MINTSCOUT_STATE_DIR", str(ROOT / "data")))
OPENSEA_BASE = "https://api.opensea.io/api/v2"
PULSE_BASE = "https://pulse.walls.sh"

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Simple shared rate limiter. These are free public endpoints; do not hammer them.
_last_call = [0.0]
_rl_lock = threading.Lock()
_MIN_INTERVAL = float(os.environ.get("SOCIAL_MIN_INTERVAL_S", "0.25"))  # <= 4 req/s


def _throttle() -> None:
    with _rl_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


# ------------------------------------------------------------------- cache
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]


def _cache_path(bucket: str, key: str) -> pathlib.Path:
    return CACHE_ROOT / bucket / f"{_safe(key)}.json"


def _cache_get(bucket: str, key: str) -> dict | None:
    if os.environ.get("REFRESH_SOCIAL", "").lower() in ("1", "true", "yes"):
        return None
    p = _cache_path(bucket, key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def _cache_put(bucket: str, key: str, value: dict) -> dict:
    p = _cache_path(bucket, key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, indent=1))
    except OSError:
        pass          # a read-only or full disk must never break the pipeline
    return value


# -------------------------------------------------------------------- http
def _http_get(url: str, params: dict | None = None, timeout: int = 15,
              headers: dict | None = None) -> tuple[int, str]:
    """Return (status_code, body). Never raises for HTTP errors."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    _throttle()
    h = {"user-agent": _BROWSER_UA, "accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(3_000_000).decode("utf8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(20_000).decode("utf8", "replace")
        except Exception:
            return e.code, ""
    except Exception:
        return 0, ""


_TW_RE = re.compile(r'"twitterUsername"\s*:\s*"([A-Za-z0-9_]{1,15})"')
_NAME_RE = re.compile(r'"collectionName"\s*:\s*"([^"]{1,60})"')


# ------------------------------------------------------- stage 1: opensea
def opensea_profile(chain: str, address: str) -> dict:
    """Resolve a contract to its OpenSea collection and linked X handle.

    NEVER raises into the pipeline: any failure returns available=False, which
    the gate treats as "no information", not as "bad".

    Note it deliberately does NOT read `total_supply` or `unique_item_count`
    from OpenSea. Those are live values that would leak sellout state into a
    decision made before the mint opens. Supply comes from the chain.
    """
    address = (address or "").lower()
    key = f"{chain}_{address}"
    cached = _cache_get("opensea_cache", key)
    if cached is not None:
        return cached

    out = {
        "available": False,
        "lookup_failed": False,
        "opensea_url": f"https://opensea.io/contract/{chain}/{address}",
        "twitter_username": None,
        "source": None,
    }
    api_ok = False
    try:
        # --- optional: the v2 API. Richer, but it requires a key.
        # MEASURED 2026-08-31: /chain/{chain}/contract/{address} and
        # /collections/{slug} BOTH return 401 "Missing an API Key" without one,
        # contradicting the spec. So the API is treated as an enhancement, never
        # as the only route -- an earlier version returned early when it failed,
        # which made the HTML fallback unreachable and reported 68 of 74 real
        # collections as "no social account" when the lookup had simply 401'd.
        api_key = (os.environ.get("OPENSEA_API_KEY") or "").strip()
        if api_key:
            code, body = _http_get(f"{OPENSEA_BASE}/chain/{chain}/contract/{address}",
                                   headers={"x-api-key": api_key})
            if code == 200:
                j = json.loads(body)
                out["slug"] = j.get("collection")
                out["name"] = j.get("name")
                out["contract_standard"] = j.get("contract_standard")
                api_ok = True
                if out.get("slug"):
                    code2, body2 = _http_get(f"{OPENSEA_BASE}/collections/{out['slug']}",
                                             headers={"x-api-key": api_key})
                    if code2 == 200:
                        d = json.loads(body2)
                        out.update({
                            "available": True, "source": "api",
                            "name": d.get("name") or out.get("name"),
                            "description": (d.get("description") or "")[:400],
                            "twitter_username": (d.get("twitter_username") or "").strip() or None,
                            "discord_url": d.get("discord_url") or None,
                            "project_url": d.get("project_url") or None,
                            "telegram_url": d.get("telegram_url") or None,
                            "instagram_username": d.get("instagram_username") or None,
                            "created_date": d.get("created_date"),
                            "safelist_status": d.get("safelist_status"),
                            "is_nsfw": bool(d.get("is_nsfw")),
                        })

        # --- default route: the public collection page. No credentials needed.
        if not out["twitter_username"]:
            code3, html = _http_get(out["opensea_url"],
                                    headers={"accept": "text/html"}, timeout=25)
            if code3 == 200 and html:
                m = _TW_RE.search(html)
                if m:
                    out["twitter_username"] = m.group(1)
                    out["source"] = out["source"] or "html"
                else:
                    n = _NAME_RE.search(html)
                    if n and not out.get("name"):
                        out["name"] = n.group(1)[:60]
                # The page answered. Whether or not it carried a handle, that is
                # a real answer -- "this collection has no linked X account" --
                # and must be distinguishable from "we could not look it up".
                out["available"] = True
                out["source"] = out["source"] or "html"
            elif not api_ok:
                out["lookup_failed"] = True
    except Exception:
        out["lookup_failed"] = not out["available"]

    out["has_twitter"] = out["twitter_username"] is not None
    out["social_link_count"] = sum(
        bool(out.get(k)) for k in ("twitter_username", "discord_url",
                                   "project_url", "telegram_url",
                                   "instagram_username"))
    return _cache_put("opensea_cache", key, out)


# ------------------------------------------------------------- stage 2: X
def x_profile(handle: str | None) -> dict:
    """Follower / post counts for an X handle. Free, no auth, cached.

    NEVER raises. `fetched_at` is recorded on every hit -- it is what shows the
    reading was taken at decision time rather than backfilled later, which is
    the whole basis for treating this as a forward-validated signal.
    """
    if not handle:
        return {"available": False, "handle": None, "x_url": None}
    handle = handle.lstrip("@").strip()
    key = handle.lower()
    cached = _cache_get("x_cache", key)
    if cached is not None:
        return cached

    out = {"available": False, "handle": handle, "x_url": f"https://x.com/{handle}"}
    provider = (os.environ.get("X_PROVIDER") or "pulse").strip().lower()
    try:
        if provider == "twitterapi":
            out.update(_x_via_twitterapi(handle))
        else:
            code, body = _http_get(f"{PULSE_BASE}/profile",
                                   params={"url": f"https://twitter.com/{handle}"})
            if code == 200 and body:
                j = json.loads(body)
                out.update({
                    "available": True,
                    "followers": j.get("followers"),
                    "posts": j.get("posts"),
                    "following": j.get("following"),
                    "verified": bool(j.get("verified")),
                    "bio": (j.get("bio") or "")[:280],
                    "name": j.get("name"),
                    "fetched_at": j.get("fetchedAt") or int(time.time()),
                })
    except Exception:
        pass
    out.setdefault("fetched_at", int(time.time()))
    return _cache_put("x_cache", key, out)


def _x_via_twitterapi(handle: str) -> dict:
    """Paid fallback for when the free endpoint rate-limits."""
    key = (os.environ.get("TWITTERAPI_IO_KEY") or "").strip()
    if not key:
        return {}
    code, body = _http_get("https://api.twitterapi.io/twitter/user/info",
                           params={"userName": handle},
                           headers={"x-api-key": key})
    if code != 200 or not body:
        return {}
    d = (json.loads(body) or {}).get("data") or {}
    return {"available": True,
            "followers": d.get("followers"),
            "posts": d.get("statusesCount"),
            "following": d.get("following"),
            "verified": bool(d.get("isBlueVerified")),
            "bio": (d.get("description") or "")[:280],
            "name": d.get("name"),
            "fetched_at": int(time.time())}
