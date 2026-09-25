"""1.23: active_probe - bounded active checks with the call as consent.

Pure analysis tests pin the preflight/method/redirect/reflection verdicts;
the fixture server (``active_fixture_site``) answers OPTIONS/TRACE, bounces
off-site and mirrors the query back. active_probe needs no browser at all, so
no Chrome test skips here - but no test reaches the internet either.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from pathlib import Path

import pytest

import active_fixture_site
import secret_fixture_site
from web_search_neo import audit_actions, browser_tools, main
from web_search_neo.audit import active
from web_search_neo.audit.scope import Budget
from web_search_neo.audit import report as audit_report
from web_search_neo.audit import site as audit_site
from web_search_neo.contract import notes, param_docs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://probe.example"


def ids(findings):
    return {item["id"]: item for item in findings}


# --- verdicts ----------------------------------------------------------------------------

def test_preflight_cases():
    reflected = {"access-control-allow-origin": ORIGIN, "access-control-allow-credentials": "true"}
    found = ids(active.preflight_findings("https://h.test/api", ORIGIN, reflected))
    assert found["active-cors-foreign-credentials"]["severity"] == "high"
    plain = {"access-control-allow-origin": ORIGIN}
    assert ids(active.preflight_findings("https://h.test/api", ORIGIN, plain))[
        "active-cors-foreign-origin"]["status"] == "warn"
    null = {"access-control-allow-origin": "null"}
    assert ids(active.preflight_findings("https://h.test/api", ORIGIN, null))[
        "active-cors-null"]["status"] == "fail"
    both = {"access-control-allow-origin": "*", "access-control-allow-credentials": "true"}
    assert ids(active.preflight_findings("https://h.test/api", ORIGIN, both))[
        "active-cors-wildcard-credentials"]["status"] == "fail"
    wide = {"access-control-allow-origin": ORIGIN, "access-control-allow-methods": "*",
            "access-control-allow-headers": "*"}
    assert ids(active.preflight_findings("https://h.test/api", ORIGIN, wide))[
        "active-cors-methods-open"]["status"] == "warn"
    assert ids(active.preflight_findings("https://h.test/api", ORIGIN, {}))[
        "active-cors-closed"]["status"] == "pass"


def test_methods_cases():
    found = ids(active.methods_findings("https://h.test/", 200, "GET, POST, OPTIONS", 200))
    assert found["active-trace-enabled"]["severity"] == "medium"
    assert found["active-methods-allowed"]["status"] == "info"
    refused = ids(active.methods_findings("https://h.test/", 405, "", None))
    assert refused["active-options-refused"]["status"] == "info"
    assert "active-trace-enabled" not in refused


def test_redirect_cases():
    hit = active.redirect_findings("http://127.0.0.1:9/redir",
                                   ["http://127.0.0.1:9/redir"],
                                   "http://localhost:9/landing", "http://127.0.0.1:9/")
    assert len(hit) == 1 and hit[0]["id"] == "active-open-redirect"
    assert hit[0]["severity"] == "medium" and "localhost" in hit[0]["evidence"]["to"]
    assert active.redirect_findings("http://127.0.0.1:9/a", ["http://127.0.0.1:9/a"],
                                    "http://127.0.0.1:9/b", "http://127.0.0.1:9/") == []
    cut = active.redirect_findings("http://127.0.0.1:9/redir", ["http://127.0.0.1:9/redir"],
                                   "http://127.0.0.1:9/redir", "http://127.0.0.1:9/",
                                   cut_at="http://localhost:9/landing")
    assert cut and "cut at the scope boundary" in cut[0]["detail"]


def test_reflection_cases():
    token = "wsn0123456789abcdef"
    body = f"<html><body><p>results for {token}</p><a href=\"/s?q={token}\">more</a></body></html>"
    hit = active.reflection_findings("http://127.0.0.1:9/search", token, body)
    assert len(hit) == 1 and hit[0]["id"] == "active-reflected-input"
    assert hit[0]["status"] == "warn" and hit[0]["severity"] == "low"
    assert hit[0]["evidence"]["text_contexts"] == 1 and hit[0]["evidence"]["tag_contexts"] == 1
    assert active.reflection_findings("http://127.0.0.1:9/", token, "<html><body>hi</body></html>") == []
    assert re.fullmatch(r"wsn[0-9a-f]+", active.new_canary())


def test_build_shape():
    report = active.build("http://127.0.0.1:9/", ["cors"],
                          [{"id": "x", "status": "warn", "severity": "low", "title": "T",
                            "points": 0}], ["GET http://127.0.0.1:9/"])
    assert report["success"] and report["checks"] == ["cors"]
    assert report["requests_made"] == ["GET http://127.0.0.1:9/"]
    assert report["priority"][0]["rank"] == 1 and report["counts"]["low"] == 1
    assert report["scope"].startswith("active_probe sends only")


# --- validation without any network ---------------------------------------------------------

def test_checker_refuses_method_and_scope_without_sending():
    scope = audit_site._scope_for("http://127.0.0.1:9/", None, False, "page")
    checker = audit_report.Checker(scope, Budget(), 5.0)
    refused = checker.request("DELETE", "http://127.0.0.1:9/")
    assert refused["error"].startswith("refused method") and checker.budget.made == []
    outside = checker.request("OPTIONS", "http://localhost:9/")
    assert outside["skipped"] == "scope" and checker.budget.made == []


def test_probe_validates_arguments_before_anything():
    with pytest.raises(ValueError, match="needs url"):
        asyncio.run(audit_actions.browser_active_probe(url=None))
    with pytest.raises(ValueError, match="subset"):
        asyncio.run(audit_actions.browser_active_probe(url="http://127.0.0.1:9/", checks=["bogus"]))
    with pytest.raises(ValueError, match="bare https"):
        asyncio.run(audit_actions.browser_active_probe(url="http://127.0.0.1:9/", origin="not-a-url"))
    with pytest.raises(ValueError, match="wildcard"):
        asyncio.run(audit_actions.browser_active_probe(url="http://127.0.0.1:9/", hosts=["*.example.com"]))


# --- contract ----------------------------------------------------------------------------------

def test_active_probe_in_the_action_table_and_the_contract():
    specs = {name: (wrapper, group) for name, wrapper, group, _summary in audit_actions.ACTION_SPECS}
    assert specs["active_probe"][0] is audit_actions.browser_active_probe
    assert specs["active_probe"][1] == "audit"
    assert "active_probe" in main._ACTIONS
    assert "active_probe" in param_docs._BY_ACTION and "active_probe" in notes._ACTION_NOTES
    assert len(json.dumps(main._capabilities())) <= 13_500


def test_probe_params_in_action_schema():
    schema = asyncio.run(main.web_info("action_schema", {"action": "active_probe"}))
    assert {"url", "hosts", "paths", "checks", "origin", "save_to", "sarif_to",
            "baseline", "overwrite"} <= set(schema["input_schema"]["properties"])


# --- secret_scan additions: source maps ---------------------------------------------------------------

def test_sourcemap_refs_forms():
    from web_search_neo.audit import secrets as secret_checks

    assert secret_checks.sourcemap_refs('//# sourceMappingURL=/a/b.js.map\n') == ["/a/b.js.map"]
    assert secret_checks.sourcemap_refs('/*# sourceMappingURL=x.js.map */') == ["x.js.map"]
    assert secret_checks.sourcemap_refs('//# sourceMappingURL=data:application/json;base64,xx') == []
    assert secret_checks.sourcemap_refs('//# sourceMappingURL=a.map\n//# sourceMappingURL=a.map') == ["a.map"]


def test_sourcemap_view_names_but_never_code():
    from web_search_neo.audit import secrets as secret_checks

    view = secret_checks.sourcemap_view(
        "https://h.test/a.js.map",
        '{"version": 3, "sources": ["a.ts", "b.ts"], "sourcesContent": ["code-a", "code-b"]}')
    assert view == {"url": "https://h.test/a.js.map", "sources": 2,
                    "names": ["a.ts", "b.ts"], "names_omitted": 0, "has_content": True}
    bare = secret_checks.sourcemap_view("https://h.test/a.js.map", '{"version": 3, "sources": []}')
    assert bare["has_content"] is False
    assert secret_checks.sourcemap_view("https://h.test/a.js.map", "not json") is None
    assert secret_checks.sourcemap_view("https://h.test/a.js.map", '{"info": 1}') is None


def test_sourcemap_findings_mask_code():
    from web_search_neo.audit import secrets as secret_checks

    report = secret_checks.build(
        "https://h.test/", "<html></html>", [], [],
        [{"url": "https://h.test/a.js.map", "sources": 1, "names": ["a.ts"],
          "names_omitted": 0, "has_content": True}],
        [], {}, None, [])
    found = {item["id"]: item for item in report["findings"]}
    assert found["secret-sourcemap-sources"]["severity"] == "high"
    calm = secret_checks.build(
        "https://h.test/", "<html></html>", [], [],
        [{"url": "https://h.test/a.js.map", "sources": 1, "names": ["a.ts"],
          "names_omitted": 0, "has_content": False}],
        [], {}, None, [])
    assert {item["id"] for item in calm["findings"]} == {"secret-sourcemap-exposed"}


# --- api_report addition: JWT lifetime -----------------------------------------------------------------

def test_jwt_lifetime_findings():
    from web_search_neo.audit import api_checks

    def cookie(name, alg, exp, iat):
        return {"name": name, "domain": "h.test", "secure": True, "httponly": True,
                "sameSite": "Lax", "format": "jwt",
                "jwt": {"alg": alg, "exp": exp, "iat": iat, "signed": True}}

    old = [cookie("sess", "HS256", 2_000_000_000, 1_000_000_000)]
    found = {item["id"]: item for item in api_checks.auth_findings(old, [], [], True, None)}
    assert found["api-jwt-long-lived"]["severity"] == "medium"
    assert found["api-jwt-long-lived"]["evidence"]["items"][0]["lifetime_hours"] == 277778
    fresh = [cookie("sess", "HS256", 1_000_003_600, 1_000_000_000)]
    assert "api-jwt-long-lived" not in {item["id"] for item in
                                        api_checks.auth_findings(fresh, [], [], True, None)}


# --- against the local fixture server (no browser) -----------------------------------------------

@pytest.fixture(scope="module")
def active_site():
    site = active_fixture_site.start()
    try:
        yield site
    finally:
        site.stop()


def _http(site, path, method="GET", headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoFollow())
    request = urllib.request.Request(site.base_url + path, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=10) as resp:
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def test_fixture_answers_options_trace_redirect_and_search(active_site):
    import urllib.error

    status, headers, _ = _http(active_site, "/api/echo-cors", method="OPTIONS",
                               headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
    assert status == 200 and headers["access-control-allow-origin"] == ORIGIN
    assert headers["access-control-allow-credentials"] == "true"
    status, _, _ = _http(active_site, "/", method="TRACE")
    assert status == 200
    status, headers, _ = _http(active_site, "/redir?to=" + active_site.third_party + "/landing")
    assert status == 302 and headers["location"] == active_site.third_party + "/landing"
    status, _, body = _http(active_site, "/search?q=hello")
    assert status == 200 and b"results for q=hello" in body


def test_active_probe_runs_all_families(active_site):
    result = asyncio.run(audit_actions.browser_active_probe(
        url=active_site.base_url + "/", paths=["/api/echo-cors"]))
    assert result["success"], result
    assert result["checks"] == ["cors", "methods", "redirects", "canary"]
    found = ids(result["findings"])
    assert found["active-cors-foreign-credentials"]["severity"] == "high"
    assert found["active-trace-enabled"]["status"] == "warn"
    assert found["active-open-redirect"]["severity"] == "medium"
    assert found["active-reflected-input"]["status"] == "warn"
    made = result["requests_made"]
    # Every request went to the scope host; the off-site redirect target only ever
    # appears percent-encoded inside a query value, and the chain stops at the edge.
    assert made and all(line.split(" ", 1)[1].split(" (", 1)[0].startswith(active_site.base_url)
                        for line in made)
    assert any(line.startswith("OPTIONS") for line in made)
    assert any(line.startswith("TRACE") for line in made)
    assert not browser_tools._sessions  # no browser involved at all


def test_active_probe_checks_subset_and_origin_override(active_site):
    result = asyncio.run(audit_actions.browser_active_probe(
        url=active_site.base_url + "/", paths=["/api/echo-cors"],
        checks=["cors"], origin="https://my.example"))
    assert result["success"], result
    assert result["checks"] == ["cors"]
    assert {f["id"] for f in result["findings"]} <= {
        "active-cors-closed", "active-cors-foreign-credentials", "active-cors-foreign-origin",
        "active-cors-null", "active-cors-wildcard-credentials", "active-cors-methods-open",
        "active-check-unreachable"}


def test_active_probe_save_sarif_baseline_and_min_summary(active_site, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    first = asyncio.run(main.web_action([{"action": "active_probe", "url": active_site.base_url + "/",
                                          "paths": ["/api/echo-cors"], "save_to": "active.json",
                                          "sarif_to": "active.sarif", "overwrite": True}]))
    assert first["success"], first
    data = first["results"][0]["data"]
    assert Path(data["saved_to"]).is_file()
    sarif_doc = json.loads(Path(data["sarif_saved_to"]).read_text(encoding="utf-8"))
    assert sarif_doc["version"] == "2.1.0" and sarif_doc["runs"][0]["results"]
    again = asyncio.run(main.web_action([{"action": "active_probe", "url": active_site.base_url + "/",
                                          "paths": ["/api/echo-cors"], "baseline": "active.json"}]))
    assert again["success"], again
    assert again["results"][0]["data"]["regression"]["summary"].startswith("No change")
    mini = asyncio.run(main.web_action([{"action": "active_probe", "url": active_site.base_url + "/",
                                         "paths": ["/api/echo-cors"]}], summary="min"))
    mdata = mini["results"][0]["data"]
    assert mdata["summary_mode"] == "min" and mdata["counts"] and mdata["summary_line"]
    assert len(mdata["priority"]) <= 5 and "findings" in mdata["summary_omitted"]


@pytest.fixture(scope="module")
def login_site():
    site = secret_fixture_site.start()
    try:
        yield site
    finally:
        site.stop()


def test_login_run_then_api_report_reads_the_session(login_site):
    from selenium.common.exceptions import WebDriverException

    run = asyncio.run(main.web_action([{
        "action": "test_run", "url": login_site.base_url + "/", "session_id": "login-flow",
        "keep_open": True, "steps": [
            {"action": "fill", "fields": {"input[name=pw]": "fixture-pass"}},
            {"step_name": "sign in", "action": "click", "selector": "#go",
             "expect": {"text": "secret fixture"}},
        ]}]))
    if not run["success"] and "WebDriver" in str(run["results"][0].get("error")):
        pytest.skip(f"Chrome/Selenium is unavailable: {run['results'][0]['error']}")
    assert run["success"], run
    assert run["results"][0]["data"]["success"] is True
    try:
        report = asyncio.run(audit_actions.browser_api_report(session_id="login-flow"))
        assert report["success"] and report["fresh_isolated_load"] is False
        names = {cookie["name"] for cookie in report["auth"]["cookies"]}
        assert "fixture-session" in names
        values = json.dumps(report["auth"])
        assert "logged-in" not in values
    finally:
        browser_tools.close_session("login-flow")
    assert "login-flow" not in browser_tools._sessions
