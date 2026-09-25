"""Security response headers other than CSP and cookies: what is set and what it means.

Points are Mozilla HTTP Observatory's (v1.7.1) for the tests it has - HSTS,
X-Frame-Options, X-Content-Type-Options, Referrer-Policy, and the cross-origin
headers COOP, COEP and CORP - reimplemented from its published behaviour:

* HSTS is read from the https response; a comma (the header sent twice), no
  ``max-age=`` or an unparsable one is invalid (-20); ``max-age`` under
  15552000 s -10; missing -20; no https -20; an untrusted certificate -20. The
  preload bonus (+5) needs a lookup in the browsers' preload list, which this
  report does not make, so it is never given.
* X-Frame-Options: ``frame-ancestors`` anywhere in the merged CSP +5, ``DENY`` /
  ``SAMEORIGIN`` +5, ``ALLOW-FROM`` 0, anything else -20, missing -20.
* X-Content-Type-Options: exactly ``nosniff`` 0, missing or anything else -5.
* Referrer-Policy (header, then ``<meta name="referrer">``, last recognised
  token wins): private values +5, the four leaking ones -5, unrecognised -5,
  missing 0.
* COOP and COEP: a valid token +10 (``unsafe-none`` 0), sent twice or invalid -5,
  missing 0. CORP: ``same-origin``/``same-site`` +10, ``cross-origin`` 0,
  unrecognised -5, missing 0.

Permissions-Policy and version disclosure carry 0 points. CORS: Observatory's
-50 needs a preflight with an Origin header, which this report never sends; the
page response as served is judged with ``extra_points`` (this report's own).
"""
from __future__ import annotations

import re
from typing import Any

from web_search_neo.audit.findings import finding, info, passed

HSTS_MIN_SECONDS = 15_552_000  # Observatory's six-month threshold (15768000 would be exact)
HSTS_PRELOAD_SECONDS = 31_536_000  # one year, hstspreload.org's requirement
_VERSIONED = re.compile(r"\d+(?:\.\d+)+|/\d")
_DISCLOSING = ("server", "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version", "x-generator",
               "x-runtime", "x-version")
# Observatory's lists: the last recognised token of the header (and the meta tag) wins.
_PRIVATE_REFERRER = ("no-referrer", "same-origin", "strict-origin", "strict-origin-when-cross-origin")
_UNSAFE_REFERRER = ("origin", "origin-when-cross-origin", "unsafe-url", "no-referrer-when-downgrade")
_SENSITIVE_FEATURES = ("camera", "microphone", "geolocation", "payment", "usb", "serial", "hid",
                       "display-capture", "bluetooth")
_TOKEN = r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*"
# One RFC 8941 bare item: token, integer/decimal, string, byte sequence or boolean.
_BARE_ITEM = (_TOKEN + r'|-?\d{1,15}(?:\.\d{1,3})?|"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\["\\])*"'
              r"|:[A-Za-z0-9+/=]*:|\?[01]")
_PARAMETER = r"[a-z*][a-z0-9_\-.*]*(?:=(?:" + _BARE_ITEM + r"))?"
_SF_ITEM = re.compile(r"(" + _TOKEN + r"|" + _BARE_ITEM + r")((?:; *" + _PARAMETER + r")*)")
_SF_TOKEN = re.compile(_TOKEN)


def sf_token(value: str) -> str | None:
    """``same-origin`` out of ``same-origin; report-to="coop"``; None when the value is no single token item.

    COOP and COEP are RFC 8941 structured headers holding one item. The value is
    matched against that grammar as a whole (bare item, then ``;key[=value]``
    parameters, spaces only after a semicolon): a list, a quoted string, a broken
    parameter or trailing junk yields None, and the caller grades that as
    invalid. Spaces at both ends are allowed.
    """
    text = str(value or "").strip(" ")
    match = _SF_ITEM.fullmatch(text)
    if not match or not _SF_TOKEN.fullmatch(match.group(1)):
        return None
    return match.group(1)


def _leading_int(text: str) -> float:
    """``parseInt``: the leading (signed) digits, or NaN when there are none."""
    match = re.match(r"\s*([+-]?\d+)", text)
    return int(match.group(1)) if match else float("nan")


def parse_hsts(value: str) -> dict[str, Any]:
    """The header as Observatory reads it; ``max_age`` None = invalid."""
    text = str(value or "")[:1024]
    parts = [part.strip().lower() for part in text.split(";")]
    out: dict[str, Any] = {"max_age": None, "include_subdomains": False, "preload": False, "twice": "," in text}
    for part in parts:
        if part.startswith("max-age="):
            out["max_age"] = _leading_int(part[8:128])
        elif part == "includesubdomains":
            out["include_subdomains"] = True
        elif part == "preload":
            out["preload"] = True
    return out


def _hsts(value: str | None, https: bool, verified: bool = True) -> list[dict[str, Any]]:
    if not https:
        return [finding("hsts-no-https", "headers", "fail", "medium", "HSTS is impossible without https",
                        detail="Browsers ignore Strict-Transport-Security received over plain http.",
                        fix="Serve the site over https, then send Strict-Transport-Security: "
                            "max-age=31536000; includeSubDomains.", points=-20)]
    if not verified:
        return [finding("hsts-invalid-cert", "headers", "fail", "high", "HSTS cannot apply with an invalid certificate",
                        fix="Fix the certificate, then send Strict-Transport-Security.", points=-20)]
    if value is None:
        return [finding(
            "hsts-missing", "headers", "fail", "medium", "No Strict-Transport-Security",
            detail="The first visit and every typed http:// link can be downgraded to plain http.",
            fix="Send Strict-Transport-Security: max-age=31536000; includeSubDomains "
                "(add preload once every subdomain serves https).", points=-20)]
    parsed = parse_hsts(value)
    if parsed["twice"] or parsed["max_age"] is None:
        why = "sent twice (the value has a comma)" if parsed["twice"] else "no max-age="
        return [finding("hsts-invalid", "headers", "fail", "medium", f"Strict-Transport-Security is invalid: {why}",
                        fix="Send the header once: max-age=31536000; includeSubDomains.", evidence=value[:300],
                        points=-20)]
    max_age = parsed["max_age"]
    out: list[dict[str, Any]] = []
    # parseInt semantics: a max-age that is not a number compares as "not less" (NaN), as in Observatory.
    if max_age == max_age and max_age < HSTS_MIN_SECONDS:
        out.append(finding(
            "hsts-short", "headers", "fail", "medium", f"HSTS max-age is short ({max_age} s)",
            detail=f"Observatory requires at least {HSTS_MIN_SECONDS} s (six months).",
            fix="Raise max-age to 31536000 (one year).", evidence=value, points=-10))
    else:
        out.append(passed("hsts-ok", "headers", "Strict-Transport-Security is set", evidence=value))
    if not parsed["include_subdomains"]:
        out.append(finding(
            "hsts-no-subdomains", "headers", "warn", "low", "HSTS without includeSubDomains",
            detail="Subdomains (and cookies scoped to the parent domain) stay open to downgrade.",
            fix="Add includeSubDomains once every subdomain serves https.", evidence=value))
    if parsed["preload"] and parsed["include_subdomains"] and max_age == max_age and max_age >= HSTS_PRELOAD_SECONDS:
        out.append(info("hsts-preload-ready", "headers", "HSTS is preload-ready",
                        detail="The header meets the preload requirements. Observatory's +5 is for sites that "
                               "are in the browsers' preload list; that list is not looked up here, so the "
                               "bonus is not given - check hstspreload.org yourself.",
                        evidence=value))
    return out


def hsts_passes(findings: list[dict[str, Any]]) -> bool:
    """Observatory's "HSTS passes" (what protects cookies without Secure)."""
    return any(f["id"] == "hsts-ok" for f in findings)


def _nosniff(headers: dict[str, str]) -> dict[str, Any]:
    value = headers.get("x-content-type-options")
    if value is not None and value[:256].strip().lower() == "nosniff":
        return passed("xcto-ok", "headers", "X-Content-Type-Options: nosniff")
    return finding(
        "xcto-missing", "headers", "fail", "low",
        "X-Content-Type-Options is not exactly nosniff" if value is not None else "No X-Content-Type-Options",
        detail="Browsers may sniff an uploaded text file as script or style.",
        fix="Send X-Content-Type-Options: nosniff (once).", evidence=value, points=-5)


def _framing(headers: dict[str, str], frame_ancestors: list[str] | None, csp_frame_ancestors: bool) -> list[dict[str, Any]]:
    """Observatory's X-Frame-Options test; the frame-ancestors credit comes from the merged CSP."""
    value = headers.get("x-frame-options")
    out: list[dict[str, Any]] = []
    if frame_ancestors is not None and "*" in frame_ancestors:
        out.append(finding("framing-open", "headers", "warn", "medium", "CSP frame-ancestors allows every site",
                           fix="Use frame-ancestors 'self' or 'none'.", evidence=" ".join(frame_ancestors)))
    if csp_frame_ancestors:
        return out + [passed("framing-csp", "headers", "Framing is governed by CSP frame-ancestors", points=5,
                             evidence=" ".join(frame_ancestors or []))]
    xfo = (value or "")[:1024].strip().lower()
    if not value:  # Observatory treats an empty header as a missing one
        return out + [finding(
            "framing-missing", "headers", "fail", "medium", "Clickjacking protection is missing",
            detail="Another site can load this page in an invisible frame and trick clicks into it.",
            fix="Send Content-Security-Policy: frame-ancestors 'self' (and X-Frame-Options: SAMEORIGIN for old "
                "browsers).", points=-20)]
    if xfo in {"deny", "sameorigin"}:
        return out + [passed("framing-xfo", "headers", f"X-Frame-Options: {xfo.upper()}", points=5)]
    if xfo.startswith("allow-from"):
        return out + [finding("framing-allow-from", "headers", "warn", "medium", "X-Frame-Options ALLOW-FROM is ignored",
                              detail="Current browsers ignore ALLOW-FROM, so the page can be framed by any site.",
                              fix="Use CSP frame-ancestors with the allowed origins instead.", evidence=value)]
    return out + [finding("framing-invalid", "headers", "fail", "medium", f"X-Frame-Options value {value!r} is not recognised",
                          detail="An invalid value (or the header sent twice) protects nothing.",
                          fix="Send X-Frame-Options: SAMEORIGIN once, or CSP frame-ancestors.",
                          evidence=value[:300], points=-20)]


def _referrer(headers: dict[str, str], meta_referrer: str | None = None) -> dict[str, Any]:
    values = [v for v in (headers.get("referrer-policy"), meta_referrer) if v is not None]
    if not values:
        return info("referrer-default", "headers", "No Referrer-Policy (browser default applies)",
                    detail="Current browsers default to strict-origin-when-cross-origin.",
                    fix="Send Referrer-Policy: strict-origin-when-cross-origin to fix it explicitly.")
    raw = ", ".join(values)
    known = [t.strip().lower() for t in raw.split(",") if t.strip().lower() in _PRIVATE_REFERRER + _UNSAFE_REFERRER]
    value = known[-1] if known else ""
    evidence = {"header": headers.get("referrer-policy"), "meta": meta_referrer}
    if value in _PRIVATE_REFERRER:
        return passed("referrer-private", "headers", f"Referrer-Policy: {value}", points=5, evidence=evidence)
    if value in _UNSAFE_REFERRER:
        return finding("referrer-unsafe", "headers", "fail", "low", f"Referrer-Policy {value} leaks to other sites",
                       detail="Full URLs (paths and query strings with tokens or search terms) or the origin "
                              "of every page are sent to other sites, over http too for some values.",
                       fix="Use strict-origin-when-cross-origin.", evidence=evidence, points=-5)
    return finding("referrer-invalid", "headers", "fail", "low", "Referrer-Policy value is not recognised",
                   fix="Use strict-origin-when-cross-origin.", evidence=evidence, points=-5)


def _permissions(headers: dict[str, str]) -> list[dict[str, Any]]:
    value = headers.get("permissions-policy", "")
    out: list[dict[str, Any]] = []
    if headers.get("feature-policy"):
        out.append(info("feature-policy-legacy", "headers", "Feature-Policy is obsolete",
                        fix="Move its rules to Permissions-Policy.", evidence=headers["feature-policy"][:300]))
    if not value:
        out.append(finding(
            "permissions-policy-missing", "headers", "warn", "low", "No Permissions-Policy",
            detail="Embedded third-party frames may ask for camera, microphone, geolocation and more.",
            fix="Send Permissions-Policy denying what the site never uses, e.g. "
                "camera=(), microphone=(), geolocation=(), payment=(), usb=()."))
        return out
    features = {}
    for item in value.split(","):
        name, _, allow = item.strip().partition("=")
        if name.strip():
            features[name.strip().lower()] = allow.strip()
    open_to_all = [name for name in _SENSITIVE_FEATURES if features.get(name, "").strip() == "*"]
    if open_to_all:
        out.append(finding("permissions-policy-wildcard", "headers", "warn", "low",
                           "Permissions-Policy grants powerful features to every origin",
                           fix="Replace * with self or an explicit origin list.", evidence=open_to_all))
    else:
        out.append(passed("permissions-policy-ok", "headers", "Permissions-Policy is set",
                          evidence=sorted(features)[:30]))
    return out


_COOP = {"same-origin": 10, "same-origin-allow-popups": 10, "noopener-allow-popups": 10, "unsafe-none": 0}
_COEP = {"require-corp": 10, "credentialless": 10, "unsafe-none": 0}


def _opener_embedder(headers: dict[str, str], name: str, table: dict[str, int], short: str,
                     missing_fix: str) -> dict[str, Any]:
    value = headers.get(name)
    if not value:  # an empty header is not implemented, as in Observatory
        return finding(f"{short}-missing", "headers", "warn" if short == "coop" else "info",
                       "low" if short == "coop" else "info", f"No {name.title()}", fix=missing_fix)
    token = sf_token(value[:1024].strip())
    if token not in table:
        return finding(f"{short}-invalid", "headers", "fail", "low", f"{name.title()} is invalid or sent twice",
                       fix=f"Send it once with one of: {', '.join(table)}.", evidence=value[:300], points=-5)
    if table[token]:
        return passed(f"{short}-ok", "headers", f"{name.title()}: {token}", points=table[token])
    return finding(f"{short}-unsafe-none", "headers", "warn", "low", f"{name.title()}: unsafe-none", fix=missing_fix)


def _isolation(headers: dict[str, str]) -> list[dict[str, Any]]:
    out = [
        _opener_embedder(headers, "cross-origin-opener-policy", _COOP, "coop",
                         "Send Cross-Origin-Opener-Policy: same-origin (same-origin-allow-popups if you rely on "
                         "OAuth/payment popups)."),
        _opener_embedder(headers, "cross-origin-embedder-policy", _COEP, "coep",
                         "Send Cross-Origin-Embedder-Policy: credentialless (or require-corp) if your embedded "
                         "resources allow it; it enables cross-origin isolation."),
    ]
    corp = headers.get("cross-origin-resource-policy")
    value = (corp or "")[:256].strip().lower()
    if value in {"same-origin", "same-site"}:
        out.append(passed("corp-ok", "headers", f"Cross-Origin-Resource-Policy: {value}", points=10))
    elif corp and value != "cross-origin":  # an empty header is not implemented
        out.append(finding("corp-invalid", "headers", "fail", "low", "Cross-Origin-Resource-Policy is not recognised",
                           fix="Send same-origin, same-site or cross-origin (once).", evidence=corp[:300], points=-5))
    else:
        out.append(finding(
            "corp-missing", "headers", "warn", "low",
            "No Cross-Origin-Resource-Policy" if not corp else "Cross-Origin-Resource-Policy: cross-origin",
            detail="Other sites may embed this response (side-channel reads such as Spectre).",
            fix="Send Cross-Origin-Resource-Policy: same-origin (same-site for assets shared across your subdomains)."))
    return out


def _disclosure(headers: dict[str, str]) -> list[dict[str, Any]]:
    versioned = {name: headers[name] for name in _DISCLOSING if headers.get(name) and _VERSIONED.search(headers[name])}
    named = {name: headers[name] for name in _DISCLOSING if headers.get(name) and name not in versioned}
    out: list[dict[str, Any]] = []
    if versioned:
        out.append(finding(
            "version-disclosure", "headers", "warn", "low", "Response headers reveal software versions",
            detail="Exact versions let anyone match the server to published vulnerabilities in seconds.",
            fix="Strip versions: nginx server_tokens off; Apache ServerTokens Prod; Express "
                "app.disable('x-powered-by'); PHP expose_php=Off; remove X-AspNet-Version.",
            evidence=versioned))
    if named:
        out.append(info("software-named", "headers", "Headers name the server software (no version)",
                        fix="Optional: remove X-Powered-By and similar headers entirely.", evidence=named))
    xss = headers.get("x-xss-protection", "").strip()
    if xss and not xss.startswith("0"):
        out.append(info("x-xss-protection-obsolete", "headers", "X-XSS-Protection is obsolete",
                        detail="The XSS auditor it controls was removed; in old browsers it could be abused.",
                        fix="Send X-XSS-Protection: 0 (or drop it) and rely on CSP.", evidence=xss))
    return out


def _cors(headers: dict[str, str]) -> list[dict[str, Any]]:
    origin = headers.get("access-control-allow-origin", "").strip()
    credentials = headers.get("access-control-allow-credentials", "").strip().lower() == "true"
    note = "Checked on the ordinary page response only (no Origin header sent, no origins tried)."
    if not origin:
        return [passed("cors-none", "cors", "The page does not allow cross-origin reads", detail=note)]
    if origin == "null":
        return [finding(
            "cors-null-origin", "cors", "fail", "high", "Access-Control-Allow-Origin: null",
            detail="Sandboxed iframes and data: documents on any site present the origin 'null'. " + note,
            fix="Never allow 'null'; list the exact trusted origins.", extra_points=-25,
            evidence={"access-control-allow-origin": origin, "credentials": credentials})]
    if origin == "*" and credentials:
        return [finding(
            "cors-wildcard-credentials", "cors", "fail", "medium",
            "Access-Control-Allow-Origin: * together with Allow-Credentials: true",
            detail="Browsers reject this combination, so credentialed calls fail; the usual 'fix' - "
                   "echoing the request's Origin - would let every site read logged-in data. " + note,
            fix="Allow credentials only for an explicit allowlist of origins, checked server-side; "
                "keep * only for public, cookie-less resources.", extra_points=-10,
            evidence={"access-control-allow-origin": origin, "access-control-allow-credentials": "true"})]
    if origin == "*":
        return [finding(
            "cors-wildcard", "cors", "warn", "low", "Any site may read this page (Access-Control-Allow-Origin: *)",
            detail="Fine for public, anonymous content; wrong for a page that ever carries user data. " + note,
            fix="Send Access-Control-Allow-Origin only on API routes that need it, with explicit origins.",
            evidence={"access-control-allow-origin": origin})]
    return [info("cors-single-origin", "cors", f"Cross-origin reads allowed for {origin}",
                 detail=note, evidence={"access-control-allow-origin": origin, "credentials": credentials})]


_UNSET = object()


def analyze(headers: dict[str, str], *, https: bool, frame_ancestors: list[str] | None = None,
            csp_frame_ancestors: bool | None = None, verified: bool = True,
            meta_referrer: str | None = None, hsts: Any = _UNSET) -> list[dict[str, Any]]:
    """Every non-CSP, non-cookie header check for one response (names lowercased).

    ``https`` says whether https is available; ``hsts`` is the Strict-Transport-
    Security value of the https response (default: this response's own).
    """
    out = _hsts(headers.get("strict-transport-security") if hsts is _UNSET else hsts, https, verified)
    out.append(_nosniff(headers))
    out.extend(_framing(headers, frame_ancestors,
                        frame_ancestors is not None if csp_frame_ancestors is None else csp_frame_ancestors))
    out.append(_referrer(headers, meta_referrer))
    out.extend(_permissions(headers))
    out.extend(_isolation(headers))
    out.extend(_disclosure(headers))
    out.extend(_cors(headers))
    return out
