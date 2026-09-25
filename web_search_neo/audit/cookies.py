"""Cookie flags: Secure, HttpOnly, SameSite, the __Host-/__Secure- prefixes, Domain scope.

Two sources are merged: every ``Set-Cookie`` of the page response (and its
redirects), and the cookies the isolated browser holds after the page ran -
which adds the ones scripts set through ``document.cookie``. Cookie values are
never copied into the report, only names and flags.

Scoring is Mozilla HTTP Observatory's (v1.7.1) cookie test: one result, the
worst in its severity order - a session cookie (name contains "login" or "sess")
without Secure -40, a session cookie without HttpOnly -30, an anti-CSRF cookie
without SameSite -20, an invalid SameSite value or SameSite=None without Secure
-20, a session cookie without Secure but with passing HSTS -10, any cookie
without Secure -20 (-5 with passing HSTS); all Secure with HttpOnly sessions 0,
and +5 only when every cookie carries SameSite. Everything else is advice with
0 points.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any

from web_search_neo.actions.cookie_scope import is_public_suffix
from web_search_neo.audit.findings import finding, info, passed

CATEGORY = "cookies"
# Names that usually hold a login or anti-forgery secret; the flags matter most there.
_SESSION_NAME = re.compile(
    r"sess|sid|auth|token|jwt|login|logged|remember|csrf|xsrf|identity|account|user_?id|^id$|connect\.",
    re.IGNORECASE)
_CSRF_NAME = re.compile(r"csrf|xsrf", re.IGNORECASE)


def parse_set_cookie(line: str) -> dict[str, Any] | None:
    """One Set-Cookie line -> ``{name, secure, httponly, samesite, domain, path, persistent}``."""
    parts = [part.strip() for part in str(line or "").split(";")]
    if not parts or "=" not in parts[0]:
        return None
    name = parts[0].split("=", 1)[0].strip()
    if not name:
        return None
    cookie: dict[str, Any] = {"name": name, "secure": False, "httponly": False, "samesite": None,
                              "domain": None, "path": None, "persistent": False, "source": "set-cookie"}
    for attribute in parts[1:]:
        key, _, value = attribute.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "secure":
            cookie["secure"] = True
        elif key == "httponly":
            cookie["httponly"] = True
        elif key == "samesite":
            cookie["samesite"] = value.capitalize() if value else ""
        elif key == "domain":
            cookie["domain"] = value.lower() or None
        elif key == "path":
            cookie["path"] = value
        elif key in {"expires", "max-age"}:
            cookie["persistent"] = True
    return cookie


def from_browser(cookie: dict[str, Any]) -> dict[str, Any]:
    """A CDP cookie object in the same shape (``hostOnly`` cookies carry no Domain)."""
    domain = str(cookie.get("domain") or "")
    return {
        "name": str(cookie.get("name") or ""),
        "secure": bool(cookie.get("secure")),
        "httponly": bool(cookie.get("httpOnly")),
        "samesite": cookie.get("sameSite") or None,
        "domain": domain.lower() if domain.startswith(".") else None,
        "host": domain.lstrip(".").lower(),
        "path": cookie.get("path"),
        "persistent": not cookie.get("session", False),
        "source": "browser",
    }


def merge(header_cookies: list[dict[str, Any]], browser_cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per cookie name; a Set-Cookie line wins (it is what the server sent).

    A cookie only the browser holds was set by a script (``document.cookie``) or
    by a subresource response, and is reported with that source.
    """
    merged: dict[str, dict[str, Any]] = {}
    for cookie in header_cookies:
        merged.setdefault(cookie["name"], cookie)
    for cookie in browser_cookies:
        if cookie["name"] and cookie["name"] not in merged:
            merged[cookie["name"]] = {**cookie, "source": "script-or-subresource"}
    return list(merged.values())


def _prefix_problem(cookie: dict[str, Any]) -> str | None:
    name = cookie["name"]
    if name.startswith("__Host-"):
        if not cookie["secure"] or cookie.get("domain") or (cookie.get("path") or "/") != "/":
            return "__Host- needs Secure, Path=/ and no Domain (the browser rejects it otherwise)"
    elif name.startswith("__Secure-") and not cookie["secure"]:
        return "__Secure- needs Secure (the browser rejects it otherwise)"
    return None


_RESULTS = {
    # id: (points, status, severity, title, fix)
    "cookies-secure": (5, "pass", "info", "Cookies are Secure; session cookies HttpOnly; every cookie has SameSite", ""),
    "cookies-secure-no-samesite": (0, "warn", "low", "Cookies are Secure and session cookies HttpOnly, but not all have SameSite",
                                   "Add SameSite=Lax (or Strict) to every cookie for Observatory's +5."),
    "cookies-no-secure-hsts": (-5, "fail", "low", "Cookies without Secure (HSTS protects them)",
                               "Add Secure to every cookie; HSTS does not cover a first visit or other clients."),
    "cookies-no-secure": (-20, "fail", "medium", "Cookies without Secure",
                          "Serve the site over https and add Secure to every cookie."),
    "cookies-session-no-secure-hsts": (-10, "fail", "medium", "Session cookies without Secure (HSTS protects them)",
                                       "Add Secure to every session cookie."),
    "cookies-samesite-invalid": (-20, "fail", "medium", "A SameSite value is invalid, or SameSite=None lacks Secure",
                                 "Use SameSite=Lax, Strict or None - and None only together with Secure."),
    "cookies-anticsrf-no-samesite": (-20, "fail", "medium", "An anti-CSRF cookie has no SameSite",
                                     "Add SameSite=Strict (or Lax) to the CSRF cookie."),
    "cookies-session-no-httponly": (-30, "fail", "high", "Session cookies readable by JavaScript",
                                    "Add HttpOnly to session cookies."),
    "cookies-session-no-secure": (-40, "fail", "high", "Session cookies without Secure",
                                  "Serve the site over https and add Secure to every session cookie."),
}
# Observatory's severity order (its "goodness" list, best first). A later entry
# replaces an earlier one; it is not the order of the points.
_ORDER = ("cookies-no-secure-hsts", "cookies-no-secure", "cookies-session-no-secure-hsts", "cookies-samesite-invalid",
          "cookies-anticsrf-no-samesite", "cookies-session-no-httponly", "cookies-session-no-secure")
# Observatory's names: "login"/"sess" make a session cookie, "csrf" an anti-CSRF one.
_OBSERVATORY_SESSION = re.compile(r"login|sess", re.IGNORECASE)
_OBSERVATORY_CSRF = re.compile(r"csrf", re.IGNORECASE)
_IGNORED = frozenset({"heroku-session-affinity"})
_VALID_SAMESITE = frozenset({"lax", "strict", "none"})


def raw_samesite_invalid(line: str) -> bool:
    """A Set-Cookie line whose SameSite attribute is empty or not Lax/Strict/None.

    Read as Observatory reads the raw header: every ``;`` part split on ``=``,
    the value being what stands between the first and the second ``=``.
    """
    for part in str(line).strip().split(";"):
        pieces = part.strip().split("=")
        if pieces[0].strip().lower() != "samesite":
            continue
        value = pieces[1] if len(pieces) > 1 else ""
        if value.strip().lower() not in _VALID_SAMESITE:
            return True
    return False


def _jar_accepts(cookie: dict[str, Any], host: str) -> bool:
    """Whether a cookie jar (RFC 6265bis, as tough-cookie runs it) stores this Set-Cookie at all.

    Refused, and so never graded by Observatory: a ``Domain`` that is a public
    suffix or does not domain-match the host, and cookies that break the
    ``__Secure-`` / ``__Host-`` prefix rules (dropped silently).
    """
    domain = str(cookie.get("domain") or "").lstrip(".")
    if domain:
        if is_public_suffix(domain) and domain != host:
            return False
        if host != domain and not (host.endswith("." + domain) and not _is_ip(host)):
            return False
    name = cookie["name"]
    path = cookie.get("path") if str(cookie.get("path") or "").startswith("/") else "/"
    if name.startswith("__Secure-") and not cookie["secure"]:
        return False
    if name.startswith("__Host-") and not (cookie["secure"] and not domain and path == "/"):
        return False
    return True


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def jar(header_cookies: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    """Observatory's cookie jar: what a jar keeps of the Set-Cookie lines, the last one per name/domain/path."""
    kept: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in header_cookies:
        if cookie.get("source") != "set-cookie" or cookie["name"] in _IGNORED or not _jar_accepts(cookie, host):
            continue
        key = (cookie["name"], str(cookie.get("domain") or host).lstrip("."), str(cookie.get("path") or "/"))
        kept.pop(key, None)
        kept[key] = cookie
    return list(kept.values())


def _worse(new: str, old: str | None) -> str:
    return new if old is None or _ORDER.index(new) > _ORDER.index(old) else old


def observatory_result(graded: list[dict[str, Any]], raw_lines: list[str], hsts: bool) -> tuple[str, list[str]]:
    """Observatory's single cookie result and the cookies behind it."""
    result: str | None = None
    names: dict[str, list[str]] = {}

    def hit(fid: str, name: str) -> None:
        nonlocal result
        result = _worse(fid, result)
        names.setdefault(fid, []).append(name)

    for line in raw_lines:
        if raw_samesite_invalid(line):
            hit("cookies-samesite-invalid", str(line).split("=", 1)[0].strip())
    missing_samesite = False
    for cookie in graded:
        name = cookie["name"]
        session = bool(_OBSERVATORY_SESSION.search(name))
        samesite = str(cookie.get("samesite") or "").lower()
        samesite = samesite if samesite in _VALID_SAMESITE else ""
        if not cookie["secure"] and samesite == "none":
            hit("cookies-samesite-invalid", name)
        if not cookie["secure"]:
            hit("cookies-no-secure-hsts" if hsts else "cookies-no-secure", name)
        if _OBSERVATORY_CSRF.search(name) and not samesite:
            hit("cookies-anticsrf-no-samesite", name)
        if session and not cookie["secure"]:
            hit("cookies-session-no-secure-hsts" if hsts else "cookies-session-no-secure", name)
        if session and not cookie["httponly"]:
            hit("cookies-session-no-httponly", name)
        missing_samesite = missing_samesite or not samesite
    if result is not None:
        return result, sorted(set(names[result]))
    if missing_samesite:
        return "cookies-secure-no-samesite", sorted(c["name"] for c in graded
                                                    if str(c.get("samesite") or "").lower() not in _VALID_SAMESITE)
    return "cookies-secure", sorted(c["name"] for c in graded)


def analyze(cookies: list[dict[str, Any]], *, host: str, https: bool, hsts: bool = False,
            raw_lines: list[str] | None = None, ignore_secure: bool = False,
            graded_lines: list[str] | None = None) -> list[dict[str, Any]]:
    """Findings for the page's own cookies; Observatory's points go to the one worst result.

    Graded: the jar of the report's page request (its redirects included), as
    Observatory's session jar (``graded_lines``, parsed and
    filtered by ``jar``); ``raw_lines`` are the final page response's own
    Set-Cookie lines, checked for an invalid SameSite. With an empty jar the
    result is "no cookies", whatever the raw lines say (Observatory's order).
    Cookies scripts set, and third-party ones, get advice only. ``ignore_secure`` grades a local
    development server as if every cookie were Secure (it cannot be on plain
    http); the production forecast grades the real flags.
    """
    if not cookies and not raw_lines and not graded_lines:
        return [passed("cookies-none", CATEGORY, "The page sets no cookies")]
    own = [c for c in cookies if not c.get("host") or c["host"] == host or host.endswith("." + c["host"])
           or c["host"].endswith("." + host)]
    foreign = [c for c in cookies if c not in own]
    out: list[dict[str, Any]] = []
    if foreign:
        out.append(info("cookies-third-party", CATEGORY, f"{len(foreign)} cookie(s) of other domains in the jar",
                        detail="Set by embedded third parties; they are not graded here.",
                        evidence=sorted({f"{c['name']}@{c.get('host')}" for c in foreign})[:30]))
    if graded_lines is not None:
        graded = jar([c for c in (parse_set_cookie(line) for line in graded_lines) if c], host)
    else:
        graded = jar([c for c in own if c.get("source") == "set-cookie"], host)
    if ignore_secure:
        graded = [{**c, "secure": True} for c in graded]
    if graded:
        result, names = observatory_result(graded, list(raw_lines or []), hsts)
        points, status, severity, title, fix = _RESULTS[result]
        detail = ("Session cookies are those whose name contains 'login' or 'sess' (Observatory's rule)."
                  if "session" in result else "")
        if not https and "secure" in result:
            detail = (detail + " " if detail else "") + "The site is served over plain http."
        out.append(finding(result, CATEGORY, status, severity, title, detail=detail, fix=fix,
                           evidence=names[:30], points=points))
    else:
        sent = bool(raw_lines or graded_lines or any(c.get("source") == "set-cookie" for c in own))
        out.append(passed("cookies-none", CATEGORY, "No cookie reached the jar" if sent else "The response sets no cookies",
                          detail=("Every Set-Cookie was one a browser drops (a public-suffix or foreign Domain, a "
                                  "broken __Host-/__Secure- prefix), so Observatory finds no cookies; see the advice."
                                  if sent else "Cookies set by scripts are listed but not graded (Observatory reads "
                                  "Set-Cookie).")))
    return out + _advice(own, host, https)


def _advice(own: list[dict[str, Any]], host: str, https: bool) -> list[dict[str, Any]]:
    """Problems Observatory does not grade: 0 points, or extra_points where the browser drops a cookie."""
    out: list[dict[str, Any]] = []
    # Names that usually hold a login secret but that Observatory's 'login'/'sess' rule misses.
    hidden = [c["name"] for c in own if _SESSION_NAME.search(c["name"]) and not _OBSERVATORY_SESSION.search(c["name"])
              and not c["httponly"] and not _CSRF_NAME.search(c["name"])]
    if hidden:
        out.append(finding("cookies-secret-no-httponly", CATEGORY, "warn", "medium",
                           "Cookies that look like credentials are readable by JavaScript",
                           detail="Their names suggest a token or login; without HttpOnly one XSS bug reads them.",
                           fix="Add HttpOnly unless your own script must read the value.", evidence=sorted(hidden)))
    script_set = [c["name"] for c in own if c.get("source") != "set-cookie" and https and not c["secure"]]
    if script_set:
        out.append(finding("cookies-script-no-secure", CATEGORY, "warn", "low", "Cookies set by scripts without Secure",
                           fix="Add '; Secure' where the script sets them.", evidence=sorted(script_set)))
    no_samesite = [c["name"] for c in own if not c.get("samesite")]
    if no_samesite:
        out.append(finding(
            "cookies-no-samesite", CATEGORY, "warn", "low", "Cookies without an explicit SameSite",
            detail="Chrome treats them as Lax, other browsers may not; cross-site requests may carry them.",
            fix="Set SameSite=Lax (Strict for session cookies that never need cross-site navigation).",
            evidence=sorted(no_samesite)))
    prefix = {c["name"]: problem for c in own if (problem := _prefix_problem(c))}
    if prefix:
        out.append(finding("cookies-prefix-invalid", CATEGORY, "fail", "medium", "Cookie prefix rules broken",
                           detail="The browser drops such a cookie.",
                           fix="Meet the prefix rules or rename the cookie.", evidence=prefix, extra_points=-10))
    wide = {}
    for cookie in own:
        domain = (cookie.get("domain") or "").lstrip(".")
        if not domain:
            continue
        if is_public_suffix(domain):
            wide[cookie["name"]] = f"Domain={domain} is a public suffix (the browser rejects it)"
        elif domain != host:
            wide[cookie["name"]] = f"Domain={domain} shares it with every subdomain of {domain}"
    if wide:
        out.append(finding(
            "cookies-domain-wide", CATEGORY, "warn", "low", "Cookies scoped wider than this host",
            detail="Any subdomain (a forgotten staging host, user content) can read or overwrite them.",
            fix="Drop the Domain attribute (host-only cookie) or use the __Host- prefix for session cookies.",
            evidence=wide))
    return out
