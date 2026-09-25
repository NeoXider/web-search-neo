"""1.20: security_report, perf_report, har_export, test_run, third_party_only, summary=min, isolated default.

Everything runs against local fixtures (``security_fixture_site``, ``local_site``):
no test reaches the internet. Chrome-backed tests skip when Chrome is missing.
The scoring tests pin Mozilla HTTP Observatory's published modifiers
(mdn/mdn-http-observatory, src/grader/charts.js and src/analyzer/tests/*).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import security_fixture_site as fixture_site
from web_search_neo import audit_actions, browser_tools, extra_actions, main, network_log
from web_search_neo.audit import cookies, csp, grading, har, headers, page, perf, report, scenario, sites
from web_search_neo.audit import transport, wellknown
from web_search_neo.perception import min_summary


def ids(findings):
    return {item["id"]: item for item in findings}


def points(findings):
    return sum(item.get("points", 0) for item in findings)


# --- CSP (Observatory's single verdict) -------------------------------------------

def test_csp_parse_rejects_duplicates_and_splits_policies():
    assert csp.split_policies(["img-src data:, default-src 'none'"]) == ["img-src data:", "default-src 'none'"]
    with pytest.raises(csp.InvalidPolicy):
        csp.parse(["Script-Src 'self'; script-src *"])  # a repeated directive (case-insensitive)
    reported = csp.parse(["default-src 'none'; report-uri /a; report-uri /b"])
    assert reported[csp.DUPLICATES] == {"report-uri"} and reported["default-src"] == {"'none'"}
    assert csp.parse(["script-src; object-src 'none'"])["script-src"] == {"'none'"}  # an empty *-src is 'none'
    # Two policies: only the sources both allow survive.
    assert csp.parse(["script-src 'self' https://a.test", "script-src https://a.test"])["script-src"] == {"https://a.test"}


@pytest.mark.parametrize("policy, verdict, score", [
    (None, "csp-missing", -25),
    ("default-src 'self' 'unsafe-inline' 'unsafe-eval'", "csp-unsafe-scripts", -20),  # one verdict, not -30
    ("default-src 'self'; script-src 'self' https:", "csp-unsafe-scripts", -20),
    ("default-src 'self'; script-src 'self' data:", "csp-unsafe-scripts", -20),
    ("script-src 'self'", "csp-unsafe-scripts", -20),  # object-src falls back to *
    ("default-src 'self'; script-src 'self' *.googleapis.com", "csp-no-unsafe", 5),  # a restricted wildcard
    ("default-src 'self'; script-src 'self' http://cdn.example.com", "csp-insecure-scheme", -20),
    ("default-src 'self'; script-src 'self' 'unsafe-eval'", "csp-unsafe-eval", -10),
    ("default-src 'self'; img-src http://img.example.com", "csp-insecure-passive", -10),
    ("default-src 'self'; style-src 'self' 'unsafe-inline'", "csp-unsafe-style-only", 0),
    ("default-src 'none'; script-src 'self'", "csp-default-none", 10),
    ("script-src 'nonce-abc' 'unsafe-inline' 'strict-dynamic' https:; object-src 'none'; style-src 'self'",
     "csp-no-unsafe", 5),
    ("script-src 'strict-dynamic'; object-src 'none'", "csp-invalid", -25),
])
def test_csp_verdict_follows_observatory(policy, verdict, score):
    found, summary = csp.analyze(policy, https=True)
    assert summary["verdict"] == verdict and ids(found)[verdict]["points"] == score
    assert points(found) == score  # the advice around it carries no points


def test_csp_report_only_and_meta_rules():
    found, _ = csp.analyze(None, report_only="default-src 'self'")
    assert points(found) == -25 and "csp-report-only" in ids(found)
    # frame-ancestors in <meta> is ignored by the browser, so it protects nothing.
    found, summary = csp.analyze(None, meta=["default-src 'none'; frame-ancestors 'none'"])
    assert summary["frame_ancestors"] is None and "csp-meta-only" in ids(found)
    assert ids(found)["csp-default-none"]["points"] == 10
    _, summary = csp.analyze("default-src 'self'; frame-ancestors 'self'")
    assert summary["frame_ancestors"] == ["'self'"]


def test_csp_inline_code_semantics():
    _, summary = csp.analyze("script-src 'self' 'sha256-abc'; object-src 'none'")
    assert summary["inline_scripts_blocked"] and summary["uses_hashes"] and summary["inline_handlers_blocked"]
    _, summary = csp.analyze("script-src 'self' 'unsafe-inline'; object-src 'none'")
    assert not summary["inline_scripts_blocked"] and not summary["inline_handlers_blocked"]


# --- other headers ------------------------------------------------------------------

def test_hsts_rules_and_no_unverified_preload_bonus():
    assert headers.parse_hsts("max-age=31536000; includeSubDomains; preload") == {
        "max_age": 31536000, "include_subdomains": True, "preload": True, "twice": False}
    assert ids(headers.analyze({}, https=True))["hsts-missing"]["points"] == -20
    short = ids(headers.analyze({"strict-transport-security": "max-age=300"}, https=True))
    assert short["hsts-short"]["points"] == -10 and "hsts-no-subdomains" in short
    ready = ids(headers.analyze({"strict-transport-security": "max-age=63072000; includeSubDomains; preload"}, https=True))
    assert ready["hsts-preload-ready"]["points"] == 0 and "not looked up" in ready["hsts-preload-ready"]["detail"]
    assert ids(headers.analyze({}, https=False))["hsts-no-https"]["points"] == -20


def test_framing_points():
    assert ids(headers.analyze({}, https=True))["framing-missing"]["points"] == -20
    assert ids(headers.analyze({"x-frame-options": "sameorigin"}, https=True))["framing-xfo"]["points"] == 5
    assert ids(headers.analyze({}, https=True, frame_ancestors=["'none'"]))["framing-csp"]["points"] == 5
    allow_from = ids(headers.analyze({"x-frame-options": "ALLOW-FROM https://a.test"}, https=True))
    assert allow_from["framing-allow-from"]["points"] == 0
    # Observatory credits any frame-ancestors, even '*'; the report warns about it separately.
    wildcard = ids(headers.analyze({}, https=True, frame_ancestors=["*"]))
    assert "framing-open" in wildcard and wildcard["framing-csp"]["points"] == 5


@pytest.mark.parametrize("value, fid, score", [
    ("no-referrer, bogus", "referrer-private", 5), ("strict-origin-when-cross-origin", "referrer-private", 5),
    ("origin", "referrer-unsafe", -5), ("origin-when-cross-origin", "referrer-unsafe", -5),
    ("unsafe-url", "referrer-unsafe", -5), ("no-referrer-when-downgrade", "referrer-unsafe", -5),
    ("bogus", "referrer-invalid", -5), (None, "referrer-default", 0),
])
def test_referrer_policy_follows_observatory(value, fid, score):
    found = ids(headers.analyze({"referrer-policy": value} if value else {}, https=True))
    assert found[fid]["points"] == score


def test_corp_coop_and_coep_follow_observatory():
    found = ids(headers.analyze({"cross-origin-resource-policy": "same-site",
                                 "cross-origin-opener-policy": "same-origin",
                                 "cross-origin-embedder-policy": "require-corp"}, https=True))
    assert found["corp-ok"]["points"] == 10 and found["coop-ok"]["points"] == 10 and found["coep-ok"]["points"] == 10
    assert ids(headers.analyze({"cross-origin-resource-policy": "bogus"}, https=True))["corp-invalid"]["points"] == -5


def test_cors_is_scored_only_as_extra_points():
    null = ids(headers.analyze({"access-control-allow-origin": "null"}, https=True))["cors-null-origin"]
    assert null["points"] == 0 and null["extra_points"] == -25
    both = ids(headers.analyze({"access-control-allow-origin": "*", "access-control-allow-credentials": "true"},
                               https=True))["cors-wildcard-credentials"]
    assert both["points"] == 0 and both["extra_points"] == -10 and "no Origin header" in both["detail"]
    assert ids(headers.analyze({"access-control-allow-origin": "*"}, https=True))["cors-wildcard"]["status"] == "warn"


def test_version_disclosure_and_permissions_policy():
    found = ids(headers.analyze({"server": "nginx/1.25.3", "x-powered-by": "Express",
                                 "permissions-policy": "camera=*, geolocation=()"}, https=True))
    assert found["version-disclosure"]["evidence"] == {"server": "nginx/1.25.3"}
    assert found["software-named"]["evidence"] == {"x-powered-by": "Express"}
    assert found["permissions-policy-wildcard"]["evidence"] == ["camera"]


# --- cookies (Observatory's worst result) ----------------------------------------

def _jar(*lines):
    return [cookies.parse_set_cookie(line) for line in lines]


def test_set_cookie_parsing_reads_flags_not_values():
    parsed = cookies.parse_set_cookie("__Host-sid=SECRET; Path=/; Secure; HttpOnly; SameSite=strict; Max-Age=60")
    assert parsed == {"name": "__Host-sid", "secure": True, "httponly": True, "samesite": "Strict", "domain": None,
                      "path": "/", "persistent": True, "source": "set-cookie"}
    assert "SECRET" not in json.dumps(parsed) and cookies.parse_set_cookie("garbage") is None


@pytest.mark.parametrize("lines, https, hsts, result, score", [
    (("sessionid=1; SameSite=None",), True, False, "cookies-session-no-secure", -40),  # one result, not -90
    (("sessionid=1; Secure; SameSite=Lax",), True, False, "cookies-session-no-httponly", -30),
    (("csrftoken=1; Secure",), True, False, "cookies-anticsrf-no-samesite", -20),
    (("prefs=1; Secure; SameSite=Bogus",), True, False, "cookies-samesite-invalid", -20),  # raw header check
    (("prefs=1",), True, False, "cookies-no-secure", -20),
    (("prefs=1",), True, True, "cookies-no-secure-hsts", -5),
    (("sessionid=1; HttpOnly; SameSite=Lax",), True, True, "cookies-session-no-secure-hsts", -10),
    (("sessionid=1; Secure; HttpOnly",), True, False, "cookies-secure-no-samesite", 0),
    (("sessionid=1; Secure; HttpOnly; SameSite=Lax", "ui=1; Secure"), True, False, "cookies-secure-no-samesite", 0),
    (("sessionid=1; Secure; HttpOnly; SameSite=Lax", "ui=1; Secure; SameSite=Strict"), True, False, "cookies-secure", 5),
    (("prefs=1; Secure; SameSite=Lax",), False, False, "cookies-secure", 5),  # Observatory reads the flags only
    (("prefs=1; SameSite=None",), True, False, "cookies-samesite-invalid", -20),  # None without Secure
])
def test_cookie_result_follows_observatory(lines, https, hsts, result, score):
    found = cookies.analyze(_jar(*lines), host="example.com", https=https, hsts=hsts, raw_lines=list(lines))
    assert ids(found)[result]["points"] == score and points(found) == score


def test_cookie_advice_carries_no_observatory_points():
    found = ids(cookies.analyze(_jar(
        "auth_token=1; Secure; SameSite=Lax", "__Host-bad=4; Secure; Path=/; Domain=example.com",
        "wide=5; Secure; SameSite=Lax; Domain=co.uk", "prefs=3; SameSite=None"), host="www.example.com", https=True))
    assert found["cookies-secret-no-httponly"]["evidence"] == ["auth_token"]  # Observatory's rule misses it
    assert found["cookies-prefix-invalid"]["extra_points"] == -10 and found["cookies-prefix-invalid"]["points"] == 0
    assert "public suffix" in found["cookies-domain-wide"]["evidence"]["wide"]
    # The graded result: prefs has SameSite=None without Secure; the dropped cookies are not graded.
    assert found["cookies-samesite-invalid"]["points"] == -20
    assert points(found.values()) == -20


def test_script_cookies_are_listed_not_graded():
    header = _jar("__Host-sid=1; Path=/; Secure; HttpOnly; SameSite=Strict")
    browser = [cookies.from_browser({"name": "__Host-sid", "domain": "example.com", "secure": True}),
               cookies.from_browser({"name": "ui", "domain": "example.com"})]
    merged = cookies.merge(header, browser)
    assert [c["source"] for c in merged] == ["set-cookie", "script-or-subresource"]
    found = ids(cookies.analyze(merged, host="example.com", https=True))
    assert found["cookies-secure"]["points"] == 5 and found["cookies-script-no-secure"]["evidence"] == ["ui"]


# --- grading -----------------------------------------------------------------------

def test_bonuses_count_only_from_an_a_grade_base_and_extras_stay_apart():
    bonus = {"id": "b", "points": 10, "title": "b", "status": "pass", "severity": "info"}
    assert grading.grade([bonus])["score"] == 110 and grading.grade([bonus])["grade"] == "A+"
    penalty = {"id": "p", "points": -20, "title": "p", "status": "fail", "severity": "high"}
    extra = {"id": "x", "points": 0, "extra_points": -50, "title": "x", "status": "fail", "severity": "high"}
    scored = grading.grade([penalty, bonus, extra])
    assert scored["score"] == 80 and scored["grade"] == "B+" and scored["explanation"]["bonuses_applied"] is False
    assert scored["extended"]["score"] == 30 and scored["extended"]["extra_penalties"][0]["id"] == "x"
    assert [grading.letter(n) for n in (100, 90, 85, 70, 50, 25, 24)] == ["A+", "A", "A-", "B", "C", "D-", "F"]
    assert grading.grade([penalty] * 9)["score"] == 0


def test_priority_puts_failures_and_severity_first():
    items = [
        {"id": "w", "status": "warn", "severity": "high", "points": 0, "title": "w"},
        {"id": "low", "status": "fail", "severity": "low", "points": -5, "title": "low"},
        {"id": "hi", "status": "fail", "severity": "high", "points": -20, "title": "hi"},
        {"id": "ok", "status": "pass", "severity": "info", "points": 0, "title": "ok"},
    ]
    assert [p["id"] for p in grading.priority(items)] == ["hi", "low", "w"]


# --- transport ---------------------------------------------------------------------

def _probe(hops, final):
    return {"url": hops[0][0], "hops": [{"url": u, "status": s, "location": loc} for u, s, loc in hops],
            "route": [u for u, _s, _loc in hops] + [final], "final_url": final}


def test_redirect_verdicts():
    target = "https://example.com/"
    ok = transport.analyze_redirect(target, _probe([("http://example.com/", 301, "https://example.com/")], target))
    assert ok[0]["id"] == "redirect-ok"
    none = transport.analyze_redirect(target, {"url": "http://example.com/", "status": 200, "hops": [],
                                               "route": ["http://example.com/"], "final_url": "http://example.com/"})
    assert none[0]["id"] == "redirect-missing" and none[0]["points"] == -20
    off = transport.analyze_redirect(target, _probe([("http://example.com/", 301, "https://www.example.com/")],
                                                    "https://www.example.com/"))
    assert off[0]["id"] == "redirect-off-host" and off[0]["points"] == -5
    first_http = transport.analyze_redirect(target, _probe([("http://example.com/", 301, "http://example.com/x"),
                                                            ("http://example.com/x", 301, "https://example.com/")], target))
    assert first_http[0]["id"] == "redirect-initial-http" and first_http[0]["points"] == -10
    closed = transport.analyze_redirect(target, {"url": "http://example.com/", "error": "ConnectionError: refused"})
    assert closed[0]["id"] == "redirect-no-http" and closed[0]["points"] == 0
    bad_cert = transport.analyze_redirect(target, {"url": "http://example.com/",
                                                   "error": "SSLError: [SSL: CERTIFICATE_VERIFY_FAILED]"})
    assert bad_cert[0]["id"] == "redirect-invalid-cert" and bad_cert[0]["points"] == -20
    never = transport.analyze_redirect(target, _probe([("http://example.com/", 301, "http://example.com/x")],
                                                      "http://example.com/x"))
    assert never[0]["id"] == "redirect-not-https" and never[0]["points"] == -20
    assert transport.analyze_redirect("http://example.com/", None)[0]["id"] == "https-missing"


def test_the_http_probe_never_carries_the_callers_path_or_query():
    assert transport.http_variant("https://example.com/p?token=abc#f") == "http://example.com/"
    assert transport.http_variant("https://example.com:8443/") is None
    assert transport.http_variant("https://[::1]/x") == "http://[::1]/"


def test_certificate_findings():
    assert transport.analyze_certificate({"valid": True, "days_left": 5})[0]["id"] == "tls-expiring"
    assert transport.analyze_certificate({"valid": True, "days_left": 20})[0]["id"] == "tls-expiring-soon"
    fine = transport.analyze_certificate({"valid": True, "days_left": 80, "issuer": {"organizationName": "Let's Encrypt"}})
    assert fine[0]["id"] == "tls-ok" and fine[0]["evidence"]["issuer"]["organizationName"] == "Let's Encrypt"
    assert transport.is_certificate_error("SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert not transport.is_certificate_error("ConnectionError: refused")


# --- page --------------------------------------------------------------------------

HTML = """<!doctype html><html><head><title>T</title>
<meta http-equiv="Content-Security-Policy" content="script-src 'self'">
<script src="https://cdn.other.test/a.js"></script>
<script src="https://cdn.other.test/b.js" integrity="sha384-x"></script>
<script src="http://static.example.com/old.js"></script>
<script>inline()</script><script type="application/ld+json">{}</script></head>
<body><img src="http://img.example.com/p.png"><form action="http://example.com/login" method="get">
<input type="password" name="pw" autocomplete="off"></form>
<a href="https://other.test/x" target="_blank">x</a><a href="https://other.test/y" target="_blank" rel="noopener">y</a>
<button onclick="go()">go</button><iframe src="https://widgets.other.test/w"></iframe></body></html>"""


def test_static_snapshot_and_page_findings():
    snap = page.snapshot_from_html(HTML, "https://www.example.com/")
    assert snap["title"] == "T" and snap["meta_csp"] == ["script-src 'self'"]
    assert snap["scripts"]["total"] == 5 and snap["inline_handlers"]["items"] == ["button[onclick]"]
    _, summary = csp.analyze(None, meta=snap["meta_csp"])
    found, parties = page.analyze(snap, "https://www.example.com/", summary)
    by_id = ids(found)
    # static.example.com is the page's own site: mixed content (this report's check), not Observatory SRI.
    assert by_id["mixed-content-active"]["evidence"]["items"] == ["http://static.example.com/old.js"]
    assert by_id["mixed-content-active"]["extra_points"] == -50 and by_id["mixed-content-active"]["points"] == 0
    assert by_id["mixed-content-passive"]["extra_points"] == -10
    # SRI is judged on the served HTML: a.js (https, no integrity) is the worst; old.js is own-site.
    assert by_id["sri-missing"]["evidence"]["items"] == [{"src": "https://cdn.other.test/a.js", "integrity": False},
                                                          {"src": "https://cdn.other.test/b.js", "integrity": True}]
    assert by_id["sri-missing"]["points"] == -5
    assert by_id["sri-no-crossorigin"]["evidence"]["items"] == ["https://cdn.other.test/b.js"]
    assert by_id["form-insecure-action"]["extra_points"] == -20 and by_id["password-get-form"]["extra_points"] == -10
    assert by_id["password-autocomplete"]["evidence"] == [{"name": "pw", "autocomplete": "off"}]
    assert by_id["blank-without-noopener"]["evidence"]["items"] == ["https://other.test/x"]
    assert by_id["inline-scripts-blocked"]["evidence"] == {"inline_scripts": 1}  # the JSON-LD block is data
    assert by_id["inline-handlers-blocked"]["status"] == "fail"
    assert parties["sites"] == {"other.test": 3}  # a.js, b.js and the frame; links are not loads


@pytest.mark.parametrize("scripts, result, score", [
    ([{"raw_src": "http://cdn.other.test/a.js"}], "sri-missing-insecure", -50),
    ([{"raw_src": "http://cdn.other.test/a.js", "integrity": "sha384-x"}], "sri-insecure", -20),
    ([{"raw_src": "https://cdn.other.test/a.js", "integrity": "sha384-x", "crossorigin": "anonymous"}],
     "sri-external-secure", 5),
    ([{"raw_src": "https://www.example.com/app.js"}], "sri-own-scripts", 0),
    ([], "sri-no-scripts", 0),
])
def test_sri_follows_observatory(scripts, result, score):
    snap = {"url": "https://www.example.com/", "scripts": {"items": [{"src": s["raw_src"], **s} for s in scripts],
                                                           "total": len(scripts)}}
    found, _ = page.analyze(snap, "https://www.example.com/", {})
    assert ids(found)[result]["points"] == score


def test_hash_csp_does_not_claim_inline_scripts_are_blocked():
    snap = page.snapshot_from_html("<script>ok()</script>", "https://www.example.com/")
    _, summary = csp.analyze("script-src 'self' 'sha256-abc'; object-src 'none'")
    found = ids(page.analyze(snap, "https://www.example.com/", summary)[0])
    assert "inline-scripts-blocked" not in found and "inline-scripts-hashes" in found


def test_junk_from_a_hostile_page_does_not_sink_the_analysis():
    for junk in ({"scripts": ["x"]}, {"scripts": {"items": [None, 5, "s"]}}, {"forms": {"items": [{"action": 5}]}},
                 {"blank_links": {"items": [{"href": None, "rel": 3}]}}, {"inline_handlers": {"items": [{}], "total": "x"}}):
        findings, _ = page.analyze(junk, "https://www.example.com/", {})
        assert isinstance(findings, list)
    assert not page.usable({"scripts": ["x"]}) and not page.usable("nope")
    view = {"page": {"status": 200, "final_url": "https://example.com/", "headers": {}, "set_cookies": [],
                     "body": "<script src='https://cdn.other.test/a.js'></script>"}, "requests_made": []}
    built = report.build("https://example.com/", view, {"snapshot": {"scripts": ["broken"]}, "network": [None, 7]})
    assert built["page_source"] == "static-html" and "broke the browser snapshot" in built["browser_note"]
    assert "sri-missing" in ids(built["findings"])


# --- well-known files --------------------------------------------------------------

def test_security_txt_and_robots():
    ok = {"status": 200, "body": fixture_site.SECURITY_TXT, "final_url": "https://e.test/.well-known/security.txt"}
    found, view = wellknown.analyze_security_txt(ok)
    assert found[0]["id"] == "security-txt-ok" and view["fields"]["contact"] == ["mailto:security@example.test"]
    expired, _ = wellknown.analyze_security_txt({"status": 200, "body": "Contact: mailto:a@b\nExpires: 2001-01-01T00:00:00Z"})
    assert expired[0]["id"] == "security-txt-expired"
    catch_all = {"status": 200, "body": "<!doctype html><html><head></head></html>"}
    assert wellknown.analyze_security_txt(catch_all)[0][0]["id"] == "security-txt-missing"
    robots, view = wellknown.analyze_robots({"status": 200, "body": fixture_site.ROBOTS_TXT})
    assert view["disallow"] == ["/admin", "/private"] and "hides nothing" in robots[0]["detail"]


# --- the whole report without a network --------------------------------------------

def _https_view(extra_headers, cookies_lines=(), body="<html><head></head><body></body></html>"):
    return {
        "page": {"status": 200, "final_url": "https://example.com/", "headers": extra_headers,
                 "set_cookies": list(cookies_lines), "body": body},
        "redirect_probe": _probe([("http://example.com/", 301, "https://example.com/")], "https://example.com/"),
        "tls": {"valid": True, "days_left": 60},
        "files": {"security_txt": {"status": 200, "body": fixture_site.SECURITY_TXT}, "robots_txt": {"status": 404}},
        "requests_made": ["GET https://example.com/"],
    }


STRONG = {
    "content-security-policy": fixture_site.STRONG_CSP,
    "strict-transport-security": "max-age=63072000; includeSubDomains; preload",
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    "permissions-policy": "camera=()", "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


def test_a_hardened_https_site_scores_like_observatory():
    built = report.build("https://example.com/", _https_view(STRONG, ["__Host-sid=1; Path=/; Secure; HttpOnly; SameSite=Lax"]))
    # 100 + CSP default-src 'none' 10 + frame-ancestors 5 + Referrer-Policy 5 + COOP 10 + CORP 10 + cookies 5;
    # the HSTS preload bonus needs a preload-list lookup and is not given.
    assert built["grade"] == "A+" and built["score"] == 145, built["score_explanation"]
    assert built["score_explanation"]["penalty_total"] == 0 and built["score_explanation"]["bonuses_applied"]
    assert built["extended"]["score"] == 145 and built["mode"] == "production" and "production_forecast" not in built


def test_a_bare_https_site_gets_an_f_with_a_fix_for_every_problem():
    built = report.build("https://example.com/", _https_view({}, ["session=1; Path=/"]))
    # CSP -25, HSTS -20, XFO -20, XCTO -5, cookies (session without Secure) -40.
    assert built["score_explanation"]["penalty_total"] == -110 and built["grade"] == "F"
    assert built["priority"][0]["severity"] == "high" and all(item["fix"] for item in built["priority"])
    assert built["summary_line"].startswith("F (0/100; with this report's own checks F)")
    assert built["cookies"] == [{"name": "session", "secure": False, "httponly": False, "samesite": None,
                                 "domain": None, "path": "/", "source": "set-cookie"}]


def test_an_untrusted_certificate_is_read_again_unverified_and_fully_graded():
    sent = []

    def fetch(target, timeout, *, verify=True, **_):
        sent.append((target, verify))
        if verify:
            return {"url": target, "route": [target], "error": "SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] self-signed"}
        return {"url": target, "final_url": target, "route": [target], "status": 200, "hops": [], "body": "",
                "set_cookies": [], "verified": False,
                "headers": {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
                            "strict-transport-security": "max-age=31536000"}}

    view = report.collect_http("https://self-signed.test/", 5, fetch=fetch,
                               certificate=lambda host, port, timeout: {"valid": False, "error": "self-signed certificate",
                                                                        "self_signed": True})
    assert ("https://self-signed.test/", False) in sent  # the one unverified retry, as Observatory does
    assert any("certificate not verified" in line for line in view["requests_made"])
    built = report.build("https://self-signed.test/", view)
    found = ids(built["findings"])
    assert built["success"] and built["status"] == 200 and isinstance(built["score"], int)
    assert found["tls-invalid"]["status"] == "fail" and found["hsts-invalid-cert"]["points"] == -20
    assert found["xcto-ok"] and found["framing-xfo"]["points"] == 5  # headers of the unverified read are graded


def test_collect_http_stays_on_the_named_host_and_redacts():
    sent = []

    def fetch(target, timeout, *, follow=None, **_):
        sent.append(target)
        if target.startswith("https://user:pw@shop.example.com/cart"):
            hop = "https://login.other.test/sso"
            if follow is not None and not follow(hop):  # the transport asks before every hop
                return {"url": target, "final_url": target, "route": [target], "status": 302, "headers": {},
                        "set_cookies": [], "hops": [], "body": "", "not_followed": hop}
            return {"url": target, "final_url": hop, "route": [target, hop], "status": 200, "headers": {},
                    "set_cookies": [], "hops": [], "body": ""}
        return {"url": target, "route": [target], "status": 404, "headers": {}, "hops": [], "body": ""}

    view = report.collect_http("https://user:pw@shop.example.com/cart?token=abc", 5, fetch=fetch,
                               certificate=lambda host, port, timeout: sent.append(f"tls {host}") or {"valid": True})
    assert all("other.test" not in target for target in sent), sent
    assert "http://shop.example.com/" in sent and "tls shop.example.com" in sent
    assert all("pw" not in line and "abc" not in line for line in view["requests_made"]), view["requests_made"]
    built = report.build("https://user:pw@shop.example.com/cart?token=abc", view)
    assert "redirect-out-of-scope" in ids(built["findings"]) and "pw@" not in built["url"]
    assert "other.test" in json.dumps(built["findings"])  # named, never requested


def test_browser_requests_are_reported_apart_from_the_reports_own():
    summary = report.browser_requests([{"url": "https://www.example.com/"}, {"url": "https://cdn.other.test/a.js"},
                                       {"url": "data:,x"}], "https://www.example.com/")
    assert summary["count"] == 3 and summary["other_sites"] == ["other.test"] and "Not sent by the report" in summary["note"]


def test_an_unreadable_page_is_a_failed_report_not_a_grade():
    built = report.build("https://nope.test/", {"error": "ConnectionError: refused", "requests_made": ["GET https://nope.test/"]})
    assert built["success"] is False and "could not be read" in built["error"]


def test_metadata_hosts_are_refused_before_any_request():
    view = report.collect_http("http://169.254.169.254/latest", 2.0)
    assert view["requests_made"] == [] and "blocked" in view["error"]


def test_site_of_is_cached():
    sites.site_of.cache_clear()
    for _ in range(50):
        sites.site_of("cdn.example.com")
    assert sites.site_of.cache_info().hits >= 49


# --- against the local fixture server (no Chrome) ------------------------------------

@pytest.fixture(scope="module")
def fixture_sites():
    weak = fixture_site.start("weak.html", fixture_site.WEAK_HEADERS, well_known=False)
    strong = fixture_site.start("strong.html", fixture_site.STRONG_HEADERS, well_known=True)
    try:
        yield SimpleNamespace(weak=weak, strong=strong)
    finally:
        weak.stop()
        strong.stop()


def _report(url, **extra):
    result = asyncio.run(main.web_action([{"action": "security_report", "url": url, **extra}]))
    assert result["success"], result
    return result["results"][0]["data"]


def test_security_report_over_http_without_a_browser(fixture_sites):
    weak = _report(fixture_sites.weak.base_url + "/", browser=False)
    strong = _report(fixture_sites.strong.base_url + "/", browser=False)
    assert weak["page_source"] == strong["page_source"] == "static-html"
    assert weak["browser_requests"] is None  # no browser, no page-load traffic
    origin = fixture_sites.weak.base_url
    assert all(line.split(" ", 1)[1].startswith(origin) for line in weak["requests_made"])
    assert sorted(set(fixture_sites.weak.paths)) == ["/", "/.well-known/security.txt", "/robots.txt", "/security.txt"]
    weak_ids, strong_ids = ids(weak["findings"]), ids(strong["findings"])
    # 127.0.0.1 is a development address: cookie Secure is not applicable there, HttpOnly still is.
    assert {"csp-missing", "cors-wildcard-credentials", "cookies-session-no-httponly", "referrer-unsafe",
            "version-disclosure", "sri-missing-insecure", "password-get-form", "security-txt-missing"} <= set(weak_ids)
    assert weak_ids["version-disclosure"]["evidence"]["x-powered-by"] == "PHP/8.1.2"
    assert weak_ids["sri-missing-insecure"]["points"] == -50  # a third-party script over http, no SRI
    assert {"csp-default-none", "framing-csp", "referrer-private", "corp-ok", "security-txt-ok",
            "inline-scripts-blocked", "inline-handlers-blocked"} <= set(strong_ids)
    assert weak["mode"] == strong["mode"] == "local_development"
    assert weak["grade"] == "F" and weak["production_forecast"]["grade"] == "F"
    # CSP default-src 'none' +10, XFO/frame-ancestors +5, Referrer-Policy +5, COOP +10, CORP +10, cookies +5.
    assert strong["score"] == 145 and strong["grade"] == "A+", strong["score_explanation"]
    # As served on plain http: Observatory's -20 for the missing redirect and -20 for HSTS, no bonuses below 90.
    assert strong["production_forecast"]["grade"] == "A+" and strong["as_served"] == {"score": 60, "grade": "C+"}
    assert ids(strong["findings"])["https-missing"]["status"] == "skip"
    assert weak["extended"]["score"] <= weak["score"]
    assert "abc123" not in json.dumps(weak)  # cookie values never leave the server


def test_security_report_min_summary_keeps_the_verdict(fixture_sites):
    result = asyncio.run(main.web_action([{"action": "security_report", "url": fixture_sites.strong.base_url + "/",
                                           "browser": False}], summary="min"))
    data = result["results"][0]["data"]
    assert data["summary_mode"] == "min" and data["grade"] and len(data["priority"]) <= 5
    assert "findings" in data["summary_omitted"] and "fix" in data["priority"][0]
    assert len(json.dumps(data)) < 5000


# --- Chrome-backed --------------------------------------------------------------------

def _skip_without_browser(result):
    if result.get("browser_error"):
        pytest.skip(f"Chrome/Selenium is unavailable: {result['browser_error']}")


def test_security_report_in_the_isolated_browser(fixture_sites):
    weak = _report(fixture_sites.weak.base_url + "/")
    _skip_without_browser(weak)
    assert weak["page_source"] == "browser"
    names = {c["name"]: c for c in weak["cookies"]}
    assert names["tracker"]["source"] == "script-or-subresource"  # set by document.cookie
    assert weak["third_parties"]["scripts"][0]["site"] == "localhost"
    assert weak["browser_requests"]["count"] >= 3 and "localhost" in weak["browser_requests"]["other_sites"]
    assert not browser_tools._sessions, "the report closes its own session"
    strong = _report(fixture_sites.strong.base_url + "/")
    assert ids(strong["findings"])["inline-scripts-blocked"]["status"] == "fail"


def test_a_failed_snapshot_with_keep_open_closes_the_session(fixture_sites, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("page script failed")

    monkeypatch.setattr(audit_actions, "_script_value", broken)
    result = _report(fixture_sites.weak.base_url + "/", keep_open=True, session_id="kept")
    assert "page script failed" in result["browser_error"] or "WebDriver" in result["browser_error"]
    assert "kept" not in browser_tools._sessions and "session_id" not in result


def _open(url, session_id):
    from selenium.common.exceptions import WebDriverException

    try:
        return browser_tools.open_page(url, session_id=session_id, headless=True, profile_mode="temporary")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def test_third_party_network_filter_and_har_export(fixture_sites, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    _open(fixture_sites.weak.base_url + "/", "har")
    time.sleep(0.5)
    third = asyncio.run(main.web_info("network", {"session_id": "har", "third_party_only": True, "output": "json"}))
    assert third["third_party_only"] and third["first_party_site"] == "127.0.0.1"
    assert third["requests"] and all("//localhost:" in row["url"] for row in third["requests"])
    result = asyncio.run(main.web_action([{"action": "har_export", "session_id": "har", "save_to": "page.har"}]))
    data = result["results"][0]["data"]
    text = Path(data["saved_to"]).read_text(encoding="utf-8")
    log = json.loads(text)["log"]
    assert log["version"] == "1.2" and log["creator"]["name"] == "Web Search Neo"
    urls = [entry["request"]["url"] for entry in log["entries"]]
    assert fixture_sites.weak.base_url + "/" in urls and data["entries"] == len(urls)
    assert "request headers are not recorded" in log["comment"] and "abc123" not in text  # Set-Cookie redacted
    again = asyncio.run(main.web_action([{"action": "har_export", "session_id": "har", "save_to": "page.har"}]))
    assert again["success"] is False and "overwrite" in again["results"][0]["error"]
    only = asyncio.run(main.web_action([{"action": "har_export", "session_id": "har", "save_to": "third.har",
                                         "third_party_only": True, "inline": True}]))["results"][0]["data"]
    assert all("//localhost:" in entry["request"]["url"] for entry in only["har"]["log"]["entries"])


def test_perf_report_measures_a_cold_isolated_load(fixture_sites):
    result = asyncio.run(main.web_action([{"action": "perf_report", "url": fixture_sites.weak.base_url + "/perf",
                                           "wait_seconds": 3}]))
    if not result["success"] and "WebDriver" in str(result["results"][0].get("error")):
        pytest.skip(f"Chrome/Selenium is unavailable: {result['results'][0]['error']}")
    data = result["results"][0]["data"]
    metrics = data["metrics"]
    assert data["fresh_isolated_load"] and data["session_id"] is None and not browser_tools._sessions
    assert isinstance(metrics["ttfb_ms"], int) and metrics["ttfb_ms"] >= 0
    assert isinstance(metrics["fcp_ms"], int) and metrics["load_ms"] is not None
    assert data["resources"]["count"] >= 3 and "script" in data["resources"]["by_initiator"]
    assert metrics["cls"] is not None and data["ratings"]["ttfb_ms"] == "good"
    assert data["render_blocking"]["count"] >= 1  # the head stylesheet and sync script


def test_test_run_reports_each_step(local_site):
    form = f"{local_site.base_url}/form"
    steps = [
        {"step_name": "form is there", "expect": {"selector": "#application", "title_contains": "Form"}},
        {"action": "fill", "fields": {"#candidate-name": "Ada"}},
        {"step_name": "submit", "action": "click", "selector": "#submit-button",
         "expect": {"text": "Application submitted", "no_console_errors": True, "no_failed_requests": True}},
        {"step_name": "a missing button cannot be clicked", "action": "click", "selector": "#nope",
         "expect": {"action_fails": True}},
    ]
    result = asyncio.run(main.web_action([{"action": "test_run", "url": form, "session_id": "reg",
                                           "steps": steps, "timeout_seconds": 5}]))
    data = result["results"][0]["data"]
    if data.get("error") and "could not open" in data["error"]:
        pytest.skip(data["error"])
    assert data["success"] and data["passed"] == 4 and data["failed"] == 0, data
    assert [c["expect"] for c in data["steps"][2]["checks"]] == [
        "action", "text", "no_console_errors", "no_failed_requests"]
    assert data["steps"][0]["name"] == "form is there"
    assert "reg" not in browser_tools._sessions  # opened by the run, closed by it

    failing = [{"step_name": "wrong text", "expect": {"text": "Never shown", "timeout_seconds": 0.5}},
               {"step_name": "never reached", "expect": {"selector": "body"}}]
    result = asyncio.run(main.web_action([{"action": "test_run", "url": form, "session_id": "reg2",
                                           "steps": failing}], summary="min"))
    data = result["results"][0]["data"]
    assert data["success"] is False and data["failed"] == 1 and data["skipped"] == 1
    assert data["failed_steps"][0]["name"] == "wrong text" and "Never shown" in data["summary_line"]
    assert "steps" in data["summary_omitted"]


def test_test_run_keeps_an_actions_own_name(fixture_sites):
    # cookies clear takes a name of its own; losing it would clear every cookie of the domain.
    steps = [{"action": "cookies", "op": "clear", "domain": "127.0.0.1", "name": "tracker"},
             {"step_name": "only tracker is gone",
              "expect": {"script": "!document.cookie.includes('tracker=') && document.cookie.includes('sessionid=')"}}]
    result = asyncio.run(main.web_action([{"action": "test_run", "url": fixture_sites.weak.base_url + "/",
                                           "session_id": "names", "steps": steps}]))
    data = result["results"][0]["data"]
    if data.get("error") and "could not open" in data["error"]:
        pytest.skip(data["error"])
    assert data["success"], data


def test_test_run_validates_the_whole_plan_first():
    with pytest.raises(ValueError, match="unknown expect key"):
        scenario.plan([{"expect": {"selectr": "#a"}}], "s", main)
    with pytest.raises(ValueError, match="another test_run"):
        scenario.plan([{"action": "test_run", "steps": []}], "s", main)
    with pytest.raises(ValueError, match="action_fails needs an action"):
        scenario.plan([{"expect": {"action_fails": True}}], "s", main)
    with pytest.raises(ValueError, match="Unsupported action"):
        scenario.plan([{"action": "clik"}], "s", main)
    # Arguments are checked against the action's schema before anything runs.
    with pytest.raises(ValueError, match=r"step 1: action 'click': unknown parameter\(s\) \['selectr'\]"):
        scenario.plan([{"action": "fill", "fields": {"#a": "1"}}, {"action": "click", "selectr": "#x"}], "s", main)
    planned = scenario.plan([{"action": "click", "selector": "#a", "summary": "min"},
                             {"action": "search", "query": "q"},
                             {"action": "cookies", "op": "clear", "domain": "a.com", "name": "sid"}], "s", main)
    assert planned[0]["action"]["session_id"] == "s" and "session_id" not in planned[1]["action"]
    assert planned[2]["action"]["name"] == "sid" and planned[2]["name"] == "cookies"
    result = asyncio.run(main.web_action([{"action": "test_run", "steps": [{"expect": {"bogus": 1}}]}]))
    assert result["success"] is False and "bogus" in result["results"][0]["error"]


def test_test_run_cannot_nest_through_a_macro():
    token = audit_actions._IN_TEST_RUN.set(True)
    try:
        with pytest.raises(ValueError, match="inside another test_run"):
            asyncio.run(audit_actions.browser_test_run([{"expect": {"selector": "body"}}]))
    finally:
        audit_actions._IN_TEST_RUN.reset(token)


# --- perf / har / summary units ----------------------------------------------------

def test_cls_uses_the_largest_session_window():
    shifts = [{"value": 0.05, "time": 100}, {"value": 0.05, "time": 500}, {"value": 0.5, "time": 900, "input": True},
              {"value": 0.08, "time": 3000}, {"value": 0.04, "time": 3500}]
    assert perf.cumulative_layout_shift(shifts) == 0.12
    assert perf.cumulative_layout_shift(None) is None
    assert [perf.rate("lcp_ms", v) for v in (2000, 3000, 5000)] == ["good", "needs-improvement", "poor"]


def test_perf_shape_recommends_from_the_numbers():
    raw = {"url": "https://example.com/", "ready_state": "complete", "lcp_supported": True,
           "navigation": {"start": 0, "response_start": 2000, "dom_content_loaded": 2500, "load": 3000, "transfer": 20000},
           "fcp": 2100, "lcp": {"time": 4500, "element": "img#hero"}, "shifts": [{"value": 0.3, "time": 10}],
           "resources": [{"url": "https://example.com/app.js", "type": "script", "transfer": 400_000,
                          "encoded": 400_000, "decoded": 400_000, "blocking": "blocking"},
                         {"url": "https://cdn.other.test/x.js", "type": "script", "transfer": 1000,
                          "encoded": 900, "decoded": 3000, "blocking": "non-blocking"}], "resources_total": 2}
    shaped = perf.shape(raw)
    assert shaped["ratings"] == {"ttfb_ms": "poor", "fcp_ms": "needs-improvement", "lcp_ms": "poor", "cls": "poor"}
    assert shaped["resources"]["third_party"]["sites"] == ["other.test"]
    text = " ".join(shaped["recommendations"])
    assert all(word in text for word in ("TTFB", "LCP", "CLS", "render-blocking", "uncompressed", "300 KB"))
    full = perf.shape({**raw, "resources_total": 250})
    assert "250 resource timings" in full["resources"]["buffer_note"]


def test_har_entry_shape_and_set_cookie_redaction():
    rows = [{"id": "1", "ts": 1_700_000_000_000, "method": "POST", "url": "https://e.test/api?q=1", "type": "Fetch",
             "status": 201, "mime": "application/json", "ms": 42, "size": 120, "post_data": "{\"a\":1}",
             "headers": {"location": "/next", "set-cookie": "sid=SECRET; Path=/; HttpOnly\nb=2"},
             "remote": "1.2.3.4"},
            {"id": "2", "ts": 1_700_000_000_500, "method": "GET", "url": "https://e.test/slow", "done": False}]
    log = har.build(rows, page_url="https://e.test/", title="E", version="1.20.0", dropped=3)["log"]
    first, second = log["entries"]
    assert first["startedDateTime"].endswith("Z") and first["request"]["queryString"] == [{"name": "q", "value": "1"}]
    assert first["request"]["postData"]["text"] == "{\"a\":1}" and first["response"]["redirectURL"] == "/next"
    assert first["timings"]["wait"] == 42 and first["serverIPAddress"] == "1.2.3.4"
    cookie = next(h["value"] for h in first["response"]["headers"] if h["name"] == "set-cookie")
    assert cookie == "sid=REDACTED; Path=/; HttpOnly\nb=REDACTED"
    assert second["_done"] is False and log["_dropped"] == 3


def test_min_summary_names_every_cut():
    data = {"success": True, "url": "https://e.test", "title": "x" * 500, "elements": [1, 2, 3], "summary": "own",
            "priority": [{"rank": i, "fix": "f" * 300, "evidence": {"a": 1}} for i in range(8)], "counts": {"high": 1},
            "page": {"k": "v" * 1000}}
    short = min_summary.minimize(data)
    assert short["success"] and short["counts"] == {"high": 1} and short["summary_mode"] == "min"
    assert short["summary"] == "own"  # the answer's own key is not overwritten
    assert short["summary_omitted"] == {"elements": "list[3]", "priority[].evidence": "dict",
                                        "priority": "list: kept 5 of 8", "page": "object with 1 key(s)"}
    assert short["summary_clipped"] == ["title", "priority[].fix"] and short["title"].endswith("(+260 chars)")
    arguments = {"summary": "min", "x": 1}
    assert min_summary.step_mode(arguments, {"x": None}) == "min" and arguments == {"x": 1}
    with pytest.raises(ValueError, match="summary must be one of"):
        min_summary.step_mode({"summary": "tiny"}, {})


def test_per_step_summary_min_through_web_action(local_site):
    result = asyncio.run(main.web_action([
        {"action": "http_request", "url": f"{local_site.base_url}/page", "summary": "min"},
        {"action": "http_request", "url": f"{local_site.base_url}/page"},
    ]))
    short, full = (item["data"] for item in result["results"])
    assert short["summary_mode"] == "min" and short["status"] == 200 and "body" in short["summary_clipped"]
    assert "summary_mode" not in full and len(full["body"]) > 240


# --- defaults, filters, contract ---------------------------------------------------

def test_a_new_session_opens_isolated_unless_an_option_names_another_mode():
    assert extra_actions.inherited_open_options("brand-new", None, None, None)["profile_mode"] == "isolated"
    assert extra_actions.inherited_open_options("brand-new", None, None, None, 7)["profile_mode"] == "current"
    with pytest.raises(ValueError, match="needs profile_mode"):  # persist names no mode: ask, never guess
        extra_actions.inherited_open_options("brand-new", None, None, None, None, True)
    assert extra_actions.inherited_open_options("brand-new", None, None, "127.0.0.1:9222")["profile_mode"] == "attach"
    assert extra_actions.inherited_open_options("brand-new", None, "work", None)["profile_mode"] == "persistent"
    assert extra_actions.inherited_open_options("brand-new", "current", None, None)["profile_mode"] == "current"
    schema = asyncio.run(main.web_info("action_schema", {"action": "open_many"}))
    assert schema["input_schema"]["properties"]["profile_mode"]["default"] == "isolated"


def test_third_party_rows_need_a_page_address():
    rows = [{"url": "https://www.example.com/a"}, {"url": "https://cdn.example.com/b"},
            {"url": "https://tracker.test/p"}, {"url": "data:image/png;base64,xx"}]
    kept, info = network_log.third_party_rows(rows, "https://www.example.com/")
    assert [row["url"] for row in kept] == ["https://tracker.test/p"]
    assert info["first_party_site"] == "example.com" and info["third_party_sites"] == ["tracker.test"]
    assert network_log.third_party_rows(rows, "about:blank")[0] == []
    session = SimpleNamespace(last_url="about:blank", driver=SimpleNamespace(current_url="https://a.example.com/"))
    assert network_log.page_url(session) == "https://a.example.com/"


def test_contract_carries_the_new_actions_and_recipes_within_budget():
    document = main._capabilities()
    assert len(json.dumps(document)) <= 13_500  # the 14 000 budget with a 500-character margin
    assert {"security_report", "perf_report", "har_export", "test_run"} <= set(document["actions"])
    assert "security_report" in document["actions"]
    assert {"release_check", "form_regression"} <= set(document["recipes"])
    assert "docs/site-checks.md" in document["discovery"]["playbook"]
    notes = asyncio.run(main.web_info("action_schema", {"action": "security_report"}))["notes"]
    assert "Observatory" in notes["grade"] and "browser_requests" in notes["scope"]
    skill = asyncio.run(main.web_info("skill", {"section": "audit"}))
    assert "security_report" in json.dumps(skill)
