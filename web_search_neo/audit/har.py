"""The session's network journal as HAR 1.2 (the format DevTools and proxies import).

The journal records what matters for diagnosis, not a full capture, and the
HAR says so instead of padding: request headers are not recorded (empty
lists), response headers are the security-relevant subset the journal keeps,
bodies are not included (``network_body`` reads one), and timings carry only the
total duration. Requests still in flight are exported with ``_done: false``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

LIMITATIONS = (
    "Exported from the session's network journal: request headers are not recorded; response "
    "headers are the security-relevant subset it keeps; no response bodies; timings.wait holds "
    "the whole duration; entries that fell out of the 500-request history are counted in _dropped. "
    "Set-Cookie values are replaced by REDACTED (names and attributes kept). Request bodies are the "
    "journal's copy: at most 4000 characters (postData.comment says when a body was cut or not captured)."
)
_CLIPPED = re.compile(r"\.\.\. \[(\d+) chars\]$")


def _redact_set_cookie(value: str) -> str:
    """``sid=secret; Path=/; HttpOnly`` -> ``sid=REDACTED; Path=/; HttpOnly`` (every cookie in the value)."""
    lines = []
    # Chrome joins repeated Set-Cookie headers with a newline.
    for line in str(value).split("\n"):
        head, sep, rest = line.partition(";")
        name = head.split("=", 1)[0].strip()
        lines.append(f"{name}=REDACTED" + (sep + rest if sep else ""))
    return "\n".join(lines)


def _iso(ms: Any) -> str:
    try:
        value = float(ms)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        value = datetime.now(timezone.utc).timestamp() * 1000
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _entry(row: dict[str, Any], pageref: str) -> dict[str, Any]:
    url = str(row.get("url") or "")
    headers = row.get("headers") if isinstance(row.get("headers"), dict) else {}
    post = row.get("post_data")
    duration = row.get("ms")
    total = float(duration) if isinstance(duration, (int, float)) and duration >= 0 else 0.0
    size = row.get("size") if isinstance(row.get("size"), int) else -1
    try:
        query = [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
    except ValueError:
        query = []
    request: dict[str, Any] = {
        "method": str(row.get("method") or "GET"), "url": url, "httpVersion": "", "cookies": [],
        "headers": [], "queryString": query, "headersSize": -1,
        "bodySize": len(post) if isinstance(post, str) else (-1 if row.get("has_post_data") else 0),
    }
    if isinstance(post, str):
        request["postData"] = {"mimeType": "", "text": post}
        clipped = _CLIPPED.search(post)
        if clipped:  # the journal keeps the head of a long body and names the full length
            request["bodySize"] = int(clipped.group(1))
            request["postData"]["comment"] = (f"cut by the network journal: only the head of the "
                                              f"{clipped.group(1)}-character body is recorded")
    elif row.get("has_post_data"):
        request["postData"] = {"mimeType": "", "text": "",
                               "comment": "the request had a body that the browser did not hand to the journal"}
    entry: dict[str, Any] = {
        "pageref": pageref,
        "startedDateTime": _iso(row.get("ts")),
        "time": total,
        "request": request,
        "response": {
            "status": int(row.get("status") or 0), "statusText": "", "httpVersion": "", "cookies": [],
            "headers": [{"name": name, "value": _redact_set_cookie(value) if name.lower() == "set-cookie"
                         else str(value)} for name, value in headers.items()],
            "content": {"size": -1, "mimeType": str(row.get("mime") or "")},
            "redirectURL": str(headers.get("location") or ""), "headersSize": -1, "bodySize": -1,
            "_transferSize": size,
        },
        "cache": {},
        "timings": {"blocked": -1, "dns": -1, "connect": -1, "ssl": -1, "send": 0, "wait": total, "receive": 0},
        "_resourceType": str(row.get("type") or "Other"),
        "_done": row.get("done") is not False,
    }
    if row.get("remote"):
        entry["serverIPAddress"] = str(row["remote"])
    for key in ("failed", "error", "blocked_reason", "from_cache", "initiator"):
        if row.get(key) not in (None, False, ""):
            entry[f"_{key}"] = row[key]
    return entry


def build(rows: list[dict[str, Any]], *, page_url: str, title: str, version: str, dropped: int = 0) -> dict[str, Any]:
    """A HAR ``{"log": ...}`` document for one page's journal rows (oldest first)."""
    ordered = sorted(rows, key=lambda row: float(row.get("ts") or 0))
    started = _iso(ordered[0].get("ts")) if ordered else _iso(0)
    return {"log": {
        "version": "1.2",
        "creator": {"name": "Web Search Neo", "version": version},
        "pages": [{"startedDateTime": started, "id": "page_1", "title": title or page_url,
                   "pageTimings": {"onContentLoad": -1, "onLoad": -1}}],
        "entries": [_entry(row, "page_1") for row in ordered],
        "comment": LIMITATIONS,
        "_dropped": int(dropped),
    }}
