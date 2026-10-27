"""Offline tests for the fetch policy helpers: headers, politeness, hints, transliteration.

Nothing here touches the network: the helpers are pure (or read the environment),
so the deciding is checked by handing them the clocks and bodies directly.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from web_search_neo import web_client
from web_search_neo.fetch import headers as fetch_headers
from web_search_neo.fetch import hints as fetch_hints
from web_search_neo.fetch import politeness
from web_search_neo.search_quality import has_cyrillic, transliterate


@pytest.fixture(autouse=True)
def clean_policy_state():
    """Each test starts with empty politeness records."""
    politeness.clear_all()
    yield
    politeness.clear_all()


# ---------------------------------------------------------------- headers.py

def test_host_of_lowercases_and_drops_the_path():
    assert fetch_headers.host_of("https://Example.RU/some/Path") == "example.ru"


def test_ua_for_host_is_sticky_and_from_the_pool():
    host = "ligastavok.ru"
    pick = fetch_headers.ua_for_host(host)
    assert pick in web_client.USER_AGENTS
    assert fetch_headers.ua_for_host(host) == pick, "the pick must not move between calls"
    digest = hashlib.sha256(host.encode("utf-8")).digest()
    assert pick == web_client.USER_AGENTS[digest[0] % len(web_client.USER_AGENTS)]
    assert fetch_headers.ua_for_host("") == web_client.USER_AGENTS[0]


def test_ua_rotation_spreads_across_hosts():
    picks = {fetch_headers.ua_for_host(host) for host in (
        "tips.gg", "melbet.com", "stavka.tv", "pin-up.com", "tspula.ru", "1xstavka.ru",
    )}
    assert len(picks) > 1, "stickiness per host must still rotate across hosts"


def test_accept_language_follows_the_tld():
    assert fetch_headers.accept_language_for_host("example.com") == fetch_headers.DEFAULT_ACCEPT_LANGUAGE
    assert fetch_headers.accept_language_for_host("ligastavok.ru") == fetch_headers.CYRILLIC_ACCEPT_LANGUAGE
    assert fetch_headers.accept_language_for_host("www.ligastavok.ru") == fetch_headers.CYRILLIC_ACCEPT_LANGUAGE
    # WHY: only the real TLD counts - a host merely NAMED ru is not in Cyrillic.
    assert fetch_headers.accept_language_for_host("ru.example.com") == fetch_headers.DEFAULT_ACCEPT_LANGUAGE


def test_browser_headers_shape():
    built = fetch_headers.browser_headers("https://tips.gg/article/x")
    assert built["User-Agent"] == fetch_headers.ua_for_host("tips.gg")
    assert built["Accept-Language"] == fetch_headers.DEFAULT_ACCEPT_LANGUAGE
    assert built["Sec-Fetch-Mode"] == "navigate"
    assert "Referer" not in built, "a first hit has no referer; inventing one would be a lie"
    assert "Accept-Encoding" not in built, "the shared session advertises its own encoding set"
    with_ref = fetch_headers.browser_headers("https://tips.gg/article/x", referer="https://tips.gg/")
    assert with_ref["Referer"] == "https://tips.gg/"


# ---------------------------------------------------------------- politeness.py

def test_plan_first_visit_never_waits():
    assert politeness.plan(None, 1000.0, 1.2) == 0.0


def test_plan_applies_the_floor_after_a_previous_request():
    now = 1000.0
    assert politeness.plan(now - 0.5, now, 1.2) == pytest.approx(0.7)
    assert politeness.plan(now - 2.0, now, 1.2) == 0.0, "the floor has already passed"
    assert politeness.plan(now - 0.5, now, 0.0) == 0.0, "a zero floor disables the wait"


def test_plan_honours_a_retry_after_over_the_floor():
    now = 1000.0
    assert politeness.plan(None, now, 1.2, 5.0) == 5.0
    assert politeness.plan(now - 0.5, now, 1.2, 3.0) == 3.0, "the bigger wait wins"


def test_retry_after_seconds_parses_both_forms():
    assert politeness.retry_after_seconds({"Retry-After": "120"}) == 120.0
    assert politeness.retry_after_seconds({}) is None
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 0.0 < politeness.retry_after_seconds({"Retry-After": future}) <= 61.0
    assert politeness.retry_after_seconds({"Retry-After": "Mon, 01 Jan 2001 00:00:00 GMT"}) == 0.0
    assert politeness.retry_after_seconds({"Retry-After": "not a date"}) is None


def test_min_interval_env(monkeypatch):
    monkeypatch.delenv(politeness.MIN_INTERVAL_ENV, raising=False)
    assert politeness.min_interval_seconds() == politeness.DEFAULT_MIN_INTERVAL_SECONDS
    monkeypatch.setenv(politeness.MIN_INTERVAL_ENV, "0")
    assert politeness.min_interval_seconds() == 0.0
    monkeypatch.setenv(politeness.MIN_INTERVAL_ENV, "nonsense")
    assert politeness.min_interval_seconds() == politeness.DEFAULT_MIN_INTERVAL_SECONDS


def test_gate_walks_past_local_and_zero_floor_hosts():
    assert politeness.gate("http://127.0.0.1:8765/x") == 0.0, "a fixture server is not a third party"
    assert politeness.gate("http://example.test/x") == 0.0, "first visit walks straight through"


def test_gate_waits_the_floor_after_noting_a_request(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(politeness.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setenv(politeness.MIN_INTERVAL_ENV, "0.5")
    politeness.note_request("http://example.test/a")
    waited = politeness.gate("http://example.test/b")
    assert 0.0 < waited <= 0.5
    assert len(slept) == 1 and slept[0] == pytest.approx(waited)


def test_gate_honours_a_remembered_retry_after(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(politeness.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.delenv(politeness.MIN_INTERVAL_ENV, raising=False)
    politeness.remember_retry_after("http://example.test/a", 2.0)
    assert politeness.gate("http://example.test/a") == pytest.approx(2.0, abs=0.2)
    politeness.clear_all()
    assert politeness.gate("http://example.test/a") == 0.0, "clear_all forgot the penalty"


def test_remember_retry_after_caps_and_ignores_nothings():
    politeness.remember_retry_after("http://example.test/a", None)
    assert politeness.gate("http://example.test/a") == 0.0
    start = time.monotonic()
    politeness.remember_retry_after("http://example.test/a", 3600.0)
    assert politeness.plan(None, time.monotonic(), 0.0, 30.0) == 30.0
    assert time.monotonic() - start < 1.0, "the test must not sleep through the cap"


# ---------------------------------------------------------------- hints.py

def test_classify_body_needs_a_marker_and_thin_text_together():
    assert fetch_hints.classify_body('<div id="cf_chl_opt"></div>', "Loading") == "challenge"
    assert fetch_hints.classify_body("<div>CookieConsent banner</div>", "Agree") == "consent wall"
    assert fetch_hints.classify_body("<p>Принять cookies</p>", "Ок") == "consent wall"
    long_text = "word " * 60
    assert fetch_hints.classify_body('<div id="cf_chl_opt"></div>', long_text) is None, \
        "a real article can link a captcha script; thin text must also hold"
    assert fetch_hints.classify_body("<p>plain short body</p>", "plain short body") is None
    assert fetch_hints.classify_body("", "") is None


def test_classify_body_reads_gate_statuses():
    short = "short body"
    assert fetch_hints.classify_body("<p>x</p>", short, 200) is None
    assert fetch_hints.classify_body("<p>x</p>", short, 403) == "gate"
    assert fetch_hints.classify_body("<p>x</p>", short, 401) == "gate"
    assert fetch_hints.classify_body("<p>x</p>", short, 451) == "gate"
    assert fetch_hints.classify_body("<p>x</p>", short, 429) == "rate limited"


def test_hint_for_status_only_names_gate_answers():
    assert fetch_hints.hint_for_status(200, "https://example.test/") is None
    assert fetch_hints.hint_for_status(None, "https://example.test/") is None
    hint = fetch_hints.hint_for_status(403, "https://tips.gg/article/x")
    assert hint is not None and "[fetch_hint]" in hint
    assert "web_action" in hint and "web_info" in hint
    assert "tips.gg" in hint, "the known-host note should ride along"


def test_note_for_host_knows_its_hosts():
    assert fetch_hints.note_for_host("https://www.tips.gg/x") is not None
    assert fetch_hints.note_for_host("https://melbet.com/x") is not None
    assert fetch_hints.note_for_host("https://example.test/x") is None


def test_solver_registry_is_off_until_named(monkeypatch):
    monkeypatch.delenv(fetch_hints.SOLVER_ENV, raising=False)

    def _unit_dummy(page_url: str, challenge: dict) -> str | None:
        return None

    fetch_hints.register_solver("unit-dummy", _unit_dummy)
    assert "unit-dummy" in fetch_hints.registered_solvers()
    assert fetch_hints.configured_solver() is None, "default OFF: registration alone changes nothing"
    monkeypatch.setenv(fetch_hints.SOLVER_ENV, "unit-dummy")
    assert fetch_hints.configured_solver() is _unit_dummy


def test_browser_route_points_at_the_browser_path():
    route = fetch_hints.browser_route("https://ligastavok.ru/x", "the fetch was answered by a wall")
    assert "web_action" in route and '"action":"open"' in route
    assert "web_info" in route and "browser session" in route


# ---------------------------------------------------------------- transliteration

def test_has_cyrillic_ignores_latin():
    assert has_cyrillic("Москва погода") is True
    assert has_cyrillic("Moscow weather") is False
    assert has_cyrillic("тест test") is True
    assert has_cyrillic("") is False


def test_transliterate_shape():
    assert transliterate("Moscow") is None, "nothing to convert"
    assert transliterate("Москва погода") == "moskva pogoda"
    assert transliterate("МОСКВА") == "moskva", "output is folded like the cache key"
    assert transliterate("щука") == "shchuka"
    assert transliterate("объект") == "obekt", "ъ and ь carry no letter of their own"
    assert transliterate("юг явь") == "yug yav"
    assert transliterate("Ёж 123") == "yozh 123", "non-Cyrillic characters pass through"
