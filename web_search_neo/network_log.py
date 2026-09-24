"""The network topic over both backends: finished requests plus the ones still open.

Both backends only ever "finished" a request on loadingFinished/loadingFailed,
which Chrome sends once the page has read the whole body. A fetch() whose body
was never read, a fire-and-forget POST, a long poll, an SSE stream - and a 5xx
whose body the page ignored - therefore never appeared at all, which broke the
"network only_errors -> network_body" diagnosis and "did the click send a POST".
Those requests are now listed as ``done: false`` rows (``state`` "sent" or
"headers"), and a limit never hides rows silently: ``matched`` and
``omitted_older`` say what the window left out.
"""
from __future__ import annotations

from typing import Any

from web_search_neo import diagnostics

HISTORY_LIMIT = 500


def drain(session: Any) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """``(finished rows, dropped count, pending rows)`` for one session (lock held)."""
    driver = session.driver
    if hasattr(driver, "get_events"):
        # The tab subscribed when it opened; this only repairs a subscription that
        # failed then. A stand-in driver that does not track the flag is capturing.
        if not getattr(driver, "events_subscribed", True):
            driver.subscribe_events(["console", "network"])
        payload = driver.get_events(kinds=["network"], since_seq=0, limit=HISTORY_LIMIT)
        dropped = int((payload.get("dropped") or {}).get("network") or 0)
        pending = [diagnostics.pending_view(row) for row in payload.get("pending_entries") or []]
        return list(payload.get("entries") or []), dropped, pending
    session.network_rows.extend(diagnostics.selenium_network_rows(driver, session.network_pending))
    overflow = len(session.network_rows) - HISTORY_LIMIT
    if overflow > 0:
        del session.network_rows[:overflow]
        session.network_dropped += overflow
    pending = [diagnostics.pending_view(row) for row in session.network_pending.values()]
    return list(session.network_rows), session.network_dropped, pending


def requests_since(session: Any, started_ms: float) -> list[dict[str, Any]]:
    """Requests the page started after ``started_ms`` (epoch ms), in flight included."""
    try:
        rows, _, pending = drain(session)
    except Exception:
        # A diagnostic: losing it must never turn into a failed click.
        return []
    return [row for row in rows + pending if float(row.get("ts") or 0) >= started_ms]


def report(
    session: Any, session_id: str, *, url_pattern: str | None, types: list[str] | None,
    status_min: int | None, status_max: int | None, only_errors: bool, limit: int,
    output: str, include_pending: bool,
) -> dict[str, Any]:
    """The network topic's answer (lock held)."""
    rows, dropped, pending = drain(session)
    candidates = rows + (pending if include_pending else [])
    matched = diagnostics.filter_network(
        candidates, url_pattern, types, status_min, status_max, only_errors, len(candidates) + 1
    )
    window = max(1, int(limit))
    selected = matched[-window:]
    response: dict[str, Any] = {
        "success": True,
        "session_id": session_id,
        "returned": len(selected),
        "matched": len(matched),
        "in_flight": sum(1 for row in selected if row.get("done") is False),
        "only_errors": bool(only_errors),
        "dropped": dropped,
    }
    if len(matched) > len(selected):
        response["omitted_older"] = len(matched) - len(selected)
        response["truncated"] = True
    if output == "json":
        response["requests"] = selected
    else:
        response["requests"] = diagnostics.format_network(selected)
        response["format"] = "method status type ms size url"
    return response


def read_body(driver: Any, request_id: str, session_id: str, max_chars: int,
              describe_error: Any) -> dict[str, Any]:
    """One response body (lock held); a body Chrome no longer holds is explained, not a stack."""
    try:
        if hasattr(driver, "get_network_body"):
            payload = driver.get_network_body(str(request_id))
        else:
            payload = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": str(request_id)})
    except Exception as exc:
        # Chrome answers with an inspector error, or chromedriver with a native stack.
        return {"success": False, "request_id": request_id, "session_id": session_id,
                "body_available": False, "detail": describe_error(exc)[:300], "error": (
                    "Chrome no longer holds this response body: bodies are dropped when the "
                    "page navigates, the tab's buffer fills, or the response had none (a "
                    "redirect, a 204, a preflight). Repeat the request (reload, or "
                    "replay_request) and read the body right after.")}
    body = str((payload or {}).get("body") or "")
    limit = max(256, min(int(max_chars), 500_000))
    return {
        "success": True,
        "request_id": request_id,
        "session_id": session_id,
        "binary": bool(payload.get("binary") or payload.get("base64Encoded")),
        "truncated": len(body) > limit, "total_chars": len(body),
        "body": body[:limit],
    }
