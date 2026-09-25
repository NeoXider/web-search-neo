"""Assemble the security report for one page: collect what the host serves, grade it.

``Checker`` sends every request the report makes outside the browser, only to
URLs its ``Scope`` allows and within its ``Budget``; each request, redirect
hops included, is recorded (redacted) for ``requests_made``. ``build`` is pure:
it reads the collected view (and the browser's, when there is one), which is
what lets the tests pin every verdict to a fixture without a network or Chrome.

A local development address (loopback, private networks, ``*.localhost`` and
the other private-use names) is graded in development mode: https, HSTS, the
http redirect, the certificate, cookie Secure flags, mixed content and forms
over http are "not applicable in dev",
the headline grade covers the rest, and ``production_forecast`` shows the grade
the same responses would get served over https with HSTS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from web_search_neo.audit import cookies as cookie_checks
from web_search_neo.audit import csp, grading, headers as header_checks, page as page_checks
from web_search_neo.audit import transport, wellknown
from web_search_neo.audit.findings import finding, info, passed
from web_search_neo.audit.scope import Budget, Scope, Target, redacted, scrub
from web_search_neo.audit.sites import host_of, site_of
from web_search_neo.web_client import is_local_host

SHOWN_HEADERS = (
    "content-security-policy", "content-security-policy-report-only", "strict-transport-security",
    "x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy",
    "cross-origin-opener-policy", "cross-origin-embedder-policy", "cross-origin-resource-policy",
    "access-control-allow-origin", "access-control-allow-credentials", "server", "x-powered-by",
    "x-aspnet-version", "x-xss-protection", "content-type",
)
GRADED_STATUS = "2xx, 3xx, 401 or 403"
# In development mode these only restate "this is not https"; they are skipped there.
DEV_NOT_APPLICABLE = frozenset({
    "https-missing", "hsts-no-https", "hsts-missing", "hsts-invalid", "hsts-short", "hsts-invalid-cert",
    "redirect-missing", "redirect-not-https", "redirect-initial-http", "redirect-off-host", "redirect-invalid-cert",
    "tls-invalid", "tls-untrusted-page", "password-over-http", "form-insecure-action", "mixed-content-active", "mixed-content-passive",
})
SCOPE_TEXT = ("Passive: the report's own plain GETs (requests_made) only to hosts in scope, one verifying TLS "
              "handshake per https host, plus one ordinary page load in an isolated browser (browser_requests). "
              "No probing, path guessing or fuzzing.")


@dataclass
class Checker:
    """Sends the report's own requests: in scope, within budget, every one recorded."""

    scope: Scope
    budget: Budget
    timeout: float
    fetch: Callable[..., dict[str, Any]] = transport.fetch
    certificate: Callable[..., dict[str, Any]] = transport.certificate
    handshakes: dict[str, Any] = field(default_factory=dict)
    untrusted: dict[str, str] = field(default_factory=dict)
    origins: dict[tuple[str, bool], dict[str, Any]] = field(default_factory=dict)

    def facts(self, url: str, *, probe_http: bool = True) -> dict[str, Any]:
        """The shared facts of ``url``'s origin, collected once per origin (``collect_origin``).

        A page of another host in the scope (a subdomain, a second named host)
        gets its own redirect probe, TLS handshake and well-known files - never
        the start origin's.
        """
        key = (origin_of(url), probe_http if urlsplit(url).scheme == "https" else True)
        if key not in self.origins:
            self.origins[key] = collect_origin(self, url, probe_http=key[1])
        return self.origins[key]

    def get(self, url: str, *, small: bool = False) -> dict[str, Any]:
        """One GET in scope and budget; after a certificate error, the one unverified retry.

        As Observatory's session does, an https origin whose certificate failed
        once is read without verification from then on (``untrusted``); each such
        answer carries ``verified: false`` and the ``certificate_error``.
        """
        if not self.scope.allows(url):
            return {"url": url, "route": [url], "error": "outside the scope: not requested", "skipped": "scope"}
        parts = urlsplit(url)
        origin = f"{parts.hostname}:{parts.port or 443}"
        known = self.untrusted.get(origin) if parts.scheme == "https" else None
        answer = self._send(url, small, known is None)
        error = answer.get("error")
        if known is None and parts.scheme == "https" and error and not answer.get("skipped") \
                and transport.is_certificate_error(error):
            self.untrusted[origin] = known = str(error)
            answer = self._send(url, small, False)
        if known is not None and not answer.get("skipped"):
            answer["certificate_error"] = known
        return answer

    def request(self, method: str, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """One non-GET request in scope and budget (an OPTIONS preflight, a TRACE probe).

        ``method`` is "OPTIONS" or "TRACE" - the only two ``active_probe`` sends.
        """
        if method not in {"OPTIONS", "TRACE"}:
            return {"url": url, "error": f"refused method {method}: only OPTIONS and TRACE are probed"}
        if not self.scope.allows(url):
            return {"url": url, "error": "outside the scope: not requested", "skipped": "scope"}
        label = f"{method} {redacted(url)}"
        if not self.budget.take(label):
            return {"url": url, "error": "request budget exhausted: not requested", "skipped": "budget"}
        answer = transport.options(url, self.timeout, method, headers,
                                   allow_plain_http=True, plain_http_hosts=self.scope.allows)
        error = answer.get("error")
        if urlsplit(url).scheme == "https" and error and transport.is_certificate_error(error):
            self.budget.take(f"{label} (certificate not verified)")
            answer = transport.options(url, self.timeout, method, headers,
                                       allow_plain_http=True, plain_http_hosts=self.scope.allows,
                                       verify=False)
        return answer

    def _send(self, url: str, small: bool, verify: bool) -> dict[str, Any]:
        if not self.budget.take(f"GET {redacted(url)}" + ("" if verify else " (certificate not verified)")):
            return {"url": url, "route": [url], "error": "request budget exhausted: not requested", "skipped": "budget"}

        refusal: dict[str, str] = {}

        def follow(target: str) -> bool:
            if not self.scope.allows(target):
                refusal["why"] = "scope"
                return False
            if not self.budget.take(f"GET {redacted(target)} (redirect)"):
                refusal["why"] = "budget"
                return False
            return True

        answer = self.fetch(url, self.timeout, allow_plain_http=True,
                            max_bytes=transport.SMALL_BYTES if small else transport.PAGE_BYTES,
                            plain_http_hosts=self.scope.allows, follow=follow, verify=verify)
        stopped = answer.get("not_followed")
        if stopped and answer.get("not_followed_reason") and not self.scope.allows(stopped):
            answer["not_followed_reason"] = "outside the scope"  # it would not have been followed anyway
        elif stopped and answer.get("not_followed_reason"):
            # Admitted by the scope and the budget, then refused by the client's own rules.
            label = f"GET {redacted(stopped)} (redirect)"
            if self.budget.made and self.budget.made[-1] == label:
                self.budget.made[-1] = f"GET {redacted(stopped)} (redirect refused, not sent)"
        elif stopped:
            answer["not_followed_reason"] = ("request budget exhausted" if refusal.get("why") == "budget"
                                             else "outside the scope")
        return answer

    def tls(self, host: str, port: int) -> dict[str, Any] | None:
        key = f"{host}:{port}"
        if key not in self.handshakes:
            if not self.budget.take(f"TLS handshake {key}"):
                return None
            self.handshakes[key] = self.certificate(host, port, self.timeout)
        return self.handshakes[key]


def default_scope(url: str, include_subdomains: bool = False) -> Scope:
    parts = urlsplit(url)
    return Scope((Target(host=(parts.hostname or "").lower(), port=parts.port),), include_subdomains)


def origin_of(url: str) -> str:
    """scheme://host[:port] - never the caller's user:password."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")


def collect_origin(checker: Checker, url: str, *, probe_http: bool = True) -> dict[str, Any]:
    """The facts shared by every page of one origin: redirect probe, HSTS source, TLS, well-known files."""
    requested = urlsplit(url)
    host = (requested.hostname or "").lower()
    view: dict[str, Any] = {}
    if requested.scheme == "https":
        variant = transport.http_variant(url) if probe_http else None
        view["redirect_probe"] = checker.get(variant, small=True) if variant else None
        view["tls"] = checker.tls(host, requested.port or 443)
    else:
        variant = transport.https_variant(url)
        if variant:
            probe = checker.get(variant, small=True)
            if not probe.get("error"):
                view["https_probe"] = probe  # verified: false after a certificate error (the unverified read)
                view["tls"] = checker.tls(host, 443)
            elif probe.get("certificate_error") or transport.is_certificate_error(probe["error"]):
                view["https_probe_cert_error"] = probe.get("certificate_error") or probe["error"]
                view["tls"] = checker.tls(host, 443)
    view["files"] = wellknown.read(origin_of(url), lambda target: checker.get(target, small=True))
    return view


def collect_page(checker: Checker, url: str) -> dict[str, Any]:
    """One page as served; after a certificate error, one unverified retry (as Observatory)."""
    return checker.get(url)


def collect_http(url: str, timeout: float, *, scope: Scope | None = None, budget: Budget | None = None,
                 fetch: Callable[..., dict[str, Any]] = transport.fetch,
                 certificate: Callable[..., dict[str, Any]] = transport.certificate,
                 origin: dict[str, Any] | None = None, checker: Checker | None = None) -> dict[str, Any]:
    """Everything one page's report reads without a browser, with the list of requests sent.

    ``origin`` reuses the shared facts of an origin already collected (a crawl);
    ``checker`` is the one a multi-page check shares, so the budget, the TLS
    handshakes and the untrusted-certificate memory span every page.
    """
    if checker is None:
        budget = budget if budget is not None else Budget()
        checker = Checker(scope or default_scope(url), budget, timeout, fetch, certificate)
    budget = checker.budget
    blocked = transport.destination_error(url)
    if blocked:
        return {"error": blocked, "requests_made": budget.made}
    page = collect_page(checker, url)
    view: dict[str, Any] = {"page": page, "requests_made": budget.made, "scope": checker.scope}
    if page.get("error"):
        view["error"] = page["error"]
        return view
    final = page.get("final_url") or url
    if not checker.scope.allows(final):
        final = url  # never gather origin facts (TLS, well-known files) from a host outside the scope
    if origin is not None:
        view.update(origin)
    else:
        view.update(checker.facts(final if urlsplit(final).scheme == "https" else url,
                                  probe_http=urlsplit(url).scheme != "http"))
    if urlsplit(url).scheme == "http":
        view["redirect_probe"] = page  # the page request itself started on http: its route is the check
    return view


def browser_requests(network: list[dict[str, Any]] | None, page_url: str,
                     left_scope: str | None = None) -> dict[str, Any] | None:
    """What the ordinary page load in the browser requested (the page itself included)."""
    if network is None:
        return None
    hosts: dict[str, int] = {}
    for row in network:
        host = host_of(str(row.get("url") or "")) if isinstance(row, dict) else ""
        if host:
            hosts[host] = hosts.get(host, 0) + 1
    own = site_of(host_of(page_url))
    out = {"count": len(network), "hosts": dict(sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))[:50]),
           "other_sites": sorted({site_of(h) for h in hosts if site_of(h) != own})[:50],
           "note": "The page load itself, as any visit makes it: the page, its subresources and what its "
                   "scripts fetch. Not sent by the report."}
    if left_scope:
        out["left_scope"] = left_scope
        out["note"] += (" The browser left the scope (a redirect or a script navigated it); the requests after "
                        "that are the other site's, and its page was not analysed.")
    return out


def gradeable(status: Any) -> bool:
    return isinstance(status, int) and (200 <= status < 400 or status in (401, 403))


@dataclass
class _Context:
    url: str
    final: str
    host: str
    https: bool
    page: dict[str, Any]
    view: dict[str, Any]
    served: dict[str, Any]
    snapshot: dict[str, Any]
    network: list[dict[str, Any]] | None
    jar: list[dict[str, Any]]
    html: bool
    jar_lines: list[str] = field(default_factory=list)


def _hsts_source(ctx: _Context) -> tuple[bool, bool, str | None]:
    """(https available, certificate verified, the https response's Strict-Transport-Security)."""
    if ctx.https:
        return True, ctx.page.get("verified", True) is not False, (ctx.page.get("headers") or {}).get(
            "strict-transport-security")
    probe = ctx.view.get("https_probe")
    if probe:
        return True, probe.get("verified", True) is not False, (probe.get("headers") or {}).get(
            "strict-transport-security")
    if ctx.view.get("https_probe_cert_error"):
        return True, False, None
    return False, True, None


def _findings(ctx: _Context, *, forecast: bool = False, dev: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every finding for one page. ``forecast``: as if served over https with HSTS (the dev forecast)."""
    headers = ctx.page.get("headers") or {}
    https = True if forecast else ctx.https
    findings: list[dict[str, Any]] = []
    if forecast:
        findings.append(passed("redirect-ok", "transport", "Assumed: http:// redirects to https:// on the same host"))
        available, verified, hsts_value = True, True, "max-age=31536000"
    else:
        findings += transport.analyze_redirect(ctx.final, ctx.view.get("redirect_probe"),
                                               requested_host=urlsplit(ctx.url).hostname)
        findings += transport.analyze_certificate(ctx.view.get("tls"))
        available, verified, hsts_value = _hsts_source(ctx)
    meta_csp = ctx.served.get("meta_csp") if ctx.html else []
    csp_findings, csp_summary = csp.analyze(headers.get("content-security-policy"),
                                            headers.get("content-security-policy-report-only"), meta_csp, https=https)
    header_findings = header_checks.analyze(
        headers, https=available, verified=verified, hsts=hsts_value, frame_ancestors=csp_summary["frame_ancestors"],
        csp_frame_ancestors=csp_summary["frame_ancestors_any"],
        meta_referrer=ctx.served.get("meta_referrer") if ctx.html else None)
    findings += header_findings + csp_findings
    findings += cookie_checks.analyze(ctx.jar, host=ctx.host, https=https,
                                      hsts=header_checks.hsts_passes(header_findings),
                                      raw_lines=_final_cookie_lines(ctx.page), graded_lines=ctx.jar_lines,
                                      ignore_secure=dev and not forecast)
    content_type = headers.get("content-type")
    try:
        page_findings, parties = page_checks.analyze(ctx.snapshot, ctx.final, csp_summary, ctx.network,
                                                     served=ctx.served, named_host=ctx.host, content_type=content_type)
    except Exception:  # a snapshot shaped in a way nobody foresaw must not sink the report
        page_findings, parties = page_checks.analyze(ctx.served, ctx.final, csp_summary, None, served=ctx.served,
                                                     named_host=ctx.host, content_type=content_type)
        csp_summary["browser_snapshot_failed"] = True
    return findings + page_findings, {"csp": csp_summary, "parties": parties}


def _final_cookie_lines(page: dict[str, Any]) -> list[str]:
    """The final response's own Set-Cookie lines (Observatory's raw SameSite check reads only those)."""
    own = page.get("final_set_cookies")
    return list(own if isinstance(own, list) else page.get("set_cookies") or [])


def _mark_dev(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in findings:
        if item["id"] in DEV_NOT_APPLICABLE and item["status"] in {"fail", "warn"}:
            item = {**item, "status": "skip", "points": 0, "extra_points": 0,
                    "detail": (item.get("detail", "") + " Not applicable to a local development server - "
                               "check it on the https deployment before production.").strip()}
        out.append(item)
    return out


def _jar_lines(view: dict[str, Any]) -> list[str]:
    """Set-Cookie lines of Observatory's graded jar: the page request, its redirects included.

    Observatory v1.7.1 reads robots.txt through a client without the cookie jar,
    and the http:// redirect probe and the https:// HSTS read are other sessions:
    none of their cookies are graded.
    """
    page = view.get("page") or {}
    return list(page.get("set_cookies") or [])


def _notes(page: dict[str, Any], tls: dict[str, Any] | None) -> list[dict[str, Any]]:
    notes = []
    why = page.get("not_followed_reason") or "outside the scope"
    evidence = {"location": redacted(str(page.get("not_followed") or "")), "reason": why}
    if page.get("not_followed") and why == "outside the scope":
        notes.append(info("redirect-out-of-scope", "transport", "The page redirected outside the scope",
                          detail="The redirect was not followed; the redirect response itself is graded. Add the "
                                 "target host to scope (hosts, or include_subdomains) to check it.",
                          evidence=evidence))
    elif page.get("not_followed") and why == "request budget exhausted":
        notes.append(info("redirect-over-budget", "transport", "The redirect was not followed: request budget used up",
                          detail="The report's request budget ran out before this hop; the redirect response "
                                 "itself is graded. Check fewer pages per call.", evidence=evidence))
    elif page.get("not_followed"):
        notes.append(info("redirect-refused", "transport", "The redirect was not followed",
                          detail="The HTTP client refuses this hop (plain http to a public host that is not in the "
                                 "scope, or a public-to-private redirect); the redirect response itself is graded.",
                          evidence=evidence))
    if page.get("certificate_error") and not (tls and tls.get("valid") is False):
        notes.append(finding("tls-untrusted-page", "transport", "fail", "high",
                             "The page's TLS certificate is not trusted",
                             detail="Browsers show a full-page warning instead of the site. The page was read "
                                    "once more without verification, as Observatory does, to grade the rest.",
                             fix="Install a certificate for this exact host name from a public CA (with the full "
                                 "chain) and automate renewal; a self-signed one is fine only for local development.",
                             evidence=page["certificate_error"][:300]))
    return notes


def build(url: str, http_view: dict[str, Any], browser_view: dict[str, Any] | None = None) -> dict[str, Any]:
    """The report; ``browser_view`` is ``{snapshot, cookies, network}`` or None.

    Every string of the answer is passed through ``scrub``: a URL quoted anywhere
    (a redirect route, an error, a Location) loses its userinfo and its
    sensitive query values.
    """
    return scrub(_build(url, http_view, browser_view))


def _build(url: str, http_view: dict[str, Any], browser_view: dict[str, Any] | None) -> dict[str, Any]:
    if http_view.get("error"):
        return {"success": False, "url": redacted(url), "error": f"The page could not be read: {http_view['error']}",
                "requests_made": list(http_view.get("requests_made", [])), "scope": SCOPE_TEXT}
    page = http_view["page"]
    browser_view = browser_view or {}
    final = page.get("final_url") or url
    headers = page.get("headers") or {}
    # Observatory's reading, character for character: <meta> policies only for an HTML
    # content type; the SRI test also parses a response without any content type.
    mime = page_checks.observatory_mime(headers.get("content-type"))
    html = mime in page_checks.HTML_TYPES
    served = page_checks.snapshot_from_html(page.get("body") or "" if html or not mime else "", final)
    snapshot = browser_view.get("snapshot")
    browser_note = None
    if snapshot is not None and not page_checks.usable(snapshot):
        snapshot, browser_note = None, "The page's own scripts broke the browser snapshot; page checks read the served HTML."
    scope = http_view.get("scope")
    left_scope = None
    if snapshot is not None and isinstance(scope, Scope) and not scope.allows(str(snapshot.get("url") or "")):
        # The browser ended on another site (a redirect or a script navigation): that page is not the one checked.
        left_scope = redacted(str(snapshot.get("url") or ""))
        snapshot, browser_note = None, (f"In the browser the page ended outside the scope ({left_scope}); page "
                                        "checks read the served HTML of the checked URL instead.")
    host = (urlsplit(url).hostname or "").lower()
    jar_lines = _jar_lines(http_view)
    header_jar = [c for c in (cookie_checks.parse_set_cookie(line) for line in jar_lines) if c]
    jar = cookie_checks.merge(header_jar, [cookie_checks.from_browser(c) for c in browser_view.get("cookies") or []
                                           if isinstance(c, dict)])
    ctx = _Context(url, final, host, urlsplit(final).scheme == "https", page, http_view, served,
                   snapshot or served, browser_view.get("network"), jar, html, jar_lines)
    dev = is_local_host(host)
    findings, extra = _findings(ctx, dev=dev)
    files = http_view.get("files") or {}
    security_findings, security_view = wellknown.analyze_security_txt(files.get("security_txt"))
    robots_findings, robots_view = wellknown.analyze_robots(files.get("robots_txt"))
    findings = _notes(page, http_view.get("tls")) + findings + security_findings + robots_findings
    graded = _mark_dev(findings) if dev else findings
    report = assemble(url, final, page.get("status"), graded, http_view.get("requests_made", []), dev=dev)
    report.update({
        "headers": {name: headers[name][:600] for name in SHOWN_HEADERS if name in headers},
        "cookies": [{key: c.get(key) for key in ("name", "secure", "httponly", "samesite", "domain", "path", "source")}
                    for c in jar],
        "csp": {key: extra["csp"].get(key) for key in ("source", "verdict", "directives", "inline_handlers_blocked")},
        "third_parties": extra["parties"],
        "redirect": {"route": [redacted(u) for u in (http_view.get("redirect_probe") or {}).get("route") or []]},
        "tls": http_view.get("tls"),
        "files": {"security_txt": security_view, "robots_txt": robots_view,
                  **({"not_followed": nf} if (nf := _files_not_followed(files)) else {})},
        "page_source": "browser" if snapshot is not None else "static-html",
        "browser_requests": browser_requests(browser_view.get("network"), final, left_scope),
    })
    if dev:
        forecast_findings, _ = _findings(ctx, forecast=True)
        scored = grading.grade(forecast_findings)
        report["production_forecast"] = {
            "score": scored["score"], "grade": scored["grade"],
            "assumes": "the same responses served over https with a valid certificate, HSTS "
                       "max-age=31536000 and an http->https redirect on the same host",
            "note": "A forecast only: before production, run security_report on the https deployment itself.",
        }
        report["as_served"] = {key: grading.grade(findings)[key] for key in ("score", "grade")}
        report["dev_note"] = ("Local development server: https, HSTS, the http redirect, the certificate and cookie "
                              "Secure flags are marked not applicable (status skip). Check them on https before "
                              "production.")
    if browser_view.get("error"):
        report["browser_error"] = browser_view["error"]
        browser_note = ("The isolated browser could not load the page, so page checks read the served HTML "
                        "(scripts added at runtime and loaded resources are not seen).")
    if browser_note or extra["csp"].get("browser_snapshot_failed"):
        report["browser_note"] = browser_note or "The browser snapshot could not be analysed; page checks read the served HTML."
    return report


def _files_not_followed(files: dict[str, Any]) -> list[str]:
    out = []
    for key in ("security_txt", "robots_txt"):
        item = files.get(key) or {}
        if isinstance(item, dict) and item.get("not_followed"):
            out.append(redacted(item["not_followed"]))
    return out


def assemble(url: str, final: str, status: Any, findings: list[dict[str, Any]], made: list[str],
             *, dev: bool) -> dict[str, Any]:
    """Grade, counts, priority and the summary line around a list of findings."""
    counts = grading.counts(findings)
    order = grading.priority(findings)
    report: dict[str, Any] = {"success": True, "url": redacted(url), "final_url": redacted(final), "status": status,
                              "mode": "local_development" if dev else "production"}
    if gradeable(status):
        scored = grading.grade(findings)
        report.update(grade=scored["grade"], score=scored["score"], score_explanation=scored["explanation"],
                      extended=scored["extended"])
        line = (f"{scored['grade']} ({scored['score']}/100{', local development' if dev else ''}; with this "
                f"report's own checks {scored['extended']['grade']})")
    else:
        report.update(grade=None, score=None, not_graded=(
            f"Observatory grades only responses with status {GRADED_STATUS}; this one answered {status}. "
            "The findings are listed without a grade."))
        line = f"not graded (status {status})"
    first = "; ".join(item["title"] for item in order[:3]) or "nothing to fix"
    report.update(counts=counts, summary_line=f"{line}: {counts['high']} high, {counts['medium']} medium, "
                                              f"{counts['low']} low. Fix first: {first}.",
                  priority=order, findings=findings, scope=SCOPE_TEXT, requests_made=list(made))
    return report
