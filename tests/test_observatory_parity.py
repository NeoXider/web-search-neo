"""Compatibility with Mozilla HTTP Observatory v1.7.1: one case, one expected result and score.

The expectations are Observatory's published behaviour (mdn/mdn-http-observatory
v1.7.1, ``src/analyzer/tests/*.js`` and ``src/grader/charts.js``); the cases are
written for this project and the implementation is this project's own code.
Every case runs without a network: headers, cookies and HTML go straight into
the analysers, or into ``report.build`` for the whole score.
"""
from __future__ import annotations

import pytest

from web_search_neo.audit import cookies, csp, grading, headers, page, report, transport


def ids(findings):
    return {item["id"]: item for item in findings}


def observatory_points(findings):
    return sum(item.get("points", 0) for item in findings)


# --- CSP -------------------------------------------------------------------------------

@pytest.mark.parametrize("header, meta, https, verdict, score", [
    # the basics
    (None, None, True, "csp-missing", -25),
    ("", None, True, "csp-invalid", -25),                                   # an empty header is an empty policy
    ("default-src 'none',", None, True, "csp-invalid", -25),                # a trailing comma: an empty policy
    ("default-src 'self'; script-src 'self'; script-src *", None, True, "csp-invalid", -25),  # F3 duplicate
    ("default-src 'none'; report-uri /a; report-uri /b", None, True, "csp-duplicate-report", 0),  # F3
    ("default-src 'self'; style-src 'self' 'unsafe-inline'; report-to a; report-to b", None, True,
     "csp-duplicate-report", 0),                                            # a passing style-only verdict, too
    ("default-src 'self' 'unsafe-inline'; report-uri /a; report-uri /b", None, True, "csp-unsafe-scripts", -20),
    # script sources
    ("default-src 'self'", None, True, "csp-no-unsafe", 5),
    ("default-src 'none'", None, True, "csp-default-none", 10),
    ("default-src 'none' 'self'", None, True, "csp-no-unsafe", 5),          # 'none' only counts alone
    ("script-src 'self'", None, True, "csp-unsafe-scripts", -20),           # object-src falls back to *
    ("default-src 'self'; script-src 'self' data:", None, True, "csp-unsafe-scripts", -20),
    ("default-src 'self'; script-src 'self' https:", None, True, "csp-unsafe-scripts", -20),
    ("default-src 'self'; script-src http://*.*", None, True, "csp-unsafe-scripts", -20),     # F3
    ("default-src 'self'; script-src ftp:", None, True, "csp-unsafe-scripts", -20),          # F3
    ("default-src 'self'; script-src 'self' *.example.com", None, True, "csp-no-unsafe", 5),  # a scoped wildcard
    ("default-src 'self'; object-src https:", None, True, "csp-unsafe-scripts", -20),
    ("DEFAULT-SRC 'SELF' 'UNSAFE-INLINE'", None, True, "csp-unsafe-scripts", -20),            # F3 lowercased
    ("default-src 'self'; script-src; object-src 'none'", None, True, "csp-no-unsafe", 5),    # F3 empty = 'none'
    # nonce, hash, strict-dynamic
    ("default-src 'self'; script-src 'nonce-abc' 'unsafe-inline'", None, True, "csp-no-unsafe", 5),
    ("default-src 'self'; script-src 'sha256-abc' 'unsafe-inline'", None, True, "csp-no-unsafe", 5),
    ("object-src 'none'; style-src 'self'; script-src 'nonce-a' 'strict-dynamic' https: 'unsafe-inline'", None, True,
     "csp-no-unsafe", 5),                                                   # F3 strict-dynamic makes them inert
    ("object-src 'none'; script-src 'nonce-a' 'strict-dynamic'", None, True,
     "csp-unsafe-style-only", 0),                                           # style-src unset: falls back to *
    ("object-src 'none'; script-src 'strict-dynamic' 'self'", None, True, "csp-invalid", -25),
    # the nonce/strict-dynamic edits act on default-src itself, seen by style-src and the http checks
    ("default-src 'nonce-a' 'strict-dynamic' https:", None, True, "csp-no-unsafe", 5),
    ("default-src 'nonce-a' 'strict-dynamic' http://cdn.example.com; object-src 'none'", None, True,
     "csp-no-unsafe", 5),
    # insecure schemes
    ("default-src 'self'; script-src 'self' http://cdn.example.com", None, True, "csp-insecure-scheme", -20),
    ("default-src 'self'; connect-src ftp://files.example.com", None, True, "csp-insecure-scheme", -20),  # F3 active
    ("default-src 'self'; script-src 'self' http://cdn.example.com", None, False, "csp-no-unsafe", 5),  # http page
    ("default-src 'self'; img-src http://img.example.com", None, True, "csp-insecure-passive", -10),
    ("default-src 'self'; media-src ftp://m.example.com", None, True, "csp-insecure-passive", -10),
    # eval and style
    ("default-src 'self'; script-src 'self' 'unsafe-eval'", None, True, "csp-unsafe-eval", -10),
    ("default-src 'self'; style-src 'self' 'unsafe-eval'", None, True, "csp-unsafe-eval", -10),
    ("default-src 'self'; style-src 'self' data:", None, True, "csp-unsafe-style-only", 0),
    # report-only and meta
    (None, None, True, "csp-missing", -25),
    (None, ["default-src 'none'"], True, "csp-default-none", 10),
    (None, ["frame-ancestors 'none'"], True, "csp-missing", -25),          # a meta tag of header-only directives
    ("default-src 'self'", ["default-src 'self' 'unsafe-inline'"], True, "csp-no-unsafe", 5),  # F3 merged: both allow
    ("default-src 'self'; script-src 'self' https://a.test", ["script-src https://b.test"], True,
     "csp-no-unsafe", 5),                                                   # nothing both allow: 'none'
])
def test_csp_case_to_score(header, meta, https, verdict, score):
    found, summary = csp.analyze(header, None, meta, https=https)
    assert summary["verdict"] == verdict, summary
    assert ids(found)[verdict]["points"] == score and observatory_points(found) == score


def test_csp_report_only_alone_and_the_merged_policy():
    found, summary = csp.analyze(None, "default-src 'none'")
    assert summary["verdict"] == "csp-report-only-only" and observatory_points(found) == -25
    merged = csp.parse(["script-src 'self' https://a.test", "script-src https://b.test"])
    assert merged["script-src"] == {"'none'"}


def test_meta_policies_that_repeat_report_uri_count_as_implemented():
    # Observatory counts the duplicate-warning key of the meta map, so this is "implemented".
    _found, summary = csp.analyze(None, None, ["report-uri /a; report-uri /b"])
    assert summary["verdict"] != "csp-missing"


def test_hostile_meta_values_are_ignored():  # F10
    found, summary = csp.analyze(None, None, [None, 5, {"x": 1}, "default-src 'none'"])
    assert summary["verdict"] == "csp-default-none"
    found, summary = csp.analyze(None, None, "not a list")
    assert summary["verdict"] == "csp-missing"


# --- headers ----------------------------------------------------------------------------

@pytest.mark.parametrize("value, fid, score", [
    (None, "hsts-missing", -20),
    ("", "hsts-invalid", -20),
    ("max-age=15552000", "hsts-ok", 0),                                    # F2: the threshold itself passes
    ("max-age=15551999", "hsts-short", -10),
    ("max-age=31536000, max-age=31536000", "hsts-invalid", -20),           # F2: sent twice
    ("includeSubDomains", "hsts-invalid", -20),                            # no max-age
    ("max-age=abc", "hsts-ok", 0),                                         # parseInt NaN is "not less"
    ("max-age=63072000; includeSubDomains; preload", "hsts-ok", 0),        # F14: no preload-list lookup, no +5
])
def test_hsts_case_to_score(value, fid, score):
    found = headers.analyze({} if value is None else {"strict-transport-security": value}, https=True)
    hsts = [f for f in found if f["id"].startswith("hsts-") and f["id"] not in {"hsts-no-subdomains", "hsts-preload-ready"}]
    assert [f["id"] for f in hsts] == [fid] and observatory_points(hsts) == score


def test_hsts_needs_https_and_a_trusted_certificate():
    assert ids(headers.analyze({}, https=False))["hsts-no-https"]["points"] == -20
    assert ids(headers.analyze({"strict-transport-security": "max-age=31536000"}, https=True,
                               verified=False))["hsts-invalid-cert"]["points"] == -20
    ready = ids(headers.analyze({"strict-transport-security": "max-age=63072000; includeSubDomains; preload"},
                                https=True))
    assert ready["hsts-preload-ready"]["points"] == 0 and "not looked up" in ready["hsts-preload-ready"]["detail"]


@pytest.mark.parametrize("value, frame_any, fid, score", [
    (None, False, "framing-missing", -20),
    ("", False, "framing-missing", -20),                                   # empty = not implemented
    ("DENY", False, "framing-xfo", 5),
    (" sameorigin ", False, "framing-xfo", 5),
    ("ALLOW-FROM https://a.test", False, "framing-allow-from", 0),
    ("DENY, DENY", False, "framing-invalid", -20),
    (None, True, "framing-csp", 5),                                        # F8: frame-ancestors in the merged CSP
    ("bogus", True, "framing-csp", 5),
])
def test_x_frame_options_case_to_score(value, frame_any, fid, score):
    found = ids(headers.analyze({} if value is None else {"x-frame-options": value}, https=True,
                                csp_frame_ancestors=frame_any))
    assert found[fid]["points"] == score


@pytest.mark.parametrize("value, score", [
    (None, -5), ("", -5), ("nosniff", 0), (" NoSniff ", 0), ("nosniff, nosniff", -5),
])
def test_x_content_type_options_case_to_score(value, score):
    found = headers.analyze({} if value is None else {"x-content-type-options": value}, https=True)
    assert [f["points"] for f in found if f["id"].startswith("xcto-")] == [score]


@pytest.mark.parametrize("header, meta, fid, score", [
    (None, None, "referrer-default", 0),
    ("", None, "referrer-invalid", -5),                                    # an empty header is sent, and invalid
    ("no-referrer", None, "referrer-private", 5),
    ("unsafe-url, strict-origin", None, "referrer-private", 5),            # the last valid token wins
    ("strict-origin, bogus", None, "referrer-private", 5),
    ("no-referrer", "unsafe-url", "referrer-unsafe", -5),                  # F14: the meta tag comes last
    (None, "same-origin", "referrer-private", 5),
    ("no-referrer-when-downgrade", None, "referrer-unsafe", -5),
    ("bogus", None, "referrer-invalid", -5),
])
def test_referrer_policy_case_to_score(header, meta, fid, score):
    found = ids(headers.analyze({} if header is None else {"referrer-policy": header}, https=True,
                                meta_referrer=meta))
    assert found[fid]["points"] == score


@pytest.mark.parametrize("name, value, fid, score", [
    ("cross-origin-opener-policy", None, "coop-missing", 0),
    ("cross-origin-opener-policy", "", "coop-missing", 0),
    ("cross-origin-opener-policy", "same-origin", "coop-ok", 10),          # F1
    ("cross-origin-opener-policy", "same-origin-allow-popups", "coop-ok", 10),
    ("cross-origin-opener-policy", "noopener-allow-popups", "coop-ok", 10),
    ("cross-origin-opener-policy", 'same-origin; report-to="coop"', "coop-ok", 10),
    ("cross-origin-opener-policy", "unsafe-none", "coop-unsafe-none", 0),
    ("cross-origin-opener-policy", "same-origin, same-origin", "coop-invalid", -5),  # F1: sent twice
    ("cross-origin-opener-policy", '"same-origin"', "coop-invalid", -5),   # a string, not a token
    ("cross-origin-opener-policy", 'same-origin; report-to="x', "coop-invalid", -5),  # broken parameter
    ("cross-origin-opener-policy", "bogus", "coop-invalid", -5),
    ("cross-origin-embedder-policy", "require-corp", "coep-ok", 10),
    ("cross-origin-embedder-policy", "credentialless", "coep-ok", 10),
    ("cross-origin-embedder-policy", "unsafe-none", "coep-unsafe-none", 0),
    ("cross-origin-embedder-policy", "require-corp, require-corp", "coep-invalid", -5),
    ("cross-origin-embedder-policy", None, "coep-missing", 0),
    ("cross-origin-resource-policy", None, "corp-missing", 0),
    ("cross-origin-resource-policy", "", "corp-missing", 0),
    ("cross-origin-resource-policy", "same-origin", "corp-ok", 10),
    ("cross-origin-resource-policy", " Same-Site ", "corp-ok", 10),
    ("cross-origin-resource-policy", "cross-origin", "corp-missing", 0),
    ("cross-origin-resource-policy", "same-origin, same-origin", "corp-invalid", -5),
])
def test_cross_origin_headers_case_to_score(name, value, fid, score):
    found = ids(headers.analyze({} if value is None else {name: value}, https=True))
    assert found[fid]["points"] == score


# --- cookies ----------------------------------------------------------------------------

@pytest.mark.parametrize("lines, hsts, fid, score", [
    ([], False, "cookies-none", 0),
    (["SESSIONID=x; Path=/; Secure; HttpOnly"], False, "cookies-secure-no-samesite", 0),
    (["SESSIONID=x; Path=/; Secure; HttpOnly; SameSite=Lax", "ui=1; Secure; SameSite=None"], False,
     "cookies-secure", 5),                                                 # F4: every cookie has SameSite
    (["SESSIONID=x; Secure; HttpOnly; SameSite=Lax", "ui=1; Secure"], False, "cookies-secure-no-samesite", 0),
    (["CSRFTOKEN=x; Secure; HttpOnly"], False, "cookies-anticsrf-no-samesite", -20),
    (["SESSIONID=x; Secure; HttpOnly; SameSite="], False, "cookies-samesite-invalid", -20),
    (["SESSIONID=x; Secure; HttpOnly; SameSite=True"], False, "cookies-samesite-invalid", -20),
    (["ui=1; SameSite=None"], False, "cookies-samesite-invalid", -20),     # F4: None without Secure
    (["ui=1; SameSite=None"], True, "cookies-samesite-invalid", -20),      # worse than "no Secure, HSTS"
    (["foo=bar"], True, "cookies-no-secure-hsts", -5),
    (["foo=bar"], False, "cookies-no-secure", -20),
    (["SESSIONID=x; HttpOnly"], True, "cookies-session-no-secure-hsts", -10),
    (["SESSIONID=x; Secure"], False, "cookies-session-no-httponly", -30),
    (["SESSIONID=x"], False, "cookies-session-no-secure", -40),
    (["heroku-session-affinity=x"], False, "cookies-none", 0),             # Observatory ignores this one
    (["__Host-sid=x; Path=/"], False, "cookies-none", 0),                 # a jar drops a broken prefix
    (["__Secure-sid=x"], False, "cookies-none", 0),
    (["wide=x; Domain=com"], False, "cookies-none", 0),                    # a public-suffix Domain is refused
    (["other=x; Domain=other.test"], False, "cookies-none", 0),            # a foreign Domain is refused
    (["a=1", "a=1; Secure; SameSite=Lax"], False, "cookies-secure", 5),   # the last Set-Cookie of a name wins
])
def test_cookie_case_to_score(lines, hsts, fid, score):
    jar = [c for c in (cookies.parse_set_cookie(line) for line in lines) if c]
    found = cookies.analyze(jar, host="www.example.com", https=True, hsts=hsts, raw_lines=lines, graded_lines=lines)
    assert ids(found)[fid]["points"] == score and observatory_points(found) == score


def test_only_the_final_responses_raw_lines_are_read_for_samesite():
    # An invalid SameSite on a redirect hop reaches the jar as "no SameSite", not as invalid.
    found = cookies.analyze([], host="example.com", https=True, raw_lines=[],
                            graded_lines=["sid=1; Secure; HttpOnly; SameSite=Bogus"])
    assert "cookies-secure-no-samesite" in ids(found)


# --- SRI (on the served HTML) ------------------------------------------------------------

def _sri(html, url="https://www.example.com/", content_type="text/html; charset=utf-8"):
    served = page.snapshot_from_html(html, url)
    return page.sri(served, "www.example.com", url.startswith("https:"), content_type)


@pytest.mark.parametrize("html, fid, score", [
    ("<p>no scripts</p>", "sri-no-scripts", 0),
    ("<script>inline()</script>", "sri-own-scripts", 0),
    ('<script src="/app.js"></script>', "sri-own-scripts", 0),
    ('<script src="https://cdn.example.com/a.js"></script>', "sri-own-scripts", 0),  # same registrable domain
    ('<script src="/app.js" integrity="sha384-x"></script>', "sri-all-secure", 5),
    ('<script src="https://cdn.other.test/a.js" integrity="sha384-x"></script>', "sri-external-secure", 5),
    ('<script src="https://cdn.other.test/a.js"></script>', "sri-missing", -5),
    ('<script src="http://cdn.other.test/a.js" integrity="sha384-x"></script>', "sri-insecure", -20),
    ('<script src="http://cdn.other.test/a.js"></script>', "sri-missing-insecure", -50),
    ('<script src="//cdn.other.test/a.js"></script>', "sri-missing-insecure", -50),         # F5 protocol-relative
    ('<script src="//cdn.example.com/a.js" integrity="sha384-x"></script>', "sri-insecure", -20),  # F5 even own
    # F5 onlyIfWorse: the worst result stays, whatever the order
    ('<script src="http://cdn.other.test/a.js"></script><script src="https://cdn.other.test/b.js"></script>',
     "sri-missing-insecure", -50),
    ('<script src="https://cdn.other.test/b.js"></script><script src="http://cdn.other.test/a.js"></script>',
     "sri-missing-insecure", -50),
    # own SRI earns the bonus only while nothing worse was found first
    ('<script src="https://cdn.other.test/b.js"></script><script src="/a.js" integrity="sha384-x"></script>',
     "sri-missing", -5),
])
def test_sri_case_to_score(html, fid, score):
    result = _sri(html)
    assert (result["id"], result["points"]) == (fid, score)


def test_sri_skips_non_html_and_relative_scripts_on_http():
    assert _sri('<script src="http://x.test/a.js"></script>', content_type="application/json")["id"] == "sri-not-html"
    # Observatory compares the media type as sent: "Text/HTML" is not in its list.
    assert _sri('<script src="http://x.test/a.js"></script>', content_type="Text/HTML")["id"] == "sri-not-html"
    assert _sri('<script src="http://x.test/a.js"></script>', content_type=None)["id"] == "sri-missing-insecure"
    assert _sri('<script src="/a.js" integrity="sha384-x"></script>', url="http://www.example.com/")["id"] \
        == "sri-own-scripts"  # a relative script on http is not "loaded securely"


# --- redirection --------------------------------------------------------------------------

def _route(*urls, **extra):
    return {"url": urls[0], "route": list(urls), "final_url": urls[-1], "status": 200, "hops": [], **extra}


@pytest.mark.parametrize("probe, fid, score", [
    (None, "redirect-not-checked", 0),
    ({"url": "http://example.com/", "error": "ConnectionError: refused"}, "redirect-no-http", 0),
    ({"url": "http://example.com/", "error": "SSLError: [SSL: CERTIFICATE_VERIFY_FAILED]"}, "redirect-invalid-cert", -20),
    (_route("http://example.com/"), "redirect-missing", -20),
    (_route("http://example.com/", "http://example.com/x"), "redirect-not-https", -20),
    (_route("http://example.com/", "http://example.com/x", "https://example.com/"), "redirect-initial-http", -10),
    (_route("http://example.com/", "https://www.example.com/"), "redirect-off-host", -5),   # F6 route[0] vs [1]
    (_route("http://example.com/", "https://example.com/", "https://www.example.com/"), "redirect-ok", 0),
    (_route("http://example.com/", not_followed="https://login.other.test/"), "redirect-off-host", -5),
])
def test_redirection_case_to_score(probe, fid, score):
    found = [f for f in transport.analyze_redirect("https://example.com/", probe) if f["id"].startswith("redirect-")]
    assert [(f["id"], f["points"]) for f in found] == [(fid, score)]


# --- grading ------------------------------------------------------------------------------

@pytest.mark.parametrize("score, letter", [
    (165, "A+"), (100, "A+"), (99, "A"), (95, "A"), (90, "A"), (89, "A-"), (85, "A-"), (84, "B+"), (80, "B+"),
    (79, "B"), (70, "B"), (69, "B-"), (65, "B-"), (64, "C+"), (60, "C+"), (59, "C"), (50, "C"), (49, "C-"),
    (45, "C-"), (44, "D+"), (40, "D+"), (39, "D"), (30, "D"), (29, "D-"), (25, "D-"), (24, "F"), (0, "F"),
])
def test_grade_letters_match_the_chart(score, letter):
    assert grading.letter(score) == letter


def test_bonuses_need_a_90_base_and_the_score_floors_at_zero():
    def item(points):
        return {"id": f"p{points}", "points": points, "title": "t", "status": "pass" if points > 0 else "fail",
                "severity": "info" if points > 0 else "high"}
    assert grading.grade([item(-10), item(10)])["score"] == 100  # 90 base: the bonus counts
    assert grading.grade([item(-15), item(10)])["score"] == 85   # 85 base: it does not
    assert grading.grade([item(-50), item(-50), item(-50)])["score"] == 0


# --- the whole score ----------------------------------------------------------------------

def _view(page_headers, *, cookies_lines=(), body="<html><head></head><body></body></html>", status=200,
          content_type="text/html; charset=utf-8"):
    head = {"content-type": content_type, **page_headers} if content_type else dict(page_headers)
    return {
        "page": {"url": "https://example.com/", "status": status, "final_url": "https://example.com/", "headers": head,
                 "set_cookies": list(cookies_lines), "final_set_cookies": list(cookies_lines), "body": body},
        "redirect_probe": _route("http://example.com/", "https://example.com/"),
        "tls": {"valid": True, "days_left": 90},
        "files": {"security_txt": {"status": 404}, "robots_txt": {"status": 404}},
        "requests_made": ["GET https://example.com/"],
    }


MAXIMAL = {
    "content-security-policy": "default-src 'none'; script-src 'self'; frame-ancestors 'none'; form-action 'self'",
    "strict-transport-security": "max-age=63072000; includeSubDomains; preload",
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin", "cross-origin-embedder-policy": "require-corp",
    "cross-origin-resource-policy": "same-origin",
}


@pytest.mark.parametrize("extra, lines, body, score", [
    # every bonus Observatory can give without a preload-list lookup:
    # 100 + CSP 10 + cookies 5 + COEP 10 + COOP 10 + Referrer 5 + SRI 5 + XFO 5 + CORP 10
    ({}, ["__Host-sid=1; Path=/; Secure; HttpOnly; SameSite=Strict"],
     '<script src="/a.js" integrity="sha384-x"></script>', 160),
    ({}, [], "<p></p>", 150),                                               # no cookies, no scripts: no +5 each
    ({"cross-origin-embedder-policy": "bogus"}, [], "<p></p>", 135),         # F1: +10 becomes -5 (95 + 40)
    ({"x-content-type-options": "nope"}, [], "<p></p>", 145),                # 95 base, bonuses still count
    ({"strict-transport-security": "max-age=600"}, [], "<p></p>", 140),      # 90 base: bonuses still count
    ({"strict-transport-security": "max-age=600", "x-content-type-options": "nope"}, [], "<p></p>", 85),  # 85: none
])
def test_whole_report_scores(extra, lines, body, score):
    built = report.build("https://example.com/", _view({**MAXIMAL, **extra}, cookies_lines=lines, body=body))
    assert built["score"] == score, built["score_explanation"]
    assert built["grade"] == grading.letter(score)


def test_the_hsts_preload_bonus_is_never_given():
    # Observatory's maximum is 165; the last +5 needs the site in the browsers' HSTS preload list.
    built = report.build("https://example.com/", _view(MAXIMAL, cookies_lines=["__Host-s=1; Path=/; Secure; HttpOnly; "
                                                                                 "SameSite=Lax"],
                                                        body='<script src="/a.js" integrity="sha384-x"></script>'))
    assert built["score"] == 160 and ids(built["findings"])["hsts-preload-ready"]["points"] == 0


@pytest.mark.parametrize("status, graded", [(200, True), (301, True), (401, True), (403, True), (404, False),
                                            (500, False), (199, False)])
def test_only_2xx_3xx_401_403_are_graded(status, graded):  # F14
    built = report.build("https://example.com/", _view({}, status=status))
    assert (built["grade"] is not None) is graded
    if not graded:
        assert "not_graded" in built and built["findings"]


def test_meta_referrer_counts_and_meta_corp_does_not():  # F14
    body = ('<html><head><meta name="referrer" content="no-referrer">'
            '<meta http-equiv="Cross-Origin-Resource-Policy" content="same-origin"></head></html>')
    found = ids(report.build("https://example.com/", _view({}, body=body))["findings"])
    assert found["referrer-private"]["points"] == 5 and "corp-missing" in found


def test_meta_policies_need_an_html_content_type():
    body = '<html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"></head></html>'
    assert ids(report.build("https://example.com/", _view({}, body=body))["findings"])["csp-default-none"]
    no_type = ids(report.build("https://example.com/", _view({}, body=body, content_type=None))["findings"])
    assert "csp-missing" in no_type  # Observatory parses <meta> only for text/html and application/xhtml+xml


def test_frame_ancestors_in_a_meta_policy_still_earns_the_x_frame_options_credit():  # F8
    body = ('<html><head><meta http-equiv="Content-Security-Policy" '
            'content="default-src \'self\'; frame-ancestors \'none\'"></head></html>')
    found = ids(report.build("https://example.com/", _view({}, body=body))["findings"])
    assert found["framing-csp"]["points"] == 5
    assert "browsers ignore" in found["csp-frame-ancestors-missing"]["detail"]  # and the report says it is inert


def test_redirect_probe_and_hsts_read_cookies_are_not_graded():
    view = _view(MAXIMAL)
    view["redirect_probe"]["set_cookies"] = ["tracker=1"]  # set by http://host/ - another session in Observatory
    found = ids(report.build("https://example.com/", view)["findings"])
    assert "cookies-none" in found and "cookies-no-secure" not in found
