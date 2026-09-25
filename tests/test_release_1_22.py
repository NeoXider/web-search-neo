"""1.22: secret_scan, SARIF export and baseline regressions.

Pure analysis tests pin the secret patterns (values never leak), the code
endpoint map, the openapi view, the SARIF shape and the diff; the fixture
server (``secret_fixture_site``) serves a page with planted fake secrets;
Chrome-backed tests read a real isolated load and skip when Chrome is
missing. No test reaches the internet.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path



import pytest

import secret_fixture_site
from web_search_neo import audit_actions, browser_tools, main
from web_search_neo.audit import diff, perf, sarif, secrets
from web_search_neo.contract import notes, param_docs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"
FAKE_GITHUB = "ghp_fixturetoken0123456789abcdef"
FAKE_SESSION = "Ab3dEf7hIj2kLm5nOp8Qr0sT3uV1wX4yZ6"


def ids(findings):
    return {item["id"]: item for item in findings}


# --- secret patterns ------------------------------------------------------------------

def test_cloud_keys_fire_high_and_masked():
    text = f'x = "{FAKE_AWS}";\ny = "{FAKE_GITHUB}";\nprivateKey = "x";'
    found = ids(secrets.scan_text(text, "a.js"))
    assert found["secret-aws-key"]["severity"] == "high"
    assert found["secret-github-token"]["status"] == "fail"
    assert "secret-generic" not in found  # the value already has its own finding
    dump = json.dumps(found)
    assert FAKE_AWS not in dump and FAKE_GITHUB not in dump


def test_generic_assignments_jwt_and_private_key():
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    text = ('const api_key = "not-a-real-key-just-tests";\n'
            f'const t = "{fake_jwt}";\n-----BEGIN RSA PRIVATE KEY-----')
    found = ids(secrets.scan_text(text, "b.js"))
    assert found["secret-generic"]["severity"] == "medium"
    assert found["secret-jwt"]["status"] == "warn"
    assert found["secret-private-key"]["severity"] == "high"
    dump = json.dumps(found)
    assert "not-a-real-key-just-tests" not in dump and fake_jwt not in dump
    assert "api_key" in dump  # the name stays readable, the value does not


def test_entropy_and_internal_urls():
    text = f'const session = "{FAKE_SESSION}";\nconst u = "http://10.0.0.5/internal";'
    found = ids(secrets.scan_text(text, "c.js"))
    assert found["secret-entropy"]["status"] == "warn"
    assert found["secret-internal-url"]["status"] == "info"
    dump = json.dumps(found)
    assert FAKE_SESSION not in dump
    assert "http://10.0.0.5/internal" not in dump
    assert secrets.scan_text("const a = 1;\nfunction go() { return 2; }", "d.js") == []


def test_samples_redact_every_secret_in_the_window():
    text = f'a = "{FAKE_AWS}"; b = "{FAKE_GITHUB}";'
    found = ids(secrets.scan_text(text, "e.js"))
    dump = json.dumps(found["secret-aws-key"]) + json.dumps(found["secret-github-token"])
    assert FAKE_AWS not in dump and FAKE_GITHUB not in dump


# --- code endpoints and openapi ----------------------------------------------------------

def test_code_endpoints_from_fetch_axios_and_literals():
    text = ('fetch("/api/users/123");\n'
            'fetch("/api/save", {method: "POST", body: "{}"});\n'
            'axios.put("/api/items/abc-123");\n'
            'const other = "/api/health";\n'
            'fetch("https://cdn.example.net/api/x");\n')
    own = {"h.test"}
    got = {(item["method"], item["path"]): item for item in
           secrets.code_endpoints(text, "a.js", own)}
    assert set(got) == {("GET", "/api/users/{id}"), ("POST", "/api/save"),
                        ("PUT", "/api/items/abc-123"), ("GET", "/api/health")}
    assert got[("GET", "/api/users/{id}")]["count"] == 1  # fetch + literal counted once
    assert got[("POST", "/api/save")]["sources"] == ["a.js"]


def test_openapi_refs_and_view():
    html = '<link rel="openapi" href="/openapi.json">'
    refs = secrets.openapi_refs(html, ['import x from "/swagger.json";'], ["https://h.test/app.js"])
    assert refs == ["/openapi.json", "/swagger.json"]
    view = secrets.openapi_view("https://h.test/openapi.json",
                                '{"openapi": "3.0.0", "paths": {"/a": {}, "/b": {}}}')
    assert view == {"url": "https://h.test/openapi.json", "version": "3.0.0", "paths": 2}
    assert secrets.openapi_view("https://h.test/openapi.json", "not json") is None
    assert secrets.openapi_view("https://h.test/openapi.json", '{"info": 1}') is None


def test_auth_facts_and_script_url_scope():
    forms = {"items": [{"action": "http://h.test/login", "method": "post", "has_password": True}]}
    passwords = {"items": [{"name": "pw", "autocomplete": None, "form_action": "http://h.test/login"}]}
    facts, findings = secrets.auth_facts(forms, passwords, False)
    assert facts == [{"action": "http://h.test/login", "method": "post",
                      "https": False, "has_password": True}]
    found = ids(findings)
    assert found["secret-auth-http"]["severity"] == "high"
    assert found["secret-auth-autocomplete"]["status"] == "warn"
    _, clean = secrets.auth_facts([], [], True)
    assert clean == []
    own = {"127.0.0.1"}
    urls = secrets.script_urls([{"src": "http://127.0.0.1:9/a.js"}, {"src": None}],
                               ["http://127.0.0.1:9/b.js", "https://cdn.test/c.js"],
                               "http://127.0.0.1:9/", own)
    assert urls == ["http://127.0.0.1:9/a.js", "http://127.0.0.1:9/b.js"]
    third = secrets.third_party_scripts([{"src": "https://cdn.test/c.js"},
                                         {"src": "http://127.0.0.1:9/a.js"}],
                                        "http://127.0.0.1:9/", None)
    assert third == ["https://cdn.test/c.js"]


# --- SARIF and diff ------------------------------------------------------------------------

def test_sarif_shape_and_levels():
    report = {"url": "https://h.test/", "findings": [
        {"id": "secret-aws-key", "status": "fail", "severity": "high",
         "title": "Key", "detail": "D", "fix": "F"},
        {"id": "secret-internal-url", "status": "info", "severity": "info", "title": "U"}]}
    doc = sarif.build(report, version="9.9.9")
    assert doc["version"] == "2.1.0" and doc["runs"][0]["tool"]["driver"]["version"] == "9.9.9"
    assert [rule["id"] for rule in doc["runs"][0]["tool"]["driver"]["rules"]] == [
        "secret-aws-key", "secret-internal-url"]
    assert [item["level"] for item in doc["runs"][0]["results"]] == ["error", "note"]
    assert doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"]["uri"] == "https://h.test/"
    assert isinstance(sarif.dumps(report), bytes)


def test_diff_fixed_added_and_unchanged():
    old = {"findings": [
        {"id": "a", "status": "fail", "severity": "high", "title": "A", "fix": "Fix A"},
        {"id": "b", "status": "warn", "severity": "low", "title": "B"}]}
    same = {"findings": [dict(item) for item in old["findings"]]}
    assert diff.compare(old, same) == {"fixed": [], "added": [],
                                       "summary": "No change: the same findings as the baseline."}
    new = {"findings": [
        {"id": "b", "status": "fail", "severity": "high", "title": "B"},
        {"id": "c", "status": "warn", "severity": "medium", "title": "C", "fix": "Fix C"}]}
    verdict = diff.compare(old, new)
    assert [item["id"] for item in verdict["fixed"]] == ["a"]
    assert [item["id"] for item in verdict["added"]] == ["b", "c"]  # worse counts as added
    assert verdict["summary"] == "Against the baseline: 1 fixed, 2 added."
    assert verdict["fixed"][0]["fix"] == "Fix A"


# --- outputs without a browser ---------------------------------------------------------------

def test_write_outputs_save_sarif_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    report = {"success": True, "url": "https://h.test/", "counts": {"high": 1},
              "findings": [{"id": "secret-aws-key", "status": "fail", "severity": "high",
                            "title": "Key", "fix": "F"}]}
    out = audit_actions._write_outputs(dict(report), "r.json", "r.sarif", None, False)
    assert json.loads(Path(out["saved_to"]).read_text(encoding="utf-8"))["url"] == "https://h.test/"
    sarif_doc = json.loads(Path(out["sarif_saved_to"]).read_text(encoding="utf-8"))
    assert sarif_doc["runs"][0]["results"][0]["ruleId"] == "secret-aws-key"
    regressed = audit_actions._write_outputs(dict(report), None, None, "r.json", False)
    assert regressed["regression"]["summary"].startswith("No change")
    with pytest.raises(ValueError, match="not a file"):
        audit_actions._write_outputs(dict(report), None, None, "missing.json", False)
    with pytest.raises(ValueError, match="must stay inside"):
        audit_actions._write_outputs(dict(report), None, None, "../outside.json", False)


def test_secret_scan_needs_url_or_session_and_valid_hosts():
    with pytest.raises(ValueError, match="needs url"):
        asyncio.run(audit_actions.browser_secret_scan())
    with pytest.raises(ValueError, match="wildcard"):
        asyncio.run(audit_actions.browser_secret_scan(url="http://127.0.0.1:9/",
                                                      hosts=["*.example.com"]))
    assert not browser_tools._sessions


# --- contract ----------------------------------------------------------------------------------

def test_secret_scan_in_the_action_table_and_the_contract():
    specs = {name: (wrapper, group) for name, wrapper, group, _summary in audit_actions.ACTION_SPECS}
    assert specs["secret_scan"][0] is audit_actions.browser_secret_scan
    assert specs["secret_scan"][1] == "audit"
    assert "secret_scan" in main._ACTIONS
    assert "secret_scan" in param_docs._BY_ACTION and "secret_scan" in notes._ACTION_NOTES
    assert len(json.dumps(main._capabilities())) <= 13_500


def test_sarif_and_baseline_in_report_schemas():
    import asyncio
    for action in ("secret_scan", "security_report", "api_report"):
        schema = asyncio.run(main.web_info("action_schema", {"action": action}))
        assert {"save_to", "sarif_to", "baseline", "overwrite"} <= set(
            schema["input_schema"]["properties"])


# --- against the local fixture server (no Chrome) -----------------------------------------------

@pytest.fixture(scope="module")
def secret_site():
    site = secret_fixture_site.start()
    try:
        yield site
    finally:
        site.stop()


def _http(site, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(site.base_url + path, timeout=10) as resp:
        return resp.status, resp.read()


def test_fixture_serves_bundles_openapi_and_login(secret_site):
    status, body = _http(secret_site, "/static/app.js")
    assert status == 200 and FAKE_AWS.encode() in body and b"axios.post" in body
    status, body = _http(secret_site, "/openapi.json")
    assert status == 200 and json.loads(body)["openapi"] == "3.0.0"
    status, body = _http(secret_site, "/")
    assert status == 200 and b"/static/app.js" in body and b"/openapi.json" in body
    assert b'type="password"' in body


# --- regression tests for the novita.ai field findings -------------------------------------------------

def test_sample_centers_on_the_match_in_minified_bundles():
    text = '"head:' + "p" * 500 + '";const k="AKIAIOSFODNN7EXAMPLE";trail'
    found = ids(secrets.scan_text(text, "bundle.js"))
    sample = found["secret-aws-key"]["evidence"]["items"][0]["sample"]
    assert "REDACTED" in sample and FAKE_AWS not in sample
    assert '"head:' not in sample  # centered on the match, not on the line start
    assert found["secret-aws-key"]["evidence"]["items"][0]["line"] == 1
    multi = 'line one\nline two\nkey = "ghp_fixturetoken0123456789abcdef"\nline four'
    item = ids(secrets.scan_text(multi, "m.js"))["secret-github-token"]["evidence"]["items"][0]
    assert item["line"] == 3


def test_third_party_scripts_include_journal_loads():
    named = secrets.third_party_scripts([{"src": "https://cdn.test/a.js"}],
                                        "http://127.0.0.1:9/", None,
                                        ["http://127.0.0.1:9/app.js",
                                         "https://tags.test/t.js",
                                         "https://cdn.test/a.js"])
    assert named == ["https://cdn.test/a.js", "https://tags.test/t.js"]


def test_code_endpoints_ignore_bare_identifiers():
    assert secrets.code_endpoints("const c = capitalize(x); const m = mediapipe_face();",
                                  "a.js", {"h.test"}) == []
    got = secrets.code_endpoints('fetch("/api/health");', "a.js", {"h.test"})
    assert [(item["method"], item["path"]) for item in got] == [("GET", "/api/health")]


def test_entropy_ignores_minified_code_fragments():
    code = "var a=';do t+=function(e){switch(e.tag){case 26:return eX(eT1pe);}}';"
    assert secrets.scan_text(code, "min.js") == []


def test_macro_ending_on_screenshot_reads_back(tmp_path):
    store = tmp_path / ".web-search-neo" / "macros"
    store.mkdir(parents=True)
    shot = store / "shot.json"
    shot.write_text(json.dumps({"steps": [
        {"action": "open", "url": "https://h.test/"},
        {"action": "screenshot", "session_id": "s"}]}), encoding="utf-8")
    result = main._validate_macro_file("shot", str(tmp_path))
    assert result["valid"] is True
    assert not [w for w in result["warnings"] if "never reads the result back" in w["error"]]
    clicky = store / "clicky.json"
    clicky.write_text(json.dumps({"steps": [
        {"action": "open", "url": "https://h.test/"},
        {"action": "click", "selector": "#x", "session_id": "s"}]}), encoding="utf-8")
    result = main._validate_macro_file("clicky", str(tmp_path))
    assert any("never reads the result back" in w["error"] for w in result["warnings"])
    bom = store / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps({"steps": [
        {"action": "open", "url": "https://h.test/"}]}).encode("utf-8"))
    assert main._validate_macro_file("bom", str(tmp_path))["valid"] is True


def test_only_errors_skips_aborted_prefetches():
    from web_search_neo import diagnostics

    rows = [
        {"id": "1", "method": "GET", "url": "http://h/a", "type": "Fetch",
         "status": 200, "failed": True, "error": "net::ERR_ABORTED"},
        {"id": "2", "method": "GET", "url": "http://h/b", "type": "XHR",
         "status": 500},
        {"id": "3", "method": "GET", "url": "http://h/c", "type": "Script",
         "status": None, "failed": True, "error": "net::ERR_BLOCKED_BY_CLIENT"},
    ]
    assert [row["id"] for row in diagnostics.filter_network(rows, only_errors=True)] == ["2", "3"]


def test_page_text_names_a_hidden_majority():
    from web_search_neo import page_perception

    missing, reasons = page_perception._text_exclusions(
        {"body_chars": 1000}, "x" * 100, "main", False, False, {})
    assert missing == 900 and any("aria-hidden" in reason for reason in reasons)
    missing, reasons = page_perception._text_exclusions(
        {"body_chars": 1000}, "x" * 900, "main", False, False, {})
    assert not [reason for reason in reasons if "cookie banner" in reason]


def test_perf_resource_types_are_initiators():
    raw = {"url": "https://h.test/", "ready_state": "complete", "lcp_supported": False,
           "navigation": {}, "resources": [
               {"url": "https://h.test/banner.png", "type": "css", "transfer": 400_000},
               {"url": "https://h.test/app.js", "type": "script", "transfer": 1000}],
           "resources_total": 2}
    shaped = perf.shape(raw)
    assert shaped["resources"]["by_initiator"]["css"]["count"] == 1
    assert shaped["resources"]["largest"][0]["initiator"] == "css"
    assert "by_type" not in shaped["resources"]
    assert all("type" not in item for item in shaped["resources"]["largest"])


# --- Chrome-backed ---------------------------------------------------------------------------------

def _skip_without_chrome(result):
    if not result["success"] and "WebDriver" in str(result["results"][0].get("error")):
        pytest.skip(f"Chrome/Selenium is unavailable: {result['results'][0]['error']}")


def test_secret_scan_reads_a_cold_isolated_load(secret_site):
    import asyncio
    result = asyncio.run(main.web_action([{"action": "secret_scan", "url": secret_site.base_url + "/",
                                           "wait_seconds": 3}]))
    _skip_without_chrome(result)
    assert result["success"], result
    data = result["results"][0]["data"]
    assert [item["url"] for item in data["scripts"]] == [secret_site.base_url + "/static/app.js"]
    paths = {(entry["method"], entry["path"]) for entry in data["code_endpoints"]}
    assert ("GET", "/api/users/{id}") in paths and ("POST", "/api/login") in paths
    assert data["openapi"] == [{"url": secret_site.base_url + "/openapi.json",
                                "version": "3.0.0", "paths": 2}]
    assert data["auth_forms"] == [{"action": secret_site.base_url + "/login", "method": "post",
                                   "https": False, "has_password": True}]
    assert data["third_party_scripts"] == [secret_site.third_party + "/static/vendor.js"]
    found = ids(data["findings"])
    assert found["secret-auth-http"]["severity"] == "high"
    assert found["secret-aws-key"]["status"] == "fail" and found["secret-github-token"]["status"] == "fail"
    assert found["secret-openapi-exposed"]["status"] == "warn"
    assert data["sourcemaps"] == [{"url": secret_site.base_url + "/static/app.js.map",
                                   "sources": 2, "names": ["src/app.ts", "src/secret.ts"],
                                   "names_omitted": 0, "has_content": True}]
    assert found["secret-sourcemap-sources"]["severity"] == "high"
    assert data["requests_made"] == [f"GET {secret_site.base_url}/",
                                     f"GET {secret_site.base_url}/static/app.js",
                                     f"GET {secret_site.base_url}/openapi.json",
                                     f"GET {secret_site.base_url}/static/app.js.map"]
    assert data["fresh_isolated_load"] is True and data["session_id"] is None
    assert not browser_tools._sessions
    dump = json.dumps(data)
    for secret in (FAKE_AWS, FAKE_GITHUB, FAKE_SESSION, "10.0.0.5",
                   "console.log(1)", "const fixtureSource = 1"):
        assert secret not in dump


def test_secret_scan_sarif_baseline_and_min_summary(secret_site, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    first = asyncio.run(main.web_action([{"action": "secret_scan", "url": secret_site.base_url + "/",
                                          "wait_seconds": 3, "save_to": "secret.json",
                                          "sarif_to": "secret.sarif", "overwrite": True}]))
    _skip_without_chrome(first)
    assert first["success"], first
    data = first["results"][0]["data"]
    assert Path(data["saved_to"]).is_file()
    sarif_doc = json.loads(Path(data["sarif_saved_to"]).read_text(encoding="utf-8"))
    assert sarif_doc["version"] == "2.1.0"
    assert {rule["id"] for rule in sarif_doc["runs"][0]["tool"]["driver"]["rules"]} == {
        item["id"] for item in data["findings"]}
    again = asyncio.run(main.web_action([{"action": "secret_scan", "url": secret_site.base_url + "/",
                                          "wait_seconds": 3, "baseline": "secret.json"}]))
    _skip_without_chrome(again)
    assert again["results"][0]["data"]["regression"]["summary"].startswith("No change")
    mini = asyncio.run(main.web_action([{"action": "secret_scan", "url": secret_site.base_url + "/",
                                         "wait_seconds": 3}], summary="min"))
    _skip_without_chrome(mini)
    mdata = mini["results"][0]["data"]
    assert mdata["summary_mode"] == "min" and mdata["counts"] and mdata["summary_line"]
    assert len(mdata["priority"]) <= 5 and "findings" in mdata["summary_omitted"]


@pytest.fixture(scope="module")
def click_site():
    import api_fixture_site

    site = api_fixture_site.start()
    try:
        yield site
    finally:
        site.stop()


def test_click_hints_trusted_when_a_link_does_not_navigate(click_site):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(click_site.base_url + "/clickme", session_id="click-hint",
                                headless=True, profile_mode="temporary")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    try:
        held = asyncio.run(main.web_action([{"action": "click", "selector": "#nope",
                                             "session_id": "click-hint"}]))
        assert held["success"], held
        data = held["results"][0]["data"]
        assert data["success"] is True and data["verified"] is False
        assert "trusted=true" in data.get("navigation_hint", "")
        went = asyncio.run(main.web_action([{"action": "click", "selector": "#go",
                                             "session_id": "click-hint"}]))
        assert went["success"], went
        gdata = went["results"][0]["data"]
        assert gdata["verified"] is True and "navigation_hint" not in gdata
    finally:
        browser_tools.close_session("click-hint")
    assert "click-hint" not in browser_tools._sessions


def test_secret_scan_reads_an_open_session(secret_site):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(secret_site.base_url + "/", session_id="secret-open",
                                headless=True, profile_mode="temporary")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    try:
        time.sleep(3)
        result = asyncio.run(audit_actions.browser_secret_scan(session_id="secret-open"))
        assert result["success"] and result["fresh_isolated_load"] is False
        assert result["code_endpoints"] and "secret-open" in browser_tools._sessions
    finally:
        browser_tools.close_session("secret-open")
    assert "secret-open" not in browser_tools._sessions
