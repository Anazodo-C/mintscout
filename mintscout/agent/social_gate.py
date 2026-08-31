"""Social gate: does a project's linked X account clear the thresholds?

Two modes, one env var:

    SOCIAL_AUTO_FLAG=true   (default)  a passing check is SUFFICIENT -> MINT
    SOCIAL_AUTO_FLAG=false             a passing check CONFIRMS     -> PASS,
                                       and the existing rubric still decides

The permissive direction is deliberate and worth stating plainly: **follower
counts are purchasable**. 1,000 followers costs a few dollars, and 10 posts
costs nothing, so auto-flag mode is trivially game-able by anyone who knows the
threshold. It is the right setting for observing the signal in live operation;
it is the wrong setting for unattended spending against an adversary. Gate mode
(`false`) keeps the same evidence in the log without letting it authorise a mint
on its own.

Absence of data is never treated as evidence of spam. UNRESOLVED and NO_SOCIAL
both fall through to the existing path untouched -- OpenSea coverage on these
chains is patchy, and "we could not find an account" says nothing about quality.
"""
from __future__ import annotations

import os

from ..social import opensea_profile, x_profile


def _int_env(name: str, default: int) -> int:
    try:
        return int(float((os.environ.get(name) or "").strip() or default))
    except ValueError:
        return default


def min_followers() -> int:
    return _int_env("SOCIAL_MIN_FOLLOWERS", 1000)


def min_posts() -> int:
    return _int_env("SOCIAL_MIN_POSTS", 10)


def auto_flag_enabled() -> bool:
    return (os.environ.get("SOCIAL_AUTO_FLAG", "true") or "true").strip().lower() \
        in ("1", "true", "yes")


def social_enabled() -> bool:
    return (os.environ.get("SOCIAL_ENABLED", "true") or "true").strip().lower() \
        in ("1", "true", "yes")


FLAGS = ("MINT", "PASS", "BELOW", "UNRESOLVED", "NO_SOCIAL", "DISABLED")


def evaluate_social(chain: str, address: str) -> dict:
    """Resolve socials and apply the thresholds. Never raises."""
    if not social_enabled():
        return {"flag": "DISABLED", "meets_thresholds": False,
                "followers": None, "posts": None, "handle": None,
                "opensea_url": f"https://opensea.io/contract/{chain}/{address}",
                "x_url": None, "fetched_at": None, "social_link_count": 0,
                "name": None, "slug": None}

    os_p = opensea_profile(chain, address)
    x_p = x_profile(os_p.get("twitter_username")) if os_p.get("has_twitter") \
        else {"available": False, "x_url": None}

    followers = x_p.get("followers")
    posts = x_p.get("posts")

    # Boundary is INCLUSIVE: exactly 1000 followers and exactly 10 posts passes.
    meets = bool(
        x_p.get("available")
        and followers is not None and followers >= min_followers()
        and posts is not None and posts >= min_posts()
    )

    if meets and auto_flag_enabled():
        flag = "MINT"
    elif meets:
        flag = "PASS"
    elif x_p.get("available"):
        flag = "BELOW"
    elif os_p.get("has_twitter") or os_p.get("lookup_failed") \
            or not os_p.get("available"):
        # Either the handle is known but X did not answer, or the OpenSea lookup
        # itself failed. Both mean "we have no reading", which is NOT the same as
        # "this project has no X account" -- conflating them would have reported
        # 68 of 74 real collections as socially absent when the request had 401'd.
        flag = "UNRESOLVED"
    else:
        # OpenSea answered and the collection genuinely has no linked account.
        flag = "NO_SOCIAL"

    return {
        "flag": flag,
        "meets_thresholds": meets,
        "followers": followers,
        "posts": posts,
        "handle": os_p.get("twitter_username"),
        "slug": os_p.get("slug"),
        "name": os_p.get("name"),
        "verified": x_p.get("verified"),
        "social_link_count": os_p.get("social_link_count", 0),
        "opensea_url": os_p.get("opensea_url"),
        "x_url": x_p.get("x_url"),
        "fetched_at": x_p.get("fetched_at"),
        "thresholds": {"followers": min_followers(), "posts": min_posts()},
    }


ICONS = {"MINT": "🟢", "PASS": "🔵", "BELOW": "🟡",
         "UNRESOLVED": "⚪", "NO_SOCIAL": "⚫", "DISABLED": "⬜"}


def format_social_line(chain: str, address: str, s: dict) -> str:
    """One greppable line per candidate, with a clickable OpenSea link."""
    f = f"{s['followers']:,}" if s.get("followers") is not None else "—"
    p = f"{s['posts']:,}" if s.get("posts") is not None else "—"
    name = (s.get("name") or address[:10])[:24]
    handle = s.get("handle") or "—"
    return (f"{ICONS.get(s['flag'], '?')} [{s['flag']:<10}] {chain:<9} {name:<24} "
            f"followers={f:>10}  posts={p:>6}  x=@{handle:<16} "
            f"opensea={s.get('opensea_url')}")
