"""Direct HTTP requests without a browser (REST API testing)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from web_search_neo.web_client import request, validate_http_url

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
    *,
    request_client: Any | None = None,
) -> dict[str, Any]:
    """Send one HTTP request and return its status, headers, and body.

    ``query`` becomes the URL query string; ``body`` (raw str) and
    ``body_json`` (serialised as JSON) are mutually exclusive. HTTP error
    statuses (4xx/5xx) are returned as a success envelope with their real
    status, while transport failures (DNS, timeout) raise like fetch_text.
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
    log.info("HTTP %s %s", normalized_method, normalized_url)
    byte_budget = min(max(1_000_000, int(max_chars) * 8), 10_000_000)
    client = request if request_client is None else request_client
    try:
        response = client(
            normalized_url,
            method=normalized_method,
            timeout_seconds=timeout_seconds,
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
    text_value = getattr(response, "text", "")
    if not isinstance(text_value, str):
        text_value = "" if text_value is None else str(text_value)
    raw = getattr(response, "content", None)
    if isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
    else:
        raw_bytes = text_value.encode("utf-8")
    size_bytes = len(raw_bytes)
    if save_to:
        path = Path(str(save_to)).expanduser()
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ValueError(f"save_to is not a writable path: {exc}") from exc
        parent = resolved.parent
        if not parent.is_dir():
            raise ValueError(f"save_to directory does not exist: {parent}")
        resolved.write_bytes(raw_bytes)
        return {
            "success": True,
            "url": final_url,
            "status": status,
            "headers": resp_headers,
            "saved_to": str(resolved),
            "size_bytes": size_bytes,
        }
    limit = max(1, int(max_chars))
    return {
        "success": True,
        "url": final_url,
        "status": status,
        "headers": resp_headers,
        "body": text_value[:limit],
        "truncated": len(text_value) > limit,
        "size_bytes": size_bytes,
    }
