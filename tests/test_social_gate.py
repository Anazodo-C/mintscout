"""Social gate behaviour, per SOCIAL-GATE.md §9.

All tests are offline: the network layer is monkeypatched, so these run in CI
with no credentials and no external calls.
"""
from __future__ import annotations

import pytest

from mintscout import social
from mintscout.agent import social_gate

CHAIN = "robinhood"
ADDR = "0xc9da285def71048c352c6bfd60c78037d22d09fe"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the cache at a temp dir and clear threshold env for every test."""
    monkeypatch.setattr(social, "CACHE_ROOT", tmp_path)
    for k in ("SOCIAL_MIN_FOLLOWERS", "SOCIAL_MIN_POSTS", "SOCIAL_AUTO_FLAG",
              "SOCIAL_ENABLED", "OPENSEA_API_KEY", "REFRESH_SOCIAL", "X_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    yield


def _fake_http(responses):
    """responses: list of (status, body) returned in order."""
    calls = {"n": 0, "urls": []}

    def fake(url, params=None, timeout=15, headers=None):
        calls["urls"].append(url)
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    fake.calls = calls
    return fake


def test_no_social_when_page_has_no_handle(monkeypatch):
    """OpenSea answers but the collection has no linked X account."""
    monkeypatch.setattr(social, "_http_get",
                        _fake_http([(200, "<html>no handle here</html>")]))
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["flag"] == "NO_SOCIAL"
    assert s["meets_thresholds"] is False
    assert s["followers"] is None


def test_lookup_failure_is_unresolved_not_no_social(monkeypatch):
    """A failed fetch must NOT be reported as 'this project has no socials'.

    This is the bug that reported 68 of 74 real collections as socially absent
    when the request had simply returned 401.
    """
    monkeypatch.setattr(social, "_http_get", _fake_http([(401, "")]))
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["flag"] == "UNRESOLVED"


def test_unresolved_when_handle_found_but_x_lookup_fails(monkeypatch):
    monkeypatch.setattr(social, "_http_get", _fake_http([
        (200, '<html>"twitterUsername":"someproject"</html>'),
        (500, ""),                       # X lookup fails
    ]))
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["flag"] == "UNRESOLVED"
    assert s["handle"] == "someproject"


@pytest.mark.parametrize("followers,posts,expected", [
    (1000, 10, True),      # boundary is INCLUSIVE on both
    (1001, 11, True),
    (999, 50, False),      # below on followers
    (5000, 9, False),      # below on posts
])
def test_threshold_boundaries(monkeypatch, followers, posts, expected):
    monkeypatch.setattr(social, "_http_get", _fake_http([
        (200, '<html>"twitterUsername":"proj"</html>'),
        (200, f'{{"followers":{followers},"posts":{posts},"fetchedAt":"now"}}'),
    ]))
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["meets_thresholds"] is expected
    assert s["flag"] == ("MINT" if expected else "BELOW")


def test_gate_mode_yields_pass_not_mint(monkeypatch):
    monkeypatch.setenv("SOCIAL_AUTO_FLAG", "false")
    monkeypatch.setattr(social, "_http_get", _fake_http([
        (200, '<html>"twitterUsername":"proj"</html>'),
        (200, '{"followers":6250,"posts":98,"fetchedAt":"now"}'),
    ]))
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["meets_thresholds"] is True
    assert s["flag"] == "PASS"


def test_opensea_url_present_in_result(monkeypatch):
    monkeypatch.setattr(social, "_http_get", _fake_http([(404, "")]))
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["opensea_url"] == f"https://opensea.io/contract/{CHAIN}/{ADDR}"


def test_cache_prevents_second_network_call(monkeypatch):
    fake = _fake_http([
        (200, '<html>"twitterUsername":"proj"</html>'),
        (200, '{"followers":2000,"posts":40,"fetchedAt":"now"}'),
    ])
    monkeypatch.setattr(social, "_http_get", fake)
    social_gate.evaluate_social(CHAIN, ADDR)
    n_after_first = fake.calls["n"]
    social_gate.evaluate_social(CHAIN, ADDR)
    assert fake.calls["n"] == n_after_first, "cache should serve the second call"


def test_disabled_short_circuits(monkeypatch):
    monkeypatch.setenv("SOCIAL_ENABLED", "false")
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not touch the network when disabled")

    monkeypatch.setattr(social, "_http_get", boom)
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["flag"] == "DISABLED"
    assert called["n"] == 0


def test_no_supply_fields_leak_from_opensea(monkeypatch):
    """OpenSea exposes live supply counts; reading them would leak sellout state
    into a decision made before the mint opens."""
    monkeypatch.setattr(social, "_http_get", _fake_http([
        (200, '<html>"twitterUsername":"proj","total_supply":2525,'
              '"unique_item_count":2525</html>'),
        (200, '{"followers":2000,"posts":40,"fetchedAt":"now"}'),
    ]))
    p = social.opensea_profile(CHAIN, ADDR)
    for banned in ("total_supply", "unique_item_count", "owner_count"):
        assert banned not in p, f"opensea_profile leaks {banned!r}"


def test_evaluate_social_never_raises(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("network on fire")

    monkeypatch.setattr(social, "_http_get", explode)
    s = social_gate.evaluate_social(CHAIN, ADDR)
    assert s["flag"] in social_gate.FLAGS
    assert s["meets_thresholds"] is False
