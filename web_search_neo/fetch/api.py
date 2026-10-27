"""Direct HTTP requests without a browser (REST API testing)."""

from __future__ import annotations

import logging
from typing import Any

from web_search_neo import web_client
from web_search_neo.fetch import headers as fetch_headers
from web_search_neo.fetch import hints as fetch_hints
from web_search_neo.fetch import politeness
from web_search_neo.fetch import sessions as http_sessions
from web_search_neo.fetch import variants
from web_search_neo.fetch.decoding import decode_response
from web_search_neo.fetch.safety import redact_url, write_download
from web_search_neo.web_client import clamp_timeout, request, validate_http_url

log = logging.getLogger("web_search_neo")

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    body: str | None = None,
    body_json: Any | None = None,
    timeout_seconds: float = 20.0,
    max_chars: int = 20_000,
    save_to: str | None = None,
    overwrite: bool = False,
    http_session: str | None = None,
    agent_label: str | None = None,
    http_session_clear: bool = False,
    show_values: bool = False,
    vary: dict[str, Any] | None = None,
    *,
    request_client: Any | None = None,
) -> dict[str, Any]:
    """Send one HTTP request and return its status, headers, and body.

    ``query`` becomes the URL query string; ``body`` (raw str) and
    ``body_json`` (serialised as JSON) are mutually exclusive. HTTP error
    statuses (4xx/5xx) are returned as a success envelope with their real
    status, while transport failures (DNS, timeout) raise like fetch_text.
    ``save_to`` is confined to the download directory (see fetch.safety) and
    refuses to replace an existing file unless ``overwrite`` is true.
    No cookies carry over between calls unless ``http_session`` names a jar
    (see fetch.sessions): kept per ``(agent_label, name)``, sent on the request,
    fed by every Set-Cookie of the response and its redirect hops, and reported
    as ``http_session`` - names and flags, values only with ``show_values``.
    ``http_session_clear`` empties that jar before this request is sent.
    Every Set-Cookie of the final response is listed in ``set_cookies``.
    ``vary`` turns this one request into a delta sweep (see fetch.variants):
    {"headers": {"X-Test": ["a", "b"]}} sends the baseline plus one request per
    change, each reported as a structural diff from the baseline. A body delta
    upgrades GET to POST. Sweep only a host you are allowed to probe.
    """
    if vary is not None and save_to:
        raise ValueError("a vary sweep reports several bodies: drop save_to, "
                         "or send vary=None")
    normalized_method = "GET" if method is None else str(method).strip().upper()
    if normalized_method not in _METHODS:
        raise ValueError(f"method must be one of {sorted(_METHODS)}, not {method!r}")
    if body is not None and body_json is not None:
        raise ValueError("body and body_json are mutually exclusive")
    extra: dict[str, Any] = {}
    if headers is not None:
        if not isinstance(headers, dict):
            raise ValueError("headers must be a {name: value} map")
        extra["headers"] = {str(key): str(value) for key, value in headers.items()}
    if query is not None:
        if not isinstance(query, dict):
            raise ValueError("query must be a {name: value} map")
        extra["params"] = dict(query)
    if body is not None:
        extra["data"] = body
    if body_json is not None:
        extra["json"] = body_json
        sent = extra.get("headers", {})
        if not any(str(name).lower() == "content-type" for name in sent):
            extra["headers"] = {**sent, "Content-Type": "application/json"}
    normalized_url = validate_http_url(url)
    baseline_spec = {
        "url": normalized_url,
        "method": normalized_method,
        "headers": dict(extra.get("headers") or {}),
        "query": dict(extra.get("params") or {}),
        "body": extra["data"] if "data" in extra else extra.get("json"),
    }
    session_jar = session_info = None
    if http_session is not None or http_session_clear:
        if http_session is None:
            raise ValueError("http_session_clear needs http_session: the name of the jar to empty")
        if any(str(name).lower() == "cookie" for name in extra.get("headers", {})):
            raise ValueError("A Cookie header replaces the http_session jar; send one or the other")
        session_jar, session_info = http_sessions.open_jar(
            http_session, agent_label, clear=bool(http_session_clear)
        )
        extra["cookie_jar"] = session_jar
    before = list(session_jar) if session_jar is not None else []
    log.info("HTTP %s %s", normalized_method, redact_url(normalized_url))
    byte_budget = min(max(1_000_000, int(max_chars) * 8), 10_000_000)
    client = request if request_client is None else request_client
    # Hardening fires only on the real client's path; injected clients (tests, plugins)
    # must see exactly the caller's words. Named headers always win too.
    live = client is web_client.request
    def _send(spec: dict[str, Any]) -> dict[str, Any]:
        """Send one spec through this call's client, jar and byte budget."""
        send_extra = dict(extra)
        send_extra["headers"] = dict(spec.get("headers") or {})
        send_extra["params"] = dict(spec.get("query") or {})
        for key in ("data", "json"):
            send_extra.pop(key, None)
        payload = spec.get("body")
        if isinstance(payload, (dict, list)):
            send_extra["json"] = payload
        elif payload is not None:
            send_extra["data"] = payload
        send_method = str(spec.get("method") or normalized_method)
        send_url = str(spec.get("url") or normalized_url)
        if live:
            if not send_extra.get("headers"):
                send_extra["headers"] = fetch_headers.browser_headers(send_url)
            politeness.gate(send_url)
            politeness.note_request(send_url)
        log.info("HTTP %s %s", send_method, redact_url(send_url))
        try:
            response = client(
                send_url,
                method=send_method,
                timeout_seconds=clamp_timeout(timeout_seconds),
                max_response_bytes=byte_budget,
                **send_extra,
            )
        except Exception as exc:
            error_response = getattr(exc, "response", None)
            if error_response is None or not isinstance(
                getattr(error_response, "status_code", None), int
            ):
                raise
            response = error_response
        if live:
            politeness.remember_retry_after(
                send_url, politeness.retry_after_seconds(getattr(response, "headers", None))
            )
        return _envelope(response, session_jar, session_info, before,
                         bool(show_values), save_to=save_to, overwrite=overwrite,
                         max_chars=max_chars)

    if vary is not None:
        sweep = variants.run_variants(_send, baseline_spec, vary)
        return {
            "success": True,
            "url": normalized_url,
            "method": normalized_method,
            "swept": sweep,
        }
    return _send(baseline_spec)


def _envelope(response: Any, session_jar: Any, session_info: Any, before: list,
        show_values: bool, save_to: str | None = None, overwrite: bool = False,
        max_chars: int = 20_000) -> dict[str, Any]:
    """One response object as the published http_request envelope."""
    status = int(response.status_code)
    final_url = getattr(response, "url", None) or ""
    resp_headers = dict(getattr(response, "headers", None) or {})
    # requests folds repeated Set-Cookie headers into one ", "-joined string,
    # which the commas inside Expires make unparseable: keep them apart too.
    set_cookies = http_sessions.set_cookie_values(response)
    if session_jar is not None:
        session_field = {"http_session": http_sessions.report(
            session_info, session_jar, before, response, bool(show_values))}
        if not show_values:
            http_sessions.redact_headers(resp_headers, set_cookies)
    else:
        session_field = {}
    cookie_field = {"set_cookies": set_cookies} if set_cookies else {}
    cookie_field.update(session_field)
    text_value, _charset = decode_response(response)
    raw = getattr(response, "content", None)
    if isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
    else:
        raw_bytes = text_value.encode("utf-8")
    size_bytes = len(raw_bytes)
    # An error body over the byte budget arrives cut short (web_client.request).
    cut_by_transport = getattr(response, "wsn_truncated", False) is True
    if save_to:
        resolved = write_download(str(save_to), raw_bytes, overwrite=bool(overwrite))
        return {
            "success": True,
            "url": final_url,
            "status": status,
            "headers": resp_headers, **cookie_field,
            "saved_to": str(resolved),
            "size_bytes": size_bytes,
            **({"truncated": True} if cut_by_transport else {}),
        }
    limit = max(1, int(max_chars))
    # A gate status is an answer, but not as a visitor yet: say so in the envelope.
    hint = fetch_hints.hint_for_status(status, final_url)
    return {
        "success": True,
        "url": final_url,
        "status": status,
        "headers": resp_headers, **cookie_field,
        "body": text_value[:limit], "total_chars": len(text_value),
        "truncated": len(text_value) > limit or cut_by_transport,
        "size_bytes": size_bytes,
        **({"hint": hint.strip()} if hint else {}),
    }
