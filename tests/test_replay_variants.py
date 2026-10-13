"""replay_request(vary=...): one request replayed with one thing changed at a time.

Canned drivers and pure calls only: no Chrome, no network.
"""

from __future__ import annotations

import json

import pytest

from web_search_neo import browser_tools
from web_search_neo.fetch import variants


BASELINE_BODY = '{"id": 1, "role": "user", "profile": {"name": "a"}}'

BASELINE_RESPONSE = {
    "ok": True,
    "status": 200,
    "url": "https://host/api/record",
    "headers": {"content-type": "application/json"},
    "body": BASELINE_BODY,
    "truncated": False,
    "ms": 12,
}


class _CannedDriver:
    is_extension_bridge = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.scripts: list[tuple[str, list]] = []

    def execute_cdp_cmd(self, command, params, timeout=None):
        if command == "Runtime.evaluate":
            expression = params["expression"]
            head, _, arguments = expression.rpartition(".apply(null, ")
            self.scripts.append((head, json.loads(arguments[:-1])))
        index = min(len(self.scripts) - 1, len(self.responses) - 1)
        return {"result": {"value": self.responses[index]}}

    def execute_script(self, script, *args):
        self.scripts.append((script, list(args)))
        return self.responses[0]

    def quit(self):
        pass


def _register(driver, session_id="vary"):
    session = browser_tools.BrowserSession(driver=driver, headless=False)
    browser_tools._sessions[session_id] = session
    return session


def _response(**changes) -> dict:
    return {**BASELINE_RESPONSE, **changes}


# --- the sweep itself ---------------------------------------------------------


def test_a_change_that_does_nothing_says_so():
    out = variants.run_variants(
        lambda spec: dict(BASELINE_RESPONSE),
        {"url": "https://host/api/record", "method": "GET", "headers": {}},
        {"headers": [{"x-role": "admin"}]},
        baseline=dict(BASELINE_RESPONSE),
    )
    row = out["variants"][0]
    assert row["label"] == "header x-role=admin"
    assert row["same_as_baseline"] is True
    assert row["status"] == 200
    assert out["baseline"] == {"status": 200, "bytes": len(BASELINE_BODY)}


def test_a_variant_that_works_names_the_status_the_paths_and_the_headers():
    changed = _response(
        status=203,
        body='{"id": 1, "role": "admin", "profile": {"name": "a"}, "scope": ["write"]}',
        headers={"content-type": "application/json", "x-granted": "yes"},
    )
    out = variants.run_variants(
        lambda spec: changed,
        {"url": "https://host/api/record", "method": "GET", "headers": {}},
        {"headers": [{"x-role": "admin"}]},
        baseline=dict(BASELINE_RESPONSE),
    )
    row = out["variants"][0]
    assert row["same_as_baseline"] is False
    assert row["status_changed"] is True and row["status_from"] == 200 and row["status"] == 203
    assert row["paths_added"] == ["scope"]
    assert row["values_changed"] == [{"path": "role", "from": "string(4)", "to": "string(5)"}]
    assert row["headers_added"] == ["x-granted"] and row["headers_removed"] == []


def test_a_delta_never_quotes_a_value_a_secret_could_be():
    leaked = _response(body='{"id": 1, "role": "user", "token": "om-SUPERSECRETVALUE"}')
    out = variants.run_variants(
        lambda spec: leaked,
        {"url": "https://host/api/record", "method": "GET", "headers": {}},
        {"query": [{"uid": "2"}]},
        baseline=dict(BASELINE_RESPONSE),
    )
    assert json.dumps(out).count("SUPERSECRET") == 0
    assert out["variants"][0]["paths_added"] == ["token"]


def test_query_and_body_mutations_reach_the_request_they_are_named_for():
    seen: list[dict] = []

    def execute(spec):
        seen.append(dict(spec))
        return dict(BASELINE_RESPONSE)

    out = variants.run_variants(
        execute,
        {"url": "https://host/api/record?uid=1", "method": "GET", "headers": {"x-a": "1"}},
        {"query": [{"uid": "61808"}], "body": ['{"role": "admin"}']},
        baseline=dict(BASELINE_RESPONSE),
    )
    assert [row["label"] for row in out["variants"]] == ["query uid=61808", "body #1"]
    assert seen[0]["url"] == "https://host/api/record?uid=61808"
    assert seen[0]["method"] == "GET" and seen[0]["headers"] == {"x-a": "1"}
    # A body only makes sense as a POST: the sweep says POST rather than sending
    # a GET with a body most servers will ignore.
    assert seen[1]["body"] == '{"role": "admin"}' and seen[1]["method"] == "POST"
    assert seen[1]["url"] == "https://host/api/record?uid=1"


def test_a_sweep_is_bounded_and_reports_what_it_dropped():
    vary = {"headers": [{f"x-a{i}": str(i)} for i in range(variants.MAX_VARIANTS + 3)]}
    out = variants.run_variants(
        lambda spec: dict(BASELINE_RESPONSE),
        {"url": "https://host/api/record", "method": "GET", "headers": {}},
        vary,
        baseline=dict(BASELINE_RESPONSE),
    )
    assert out["variants_ran"] == variants.MAX_VARIANTS
    assert out["variants_dropped"] == 3 and out["variants_limit"] == variants.MAX_VARIANTS


def test_a_vary_key_that_is_not_a_mutation_is_named_not_ignored():
    out = variants.run_variants(
        lambda spec: dict(BASELINE_RESPONSE),
        {"url": "https://host/api/record", "method": "GET", "headers": {}},
        {"cookies": [{"a": "b"}], "headers": [{"x-a": "1"}]},
        baseline=dict(BASELINE_RESPONSE),
    )
    assert out["vary_unsupported"] == ["cookies"]
    assert len(out["variants"]) == 1

    empty = variants.run_variants(lambda spec: dict(BASELINE_RESPONSE), {}, {})
    assert empty["variants"] == [] and "vary" in empty["variant_note"]


def test_a_variant_request_that_fails_is_reported_as_its_own_failure():
    def execute(spec):
        if spec.get("headers"):
            return {"ok": False, "error": "the page-side fetch was blocked"}
        return dict(BASELINE_RESPONSE)

    out = variants.run_variants(
        execute,
        {"url": "https://host/api/record", "method": "GET"},
        {"headers": [{"x-a": "1"}]},
        baseline=dict(BASELINE_RESPONSE),
    )
    assert out["variants"][0]["error"] == "the page-side fetch was blocked"
    assert "same_as_baseline" not in out["variants"][0]


def test_a_new_field_under_an_object_that_already_existed_keeps_its_full_path():
    changed = _response(body='{"id": 1, "role": "user", "profile": {"name": "a", "email": "x"}}')
    out = variants.run_variants(
        lambda spec: changed,
        {"url": "https://host/api/record", "method": "GET"},
        {"headers": [{"x-role": "admin"}]},
        baseline=dict(BASELINE_RESPONSE),
    )
    assert out["variants"][0]["paths_added"] == ["profile.email"]


def test_a_body_that_is_not_json_is_compared_by_status_and_size_only():
    out = variants.run_variants(
        lambda spec: _response(status=500, body="<html>boom</html>"),
        {"url": "https://host/api/record", "method": "GET"},
        {"headers": [{"x-a": "1"}]},
        baseline=_response(body="<html>ok</html>"),
    )
    row = out["variants"][0]
    assert row["body_kind"] == "not_json"
    assert "paths_added" not in row and row["status_changed"] is True


# --- through replay_request ----------------------------------------------------


def test_replay_with_vary_sends_the_baseline_once_then_one_request_per_change(monkeypatch):
    driver = _CannedDriver(
        [
            BASELINE_RESPONSE,
            _response(status=403, body='{"id": 1, "role": "user", "profile": {"name": "a"}}'),
            _response(body='{"id": 1, "role": "admin", "profile": {"name": "a"}}'),
        ]
    )
    _register(driver, "vary-sweep")
    monkeypatch.setattr(browser_tools, "_page_summary", lambda driver, session_id: {})
    result = browser_tools.replay_request(
        url="https://host/api/record",
        method="GET",
        session_id="vary-sweep",
        vary={"headers": [{"x-role": "admin"}], "body": ['{"role": "admin"}']},
    )
    assert result["success"] is True
    assert result["response"]["status"] == 200
    specs = [args[0] for _head, args in driver.scripts]
    assert len(specs) == 3, "the baseline plus one request per mutation"
    assert specs[0]["headers"] == {} and specs[1]["headers"] == {"x-role": "admin"}
    sweep = result["variants"]
    assert [row["label"] for row in sweep["variants"]] == ["header x-role=admin", "body #1"]
    assert sweep["variants"][0]["status_changed"] is True
    assert sweep["variants"][1]["values_changed"] == [
        {"path": "role", "from": "string(4)", "to": "string(5)"}
    ]


def test_replay_without_vary_is_unchanged_and_carries_no_sweep(monkeypatch):
    driver = _CannedDriver([BASELINE_RESPONSE])
    _register(driver, "vary-plain")
    monkeypatch.setattr(browser_tools, "_page_summary", lambda driver, session_id: {})
    result = browser_tools.replay_request(url="https://host/api/record", session_id="vary-plain")
    assert "variants" not in result
    assert len(driver.scripts) == 1


def test_a_sweep_that_the_page_refused_still_returns_the_baseline_answer(monkeypatch):
    driver = _CannedDriver(
        [BASELINE_RESPONSE, {"ok": False, "status": 0, "error": "NetworkError"}]
    )
    _register(driver, "vary-refused")
    monkeypatch.setattr(browser_tools, "_page_summary", lambda driver, session_id: {})
    result = browser_tools.replay_request(
        url="https://host/api/record", session_id="vary-refused", vary={"headers": [{"x-a": "1"}]}
    )
    assert result["success"] is True
    assert result["variants"]["variants"][0]["error"] == "NetworkError"


def test_vary_is_documented_where_agents_read_actions():
    from web_search_neo import main

    spec = main._ACTIONS["replay_request"]
    text = json.dumps(main._capabilities())
    assert "replay_request" in text
    assert "vary" in browser_tools.replay_request.__doc__
    assert getattr(spec, "handler", None) is not None or callable(spec)
