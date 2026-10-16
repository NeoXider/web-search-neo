"""vary on the browserless http_request: sweep headers without Chrome."""

from __future__ import annotations

from typing import Any

import web_search_neo.fetch.api as api

from tests.test_http_request import URL, _FakeResponse


def _install(monkeypatch, responses: dict[tuple[str, str], _FakeResponse]):
    seen: list[dict[str, Any]] = []

    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        seen.append({"url": url, **kwargs})
        headers = kwargs.get("headers") or {}
        return responses.get((str(kwargs.get("method")), str(headers.get("X-Test"))),
                             _FakeResponse(status=200, text="baseline"))

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    return seen


def test_vary_sweeps_headers_without_a_browser(monkeypatch):
    seen = _install(monkeypatch, {
        ("GET", None): _FakeResponse(status=200, text="baseline"),
        ("GET", "a"): _FakeResponse(status=403, text="forbidden"),
        ("GET", "b"): _FakeResponse(status=200, text='{"b": 1}'),
    })
    result = api.http_request(
        URL, vary={"headers": [{"X-Test": "a"}, {"X-Test": "b"}]})
    assert result["success"] is True and result["method"] == "GET"
    sweep = result["swept"]
    assert sweep["baseline"] == {"status": 200, "bytes": len("baseline")}
    assert sweep["variants_ran"] == 2
    labels = {v["label"] for v in sweep["variants"]}
    assert labels == {"header X-Test=a", "header X-Test=b"}
    one = next(v for v in sweep["variants"] if v["label"] == "header X-Test=a")
    assert one["status"] == 403
    assert one["status_changed"] is True and one["status_from"] == 200
    # the baseline request is sent first, then one request per change
    assert len(seen) == 3
    assert seen[0]["headers"] == {}
    assert [s["headers"].get("X-Test") for s in seen[1:]] == ["a", "b"]


def test_vary_body_delta_upgrades_get_to_post(monkeypatch):
    seen = _install(monkeypatch, {})
    result = api.http_request(URL, vary={'body': ['{"grant": 1}', '{"trial": 2}']})
    sweep = result["swept"]
    assert sweep["variants_ran"] == 2
    # the sweep itself does not echo methods: the client sees them
    assert str(seen[0]["method"]) == "GET"
    assert [str(s["method"]) for s in seen[1:]] == ["POST", "POST"]


def test_vary_with_save_to_raises():
    try:
        api.http_request(URL, save_to="x.bin", vary={"headers": [{"A": "1"}]})
    except ValueError as exc:
        assert "save_to" in str(exc)
    else:
        raise AssertionError("vary + save_to must raise")


def test_vary_reports_query_diffs(monkeypatch):
    seen = _install(monkeypatch, {})
    result = api.http_request(URL, vary={"query": [{"lang": "en"}, {"lang": "ru"}]})
    sweep = result["swept"]
    assert sweep["variants_ran"] == 2
    assert {v["label"] for v in sweep["variants"]} == {"query lang=en", "query lang=ru"}
    assert [s["url"].split("?")[-1] for s in seen[1:]] == ["lang=en", "lang=ru"]
