"""Secrets and endpoints in the page's own client code: pure analysis.

``secret_scan`` sends only ordinary GETs (the page, its same-scope scripts,
an API description the page itself references - everything listed in
``requests_made``). This module never touches the network or the browser: on
the way in are the served HTML, the fetched script texts, an optional journal
(script URLs the browser really loaded), the cookie jar and the storage
snapshot; on the way out the findings with priority and fixes, in the shape of
``security_report``. Secret values never reach the answer: evidence samples
carry the match replaced by REDACTED.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

from web_search_neo.audit import api_parts, grading, page as page_checks, sites
from web_search_neo.audit.findings import clip_list, finding
from web_search_neo.fetch.safety import REDACTED

# How many script files one report fetches, and how many API descriptions and
# source maps it opens. Maps are followed by reference only, never guessed.
MAX_SCRIPTS = 10
MAX_OPENAPI = 2
MAX_SOURCEMAPS = 2
# A string literal worth a second look: long, mixed-case alphanumerics.
_ENTROPY_MIN_LEN = 24
_ENTROPY_MIN_BITS = 4.0
# Token-shaped only: code fragments split by quotes carry braces, semicolons
# and spaces, while keys and tokens do not - that is what tells them apart.
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9+/=_.-]+$")


def _looks_like_token(value: str) -> bool:
    return len(value) >= _ENTROPY_MIN_LEN and _entropy(value) >= _ENTROPY_MIN_BITS \
        and _TOKEN_CHARS.match(value) is not None \
        and re.search(r"[a-z]", value) and re.search(r"[A-Z]", value) \
        and re.search(r"\d", value)

_PATTERNS: list[tuple[str, str, str, str, str, str]] = [
    # (finding id, status, severity, title, fix, regex).
    ("secret-private-key", "fail", "high", "A private key ships in client code",
     "Remove the key from everything the browser receives; issue a new one - this one is public.",
     r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ("secret-aws-key", "fail", "high", "An AWS access key ships in client code",
     "Delete the key in IAM and move the call server-side; the key in the bundle is compromised.",
     r"\bAKIA[0-9A-Z]{16}\b"),
    ("secret-github-token", "fail", "high", "A GitHub token ships in client code",
     "Revoke the token on github.com and move the call server-side.",
     r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    ("secret-slack-token", "fail", "high", "A Slack token ships in client code",
     "Revoke the token in the Slack app settings and move the call server-side.",
     r"\bxox[bpas]-[A-Za-z0-9-]{8,}\b"),
    ("secret-google-key", "fail", "high", "A Google API key ships in client code",
     "Restrict the key to your bundle id / IP in Google Cloud, or proxy the call server-side.",
     r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    ("secret-stripe-key", "fail", "high", "A Stripe key ships in client code",
     "Publishable keys are public by design, but a secret key here is compromised: roll it and "
     "keep it server-side.",
     r"\b(?:sk|rk)-(?:live|test)-[0-9A-Za-z]{8,}\b"),
    ("secret-generic", "warn", "medium", "A secret-looking assignment in client code",
     "Move the secret server-side; a value the browser receives is public, whatever its name.",
     r"(?i)(?:api[_-]?key|secret|passwd|pwd|auth[_-]?token)\s*[:=]\s*['\"]([^'\"]{8,})['\"]"),
    ("secret-jwt", "warn", "medium", "A JWT ships in client code",
     "Short-lived, tightly-scoped tokens only; a readable token must not unlock anything its "
     "reader should not have.",
     r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
    ("secret-internal-url", "info", "info", "Internal URLs in client code",
     "Internal hostnames and addresses tell an attacker where to look next; keep them out of "
     "the bundle when you can.",
     r"https?://(?:10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+"
     r"|localhost(?::\d+)?(?:/[^\s'\"`]*)?|[\w.-]*\.(?:internal|local|lan|home\.arpa)(?::\d+)?"
     r"(?:/[^\s'\"`]*)?)"),
]
_PATTERNS_COMPILED = [(fid, status, severity, title, fix, re.compile(rx)) for
                      fid, status, severity, title, fix, rx in _PATTERNS]
# A generic assignment whose value is already a known credential kind stays one finding.
_CREDENTIAL_RES = [rx for fid, _s, _v, _t, _f, rx in _PATTERNS_COMPILED
                   if fid not in {"secret-generic", "secret-entropy", "secret-internal-url"}]

_FETCH_CALL = re.compile(r"fetch\(\s*['\"`]([^'\"`]+)['\"`]")
_AXIOS_CALL = re.compile(r"axios\.(get|post|put|patch|delete)\(\s*['\"`]([^'\"`]+)['\"`]", re.IGNORECASE)
_METHOD_NEARBY = re.compile(r"method\s*:\s*['\"`](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"`]", re.IGNORECASE)
_API_LITERAL = re.compile(r"['\"`]((?:/[^'\"`\n]*api[^'\"`\n]*|[^'\"`\n]*api[^'\"`\n]*/[^'\"`\n]*))['\"`]")
_OPENAPI_REF = re.compile(r"(openapi\.json|swagger\.json|swagger/v\d+/swagger\.json|api-docs(?:\.json)?)"
                          r"|(['\"`])((?:https?://[^\s'\"`]+|/)[^\s'\"`]*?(?:openapi|swagger)[^\s'\"`]*\.json)\2")
_LITERAL = re.compile(r"['\"`]([^'\"`\n]{1,400})['\"`]")
_SOURCEMAP = re.compile(r"//# sourceMappingURL=(\S+)|/\*# sourceMappingURL=(\S+?)\s*\*/")


def sourcemap_refs(text: str) -> list[str]:
    """Source-map references a script carries: data: URLs are not fetchable files."""
    refs: list[str] = []
    for match in _SOURCEMAP.finditer(text or ""):
        ref = match.group(1) or match.group(2)
        if ref and not ref.lower().startswith("data:") and ref not in refs:
            refs.append(ref)
    return refs


def sourcemap_view(url: str, text: str) -> dict[str, Any] | None:
    """A fetched source map in one line: source names and whether it ships code.

    ``sourcesContent`` never reaches the answer: names and counts travel, code
    does not - the map itself is the leak being reported.
    """
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("sources"), list):
        return None
    names = [str(name) for name in doc["sources"] if isinstance(name, str)]
    content = doc.get("sourcesContent")
    has_content = isinstance(content, list) and any(isinstance(item, str) and item for item in content)
    return {"url": api_parts.safe_url(url), "sources": len(names),
            "names": names[:8], "names_omitted": max(0, len(names) - 8),
            "has_content": bool(has_content)}


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    return -sum(hit / len(text) * math.log2(hit / len(text)) for hit in counts.values())


def _redact_match(match: re.Match[str]) -> str:
    """The match with its secret value replaced; the assignment around it stays readable."""
    if match.lastindex:
        full = match.group(0)
        value = match.group(match.lastindex) or ""
        head, _sep, tail = full.partition(value) if value else (full, "", "")
        return head + REDACTED + tail
    return REDACTED


def _redact_entropy(match: re.Match[str]) -> str:
    if _looks_like_token(match.group(1)):
        return match.group(0)[:1] + REDACTED + match.group(0)[-1:]
    return match.group(0)


def _sample(text: str, pos: int) -> tuple[str, int]:
    """The match's own line, fully redacted: no window ever cuts a secret in half."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start: end if end != -1 else len(text)]
    for _fid, _status, _severity, _title, _fix, rx in _PATTERNS_COMPILED:
        line = rx.sub(_redact_match, line)
    line = _LITERAL.sub(_redact_entropy, line)
    line = api_parts.mask_text(line).strip()
    lineno = text.count("\n", 0, pos) + 1
    if len(line) > 200:
        # Center on the redacted match, not on the line start: on a minified
        # single-line bundle line[:200] would show an unrelated head of code.
        hit = line.find(REDACTED)
        start = max(0, hit - 80) if hit != -1 else 0
        line = line[start: start + 200]
    return line, lineno


def scan_text(text: str, source: str) -> list[dict[str, Any]]:
    """Findings for one script text; secrets never reach the evidence, even across samples."""
    hits: dict[str, list[dict[str, Any]]] = {}
    for fid, status, severity, title, fix, rx in _PATTERNS_COMPILED:
        for match in rx.finditer(text or ""):
            if fid == "secret-generic" and match.lastindex \
                    and any(rx2.search(match.group(match.lastindex) or "") for rx2 in _CREDENTIAL_RES):
                continue  # the value already has its own finding (aws/github/jwt/...)
            sample, lineno = _sample(text, match.start())
            if sample:
                hits.setdefault(fid, []).append({"file": source, "line": lineno, "sample": sample})
    for match in _LITERAL.finditer(text or ""):
        if _looks_like_token(match.group(1)):
            sample, lineno = _sample(text, match.start())
            if sample:
                hits["secret-entropy"] = [{"file": source, "line": lineno, "sample": sample}]
                break  # one flag per file is enough: the file, not the string, is the problem
    out = []
    for fid, status, severity, title, fix, _rx in _PATTERNS_COMPILED:
        if fid in hits:
            out.append(finding(fid, "secrets", status, severity, title,
                               detail="A value the browser receives is public, whatever it unlocks: "
                                      "it ships to every visitor and lives in caches and archives.",
                               fix=fix, evidence=clip_list(hits[fid], api_parts.EVIDENCE_LIMIT)))
    if "secret-entropy" in hits:
        out.append(finding("secret-entropy", "secrets", "warn", "medium",
                           "A high-entropy literal in client code",
                           detail="A long mixed-case alphanumeric string: often a token or key baked "
                                  "into the bundle. Values are masked here; check what it unlocks.",
                           fix="Move secrets server-side; keep only short-lived scoped tokens in the bundle.",
                           evidence=clip_list(hits["secret-entropy"], api_parts.EVIDENCE_LIMIT)))
    return out


def code_endpoints(text: str, source: str, own: set[str]) -> list[dict[str, Any]]:
    """API routes the code calls: fetch/axios targets and /api/ literals, own scope only."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    taken: list[tuple[int, int]] = []
    for match in _FETCH_CALL.finditer(text or ""):
        nearby = text[match.end(): match.end() + 200].split(";", 1)[0]
        method = _METHOD_NEARBY.search(nearby)
        _add_call(found, (method.group(1).upper() if method else "GET"), match.group(1), source, own)
        taken.append(match.span(1))
    for match in _AXIOS_CALL.finditer(text or ""):
        _add_call(found, match.group(1).upper(), match.group(2), source, own)
        taken.append(match.span(2))
    for match in _API_LITERAL.finditer(text or ""):
        if any(start <= match.start(1) and match.end(1) <= end for start, end in taken):
            continue  # already counted as a fetch/axios target
        _add_call(found, "GET", match.group(1), source, own)
    return sorted(found.values(), key=lambda item: (item["path"], item["method"]))


def _add_call(found: dict[tuple[str, str], dict[str, Any]], method: str, target: str,
              source: str, own: set[str]) -> None:
    if not target or target.startswith(("data:", "blob:", "javascript:", "#")):
        return
    if "://" in target and not api_parts.is_own(target, own):
        return  # third-party calls are api_report's traffic, not this map
    try:
        path = api_parts.path_template(target)
    except Exception:
        return
    key = (method, path)
    entry = found.setdefault(key, {"method": method, "path": path, "count": 0, "sources": []})
    entry["count"] += 1
    if source not in entry["sources"]:
        entry["sources"].append(source)


def openapi_refs(html: str, texts: list[str], journal_urls: list[str]) -> list[str]:
    """API descriptions the page itself references: links, code literals, loaded URLs."""
    refs: list[str] = []
    for match in _OPENAPI_REF.finditer(html or ""):
        refs.append(match.group(1) or match.group(3))
    for text in texts:
        for match in _OPENAPI_REF.finditer(text or ""):
            refs.append(match.group(1) or match.group(3))
    refs.extend(url for url in journal_urls if _OPENAPI_REF.search(url or ""))
    seen: list[str] = []
    for ref in refs:
        if ref and ref not in seen:
            seen.append(ref)
    return seen


def openapi_view(url: str, text: str) -> dict[str, Any] | None:
    """A fetched API description in one line: version and path count, or None."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    version = doc.get("openapi") or doc.get("swagger")
    paths = doc.get("paths")
    if version is None and not isinstance(paths, dict):
        return None
    return {"url": api_parts.safe_url(url), "version": str(version or "unknown"),
            "paths": len(paths) if isinstance(paths, dict) else 0}


def _entries(items: Any) -> list[dict[str, Any]]:
    """Snapshot lists arrive capped as {"items": [...]}; anything else is dropped."""
    if isinstance(items, dict):
        items = items.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def link_urls(snapshot: Any) -> list[str]:
    """The snapshot's links: strings, not dicts, in arrival order, deduplicated."""
    value = snapshot.get("links") if isinstance(snapshot, dict) else None
    items = value.get("items") if isinstance(value, dict) else value
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        text = str(item)
        if text and text not in out:
            out.append(text)
    return out


def auth_facts(forms: Any, passwords: Any,
               page_https: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Login forms as facts, plus the findings no other report owns for them."""
    forms = _entries(forms)
    passwords = _entries(passwords)
    facts = [{"action": form.get("action"), "method": form.get("method"),
              "https": str(form.get("action") or "").startswith("https:"),
              "has_password": bool(form.get("has_password"))} for form in forms]
    findings: list[dict[str, Any]] = []
    http_logins = [fact for fact in facts if fact["has_password"] and not (page_https and fact["https"])]
    if http_logins:
        findings.append(finding(
            "secret-auth-http", "auth", "fail", "high", "A sign-in form posts over http",
            detail="The password travels in clear text on the network.",
            fix="Serve the page and the form action over https, with HSTS.",
            evidence=clip_list(http_logins, api_parts.EVIDENCE_LIMIT)))
    missing = [item for item in passwords or [] if not item.get("autocomplete")]
    if missing:
        findings.append(finding(
            "secret-auth-autocomplete", "auth", "warn", "low",
            "Password fields without a password autocomplete token",
            detail="Password managers cannot recognise the field reliably.",
            fix='Use autocomplete="current-password" on sign-in and "new-password" on sign-up forms.',
            evidence=clip_list([{"name": item.get("name")} for item in missing],
                               api_parts.EVIDENCE_LIMIT)))
    return facts, findings


def script_urls(snapshot_scripts: Any, journal_urls: list[str],
                page_url: str, own: set[str]) -> list[str]:
    """Same-scope script files: the served HTML's <script src> plus what the browser loaded."""
    urls: list[str] = []
    for entry in _entries(snapshot_scripts):
        src = entry.get("src") if isinstance(entry, dict) else None
        if src and api_parts.is_own(str(src), own) and str(src) not in urls:
            urls.append(str(src))
    for url in journal_urls or []:
        if urlsplit(str(url)).path.lower().endswith(".js") and api_parts.is_own(str(url), own) \
                and str(url) not in urls:
            urls.append(str(url))
    _ = page_url
    return urls[:MAX_SCRIPTS]


def build(page_url: str, html: str, scripts: list[dict[str, Any]],
          openapi_docs: list[dict[str, Any]], sourcemaps: list[dict[str, Any]],
          jar: list[Any] | None, storage: Any, hosts: list[str] | None,
          requests_made: list[str]) -> dict[str, Any]:
    """The assembled secret_scan report: findings with priority and fixes."""
    parts = api_parts
    own = parts.own_sites(str(page_url or ""), hosts)
    findings: list[dict[str, Any]] = []
    for script in scripts:
        findings += scan_text(str(script.get("text") or ""), str(script.get("url") or "inline"))
    endpoints: dict[tuple[str, str], dict[str, Any]] = {}
    for script in scripts:
        for item in code_endpoints(str(script.get("text") or ""), str(script.get("url") or "inline"), own):
            key = (item["method"], item["path"])
            slot = endpoints.setdefault(key, {"method": key[0], "path": key[1], "count": 0, "sources": []})
            slot["count"] += item["count"]
            for source in item["sources"]:
                if source not in slot["sources"]:
                    slot["sources"].append(source)
    snapshot = page_checks.snapshot_from_html(html or "", str(page_url or ""))
    facts, auth_hit = auth_facts(snapshot.get("forms") or [], snapshot.get("passwords") or [],
                                 str(page_url or "").startswith("https:"))
    findings += auth_hit
    if openapi_docs:
        findings.append(finding(
            "secret-openapi-exposed", "surface", "warn", "medium",
            "The API description is public",
            detail="An OpenAPI/Swagger document answers unauthenticated GETs: it maps every "
                   "endpoint, parameter and schema for an attacker.",
            fix="Gate the description behind auth on non-public APIs, or serve a redacted copy.",
            evidence=clip_list(openapi_docs, parts.EVIDENCE_LIMIT)))
    for seen in sourcemaps or []:
        if seen.get("has_content"):
            findings.append(finding(
                "secret-sourcemap-sources", "surface", "fail", "high",
                "A source map ships original sources",
                detail="The map carries sourcesContent: the bundle's original code - comments, "
                       "internal paths and any secret baked into it - downloads with one GET.",
                fix="Do not deploy source maps (or their sourcesContent) to production; keep "
                    "them on the build host for debugging.",
                evidence={"url": seen.get("url"), "sources": seen.get("sources"),
                          "names": seen.get("names")}))
        else:
            findings.append(finding(
                "secret-sourcemap-exposed", "surface", "warn", "medium",
                "A source map exposes the file layout",
                detail="The map names every original source file: an attacker learns the code "
                       "structure and where to look next.",
                fix="Do not deploy source maps to production unless the code is public anyway.",
                evidence={"url": seen.get("url"), "sources": seen.get("sources"),
                          "names": seen.get("names")}))
    cookies = parts.cookie_views(jar or [], own)
    local, session, storage_error = parts.storage_views(storage)
    token_names = sorted({entry["name"] for entry in cookies + local + session
                          if entry["format"] == "jwt" or parts._TOKEN_NAME.search(entry["name"])})
    counts = grading.counts(findings)
    order = grading.priority(findings)
    first = "; ".join(item["title"] for item in order[:3]) or "nothing to fix"
    report: dict[str, Any] = {
        "success": True, "url": parts.safe_url(str(page_url or "")),
        "scripts": [{"url": parts.safe_url(str(item.get("url") or "inline")),
                     "bytes": int(item.get("bytes") or 0)} for item in scripts],
        "code_endpoints": sorted(endpoints.values(), key=lambda item: (-item["count"], item["path"])),
        "openapi": openapi_docs,
        "sourcemaps": list(sourcemaps or []),
        "auth_forms": facts,
        "token_names": token_names,
        **({"storage_note": storage_error} if storage_error else {}),
        "counts": counts,
        "summary_line": f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low. "
                        f"Fix first: {first}.",
        "priority": order, "findings": findings,
        "scope": ("secret_scan is passive: the page, its same-scope scripts and an API description "
                  "the page references, over ordinary GETs only - everything listed in requests_made. "
                  "Third-party scripts are named, never fetched."),
        "requests_made": list(requests_made),
    }
    return report


def third_party_scripts(snapshot_scripts: Any, page_url: str,
                        hosts: list[str] | None,
                        journal_urls: list[str] | None = None) -> list[str]:
    """Script files outside the scope: named in the answer, never fetched.

    Served HTML names the static ones; the journal adds what scripts injected
    later (consent managers, tag loaders) - both are only named.
    """
    parts = api_parts
    own = parts.own_sites(str(page_url or ""), hosts)
    out: list[str] = []
    candidates: list[str] = []
    for entry in _entries(snapshot_scripts):
        src = entry.get("src") if isinstance(entry, dict) else None
        if src:
            candidates.append(str(src))
    for url in journal_urls or []:
        if urlsplit(str(url)).path.lower().endswith(".js"):
            candidates.append(str(url))
    for candidate in candidates:
        if not parts.is_own(candidate, own):
            safe = parts.safe_url(candidate)
            if safe not in out:
                out.append(safe)
    return out
