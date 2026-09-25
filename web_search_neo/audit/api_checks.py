"""Проверки api_report: CORS, кэширование, Content-Type, тексты ошибок, транспорт, токены, CSRF, URL.

Каждая функция — чистая: список строк журнала (или ответов своего сайта) на
входе, список находок в форме audit.findings на выходе. Ни одна не шлёт запрос
и не читает браузер.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

from web_search_neo.audit import sites
from web_search_neo.audit.api_parts import (EVIDENCE_LIMIT, _CSRF_NAME, _FRAMEWORK_VERSION, _name_is_sensitive,
                                            _STACK, _SERVER_PATH, _status, _TOKEN_NAME, _token_evidence,
                                            _value_is_secret, channel, mask_text, safe_url, url_origin,
                                            WRITE_METHODS)
from web_search_neo.audit.findings import clip_list, finding, info, passed

def cors_findings(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CORS на ответах своего API: какой origin разрешён и как это сочетается с credentials."""
    out: list[dict[str, Any]] = []
    wildcard_credentials: list[str] = []
    null_origin: list[str] = []
    wildcard: list[str] = []
    allowed: dict[str, int] = {}
    preflights: list[dict[str, Any]] = []
    cors_seen = False
    for row in responses:
        headers = row.get("headers") or {}
        if not isinstance(headers, dict) or not headers:
            continue
        url = safe_url(str(row.get("url") or ""))
        acao = str(headers.get("access-control-allow-origin") or "").strip()
        if acao:
            cors_seen = True
            credentials = str(headers.get("access-control-allow-credentials") or "").strip().lower() == "true"
            if acao == "*" and credentials:
                wildcard_credentials.append(url)
            elif acao == "null":
                null_origin.append(url)
            elif acao == "*":
                wildcard.append(url)
            else:
                allowed[acao] = allowed.get(acao, 0) + 1
        if str(row.get("method") or "").upper() == "OPTIONS":
            entry: dict[str, Any] = {"url": url}
            for key, header in (("allow_methods", "access-control-allow-methods"),
                                ("allow_headers", "access-control-allow-headers"),
                                ("max_age", "access-control-max-age")):
                if headers.get(header):
                    entry[key] = str(headers[header])
            preflights.append(entry)
    if wildcard_credentials:
        out.append(finding(
            "api-cors-wildcard-credentials", "cors", "fail", "medium",
            "API responses allow '*' together with credentials",
            detail="Access-Control-Allow-Origin: * with Allow-Credentials: true is a combination browsers "
                   "refuse, so cross-origin calls with cookies cannot work - and it shows the API intended "
                   "to answer everyone with the session attached.",
            fix="Answer with the exact origin you trust (https://your-site.example) plus "
                "Allow-Credentials: true, or drop credentials; never '*'.",
            evidence=clip_list(sorted(set(wildcard_credentials)), EVIDENCE_LIMIT)))
    if null_origin:
        out.append(finding(
            "api-cors-null-origin", "cors", "fail", "high",
            "API responses allow the null origin",
            detail="Access-Control-Allow-Origin: null answers sandboxed iframes and data: documents - "
                   "any script that can reach the API from one of those reads the response.",
            fix="Allow only your own origin(s) explicitly; never 'null'.",
            evidence=clip_list(sorted(set(null_origin)), EVIDENCE_LIMIT)))
    if wildcard:
        out.append(finding(
            "api-cors-wildcard", "cors", "warn", "low",
            "API responses allow any origin (*)",
            detail="Any website can read these responses in a visitor's browser. Without credentials the "
                   "data is whatever an anonymous caller sees - if the response changes with cookies or "
                   "a bearer token, this is a data leak.",
            fix="Allow your own origin(s) explicitly; keep '*' only for genuinely public data.",
            evidence=clip_list(sorted(set(wildcard)), EVIDENCE_LIMIT)))
    if allowed:
        out.append(info(
            "api-cors-allowed-origin", "cors", "The API allows specific origins",
            detail="Access-Control-Allow-Origin values seen on your API responses, with how many "
                   "responses carried each; check every one is an origin you own or trust.",
            evidence=clip_list([{"origin": origin, "responses": count}
                                for origin, count in sorted(allowed.items(), key=lambda kv: -kv[1])],
                               EVIDENCE_LIMIT)))
    if preflights:
        out.append(info(
            "api-cors-preflight", "cors", "CORS preflights answered with methods and headers",
            detail="What the API's OPTIONS responses allow cross-origin requests to send; a preflight "
                   "allowing more methods or headers than your API needs widens its attack surface.",
            evidence=clip_list(preflights, EVIDENCE_LIMIT)))
    if responses and not cors_seen:
        out.append(passed(
            "api-cors-none", "cors", "API responses do not allow cross-origin reads",
            detail="No Access-Control-Allow-Origin header was recorded on your API responses: browsers "
                   "keep them same-origin only.",
            evidence={"responses": len(responses)}))
    return out


def cache_findings(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Кэширование: приватные данные (JSON, cookie) не должны кэшироваться как public."""
    out: list[dict[str, Any]] = []
    public: list[dict[str, Any]] = []
    public_user_data: list[dict[str, Any]] = []
    missing: list[str] = []
    private = 0
    considered = 0
    for row in responses:
        status = _status(row)
        if not 200 <= status < 300:
            continue
        headers = row.get("headers") or {}
        if not isinstance(headers, dict) or not headers:
            continue
        jsonish = "json" in str(row.get("mime") or "").lower()
        set_cookie = bool(headers.get("set-cookie"))
        if not jsonish and not set_cookie:
            continue  # «данные пользователя»: JSON-ответ или ответ, ставящий cookie
        considered += 1
        url = safe_url(str(row.get("url") or ""))
        cache_control = str(headers.get("cache-control") or "")
        pragma = str(headers.get("pragma") or "")
        lowered = cache_control.lower()
        entry = {"url": url, "cache_control": cache_control, "set_cookie": set_cookie}
        if "public" in lowered and not any(word in lowered for word in ("private", "no-store", "no-cache")):
            (public_user_data if set_cookie else public).append(entry)
        elif any(word in lowered for word in ("no-store", "private", "no-cache")) or "no-cache" in pragma.lower():
            private += 1
        else:
            missing.append(url)
    if public_user_data:
        out.append(finding(
            "api-cache-public-user-data", "cache", "fail", "high",
            "A user-data response is cached as public while it sets a cookie",
            detail="Cache-Control: public on a response that also sets a cookie lets shared caches store "
                   "per-user data and replay it to other visitors.",
            fix="Answer with Cache-Control: no-store (or private) on any response that identifies the user.",
            evidence=clip_list(public_user_data, EVIDENCE_LIMIT)))
    if public:
        out.append(finding(
            "api-cache-public", "cache", "warn", "medium",
            "A JSON API response is cached as public",
            detail="Cache-Control: public on JSON data: shared caches and proxies may store it. If the "
                   "payload ever depends on who asked (even indirectly, via a token), this serves it to "
                   "the wrong people.",
            fix="Use Cache-Control: private or no-store for data that varies with the user; keep public "
                "only for payloads every visitor may share.",
            evidence=clip_list(public, EVIDENCE_LIMIT)))
    if missing:
        out.append(finding(
            "api-cache-missing", "cache", "warn", "low",
            "A JSON API response carries no Cache-Control or Pragma",
            detail="Without an explicit policy a browser may heuristically cache the response and reuse "
                   "it after a logout or a profile change.",
            fix="Set an explicit Cache-Control (no-store for user data, or a max-age you mean) on JSON "
                "API responses.",
            evidence=clip_list(sorted(set(missing)), EVIDENCE_LIMIT)))
    if private:
        out.append(passed(
            "api-cache-private", "cache", "User-data responses opt out of shared caching",
            detail="Responses with Cache-Control no-store/private/no-cache (or Pragma: no-cache).",
            evidence={"responses": private, "considered": considered}))
    return out


def content_findings(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content-Type и X-Content-Type-Options на JSON-ответах своего API."""
    out: list[dict[str, Any]] = []
    nosniff_missing: list[str] = []
    type_missing: list[str] = []
    json_responses = 0
    for row in responses:
        if "json" not in str(row.get("mime") or "").lower():
            continue
        headers = row.get("headers") or {}
        if not isinstance(headers, dict) or not headers:
            continue
        json_responses += 1
        url = safe_url(str(row.get("url") or ""))
        if not str(headers.get("content-type") or "").strip():
            type_missing.append(url)
        if str(headers.get("x-content-type-options") or "").strip().lower() != "nosniff":
            nosniff_missing.append(url)
    if nosniff_missing:
        out.append(finding(
            "api-json-nosniff", "content", "warn", "low",
            "JSON responses without X-Content-Type-Options: nosniff",
            detail="A browser may sniff a JSON response into another type (HTML, image) and treat it as "
                   "such; nosniff pins the Content-Type.",
            fix="Add X-Content-Type-Options: nosniff to every JSON API response.",
            evidence=clip_list(sorted(set(nosniff_missing)), EVIDENCE_LIMIT)))
    if type_missing:
        out.append(finding(
            "api-json-content-type", "content", "warn", "low",
            "A JSON response arrived with no Content-Type header",
            detail="Without Content-Type the browser decides the type by sniffing, and intermediaries "
                   "handle the response unpredictably.",
            fix="Set Content-Type: application/json (with charset) on JSON API responses.",
            evidence=clip_list(sorted(set(type_missing)), EVIDENCE_LIMIT)))
    if json_responses and not nosniff_missing and not type_missing:
        out.append(passed(
            "api-json-headers", "content", "JSON responses carry Content-Type and nosniff",
            detail="Every recorded JSON API response had a Content-Type and "
                   "X-Content-Type-Options: nosniff.",
            evidence={"responses": json_responses}))
    return out


def error_findings(error_rows: list[dict[str, Any]], bodies: dict[str, str], unread: int) -> list[dict[str, Any]]:
    """Тексты ответов с 4xx/5xx: стек-трейсы, пути сервера, версии фреймворков."""
    out: list[dict[str, Any]] = []
    hits: dict[str, list[str]] = {"api-error-stack-trace": [], "api-error-server-path": [],
                                  "api-error-framework-version": []}
    samples: dict[str, list[str]] = {key: [] for key in hits}
    patterns = {"api-error-stack-trace": _STACK, "api-error-server-path": _SERVER_PATH,
                "api-error-framework-version": _FRAMEWORK_VERSION}
    checked = 0
    for row in error_rows:
        text = bodies.get(str(row.get("id") or ""))
        if not isinstance(text, str) or not text:
            continue
        checked += 1
        url = safe_url(str(row.get("url") or ""))
        for key, pattern in patterns.items():
            match = pattern.search(text)
            if match:
                hits[key].append(url)
                snippet = mask_text(text[max(0, match.start() - 60): match.end() + 80]).strip()
                if len(samples[key]) < 2 and snippet:
                    samples[key].append(snippet[:200])
    titles = {"api-error-stack-trace": ("An error response leaks a stack trace", "fail", "high",
                                        "The error body carries a stack trace: it names code structure, "
                                        "internal call paths and sometimes arguments.",
                                        "Turn debug tracebacks off in production; answer with an error "
                                        "code/id and log the details server-side."),
              "api-error-server-path": ("An error response leaks server file paths", "fail", "medium",
                                        "The error body contains filesystem paths of the server: an "
                                        "attacker learns the layout and can aim further requests at it.",
                                        "Return a generic error message; keep paths and file names in the "
                                        "server log only."),
              "api-error-framework-version": ("An error response discloses a framework version", "warn", "medium",
                                              "The error body names the framework and its version: known "
                                              "vulnerabilities can be matched against it.",
                                              "Suppress version banners and debug pages; return a plain "
                                              "JSON error without the stack or the version.")}
    for key, (title, status, severity, detail, fix) in titles.items():
        if hits[key]:
            out.append(finding(
                key, "errors", status, severity, title, detail=detail, fix=fix,
                evidence={"urls": clip_list(sorted(set(hits[key])), EVIDENCE_LIMIT),
                          "samples": samples[key]}))
    if error_rows and checked < len(error_rows):
        out.append(info(
            "api-error-body-unread", "errors", "Not every error body could be read",
            detail=f"{checked} of {len(error_rows)} error responses had a body in Chrome's buffer; the "
                   "rest were dropped (a navigation, a filled buffer, a redirect without a body). Reload "
                   "the page or use replay_request and run api_report again to see them.",
            evidence={"read": checked, "total": len(error_rows), "failed_reads": int(unread)}))
    elif error_rows and not any(hits.values()):
        out.append(passed(
            "api-error-body-clean", "errors", "Error bodies leak nothing",
            detail="The error responses' bodies carried no stack traces, server paths or framework "
                   "versions.",
            evidence={"responses": checked}))
    return out


def transport_findings(rows: list[dict[str, Any]], page_url: str) -> list[dict[str, Any]]:
    """Транспорт: ws:// против wss://, http с https-страницы, API на стороннем origin."""
    out: list[dict[str, Any]] = []
    page_https = str(page_url or "").startswith("https:")
    cleartext_ws = [safe_url(str(row.get("url") or "")) for row in rows if channel(row) == "websocket"
                    and str(row.get("url") or "").lower().startswith("ws://")]
    if cleartext_ws:
        if page_https:
            out.append(finding(
                "api-ws-cleartext", "transport", "fail", "high",
                "The https page opens a ws:// WebSocket",
                detail="Browsers block cleartext ws:// from an https page as mixed content, so the "
                       "socket cannot connect - and anywhere it did connect, the traffic is readable on "
                       "the network.",
                fix="Serve the socket over wss:// (TLS) and redirect ws:// to it.",
                evidence=clip_list(sorted(set(cleartext_ws)), EVIDENCE_LIMIT)))
        else:
            out.append(finding(
                "api-ws-cleartext", "transport", "warn", "low",
                "The page opens a cleartext ws:// WebSocket",
                detail="Normal for a local http development server, but an https page cannot open it: "
                       "browsers block cleartext WebSockets from https.",
                fix="Use wss:// in production and keep ws:// for localhost only.",
                evidence=clip_list(sorted(set(cleartext_ws)), EVIDENCE_LIMIT)))
    plain = [safe_url(str(row.get("url") or "")) for row in rows
             if str(row.get("url") or "").lower().startswith("http://")]
    if page_https and plain:
        out.append(finding(
            "api-http-from-https", "transport", "fail", "high",
            "The https page sent http:// requests",
            detail="Cleartext requests from an https page: the browser blocks most of them (mixed "
                   "content), and any that go through travel readable.",
            fix="Call your API over https://; redirect http:// to https:// on the same host.",
            evidence=clip_list(sorted(set(plain)), EVIDENCE_LIMIT)))
    third_party: dict[str, dict[str, Any]] = {}
    for row in rows:
        if channel(row) is None or not sites.is_third_party(str(row.get("url") or ""), str(page_url or "")):
            continue
        origin = url_origin(str(row.get("url") or ""))
        if not origin:
            continue
        entry = third_party.setdefault(origin, {"origin": origin, "calls": 0, "channels": {}})
        entry["calls"] += 1
        entry["channels"][channel(row)] = entry["channels"].get(channel(row), 0) + 1
    if third_party:
        out.append(finding(
            "api-third-party-api", "transport", "warn", "medium",
            "The page calls backends on other sites' origins",
            detail="API traffic to origins you do not own: their CORS, TLS and retention policy applies "
                   "to your users' data, and each one is a supply-chain dependency.",
            fix="Confirm every listed origin is required, route what you can through your own origin, "
                "and send third parties only the data they need.",
            evidence=clip_list(sorted(third_party.values(), key=lambda item: -item["calls"]),
                               EVIDENCE_LIMIT)))
    return out


def auth_findings(cookies: list[dict[str, Any]], local: list[dict[str, Any]], session: list[dict[str, Any]],
                  page_https: bool, storage_error: str | None) -> list[dict[str, Any]]:
    """Где живут токены: cookie с флагами против localStorage/sessionStorage, факты JWT."""
    out: list[dict[str, Any]] = []
    token_cookies = [entry for entry in cookies if entry["format"] == "jwt" or _TOKEN_NAME.search(entry["name"])]
    local_tokens = _token_evidence(local, "localStorage")
    session_tokens = _token_evidence(session, "sessionStorage")
    if local_tokens:
        out.append(finding(
            "api-token-in-localstorage", "auth", "warn", "medium",
            "Tokens in localStorage",
            detail="localStorage is readable by any script on the origin: one XSS or a compromised "
                   "third-party script can read and exfiltrate these tokens. Names and formats only - "
                   "values are never reported.",
            fix="Keep access and refresh tokens in HttpOnly, Secure, SameSite cookies (or in memory "
                "only), not in localStorage.",
            evidence=clip_list(local_tokens, EVIDENCE_LIMIT)))
    if session_tokens:
        out.append(finding(
            "api-token-in-sessionstorage", "auth", "warn", "low",
            "Tokens in sessionStorage",
            detail="sessionStorage dies with the tab, but any script on the origin reads it while the "
                   "tab lives - the same XSS exposure as localStorage, only narrower.",
            fix="Prefer HttpOnly cookies or in-memory tokens over sessionStorage.",
            evidence=clip_list(session_tokens, EVIDENCE_LIMIT)))
    not_httponly = [entry for entry in token_cookies if not entry["httponly"]]
    if not_httponly:
        out.append(finding(
            "api-token-cookie-not-httponly", "auth", "warn", "medium",
            "A token cookie is readable from JavaScript",
            detail="A session/token cookie without HttpOnly is handed to every script on the origin: "
                   "document.cookie leaks it under XSS.",
            fix="Set HttpOnly on session and token cookies.",
            evidence=clip_list([{key: entry[key] for key in ("name", "domain", "format")}
                                for entry in not_httponly], EVIDENCE_LIMIT)))
    insecure = [entry for entry in token_cookies if page_https and not entry["secure"]]
    if insecure:
        out.append(finding(
            "api-token-cookie-not-secure", "auth", "warn", "low",
            "A token cookie is sent without the Secure flag",
            detail="On an https page a cookie without Secure may still be sent over a plain http "
                   "redirect or a captive portal's http response.",
            fix="Set Secure on session and token cookies (and serve the site over https).",
            evidence=clip_list([{key: entry[key] for key in ("name", "domain", "format")}
                                for entry in insecure], EVIDENCE_LIMIT)))
    if token_cookies and not not_httponly:
        out.append(passed(
            "api-token-cookie-httponly", "auth", "Token cookies are HttpOnly",
            detail="Session/token cookies in the jar all carry HttpOnly; scripts cannot read them.",
            evidence=clip_list([entry["name"] for entry in token_cookies], EVIDENCE_LIMIT)))
    unsigned: list[dict[str, Any]] = []
    for entry in cookies:
        facts = entry.get("jwt")
        if entry["format"] == "jwt" and isinstance(facts, dict) and (not facts.get("signed")
                                                                     or facts.get("alg") == "none"):
            unsigned.append({"where": f"cookie {entry['name']}", **facts})
    for where, views in (("localStorage", local), ("sessionStorage", session)):
        for entry in views:
            facts = entry.get("jwt")
            if entry["format"] == "jwt" and isinstance(facts, dict) and (not facts.get("signed")
                                                                         or facts.get("alg") == "none"):
                unsigned.append({"where": f"{where} {entry['name']}", **facts})
    if unsigned:
        out.append(finding(
            "api-jwt-unsigned", "auth", "fail", "high",
            "A JWT is unsigned (alg 'none' or an empty signature)",
            detail="Anyone can mint a token this API accepts: sign it server-side (HS256/RS256/ES256) "
                   "and reject alg 'none' and tokens without a signature.",
            fix="Verify the signature and the alg claim on every protected endpoint; reject unsigned "
                "tokens.",
            evidence=clip_list(unsigned, EVIDENCE_LIMIT)))
    if storage_error:
        out.append(info(
            "api-storage-unavailable", "auth", "The page's storage could not be read",
            detail="localStorage/sessionStorage enumeration failed, so token locations are only "
                   "half-covered (cookie jar only). Reason: " + storage_error,
            evidence={"error": storage_error}))
    return out


def _csrf_param_names(row: dict[str, Any]) -> list[str]:
    """Имёна CSRF-подобных параметров в теле или query state-changing запроса (значения не нужны)."""
    names: list[str] = []
    text = str(row.get("post_data") or "")
    if text:
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                names += [str(key) for key in payload]
        if not names:
            names += [name for name, _ in parse_qsl(text, keep_blank_values=True)]
    url = str(row.get("url") or "")
    if "?" in url:
        names += [name for name, _ in parse_qsl(url.split("?", 1)[1].split("#", 1)[0], keep_blank_values=True)]
    seen: set[str] = set()
    out = []
    for name in names:
        if _CSRF_NAME.search(name) and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def csrf_findings(rows: list[dict[str, Any]], cookies: list[dict[str, Any]], page_url: str) -> list[dict[str, Any]]:
    """CSRF: SameSite на сессийных cookie, токен в state-changing запросе, запись на чужой origin."""
    out: list[dict[str, Any]] = []
    session_cookies = [entry for entry in cookies if entry["format"] == "jwt" or _TOKEN_NAME.search(entry["name"])]
    without_samesite = [entry for entry in session_cookies if not entry["sameSite"]]
    none_insecure = [entry for entry in session_cookies
                     if str(entry["sameSite"] or "").lower() == "none" and not entry["secure"]]
    if none_insecure:
        out.append(finding(
            "api-csrf-samesite-none-insecure", "csrf", "fail", "high",
            "A session cookie uses SameSite=None without Secure",
            detail="SameSite=None is only honoured over https; without Secure the browser drops the "
                   "attribute, and the cookie is sent in cross-site requests as it likes.",
            fix="Set Secure together with SameSite=None (or use SameSite=Lax/Strict instead).",
            evidence=clip_list([{key: entry[key] for key in ("name", "domain")}
                                for entry in none_insecure], EVIDENCE_LIMIT)))
    if without_samesite:
        out.append(finding(
            "api-csrf-samesite-missing", "csrf", "warn", "low",
            "A session/token cookie declares no SameSite",
            detail="Browsers default a missing attribute to Lax (a cross-site POST then carries no "
                   "cookie), but the guarantee is browser-dependent; Lax/Strict stated explicitly is "
                   "what every deployment can rely on.",
            fix="Set SameSite=Lax (Strict for session cookies never needed cross-site) on session and "
                "token cookies.",
            evidence=clip_list([{key: entry[key] for key in ("name", "domain")}
                                for entry in without_samesite], EVIDENCE_LIMIT)))
    if session_cookies and not without_samesite and not none_insecure:
        out.append(passed(
            "api-csrf-samesite", "csrf", "Session cookies declare SameSite",
            detail="Every session/token cookie in the jar carries an explicit SameSite attribute.",
            evidence=clip_list([f"{entry['name']}={entry['sameSite']}" for entry in session_cookies],
                               EVIDENCE_LIMIT)))
    with_token: list[dict[str, Any]] = []
    without_token: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("method") or "").upper() not in WRITE_METHODS:
            continue
        names = _csrf_param_names(row)
        entry = {"method": str(row.get("method") or "").upper(), "url": safe_url(str(row.get("url") or "")),
                 "csrf_params": names[:6]}
        (with_token if names else without_token).append(entry)
    if with_token:
        out.append(passed(
            "api-csrf-token-in-request", "csrf", "State-changing requests carry a CSRF token",
            detail="A CSRF-style parameter (name matching csrf/xsrf/authenticity_token/_token/...) was "
                   "visible in the body or query of these requests.",
            evidence=clip_list(with_token, EVIDENCE_LIMIT)))
    if without_token:
        out.append(info(
            "api-csrf-token-unseen", "csrf", "No CSRF token visible in some state-changing requests",
            detail="The journal records no request headers, so a token sent as X-CSRF-Token or "
                   "Authorization - and the server's Origin check - cannot be seen from here; if the "
                   "token is not in the body or query either, verify the server's Origin/SameSite "
                   "handling (replay_request can resend a request the page already made).",
            fix="Validate Origin/Referer or a per-session CSRF token server-side on every "
                "state-changing request.",
            evidence=clip_list(without_token, EVIDENCE_LIMIT)))
    cross_site = [entry for entry in without_token
                  if sites.is_third_party(entry["url"], str(page_url or ""))]
    if cross_site:
        out.append(finding(
            "api-csrf-cross-origin-write", "csrf", "warn", "medium",
            "State-changing requests leave your site without a visible CSRF token",
            detail="A POST/PUT/PATCH/DELETE to another registrable domain with no token in body or "
                   "query: cross-site cookies (SameSite=None setups) would ride along, and the receiving "
                   "server must validate Origin itself.",
            fix="Confirm the receiving endpoint validates Origin/Referer or a CSRF token; drop "
                "SameSite=None unless the cross-site write is deliberate.",
            evidence=clip_list(cross_site, EVIDENCE_LIMIT)))
    return out


def url_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Чувительные данные в query: токены, e-mail, идентификаторы сессий — значения замаскированы."""
    seen: dict[str, set[str]] = {}
    for row in rows:
        url = str(row.get("url") or "")
        if "?" not in url:
            continue
        query = url.split("?", 1)[1].split("#", 1)[0]
        if not query:
            continue
        hits = {name for name, value in parse_qsl(query, keep_blank_values=True)
                if _name_is_sensitive(name) or _value_is_secret(value)}
        if hits:
            seen[safe_url(url)] = hits
    if not seen:
        return []
    return [finding(
        "api-sensitive-query", "urls", "warn", "medium",
        "Secrets in request URLs",
        detail="Tokens, e-mail addresses or session ids in the query string end up in server logs, "
               "proxies, browser history and Referer headers. Values are masked here; only parameter "
               "names are listed.",
        fix="Move credentials into the request body, an Authorization header or an HttpOnly cookie, "
            "and keep URLs free of user data.",
        evidence=clip_list([{"url": url, "params": sorted(names)}
                            for url, names in sorted(seen.items())], EVIDENCE_LIMIT))]


