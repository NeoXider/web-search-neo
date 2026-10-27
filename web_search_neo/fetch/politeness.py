"""Politeness for plain fetches: one visitor should look like one visitor.

WHY: recon walks a site hop by hop, and a host that sees a request every
hundred milliseconds from the same tool answers the tenth one with a 429 or a
challenge that costs far longer than the pause would have. The gate keeps a
small, fixed floor between requests to one host and honours whatever
Retry-After the host itself asked for.

The pure core (``plan``) does all the deciding; ``gate`` is the thin live part
that sleeps. Private and loopback hosts are skipped outright - a local fixture
server is not a third party, and tests that fetch from 127.0.0.1 must stay
fast.

Set WEB_SEARCH_NEO_FETCH_MIN_INTERVAL to 0 to turn the floor off; a larger
value is the pause between two requests to the same host.
"""

from __future__ import annotations

import os
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from web_search_neo.web_client import is_local_host

MIN_INTERVAL_ENV = "WEB_SEARCH_NEO_FETCH_MIN_INTERVAL"
DEFAULT_MIN_INTERVAL_SECONDS = 1.2
# A Retry-After longer than this is not slept through inside a tool call; the
# gate waits its cap and lets the caller see that the host asked for more.
MAX_WAIT_SECONDS = 30.0

_lock = threading.Lock()
_last_request: dict[str, float] = {}
_penalty_until: dict[str, float] = {}


def min_interval_seconds() -> float:
    """The floor between two requests to one host; WEB_SEARCH_NEO_FETCH_MIN_INTERVAL, 0 disables."""
    raw = os.getenv(MIN_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_MIN_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MIN_INTERVAL_SECONDS
    if value != value:  # NaN
        return DEFAULT_MIN_INTERVAL_SECONDS
    return max(0.0, value)


def retry_after_seconds(headers: Any) -> float | None:
    """Seconds a host asked to wait (Retry-After: 120 or an HTTP-date); None when absent."""
    try:
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    except AttributeError:
        return None
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - now).total_seconds())


def plan(
    last_monotonic: float | None,
    now_monotonic: float,
    min_interval_seconds_: float,
    retry_after: float | None = None,
) -> float:
    """Seconds to wait before the next request to this host; 0 means go now.

    Pure on purpose: both clocks are passed in, so the deciding is testable
    without sleeping. The floor applies only when a previous request happened;
    the first visit to a host never waits.
    """
    waits = [float(retry_after or 0.0)]
    if last_monotonic is not None and min_interval_seconds_ > 0:
        waits.append(float(min_interval_seconds_) - (float(now_monotonic) - float(last_monotonic)))
    return max(0.0, max(waits))


def host_of(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def gate(url: str) -> float:
    """Sleep the planned pause for this URL's host; report the seconds it waited.

    Local/private hosts and a zero floor walk straight through.
    """
    host = host_of(url)
    if not host or is_local_host(host):
        return 0.0
    floor = min_interval_seconds()
    penalty = 0.0
    with _lock:
        last = _last_request.get(host)
        until = _penalty_until.get(host)
        if until is not None:
            penalty = max(0.0, until - time.monotonic())
    wait = plan(last, time.monotonic(), floor, penalty)
    if wait <= 0:
        return 0.0
    slept = min(wait, MAX_WAIT_SECONDS)
    if slept > 0:
        time.sleep(slept)
    return slept


def note_request(url: str) -> None:
    """Record that this host got a request now (the floor counts from here)."""
    host = host_of(url)
    if host:
        with _lock:
            _last_request[host] = time.monotonic()


def remember_retry_after(url: str, seconds: float | None) -> None:
    """Honour a Retry-After a host answered with, for as long as it asked."""
    host = host_of(url)
    if not host or seconds is None:
        return
    penalty = min(float(seconds), MAX_WAIT_SECONDS)
    with _lock:
        _penalty_until[host] = time.monotonic() + penalty


def clear_all() -> None:
    """Forget every host (tests, process reset)."""
    with _lock:
        _last_request.clear()
        _penalty_until.clear()
