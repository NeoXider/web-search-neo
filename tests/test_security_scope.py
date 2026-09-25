"""security_report scope: page, site crawl, hosts, paths, and the local development mode.

Every server here is a local ``http.server`` (``scope_fixture_site``) on
127.0.0.1; a second server on another port stands for "another site" and must
never see a request. No test reaches the internet or opens a browser.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import scope_fixture_site as sf
from web_search_neo import main
from web_search_neo.audit import site
from web_search_neo.audit.scope import MAX_HOSTS, Budget, Scope, ScopeError, parse_target


def ids(findings):
    return {item["id"]: item for item in findings}


# --- validating the scope before anything is sent --------------------------------------------

@pytest.mark.parametrize("entry", [
    "*.example.com", "example.*", "com", "co.uk", "github.io", "intranet", "https://example.com/path",
    "https://user:pw@example.com", "ftp://example.com", "", "exa mple.com", "example.com:99999",
])
def test_refused_scope_entries(entry):
    with pytest.raises(ScopeError):
        parse_target(entry)


@pytest.mark.parametrize("entry, host, port, scheme", [
    ("example.com", "example.com", None, None), ("API.Example.com.", "api.example.com", None, None),
    ("localhost:8080", "localhost", 8080, None), ("http://localhost:3000", "localhost", 3000, "http"),
    ("127.0.0.1", "127.0.0.1", None, None), ("[::1]:8443", "::1", 8443, None), ("10.0.0.5:81", "10.0.0.5", 81, None),
    ("https://shop.example.co.uk", "shop.example.co.uk", None, "https"),
])
def test_accepted_scope_entries(entry, host, port, scheme):
    target = parse_target(entry)
    assert (target.host, target.port, target.scheme) == (host, port, scheme)


def test_at_most_ten_hosts():
    Scope.build([f"h{i}.example.com" for i in range(MAX_HOSTS)])
    with pytest.raises(ScopeError, match="at most"):
        Scope.build([f"h{i}.example.com" for i in range(MAX_HOSTS + 1)])


def test_scope_matching_ports_and_subdomains():
    scope = Scope.build(["example.com", "localhost:3000"])
    assert scope.allows("https://example.com/x") and scope.allows("http://example.com:80/")
    assert not scope.allows("https://example.com:8443/")          # another port is another service
    assert not scope.allows("https://cdn.example.com/")           # no subdomains unless asked
    assert scope.allows("http://localhost:3000/a") and not scope.allows("http://localhost:3001/")
    assert not scope.allows("http://localhost/")
    wide = Scope.build(["example.com"], include_subdomains=True)
    assert wide.allows("https://cdn.example.com/") and not wide.allows("https://example.com.evil.test/")
    assert not wide.allows("javascript:alert(1)") and not wide.allows("file:///etc/passwd")


@pytest.mark.parametrize("paths", [["x"], ["//evil.test/x"], ["/a/../b"], ["http://evil.test/"], ["/ a"],
                                   ["/é"], [f"/p{i}" for i in range(51)], "/a"])
def test_refused_paths(paths):
    with pytest.raises(ScopeError):
        site.validate_paths(paths)


def test_paths_are_deduplicated():
    assert site.validate_paths(["/a", "/a", "/b?x=1"]) == ["/a", "/b?x=1"]


def test_the_budget_refuses_past_its_limit():
    budget = Budget(limit=2)
    assert budget.take("a") and budget.take("b") and not budget.take("c")
    assert budget.made == ["a", "b"] and budget.refused == 1


def test_a_bad_scope_is_refused_before_any_request():
    external = sf.RouteSite({"/": (200, [], sf.page())}).start()
    try:
        with pytest.raises(ScopeError):
            site.run(external.base_url + "/", mode="hosts", hosts=["*.example.com"])
        with pytest.raises(ScopeError):
            site.run(external.base_url + "/", paths=["../etc/passwd"])
        assert external.seen == []
    finally:
        external.stop()


# --- site crawl ----------------------------------------------------------------------------

@pytest.fixture()
def sites():
    external = sf.RouteSite({"/": (200, [], sf.page())}).start()
    routes = {
        "/": (200, sf.HARDENED, sf.page(["/a", "/b", "/private/secret", "/a#top", "mailto:x@example.test",
                                         external.base_url + "/"])),
        "/a": (200, sf.HARDENED, sf.page(["/c"])),
        "/b": (200, [], sf.page()),
        "/c": (200, sf.HARDENED, sf.page(["/d"])),
        "/d": (200, sf.HARDENED, sf.page()),
        "/from-sitemap": (200, sf.HARDENED, sf.page()),
        "/listed": (200, sf.HARDENED, sf.page()),
        "/robots.txt": (200, [("Content-Type", "text/plain")],
                        "User-agent: *\nDisallow: /private\nSitemap: {base}/sitemap.xml\n"),
        "/sitemap.xml": (200, [("Content-Type", "application/xml")],
                         "<urlset><url><loc>{base}/from-sitemap</loc></url>"
                         "<url><loc>https://elsewhere.example/x</loc></url></urlset>"),
    }
    own = sf.RouteSite(routes).start()
    try:
        yield own, external
    finally:
        own.stop()
        external.stop()


def test_site_crawl_stays_in_scope_and_honours_robots_depth_and_delay(sites):
    own, external = sites
    slept = []
    result = site.run(own.base_url + "/", mode="site", max_pages=10, max_depth=2, delay_ms=500, sleep=slept.append)
    checked = [section["url"] for section in result["sections"]]
    assert checked[0] == own.base_url + "/"
    assert {own.base_url + p for p in ("/a", "/b", "/c", "/from-sitemap")} <= set(checked)
    assert own.base_url + "/d" not in checked                       # three links deep, max_depth 2
    assert "/private/secret" not in own.seen                        # robots.txt Disallow
    assert result["not_crawled"]["robots"] == [own.base_url + "/private/secret"]
    assert external.base_url + "/" in result["not_crawled"]["out_of_scope"]
    assert "https://elsewhere.example/x" in result["not_crawled"]["out_of_scope"]
    assert external.seen == []                                     # never contacted
    assert slept and all(delay == 0.5 for delay in slept) and len(slept) == len(checked) - 1
    assert len(checked) == len(set(checked))                        # /a#top is /a
    # every request the report sent is listed, and nothing else reached the server
    assert sorted(line.split(" ", 2)[1].replace(own.base_url, "") for line in result["requests_made"]) == sorted(own.seen)


def test_site_crawl_aggregates_the_weakest_page_and_dedupes_findings(sites):
    own, _external = sites
    result = site.run(own.base_url + "/", mode="site", delay_ms=0)
    weakest = min(result["sections"], key=lambda s: s["score"])
    assert result["scope_mode"] == "site" and result["score"] == weakest["score"] and weakest["url"].endswith("/b")
    assert result["graded"] == result["checked"] == len(result["sections"])
    by_id = {entry["id"]: entry for entry in result["priority"]}
    assert by_id["csp-missing"]["pages"] == [own.base_url + "/b"] and by_id["csp-missing"]["page_count"] == 1
    assert len(result["priority"]) == len(by_id)                     # one entry per problem, pages listed in it
    assert all(r["mode"] == "local_development" for r in result["reports"])


def test_site_crawl_page_limit(sites):
    own, _external = sites
    result = site.run(own.base_url + "/", mode="site", max_pages=2, delay_ms=0)
    assert result["checked"] == 2 and result["queue_left"] > 0
    assert site.run(own.base_url + "/", mode="site", max_pages=500, delay_ms=0)["checked"] <= site.MAX_PAGES


def test_robots_can_be_ignored_on_request(sites):
    own, _external = sites
    result = site.run(own.base_url + "/", mode="site", respect_robots=False, delay_ms=0, max_depth=1)
    assert own.base_url + "/private/secret" in [s["url"] for s in result["sections"]]


# --- paths and hosts ---------------------------------------------------------------------------

def test_paths_are_the_only_extra_routes_requested(sites):
    own, _external = sites
    result = site.run(own.base_url + "/", paths=["/listed", "/b"])
    assert result["scope_mode"] == "page" and [s["url"] for s in result["sections"]] == [
        own.base_url + "/", own.base_url + "/listed", own.base_url + "/b"]
    assert set(own.seen) == {"/", "/listed", "/b", "/.well-known/security.txt", "/security.txt", "/robots.txt"}


def test_hosts_mode_checks_each_named_origin_in_its_own_section():
    first = sf.RouteSite({"/": (200, sf.HARDENED, sf.page())}).start()
    second = sf.RouteSite({"/": (200, [], sf.page())}).start()
    bystander = sf.RouteSite({"/": (200, [], sf.page())}).start()
    try:
        hosts = [first.base_url.split("//", 1)[1], second.base_url]  # without and with a scheme
        result = site.run(None, mode="hosts", hosts=hosts)
        assert result["scope_mode"] == "hosts" and result["checked"] == 2
        assert [s["url"] for s in result["sections"]] == [first.base_url + "/", second.base_url + "/"]
        assert result["grade"] == min(result["sections"], key=lambda s: s["score"])["grade"]
        assert "/" in first.seen and "/" in second.seen and bystander.seen == []
        # a host named without a scheme is tried over https first; the dev server answers http only
        assert any(line.startswith("GET https://127.0.0.1") for line in result["requests_made"])
    finally:
        for server in (first, second, bystander):
            server.stop()


# --- local development mode ------------------------------------------------------------------

def test_a_local_http_dev_server_is_graded_without_transport_penalties():
    dev = sf.RouteSite({"/": (200, sf.HARDENED + [("Set-Cookie", "sid=1; Path=/; HttpOnly; SameSite=Lax")],
                              sf.page())}).start()
    try:
        built = site.run(dev.base_url + "/")
        found = ids(built["findings"])
        assert built["mode"] == "local_development"
        for fid in ("https-missing", "hsts-no-https", "redirect-missing"):
            if fid in found:
                assert found[fid]["status"] == "skip" and found[fid]["points"] == 0
                assert "Not applicable to a local development server" in found[fid]["detail"]
        assert built["score_explanation"]["penalty_total"] == 0 and built["grade"] == "A+"
        assert built["as_served"]["grade"] != built["grade"]          # the plain-http numbers, kept apart
        forecast = built["production_forecast"]
        assert forecast["grade"] and "https" in forecast["assumes"] and "forecast" in forecast["note"].lower()
        assert "sid=1" not in json.dumps(built) and "Path=/; HttpOnly" not in json.dumps(built)
    finally:
        dev.stop()


def test_a_self_signed_dev_certificate_costs_nothing():
    security_txt = "Contact: mailto:security@example.test\nExpires: 2099-01-01T00:00:00Z\n"
    dev = sf.RouteSite({"/": (200, sf.HARDENED + [("Strict-Transport-Security", "max-age=31536000")],
                              sf.page(["/next"])),
                        "/next": (200, sf.HARDENED, sf.page()),
                        "/.well-known/security.txt": (200, [("Content-Type", "text/plain")], security_txt)},
                       tls=True).start()
    try:
        built = site.run(dev.base_url + "/")
        found = ids(built["findings"])
        assert built["success"] and built["mode"] == "local_development" and built["status"] == 200
        # One verified attempt for the origin; after the certificate error every read is unverified.
        verified = [line for line in built["requests_made"] if line.startswith("GET") and "not verified" not in line]
        assert len(verified) == 1, built["requests_made"]
        assert found["security-txt-ok"]  # the well-known files are read over the same unverified connection
        for fid in ("tls-invalid", "tls-untrusted-page", "hsts-invalid-cert"):
            if fid in found:
                assert found[fid]["status"] == "skip" and found[fid]["points"] == 0
        assert built["score_explanation"]["penalty_total"] == 0
        assert found["csp-default-none"]["points"] == 10              # the headers were read and graded
        crawl = site.run(dev.base_url + "/", mode="site", delay_ms=0)
        assert crawl["checked"] == 2
        verified = [line for line in crawl["requests_made"] if line.startswith("GET") and "not verified" not in line]
        assert len(verified) == 1, crawl["requests_made"]           # the memory spans every page of the crawl
    finally:
        dev.stop()


def test_several_dev_servers_through_web_action():
    api = sf.RouteSite({"/": (200, [("Content-Type", "application/json")], "{}")}).start()
    web = sf.RouteSite({"/": (200, sf.HARDENED, sf.page())}).start()
    try:
        result = asyncio.run(main.web_action([{"action": "security_report", "scope": "hosts", "browser": False,
                                               "hosts": [api.base_url, web.base_url]}]))
        data = result["results"][0]["data"]
        assert result["success"] and data["checked"] == 2 and all(s["mode"] == "local_development"
                                                                    for s in data["sections"])
        site_run = asyncio.run(main.web_action([{"action": "security_report", "scope": "site", "browser": False,
                                                 "url": web.base_url + "/", "delay_ms": 0}], summary="min"))
        assert site_run["success"] and site_run["results"][0]["data"]["summary_mode"] == "min"
    finally:
        api.stop()
        web.stop()


# --- audit round 3: redirects, redaction, per-origin facts, the browser, budget ------------------

def _pretend_public(monkeypatch):
    """Both loopback names behave like public hosts (as example.com and www.example.com would)."""
    from web_search_neo import web_client
    monkeypatch.setattr(web_client, "is_local_host", lambda host: False)
    monkeypatch.setattr(web_client, "classify_destination", lambda url: web_client.DESTINATION_PUBLIC)
    monkeypatch.setattr(web_client, "_plain_http_allowed", lambda: False)


def test_a_plain_http_hop_between_public_hosts_keeps_the_chain(monkeypatch):
    from web_search_neo.audit import report, transport
    server = sf.RouteSite({}).start()
    port = server.port
    server.routes.update({
        "/": (301, [("Location", f"http://localhost:{port}/x")], ""),
        "/x": (301, [("Location", "https://www.elsewhere.example/")], ""),
    })
    try:
        _pretend_public(monkeypatch)
        # localhost is named: the http->http hop is followed, the https target outside is not.
        checker = report.Checker(Scope.build([f"127.0.0.1:{port}", f"localhost:{port}"]), Budget(), 5)
        probe = checker.get(f"http://127.0.0.1:{port}/", small=True)
        assert probe["route"] == [f"http://127.0.0.1:{port}/", f"http://localhost:{port}/x"]
        assert probe["not_followed"] == "https://www.elsewhere.example/"
        found = [f for f in transport.analyze_redirect("https://127.0.0.1/", probe) if f["id"].startswith("redirect-")]
        assert [(f["id"], f["points"]) for f in found] == [("redirect-initial-http", -10)]
        # localhost not named: the client refuses plain http to a public host - the chain is kept anyway.
        checker = report.Checker(Scope.build([f"127.0.0.1:{port}"]), Budget(), 5)
        probe = checker.get(f"http://127.0.0.1:{port}/", small=True)
        assert not probe.get("error") and probe["not_followed"] == f"http://localhost:{port}/x"
        assert probe["not_followed_reason"] == "outside the scope"
        assert server.seen.count("/x") == 1  # only the first probe, where localhost was in scope
    finally:
        server.stop()


def test_credentials_and_tokens_never_leave_in_the_report():
    server = sf.RouteSite({
        "/cb": (302, [("Location", "/done?session=SECRETSESSION")], ""),
        "/done": (200, [], sf.page()),
    }).start()
    try:
        url = f"http://user:hunter2@127.0.0.1:{server.port}/cb?token=SECRETTOKEN&code=abc"
        text = json.dumps(site.run(url, timeout=5)) + json.dumps(site.run(url, paths=["/done?code=SECRETCODE"]))
        for secret in ("hunter2", "SECRETTOKEN", "SECRETSESSION", "SECRETCODE", "user:"):
            assert secret not in text, secret
    finally:
        server.stop()
    # an error message quoting the URL is redacted too
    from web_search_neo.audit.scope import redact_text
    assert "SECRETTOKEN" not in redact_text("Max retries exceeded with url: /cb?token=SECRETTOKEN (Caused by x)")


def test_each_host_of_a_crawl_gets_its_own_origin_facts():
    security_txt = "Contact: mailto:security@example.test\nExpires: 2099-01-01T00:00:00Z\n"
    second = sf.RouteSite({"/": (200, sf.HARDENED, sf.page()),
                           "/.well-known/security.txt": (200, [("Content-Type", "text/plain")], security_txt)}).start()
    first = sf.RouteSite({"/": (200, sf.HARDENED, sf.page([second.base_url + "/"]))}).start()
    try:
        result = site.run(first.base_url + "/", mode="site", hosts=[second.base_url], delay_ms=0)
        by_url = {r["url"]: ids(r["findings"]) for r in result["reports"]}
        assert "security-txt-missing" in by_url[first.base_url + "/"]
        assert "security-txt-ok" in by_url[second.base_url + "/"]         # its own file, not the first host's
        assert "/.well-known/security.txt" in second.seen and "/robots.txt" in second.seen
    finally:
        first.stop()
        second.stop()


def test_a_browser_that_left_the_scope_is_not_analysed():
    from web_search_neo.audit import report
    scope = Scope.build(["example.com"])
    view = {"page": {"status": 200, "final_url": "https://example.com/", "headers": {"content-type": "text/html"},
                     "set_cookies": [], "body": "<p>own</p>"}, "requests_made": [], "scope": scope}
    snapshot = {"url": "https://login.other.test/sso", "scripts": {"items": [], "total": 0}}
    built = report.build("https://example.com/", view, {"snapshot": snapshot, "network": [{"url": "https://example.com/"}]})
    assert built["page_source"] == "static-html" and "outside the scope" in built["browser_note"]
    assert built["browser_requests"]["left_scope"] == "https://login.other.test/sso"


def test_robots_txt_cookies_are_not_graded():
    from web_search_neo.audit import report
    view = {"page": {"status": 200, "final_url": "https://example.com/", "headers": {"content-type": "text/html"},
                     "set_cookies": [], "body": ""},
            "files": {"robots_txt": {"status": 200, "body": "User-agent: *", "set_cookies": ["sessionid=1"]}},
            "requests_made": []}
    found = ids(report.build("https://example.com/", view)["findings"])
    assert "cookies-session-no-secure" not in found and "cookies-none" in found


def test_a_port_named_in_scope_is_that_port_only():
    scope = Scope.build(["example.com:80"])
    assert scope.allows("http://example.com/") and not scope.allows("https://example.com/")
    assert Scope.build(["example.com:443"]).allows("https://example.com/")


def test_a_redirect_over_the_budget_is_not_called_out_of_scope():
    from web_search_neo.audit import report
    server = sf.RouteSite({"/": (302, [("Location", "/next")], ""), "/next": (200, [], sf.page())}).start()
    try:
        checker = report.Checker(Scope.build([f"127.0.0.1:{server.port}"]), Budget(limit=1), 5)
        page = checker.get(server.base_url + "/")
        assert page["not_followed_reason"] == "request budget exhausted" and "/next" not in server.seen
        built = report.build(server.base_url + "/", {"page": page, "requests_made": checker.budget.made})
        assert "redirect-over-budget" in ids(built["findings"]) and "redirect-out-of-scope" not in ids(built["findings"])
    finally:
        server.stop()


def test_paths_apply_to_the_first_host_only():
    first = sf.RouteSite({"/": (200, [], sf.page()), "/login": (200, [], sf.page())}).start()
    second = sf.RouteSite({"/": (200, [], sf.page()), "/login": (200, [], sf.page())}).start()
    try:
        site.run(None, mode="hosts", hosts=[first.base_url, second.base_url], paths=["/login"])
        assert "/login" in first.seen and "/login" not in second.seen
    finally:
        first.stop()
        second.stop()


# --- audit round 4 ------------------------------------------------------------------------------

def _two_names(monkeypatch):
    """example.test and www.example.test both resolve to this machine and count as public hosts."""
    import socket
    _pretend_public(monkeypatch)
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: real("127.0.0.1" if str(host).endswith("example.test") else host, *a, **k))


@pytest.mark.parametrize("include_subdomains", [False, True])
def test_http_to_www_over_http_is_the_initial_http_verdict(monkeypatch, include_subdomains):
    from web_search_neo.audit import report, transport
    server = sf.RouteSite({}).start()
    port = server.port
    server.routes.update({"/": (301, [("Location", f"http://www.example.test:{port}/w")], ""),
                          "/w": (301, [("Location", "https://www.example.test/")], "")})
    try:
        _two_names(monkeypatch)
        scope = Scope.build([f"example.test:{port}"], include_subdomains=include_subdomains)
        checker = report.Checker(scope, Budget(), 5)
        probe = checker.get(f"http://example.test:{port}/", small=True)
        found = [f for f in transport.analyze_redirect("https://example.test/", probe) if f["id"].startswith("redirect-")]
        assert [(f["id"], f["points"]) for f in found] == [("redirect-initial-http", -10)]  # Observatory's -10
        if include_subdomains:  # www is in scope: the plain-http hop is read, the chain reaches https
            assert "/w" in server.seen and probe["not_followed"] == "https://www.example.test/"
            assert "cut" not in found[0]["evidence"]
        else:  # www is outside: the chain is cut there, and that is said instead of "never reaches https"
            assert "/w" not in server.seen and "chain cut at the scope boundary" in found[0]["evidence"]["cut"]
    finally:
        server.stop()


def _ipv6_site():
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            body = sf.page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    try:
        server = Server(("::1", 0), Handler)
    except OSError as exc:
        pytest.skip(f"no IPv6 loopback here: {exc}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_an_ipv6_start_url_and_an_ipv6_host_in_scope():
    server = _ipv6_site()
    try:
        port = server.server_address[1]
        page = site.run(f"http://[::1]:{port}/")
        assert page["success"] and page["status"] == 200 and page["mode"] == "local_development"
        assert parse_target(f"[::1]:{port}").label() == f"[::1]:{port}"
        hosts = site.run(None, mode="hosts", hosts=[f"http://[::1]:{port}"])
        assert hosts["checked"] == 1 and hosts["scope"]["hosts"] == [f"http://[::1]:{port}"]
    finally:
        server.shutdown()
        server.server_close()


def test_ipv6_urls_in_error_texts_are_redacted():
    from web_search_neo.audit.scope import redact_text
    text = redact_text("failed http://user:pw@[::1]:8080/x?token=SECRET and https://[fe80::1%25eth0]/y?session=S2")
    assert "pw@" not in text and "SECRET" not in text and "S2" not in text and "[::1]:8080" in text
