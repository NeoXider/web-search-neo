"""Direct HTTP requests without a browser (REST API testing)."""

from __future__ import annotations

import logging
from typing import Any

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
    No cookies carry over between calls: send a Cookie header yourself; every
    Set-Cookie of the response is listed in ``set_cookies``.
    """
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
    log.info("HTTP %s %s", normalized_method, redact_url(normalized_url))
    byte_budget = min(max(1_000_000, int(max_chars) * 8), 10_000_000)
    client = request if request_client is None else request_client
    try:
        response = client(
            normalized_url,
            method=normalized_method,
            timeout_seconds=clamp_timeout(timeout_seconds),
            max_response_bytes=byte_budget,
            **extra,
        )
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        if error_response is None or not isinstance(
            getattr(error_response, "status_code", None), int
        ):
            raise
        response = error_response
    status = int(response.status_code)
    final_url = getattr(response, "url", None) or normalized_url
    resp_headers = dict(getattr(response, "headers", None) or {})
    # requests folds repeated Set-Cookie headers into one ", "-joined string,
    # which the commas inside Expires make unparseable: keep them apart too.
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    listed = raw_headers.getlist("Set-Cookie") if hasattr(raw_headers, "getlist") else None
    set_cookies = [str(item) for item in listed] if isinstance(listed, (list, tuple)) else []
    cookie_field = {"set_cookies": set_cookies} if set_cookies else {}
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
    return {
        "success": True,
        "url": final_url,
        "status": status,
        "headers": resp_headers, **cookie_field,
        "body": text_value[:limit], "total_chars": len(text_value),
        "truncated": len(text_value) > limit or cut_by_transport,
        "size_bytes": size_bytes,
    }
