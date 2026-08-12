from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import msp_search


RESULT = [{"title": "Result", "url": "https://example.test", "snippet": "ok"}]


@pytest.fixture(autouse=True)
def reset_search_runtime(monkeypatch):
    monkeypatch.setattr(msp_search, "_status_cache", None)
    monkeypatch.setattr(msp_search, "_search_cache", msp_search.OrderedDict())
    monkeypatch.setattr(
        msp_search,
        "_provider_state",
        {name: {} for name in msp_search.ENGINE_ORDER},
    )
    monkeypatch.setattr(
        msp_search,
        "_provider_last_request",
        {},
    )
    monkeypatch.setattr(msp_search, "MIN_PROVIDER_INTERVAL_SECONDS", 0)


def _provider(name, fn):
    return msp_search.FunctionSearchProvider(
        name, fn, f"https://{name}.example/search?q={{query}}"
    )


def test_ddgs_provider_normalizes_results_and_passes_backend(monkeypatch):
    calls = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def text(self, query, **kwargs):
            calls.append((query, kwargs))
            return [{"title": "  Unity   job ", "href": "https://jobs.test/1", "body": " Great\nrole "}]

    monkeypatch.setattr(msp_search, "DDGS", FakeDDGS)
    provider = msp_search.DdgsSearchProvider(
        "test", "test-backend", "https://search.test/?q={query}"
    )

    assert provider.search("unity", 3, 4.2) == [
        {"title": "Unity job", "url": "https://jobs.test/1", "snippet": "Great role"}
    ]
    assert calls[1][1]["backend"] == "test-backend"
    assert calls[1][1]["max_results"] == 3


def test_ddgs_provider_classifies_captcha(monkeypatch):
    class FakeDDGS:
        def __init__(self, **_kwargs):
            pass

        def text(self, *_args, **_kwargs):
            raise RuntimeError("HTTP 429 CAPTCHA required")

    monkeypatch.setattr(msp_search, "DDGS", FakeDDGS)
    provider = msp_search.DdgsSearchProvider("test", "test", "https://x/?q={query}")
    with pytest.raises(msp_search.SearchProviderError) as error:
        provider.search("query", 1, 2)
    assert error.value.kind == "challenge"


def test_ddgs_provider_treats_blocked_no_results_as_challenge(monkeypatch):
    class FakeDDGS:
        def __init__(self, **_kwargs):
            pass

        def text(self, *_args, **_kwargs):
            raise RuntimeError("No results found.")

    monkeypatch.setattr(msp_search, "DDGS", FakeDDGS)
    provider = msp_search.DdgsSearchProvider("test", "test", "https://x/?q={query}")
    with pytest.raises(msp_search.SearchProviderError) as error:
        provider.search("common query", 1, 2)
    assert error.value.kind == "challenge"


def test_bing_html_provider_parses_real_bing_markup(monkeypatch):
    class Response:
        text = """
        <ol id="b_results"><li class="b_algo">
          <h2><a href="https://example.test/result"> Example result </a></h2>
          <div class="b_caption"><p> Useful snippet </p></div>
        </li></ol>
        """

    monkeypatch.setattr(msp_search, "request", lambda *_args, **_kwargs: Response())
    provider = msp_search.BingHtmlSearchProvider()
    assert provider.search("query", 5, 2) == [
        {
            "title": "Example result",
            "url": "https://example.test/result",
            "snippet": "Useful snippet",
        }
    ]


def test_bing_html_provider_reports_challenge_instead_of_false_availability(monkeypatch):
    class Response:
        text = '<div id="b_captcha">Please solve the challenge below</div>'

    monkeypatch.setattr(msp_search, "request", lambda *_args, **_kwargs: Response())
    with pytest.raises(msp_search.SearchProviderError) as error:
        msp_search.BingHtmlSearchProvider().search("query", 1, 2)
    assert error.value.kind == "challenge"


def test_search_defaults_to_duckduckgo_and_falls_back(monkeypatch):
    calls = []

    def blocked(query, num, timeout):
        calls.append(("duckduckgo", query, num, timeout))
        raise msp_search.SearchProviderError("CAPTCHA", "challenge")

    def works(query, num, timeout):
        calls.append(("brave", query, num, timeout))
        return RESULT

    monkeypatch.setitem(msp_search.SEARCH_PROVIDERS, "duckduckgo", _provider("duckduckgo", blocked))
    monkeypatch.setitem(msp_search.SEARCH_PROVIDERS, "brave", _provider("brave", works))
    monkeypatch.setattr(msp_search, "ENGINE_ORDER", ["duckduckgo", "brave"])

    response = msp_search.search_web("  unity jobs  ", num=99)

    assert response["success"] is True
    assert response["requested_engine"] == "duckduckgo"
    assert response["engine_used"] == "brave"
    assert response["fallback_used"] is True
    assert response["results"] == RESULT
    assert response["errors"]["duckduckgo"]["kind"] == "challenge"
    suggested = response["challenge_recoveries"][0]["suggested_arguments"]
    assert suggested["actions"][0]["action"] == "open"
    assert suggested["actions"][0]["headless"] is False
    assert calls == [("duckduckgo", "unity jobs", 20, 4.0), ("brave", "unity jobs", 20, 4.0)]


def test_search_uses_one_overall_deadline_for_fallback(monkeypatch):
    calls = []

    def slow_failure(_query, _num, timeout):
        calls.append(("slow", timeout))
        msp_search.time.sleep(1.05)
        raise msp_search.SearchProviderError("timed out", "timeout")

    def should_not_run(_query, _num, timeout):
        calls.append(("fallback", timeout))
        return RESULT

    monkeypatch.setattr(msp_search, "ENGINE_ORDER", ["duckduckgo", "brave"])
    monkeypatch.setitem(
        msp_search.SEARCH_PROVIDERS,
        "duckduckgo",
        _provider("duckduckgo", slow_failure),
    )
    monkeypatch.setitem(
        msp_search.SEARCH_PROVIDERS,
        "brave",
        _provider("brave", should_not_run),
    )

    response = msp_search.search_web("query", timeout_seconds=1.0)

    assert calls == [("slow", pytest.approx(1.0, abs=0.05))]
    assert response["success"] is False
    assert response["errors"]["overall_deadline"]["kind"] == "timeout"


def test_challenged_provider_is_skipped_during_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(msp_search, "ENGINE_ORDER", ["duckduckgo", "brave"])
    monkeypatch.setitem(
        msp_search.SEARCH_PROVIDERS,
        "duckduckgo",
        _provider("duckduckgo", lambda *_args: calls.append("duckduckgo") or RESULT),
    )
    monkeypatch.setitem(
        msp_search.SEARCH_PROVIDERS,
        "brave",
        _provider("brave", lambda *_args: calls.append("brave") or RESULT),
    )
    msp_search._provider_state["duckduckgo"] = {
        "cooldown_until": msp_search.time.monotonic() + 60,
        "error_kind": "challenge",
        "last_error": "CAPTCHA",
    }

    response = msp_search.search_web("query")
    assert calls == ["brave"]
    assert response["engine_used"] == "brave"
    assert response["errors"]["duckduckgo"]["cooldown_seconds"] > 0


def test_search_result_cache_avoids_second_provider_request(monkeypatch):
    calls = []
    monkeypatch.setattr(msp_search, "ENGINE_ORDER", ["duckduckgo"])
    monkeypatch.setitem(
        msp_search.SEARCH_PROVIDERS,
        "duckduckgo",
        _provider("duckduckgo", lambda *_args: calls.append(1) or RESULT),
    )
    first = msp_search.search_web("query", fallback=False)
    second = msp_search.search_web("query", fallback=False)
    assert first["cached"] is False
    assert second["cached"] is True
    assert calls == [1]


def test_search_without_fallback_returns_structured_error(monkeypatch):
    def fails(*_args):
        raise RuntimeError("offline")

    monkeypatch.setattr(msp_search, "ENGINE_ORDER", ["duckduckgo"])
    monkeypatch.setitem(msp_search.SEARCH_PROVIDERS, "duckduckgo", _provider("duckduckgo", fails))
    response = msp_search.search_web("query", fallback=False)
    assert response["success"] is False
    assert response["engine_used"] is None
    assert response["errors"]["duckduckgo"]["kind"] == "provider_error"


def test_search_validates_query_and_engine():
    with pytest.raises(ValueError, match="query must not be empty"):
        msp_search.search_web("  ")
    with pytest.raises(ValueError, match="Unknown engine"):
        msp_search.search_web("query", engine="missing")


def test_status_without_live_check_lists_provider_registry():
    status = msp_search.get_search_engines_status(check_live=False)
    assert status["default_engine"] == "duckduckgo"
    assert status["configured"] == list(msp_search.ENGINE_ORDER)
    assert all(item["state"] == "configured" for item in status["engines"])


def test_live_status_probes_providers_concurrently_and_caches(monkeypatch):
    names = ["duckduckgo", "brave", "mojeek"]
    barrier = threading.Barrier(len(names))
    calls = []
    monkeypatch.setattr(msp_search, "ENGINE_ORDER", names)
    monkeypatch.setattr(msp_search, "_provider_locks", {name: threading.Lock() for name in names})

    for name in names:
        def run(_q, _n, _t, current=name):
            calls.append(current)
            barrier.wait(timeout=2)
            if current == "brave":
                raise RuntimeError("offline")
            return RESULT
        monkeypatch.setitem(msp_search.SEARCH_PROVIDERS, name, _provider(name, run))

    first = msp_search.get_search_engines_status(force_refresh=True)
    second = msp_search.get_search_engines_status()
    assert first["available"] == ["duckduckgo", "mojeek"]
    assert second["cached"] is True
    assert sorted(calls) == sorted(names)


def test_live_status_probe_does_not_cool_down_the_real_search_path(monkeypatch):
    """A diagnostic status check must never disable a provider for real searches."""
    names = ["duckduckgo", "brave"]
    monkeypatch.setattr(msp_search, "ENGINE_ORDER", names)
    monkeypatch.setattr(msp_search, "_provider_locks", {name: threading.Lock() for name in names})
    attempts = []

    def challenged(_query, _num, _timeout):
        attempts.append("duckduckgo")
        if len(attempts) == 1:
            raise msp_search.SearchProviderError("CAPTCHA", "challenge")
        return RESULT

    monkeypatch.setitem(msp_search.SEARCH_PROVIDERS, "duckduckgo", _provider("duckduckgo", challenged))
    monkeypatch.setitem(msp_search.SEARCH_PROVIDERS, "brave", _provider("brave", lambda *_a: RESULT))

    status = msp_search.get_search_engines_status(force_refresh=True)
    challenged_engine = next(item for item in status["engines"] if item["name"] == "duckduckgo")
    assert challenged_engine["state"] == "challenge"
    assert challenged_engine["cooldown_seconds"] == 0
    assert msp_search._cooldown_remaining("duckduckgo") == 0

    response = msp_search.search_web("query", engine="duckduckgo", fallback=False)
    assert response["engine_used"] == "duckduckgo"
    assert len(attempts) == 2


def test_real_search_challenge_still_records_cooldown(monkeypatch):
    monkeypatch.setattr(msp_search, "ENGINE_ORDER", ["duckduckgo"])
    monkeypatch.setattr(msp_search, "_provider_locks", {"duckduckgo": threading.Lock()})

    def blocked(*_args):
        raise msp_search.SearchProviderError("CAPTCHA", "challenge")

    monkeypatch.setitem(msp_search.SEARCH_PROVIDERS, "duckduckgo", _provider("duckduckgo", blocked))
    msp_search.search_web("query", engine="duckduckgo", fallback=False)
    assert msp_search._cooldown_remaining("duckduckgo") > 0


def test_new_provider_registration_updates_dispatch_and_invalidates_status(monkeypatch):
    monkeypatch.setattr(msp_search, "SEARCH_PROVIDERS", dict(msp_search.SEARCH_PROVIDERS))
    monkeypatch.setattr(msp_search, "ENGINE_ORDER", list(msp_search.ENGINE_ORDER))
    monkeypatch.setattr(msp_search, "_provider_locks", dict(msp_search._provider_locks))
    monkeypatch.setattr(msp_search, "_provider_state", dict(msp_search._provider_state))
    monkeypatch.setattr(msp_search, "_status_cache", (0, {"stale": True}))
    msp_search.register_search_provider(_provider("local_test", lambda *_args: RESULT))
    response = msp_search.search_web("query", engine="local_test", fallback=False)
    assert msp_search.ENGINE_ORDER[-1] == "local_test"
    assert response["engine_used"] == "local_test"
    assert msp_search._status_cache is None
