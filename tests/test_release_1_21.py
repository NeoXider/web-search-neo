"""1.21: api_report - the map of the page's backend calls and what each response says.

Pure analysis tests pin the endpoint map, the redaction (values never leak)
and every check family case by case; the fixture server (``api_fixture_site``)
pins the advertised routes over plain HTTP; Chrome-backed tests read a real
isolated load and skip when Chrome is missing. No test reaches the internet.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import api_fixture_site
from web_search_neo import audit_actions, browser_tools, diagnostics, main
from web_search_neo.audit import api, api_checks, api_parts
from web_search_neo.contract import notes, param_docs, playbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ids(findings):
    return {item["id"]: item for item in findings}


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _row(url, *, method="GET", status=200, mime="application/json",
         rtype="fetch", headers=None, row_id="r1", post_data=None):
    row: dict = {"id": row_id, "url": url, "method": method, "status": status,
                 "mime": mime, "type": rtype, "headers": dict(headers or {})}
    if post_data is not None:
        row["post_data"] = post_data
    return row


# --- endpoint map and channels --------------------------------------------------

def test_path_template_masks_id_segments():
    assert api_parts.path_template("https://h.test/api/users/123") == "/api/users/{id}"
    assert api_parts.path_template("https://h.test/api/9f8e7d6c-5b4a-4c3d-8e2f-0123456789ab") == "/api/{id}"
    assert api_parts.path_template("https://h.test/f/0123456789abcdef?a=1") == "/f/{id}"
    assert api_parts.path_template("https://h.test/api/users") == "/api/users"
    assert api_parts.path_template("https://h.test/a/1/2") == "/a/{id}"  # neighbours collapse


def test_channel_by_resource_type():
    assert api_parts.channel({"url": "https://h.test/a", "type": "xhr"}) == "xhr"
    assert api_parts.channel({"url": "https://h.test/a", "type": "fetch"}) == "fetch"
    assert api_parts.channel({"url": "https://h.test/a", "type": "eventsource"}) == "sse"
    assert api_parts.channel({"url": "https://h.test/a", "type": "ping"}) == "beacon"
    assert api_parts.channel({"url": "wss://h.test/s", "type": "websocket"}) == "websocket"
    assert api_parts.channel({"url": "https://h.test/e", "mime": "text/event-stream"}) == "sse"
    assert api_parts.channel({"url": "https://h.test/", "type": "document"}) is None
    assert api_parts.channel({"url": "https://h.test/i.png", "type": "image"}) is None


def test_endpoint_map_groups_counts_and_orders():
    rows = [
        _row("https://h.test/api/users/1", row_id="a"),
        _row("https://h.test/api/users/2", row_id="b"),
        _row("https://h.test/api/login", method="POST", row_id="c", post_data="{}"),
        _row("https://h.test/", rtype="document", row_id="d"),  # not an API call
    ]
    groups = {g["path"]: g for g in api_parts.endpoint_map(rows)}
    assert set(groups) == {"/api/users/{id}", "/api/login"}
    assert groups["/api/users/{id}"]["count"] == 2
    assert groups["/api/login"]["channels"] == {"fetch": 1}
    ordered = api_parts.endpoint_map(rows)
    assert [g["path"] for g in ordered] == ["/api/users/{id}", "/api/login"]  # frequency first


def test_endpoints_omitted_past_the_limit():
    rows = [_row(f"https://h.test/api/r{i}", row_id=f"r{i}",
                 headers={"cache-control": "no-store"}) for i in range(61)]
    report = api.build(rows, page_url="https://h.test/", jar=[], storage={}, bodies={})
    assert len(report["endpoints"]) == api_parts.ENDPOINT_LIMIT == 60
    assert report["endpoints_omitted"] == 1


# --- redaction ------------------------------------------------------------------

def test_safe_url_masks_secrets_strips_userinfo_and_fragment():
    url = "https://us3r:s3cr3t@h.test/api/search?token=abc&sid=S3CR3T-SID&email=qa@example.test&ok=1#frag"
    out = api_parts.safe_url(url)
    assert "us3r" not in out and "s3cr3t" not in out and "#frag" not in out
    assert "token=REDACTED" in out and "sid=REDACTED" in out
    assert "S3CR3T-SID" not in out and "qa@example.test" not in out and "ok=1" in out


def test_mask_text_redacts_email_and_jwt():
    token = f"{_b64({'alg': 'HS256'})}.{_b64({'sub': '1'})}.c2ln"
    text = api_parts.mask_text(f"contact qa@example.test with {token} please")
    assert "qa@example.test" not in text and token not in text


def test_jwt_facts_without_the_value():
    header, payload = _b64({"alg": "HS256"}), _b64({"exp": 2000000000, "iat": 1000000000})
    facts = api_parts.jwt_facts(f"{header}.{payload}.c2ln")
    assert facts == {"alg": "HS256", "exp": 2000000000, "iat": 1000000000, "signed": True}
    unsigned = api_parts.jwt_facts(f"{_b64({'alg': 'none'})}.{payload}.")
    assert unsigned["alg"] == "none" and unsigned["signed"] is False
    assert api_parts.jwt_facts("not-a-token") is None


def test_cookie_and_storage_views_never_carry_values():
    token = f"{_b64({'alg': 'HS256'})}.{_b64({'sub': '1'})}.c2ln"
    jar = [
        {"name": "sid", "domain": "127.0.0.1", "value": "cookie-secret-value",
         "secure": False, "httpOnly": False, "sameSite": None},
        {"name": "jwt", "domain": "127.0.0.1", "value": token,
         "secure": True, "httpOnly": True, "sameSite": "Lax"},
        {"name": "other", "domain": "evil.test", "value": "x",
         "secure": False, "httpOnly": False, "sameSite": None},
    ]
    own = api_parts.own_sites("http://127.0.0.1:9/")
    views = api_parts.cookie_views(jar, own)
    assert {v["name"] for v in views} == {"sid", "jwt"}  # foreign domains skipped
    assert all("value" not in v for v in views)
    assert ids([{"id": v["name"], **v} for v in views])["jwt"]["format"] == "jwt"
    dump = json.dumps(views)
    assert "cookie-secret-value" not in dump and token not in dump
    local, session, error = api_parts.storage_views(
        {"local_storage": [{"name": "auth_token", "format": "jwt",
                            "jwt": {"alg": "HS256", "exp": 1, "iat": 1, "signed": True}}],
         "session_storage": [{"name": "prefs", "format": "json"}]})
    assert local[0]["name"] == "auth_token" and session[0]["format"] == "json" and error is None
    assert api_parts.storage_views({"storage_error": "boom"}) == ([], [], "boom")


# --- each check family -----------------------------------------------------------

def _api_url(path):
    return f"https://h.test{path}"


def test_cors_cases():
    wild = [_row(_api_url("/a"), headers={"access-control-allow-origin": "*",
                                          "access-control-allow-credentials": "true"})]
    found = ids(api_checks.cors_findings(wild))
    assert found["api-cors-wildcard-credentials"]["status"] == "fail"
    assert found["api-cors-wildcard-credentials"]["severity"] == "medium"
    null = [_row(_api_url("/a"), headers={"access-control-allow-origin": "null"})]
    assert ids(api_checks.cors_findings(null))["api-cors-null-origin"]["severity"] == "high"
    star = [_row(_api_url("/a"), headers={"access-control-allow-origin": "*"})]
    assert ids(api_checks.cors_findings(star))["api-cors-wildcard"]["status"] == "warn"
    named = [_row(_api_url("/a"), headers={"access-control-allow-origin": "https://h.test"})]
    assert ids(api_checks.cors_findings(named))["api-cors-allowed-origin"]["status"] == "info"
    assert ids(api_checks.cors_findings([_row(_api_url("/a"), headers={})]))["api-cors-none"]["status"] == "pass"


def test_cache_cases():
    both = [_row(_api_url("/s"), headers={"cache-control": "public, max-age=60",
                                          "set-cookie": "sid=x; Path=/"})]
    assert ids(api_checks.cache_findings(both))["api-cache-public-user-data"]["severity"] == "high"
    public = [_row(_api_url("/s"), headers={"cache-control": "public"})]
    assert ids(api_checks.cache_findings(public))["api-cache-public"]["status"] == "warn"
    missing = [_row(_api_url("/s"), headers={"content-type": "application/json"})]
    assert ids(api_checks.cache_findings(missing))["api-cache-missing"]["status"] == "warn"
    private = [_row(_api_url("/s"), headers={"cache-control": "no-store"})]
    assert ids(api_checks.cache_findings(private))["api-cache-private"]["status"] == "pass"


def test_content_cases():
    bare = [_row(_api_url("/s"), headers={"content-type": "application/json"})]
    assert ids(api_checks.content_findings(bare))["api-json-nosniff"]["status"] == "warn"
    untyped = [_row(_api_url("/s"), headers={"x-content-type-options": "nosniff"})]
    # No Content-Type header at all: the mime still says JSON.
    assert ids(api_checks.content_findings(untyped))["api-json-content-type"]["status"] == "warn"
    clean = [_row(_api_url("/s"), headers={"content-type": "application/json",
                                           "x-content-type-options": "nosniff"})]
    assert ids(api_checks.content_findings(clean))["api-json-headers"]["status"] == "pass"


def test_error_body_cases():
    body = ("Traceback (most recent call last):\n  File \"/var/www/app/views.py\", line 1\n"
            "admin@example.test reached it. Werkzeug/2.3.4 answers.")
    rows = [_row(_api_url("/crash"), status=500, mime="text/plain", row_id="e1")]
    found = ids(api_checks.error_findings(rows, {"e1": body}, 0))
    assert found["api-error-stack-trace"]["severity"] == "high"
    assert found["api-error-server-path"]["status"] == "fail"
    assert found["api-error-framework-version"]["status"] == "warn"
    dump = json.dumps(found)
    assert "admin@example.test" not in dump  # snippets are masked
    assert "/var/www/app/views.py" in dump  # ...but paths stay as evidence
    missing = ids(api_checks.error_findings(rows, {}, 1))
    assert missing["api-error-body-unread"]["evidence"]["failed_reads"] == 1
    clean = ids(api_checks.error_findings(rows, {"e1": "oops"}, 0))
    assert clean["api-error-body-clean"]["status"] == "pass"


def test_transport_cases():
    ws = [_row("ws://h.test/socket", rtype="websocket")]
    assert ids(api_checks.transport_findings(ws, "https://h.test/"))["api-ws-cleartext"]["severity"] == "high"
    assert ids(api_checks.transport_findings(ws, "http://h.test/"))["api-ws-cleartext"]["status"] == "warn"
    plain = [_row("http://h.test/api", rtype="fetch")]
    assert ids(api_checks.transport_findings(plain, "https://h.test/"))["api-http-from-https"]["status"] == "fail"
    foreign = [_row("http://localhost:9/api", rtype="fetch")]
    assert ids(api_checks.transport_findings(foreign, "http://127.0.0.1:9/"))["api-third-party-api"]["status"] == "warn"


def _cookie_view(name, *, fmt="opaque", secure=False, httponly=False, same_site=None, jwt=None):
    view: dict = {"name": name, "domain": "127.0.0.1", "secure": secure, "httponly": httponly,
                  "sameSite": same_site, "format": fmt}
    if jwt is not None:
        view["jwt"] = jwt
    return view


def test_auth_token_locations_and_jwt():
    found = ids(api_checks.auth_findings(
        [_cookie_view("sid")],
        [{"name": "auth_token", "format": "opaque"}],
        [{"name": "session_id", "format": "opaque"}], False, None))
    assert found["api-token-in-localstorage"]["severity"] == "medium"
    assert found["api-token-in-sessionstorage"]["severity"] == "low"
    assert found["api-token-cookie-not-httponly"]["status"] == "warn"
    assert "api-jwt-unsigned" not in found
    unsigned = [_cookie_view("t", fmt="jwt", secure=True, httponly=True, same_site="Lax",
                             jwt={"alg": "none", "exp": None, "iat": None, "signed": False})]
    found = ids(api_checks.auth_findings(unsigned, [], [], True, None))
    assert found["api-jwt-unsigned"]["severity"] == "high"
    assert found["api-token-cookie-httponly"]["status"] == "pass"
    assert ids(api_checks.auth_findings([], [], [], False, "boom"))["api-storage-unavailable"]["status"] == "info"


def test_csrf_samesite_and_tokens():
    # A cookie with no SameSite warns once - it must not also fail as SameSite=None.
    bare = [_cookie_view("sid")]
    found = ids(api_checks.csrf_findings([], bare, "http://127.0.0.1:9/"))
    assert found["api-csrf-samesite-missing"]["status"] == "warn"
    assert "api-csrf-samesite-none-insecure" not in found
    lax = [_cookie_view("sid", secure=True, same_site="Lax")]
    assert ids(api_checks.csrf_findings([], lax, "http://127.0.0.1:9/"))["api-csrf-samesite"]["status"] == "pass"
    none_plain = [_cookie_view("sid", same_site="None")]
    assert ids(api_checks.csrf_findings([], none_plain,
                                        "http://127.0.0.1:9/"))["api-csrf-samesite-none-insecure"]["status"] == "fail"
    with_token = [_row("http://127.0.0.1:9/api/login", method="POST", row_id="p1",
                       post_data='{"login": "qa", "csrf_token": "x"}')]
    assert ids(api_checks.csrf_findings(with_token, lax,
                                        "http://127.0.0.1:9/"))["api-csrf-token-in-request"]["status"] == "pass"
    bare_post = [_row("http://127.0.0.1:9/api/save", method="POST", row_id="p2",
                      post_data='{"login": "qa"}')]
    assert ids(api_checks.csrf_findings(bare_post, lax,
                                        "http://127.0.0.1:9/"))["api-csrf-token-unseen"]["status"] == "info"
    cross_post = [_row("http://localhost:9/api/save", method="POST", row_id="p3",
                       post_data='{"login": "qa"}')]
    assert ids(api_checks.csrf_findings(cross_post, lax,
                                        "http://127.0.0.1:9/"))["api-csrf-cross-origin-write"]["status"] == "warn"


def test_sensitive_query_params_masked():
    rows = [_row("http://127.0.0.1:9/api/search?token=abc&sid=S3CR3T-SID&email=qa@example.test&ok=1")]
    found = ids(api_checks.url_findings(rows))
    item = found["api-sensitive-query"]
    assert item["status"] == "warn" and item["severity"] == "medium"
    assert item["evidence"]["items"][0]["params"] == ["email", "sid", "token"]
    dump = json.dumps(found)
    assert "S3CR3T-SID" not in dump and "qa@example.test" not in dump
    assert ids(api_checks.url_findings([_row("http://127.0.0.1:9/api?ok=1")])) == {}


# --- the assembled report ---------------------------------------------------------

def test_build_sends_nothing_and_shapes_the_answer():
    page = "http://us3r:s3cr3t@127.0.0.1:9/"
    rows = [
        _row(page + "api/users/7", headers={"content-type": "application/json", "cache-control": "no-store",
                                            "x-content-type-options": "nosniff"}),
        _row(page + "api/login", method="POST", row_id="p1", post_data='{"csrf_token": "x"}',
             headers={"content-type": "application/json", "cache-control": "no-store",
                      "x-content-type-options": "nosniff"}),
    ]
    report = api.build(rows, page_url=page, jar=[], storage={"local_storage": [], "session_storage": []},
                       bodies={}, unread=0, hosts=None, dropped=0)
    assert report["success"] and report["requests_made"] == []
    assert "us3r" not in report["url"] and "s3cr3t" not in report["url"]
    assert [(e["method"], e["path"]) for e in report["endpoints"]] == [("POST", "/api/login"),
                                                                      ("GET", "/api/users/{id}")]
    assert report["api_calls"] == 2 and report["requests_observed"] == 2
    assert report["priority"] == []  # a clean page has no fixes
    assert report["counts"]["high"] == report["counts"]["medium"] == report["counts"]["low"] == 0
    assert report["summary_line"].endswith("nothing to fix.")
    assert report["scope"].startswith("api_report is passive")


def test_build_names_responses_without_recorded_headers():
    rows = [_row("http://127.0.0.1:9/api/users/7")]
    del rows[0]["headers"]
    report = api.build(rows, page_url="http://127.0.0.1:9/", jar=[], storage={}, bodies={})
    assert "api-headers-unavailable" in ids(report["findings"])


# --- wrapper validation (no browser) ------------------------------------------------

def test_api_report_needs_url_or_session():
    with pytest.raises(ValueError, match="needs url"):
        asyncio.run(audit_actions.browser_api_report())


def test_api_report_validates_hosts_before_opening_a_browser():
    with pytest.raises(ValueError, match="wildcard"):
        asyncio.run(audit_actions.browser_api_report(url="http://127.0.0.1:9/", hosts=["*.example.com"]))
    assert not browser_tools._sessions


# --- contract and release pins -------------------------------------------------------

def test_api_report_in_the_action_table_and_the_contract():
    specs = {name: (wrapper, group) for name, wrapper, group, _summary in audit_actions.ACTION_SPECS}
    assert specs["api_report"][0] is audit_actions.browser_api_report
    assert specs["api_report"][1] == "audit"
    assert "api_report" in main._ACTIONS
    assert "api_report" in param_docs._BY_ACTION and "api_report" in notes._ACTION_NOTES
    assert "api_report" in Path(playbook.__file__).read_text(encoding="utf-8")
    assert len(json.dumps(main._capabilities())) <= 13_500


def test_security_headers_carry_the_api_set():
    assert {"cache-control", "pragma", "content-type", "access-control-allow-methods",
            "access-control-allow-headers", "access-control-allow-credentials",
            "access-control-max-age"} <= set(diagnostics.SECURITY_HEADERS)


def test_version_pins_say_1_21():
    assert main.__version__ == "1.21.0"
    import web_search_neo
    assert web_search_neo.__version__ == "1.21.0"
    assert 'version = "1.21.0"' in (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"version": "1.21.0"' in (PROJECT_ROOT / "chrome-extension" / "manifest.json").read_text(
        encoding="utf-8")
    popup = (PROJECT_ROOT / "chrome-extension" / "popup.js").read_text(encoding="utf-8")
    assert popup.count('"1.21.0"') == 2 and '"1.20.0"' not in popup
    install = (PROJECT_ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert "1.20.0" not in install and install.count("1.21.0") == 3


# --- against the local fixture server (no Chrome) -------------------------------------

@pytest.fixture(scope="module")
def api_site():
    site = api_fixture_site.start()
    try:
        yield site
    finally:
        site.stop()


def _http(site, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(site.base_url + path, timeout=10) as resp:
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def test_fixture_routes_carry_the_advertised_headers(api_site):
    status, headers, _ = _http(api_site, "/api/users/123")
    assert status == 200 and headers["access-control-allow-origin"] == "*"
    assert headers["access-control-allow-credentials"] == "true"
    status, headers, _ = _http(api_site, "/api/session")
    assert status == 200 and "set-cookie" in headers and "public" in headers["cache-control"]
    status, headers, _ = _http(api_site, "/api/profile")
    assert status == 200 and "x-content-type-options" not in headers
    assert "json" in headers["content-type"]
    status, _headers, body = _http(api_site, "/api/crash")
    assert status == 500 and b"Traceback" in body


def test_fixture_responses_drive_the_checks(api_site):
    rows = []
    for path, mime in (("/api/users/123", "application/json"), ("/api/session", "application/json"),
                       ("/api/profile", "application/json")):
        status, headers, _ = _http(api_site, path)
        rows.append(_row(api_site.base_url + path, status=status, mime=mime, headers=headers, row_id=path))
    found = ids(api_checks.cors_findings(rows) + api_checks.cache_findings(rows)
                + api_checks.content_findings(rows))
    assert found["api-cors-wildcard-credentials"]["status"] == "fail"
    assert found["api-cache-public-user-data"]["severity"] == "high"
    assert found["api-json-nosniff"]["status"] == "warn"


# --- Chrome-backed ---------------------------------------------------------------------

def _skip_without_chrome(result):
    if not result["success"] and "WebDriver" in str(result["results"][0].get("error")):
        pytest.skip(f"Chrome/Selenium is unavailable: {result['results'][0]['error']}")


def test_api_report_maps_a_cold_isolated_load(api_site):
    result = asyncio.run(main.web_action([{"action": "api_report", "url": api_site.base_url + "/",
                                           "wait_seconds": 3}]))
    _skip_without_chrome(result)
    assert result["success"], result
    data = result["results"][0]["data"]
    paths = {(entry["method"], entry["path"]) for entry in data["endpoints"]}
    assert ("GET", "/api/users/{id}") in paths and ("POST", "/api/login") in paths
    assert data["requests_made"] == [] and data["fresh_isolated_load"] is True
    assert data["session_id"] is None and not browser_tools._sessions
    found = ids(data["findings"])
    assert {"api-cors-wildcard-credentials", "api-error-stack-trace",
            "api-sensitive-query", "api-third-party-api"} <= set(found)
    assert data["counts"]["high"] >= 1
    dump = json.dumps(data)
    for secret in ("fixture-secret-token-value", "fixture-sid-value", "ABCDEF123456", "qa@example.test"):
        assert secret not in dump


def test_api_report_saves_json_har_and_min_summary(api_site, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    result = asyncio.run(main.web_action([{"action": "api_report", "url": api_site.base_url + "/",
                                           "wait_seconds": 3, "save_to": "api.json",
                                           "har_to": "api.har", "overwrite": True}]))
    _skip_without_chrome(result)
    assert result["success"], result
    data = result["results"][0]["data"]
    assert Path(data["saved_to"]).is_file()
    log = json.loads(Path(data["har_saved_to"]).read_text(encoding="utf-8"))["log"]
    assert log["version"] == "1.2" and log["creator"]["name"] == "Web Search Neo"
    urls = [entry["request"]["url"] for entry in log["entries"]]
    assert api_site.base_url + "/api/users/123" in urls
    mini = asyncio.run(main.web_action([{"action": "api_report", "url": api_site.base_url + "/",
                                         "wait_seconds": 3}], summary="min"))
    _skip_without_chrome(mini)
    mdata = mini["results"][0]["data"]
    assert mdata["summary_mode"] == "min" and mdata["counts"] and mdata["summary_line"]
    assert len(mdata["priority"]) <= 5 and "findings" in mdata["summary_omitted"]


def test_api_report_reads_an_open_session(api_site):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(api_site.base_url + "/", session_id="api-open",
                                headless=True, profile_mode="temporary")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    try:
        time.sleep(3)
        result = asyncio.run(audit_actions.browser_api_report(session_id="api-open"))
        assert result["success"] and result["fresh_isolated_load"] is False
        assert result["endpoints"] and "api-open" in browser_tools._sessions
    finally:
        browser_tools.close_session("api-open")
    assert "api-open" not in browser_tools._sessions
