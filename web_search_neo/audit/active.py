"""Bounded active probes: what a server answers when asked, nothing more.

``active_probe`` is the one action that sends requests beyond ordinary GETs -
OPTIONS preflights, a TRACE probe, plain GETs of the page's own links and one
inert query token - and every one of them stays inside the named scope, inside
the shared budget and inside ``requests_made``. This module never touches the
network: on the way in are the answers those requests got, on the way out the
findings with priority and fixes. No POST/PUT/DELETE, no payloads, no auth,
no fuzzing: the canary token is ``[a-z0-9]+`` and cannot execute anywhere.
"""

from __future__ import annotations

import secrets as _secrets
from typing import Any

from web_search_neo.audit import sites
from web_search_neo.audit.findings import clip_list, finding, info, passed
from web_search_neo.audit.api_parts import EVIDENCE_LIMIT, safe_url

# What a preflight claims to be when the caller names no origin.
PROBE_ORIGIN = "https://probe.example"
# The only "payload" this action ever sends: inert, random per call.
CANARY_PARAM = "wsncanary"
# Caps: a probe run stays a handful of requests, never a crawl.
MAX_LINKS = 20
MAX_CANARY = 8
MAX_PATHS = 50


def new_canary() -> str:
    """One inert token per run: lowercase hex, no HTML/JS meaning."""
    return "wsn" + _secrets.token_hex(4)


def preflight_findings(url: str, origin: str, headers: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the preflight answered a foreign origin: reflection, credentials, open methods."""
    out: list[dict[str, Any]] = []
    headers = headers if isinstance(headers, dict) else {}
    acao = str(headers.get("access-control-allow-origin") or "").strip()
    if not acao:
        return [passed("active-cors-closed", "cors", "No cross-origin access preflighted",
                       detail="The OPTIONS answer carries no Access-Control-Allow-Origin: browsers "
                              "keep the API same-origin.",
                       evidence={"url": safe_url(url)})]
    creds = str(headers.get("access-control-allow-credentials") or "").strip().lower() == "true"
    methods = str(headers.get("access-control-allow-methods") or "")
    heads = str(headers.get("access-control-allow-headers") or "")
    if acao == "null":
        out.append(finding(
            "active-cors-null", "cors", "fail", "high", "The API preflights the null origin",
            detail="Access-Control-Allow-Origin: null answers sandboxed iframes and data: documents.",
            fix="Allow only your own origin(s) explicitly; never 'null'.",
            evidence={"url": safe_url(url)}))
    elif acao == origin:
        if creds:
            out.append(finding(
                "active-cors-foreign-credentials", "cors", "fail", "high",
                "The API reflects a foreign origin with credentials",
                detail="A preflight from an origin you do not own is answered with that origin "
                       "plus Allow-Credentials: true: any site can make credentialed calls.",
                fix="Reflect only origins you own (or answer a fixed allowlist), and never "
                    "with credentials for origins you do not.",
                evidence={"url": safe_url(url), "origin": origin}))
        else:
            out.append(finding(
                "active-cors-foreign-origin", "cors", "warn", "medium",
                "The API reflects a foreign origin",
                detail="A preflight from an origin you do not own is answered with that origin: "
                       "any site reads whatever an anonymous caller sees.",
                fix="Answer a fixed allowlist of your own origins instead of reflecting.",
                evidence={"url": safe_url(url), "origin": origin}))
    elif acao == "*" and creds:
        out.append(finding(
            "active-cors-wildcard-credentials", "cors", "fail", "high",
            "The API preflights '*' together with credentials",
            detail="Browsers refuse the combination, so credentialed calls cannot work - and it "
                   "shows the API meant to answer everyone with the session attached.",
            fix="Answer the exact origin plus Allow-Credentials, or drop credentials; never '*'.",
            evidence={"url": safe_url(url)}))
    if methods.strip() == "*" or heads.strip() == "*":
        out.append(finding(
            "active-cors-methods-open", "cors", "warn", "medium",
            "The preflight allows any method or header",
            detail="Access-Control-Allow-Methods/Headers '*' lets a trusted origin send anything, "
                   "including custom headers your API may treat as privileged.",
            fix="List exactly the methods and headers your front end needs.",
            evidence={"url": safe_url(url), "methods": methods, "headers": heads}))
    return out


def methods_findings(url: str, options_status: int | None, allow: str,
                     trace_status: int | None) -> list[dict[str, Any]]:
    """OPTIONS/TRACE verdicts for one URL."""
    out: list[dict[str, Any]] = []
    if trace_status is not None and 200 <= trace_status < 300:
        out.append(finding(
            "active-trace-enabled", "transport", "warn", "medium", "TRACE answers with 2xx",
            detail="TRACE echoes the request back; behind a trusting proxy that assists "
                   "header theft (cross-site tracing). Modern browsers block it, proxies "
                   "do not always.",
            fix="Disable TRACE (and TRACK) on the server or filter it at the proxy.",
            evidence={"url": safe_url(url), "status": trace_status}))
    if allow.strip():
        out.append(info(
            "active-methods-allowed", "transport", "The server lists its methods",
            detail="The Allow header on OPTIONS: confirm every listed method is one the "
                   "route needs.",
            evidence={"url": safe_url(url), "allow": allow.strip()}))
    if options_status is not None and options_status in {405, 501}:
        out.append(info(
            "active-options-refused", "transport", "OPTIONS is not implemented here",
            detail="The route refuses OPTIONS: preflights to it fail closed.",
            evidence={"url": safe_url(url), "status": options_status}))
    return out


def redirect_findings(link: str, route: list[str], final_url: str, page_url: str,
                      cut_at: str | None = None) -> list[dict[str, Any]]:
    """An own link whose chain leaves the site is an open redirect worth a fix."""
    target = cut_at or final_url
    if not target or not sites.is_third_party(target, str(page_url or "")):
        return []
    return [finding(
        "active-open-redirect", "redirect", "warn", "medium",
        "An own link redirects off-site",
        detail="Following the link leaves your registrable domain: a convincing copy of "
               "your URL becomes a phishing hop. " +
               ("The chain was cut at the scope boundary; it may go further." if cut_at else
                "The chain ends outside your site."),
        fix="Redirect only to relative paths or an allowlist of your own hosts; "
            "validate any 'next'/'return' parameter against it.",
        evidence={"from": safe_url(link),
                  "chain": [safe_url(hop) for hop in (route or [])][-4:],
                  "to": safe_url(target)})]


def reflection_findings(url: str, token: str, body: str) -> list[dict[str, Any]]:
    """An inert token mirrored back: count it, name the contexts, stop there.

    Reflection alone is not a verdict - confirming XSS takes a payload, which
    this action never sends - so this is a warning that says where to look.
    """
    text, contexts = 0, 0
    sample = ""
    start = 0
    while True:
        hit = body.find(token, start)
        if hit == -1:
            break
        back = body[max(0, hit - 200): hit]
        if back.rfind(">") >= back.rfind("<"):
            text += 1
        else:
            contexts += 1
        if not sample:
            sample = back[-80:] + token + body[hit + len(token): hit + len(token) + 40]
        start = hit + len(token)
    if not text and not contexts:
        return []
    return [finding(
        "active-reflected-input", "injection", "warn", "low",
        "Own input is reflected in the response",
        detail=f"The inert token came back {text + contexts} time(s) "
               f"({text} in text, {contexts} inside tags): someone else's input lands in "
               "this page. Confirm the real parameters are encoded for their context.",
        fix="Encode every reflected value for its context (HTML, attribute, JS); "
            "prefer frameworks that do it by default, and set a CSP.",
        evidence={"url": safe_url(url), "text_contexts": text, "tag_contexts": contexts,
                  "sample": sample[:200]})]


def build(url: str, checks: list[str], findings: list[dict[str, Any]],
          requests_made: list[str]) -> dict[str, Any]:
    """The assembled active_probe report: findings with priority and fixes."""
    from web_search_neo.audit import grading
    counts = grading.counts(findings)
    order = grading.priority(findings)
    first = "; ".join(item["title"] for item in order[:3]) or "nothing to fix"
    return {
        "success": True, "url": safe_url(str(url or "")), "checks": list(checks),
        "counts": counts,
        "summary_line": f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low. "
                        f"Fix first: {first}.",
        "priority": order, "findings": findings,
        "scope": ("active_probe sends only OPTIONS, TRACE and plain GETs of your own pages, "
                  "links and one inert query token - everything listed in requests_made. No "
                  "POST/PUT/DELETE, no payloads, no auth, no fuzzing, nothing outside the scope."),
        "requests_made": list(requests_made),
    }
