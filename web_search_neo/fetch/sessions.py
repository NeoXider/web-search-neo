"""Named cookie jars for http_request: the opt-in ``http_session``.

Without a name every http_request starts with an empty jar (web_client.request).
A name opts into a jar that outlives the call: its cookies ride on the next
request with the same name, matched by domain, path, secure and expiry the way
``http.cookiejar`` matches them, and what the responses set or delete is stored
back. A jar is keyed by ``(agent_label, name)``, so two agents using the same
name never share one; no label is a namespace of its own. Jars live in this
process only, expire after an idle TTL and are capped in number, the least
recently used one going first.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from http.cookiejar import Cookie, CookieJar
from typing import Any
from urllib.parse import urlparse

HTTP_SESSION_TTL_ENV = "WEB_SEARCH_NEO_HTTP_SESSION_TTL"
DEFAULT_TTL_SECONDS = 1800.0
MIN_TTL_SECONDS = 60.0
MAX_SESSIONS = 32
MAX_LABEL_CHARS = 128
_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}")


class SessionJar(CookieJar):
    """A CookieJar whose iteration is a snapshot, so concurrent calls on one name are safe."""

    def __init__(self) -> None:
        super().__init__()
        self._guard = threading.RLock()

    def __iter__(self):  # type: ignore[override]
        with self._guard:
            return iter(list(super().__iter__()))

    def set_cookie(self, cookie: Cookie) -> None:
        with self._guard:
            super().set_cookie(cookie)

    def clear(self, domain: str | None = None, path: str | None = None,
              name: str | None = None) -> None:
        with self._guard:
            super().clear(domain, path, name)


@dataclass
class _Entry:
    jar: SessionJar
    last_used: float


_lock = threading.Lock()
_entries: OrderedDict[tuple[str | None, str], _Entry] = OrderedDict()


def _now() -> float:
    return time.monotonic()


def ttl_seconds() -> float:
    """Idle lifetime of a jar: WEB_SEARCH_NEO_HTTP_SESSION_TTL, at least 60 s."""
    raw = os.getenv(HTTP_SESSION_TTL_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        value = DEFAULT_TTL_SECONDS
    if value != value:  # NaN
        value = DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, value)


def normalize_name(name: Any) -> str:
    text = str(name).strip() if name is not None else ""
    if not _NAME_PATTERN.fullmatch(text):
        raise ValueError(
            "http_session must be 1-64 characters of letters, digits and . _ : @ -, "
            f"starting with a letter or digit, not {name!r}"
        )
    return text


def normalize_label(agent_label: Any) -> str | None:
    if agent_label is None:
        return None
    text = str(agent_label).strip()
    if len(text) > MAX_LABEL_CHARS:
        raise ValueError(f"agent_label is limited to {MAX_LABEL_CHARS} characters")
    return text or None


def open_jar(name: Any, agent_label: Any = None, *, clear: bool = False) -> tuple[SessionJar, dict]:
    """Return the jar for ``(agent_label, name)``, creating it, and describe what happened.

    Idle jars past the TTL are dropped first; ``clear`` drops this one before
    the request, so the call starts with an empty jar under the same name.
    """
    key = (normalize_label(agent_label), normalize_name(name))
    ttl = ttl_seconds()
    now = _now()
    with _lock:
        expired = [k for k, entry in _entries.items() if now - entry.last_used > ttl]
        for stale in expired:
            del _entries[stale]
        cleared = bool(clear) and _entries.pop(key, None) is not None
        entry = _entries.get(key)
        created = entry is None
        if entry is None:
            entry = _entries[key] = _Entry(SessionJar(), now)
        entry.last_used = now
        _entries.move_to_end(key)
        evicted: list[tuple[str | None, str]] = []
        while len(_entries) > MAX_SESSIONS:
            evicted.append(_entries.popitem(last=False)[0])
    info: dict[str, Any] = {
        "name": key[1], "agent_label": key[0], "created": created,
        "expired": key in expired, "cleared": cleared, "ttl_seconds": ttl,
    }
    own_evicted = [n for label, n in evicted if label == key[0]]
    if own_evicted:
        info["evicted"] = own_evicted
    return entry.jar, info


def clear_all() -> None:
    """Forget every jar (tests, process reset)."""
    with _lock:
        _entries.clear()


def _touch(key: tuple[str | None, str], jar: SessionJar) -> None:
    with _lock:
        entry = _entries.get(key)
        if entry is not None and entry.jar is jar:
            entry.last_used = _now()


def describe_cookie(cookie: Cookie, show_values: bool = False) -> dict[str, Any]:
    """A cookie's name and flags; the value only when asked for."""
    # http.cookiejar keeps HttpOnly/SameSite as non-standard attributes in the
    # spelling the server used, and offers no case-insensitive accessor.
    rest = {str(k).lower(): v for k, v in (getattr(cookie, "_rest", None) or {}).items()}
    expires = None
    if cookie.expires is not None:
        expires = datetime.fromtimestamp(cookie.expires, tz=timezone.utc).isoformat()
    info: dict[str, Any] = {
        "name": cookie.name, "domain": cookie.domain, "host_only": not cookie.domain_specified,
        "path": cookie.path, "secure": bool(cookie.secure), "httponly": "httponly" in rest,
        "samesite": rest.get("samesite"), "expires": expires,
    }
    if show_values:
        info["value"] = cookie.value
    return info


def _cookie_key(cookie: Cookie) -> tuple[str, str, str]:
    return cookie.domain, cookie.path, cookie.name


def _domain_matches(cookie: Cookie, host: str) -> bool:
    domain = cookie.domain.lower()
    # http.cookiejar files a dotless host (localhost) under "<host>.local".
    names = {host, host + ".local"} if "." not in host else {host}
    if domain.startswith("."):
        return any(name == domain[1:] or name.endswith(domain) for name in names)
    return domain in names


def _sent_cookies(prepared: Any, pool: list[Cookie]) -> list[Cookie | str]:
    """The cookies one hop actually carried, read back from its Cookie header."""
    headers = getattr(prepared, "headers", None) or {}
    header = headers.get("Cookie") or ""
    parsed = urlparse(str(getattr(prepared, "url", "") or ""))
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    sent: list[Cookie | str] = []
    for part in header.split(";"):
        name = part.split("=", 1)[0].strip()
        if not name:
            continue
        candidates = [c for c in pool if c.name == name and _domain_matches(c, host)
                      and path.startswith(c.path)]
        sent.append(max(candidates, key=lambda c: len(c.path)) if candidates else name)
    return sent


class _SetCookieResponse:
    def __init__(self, value: str) -> None:
        self._message = Message()
        self._message["Set-Cookie"] = value

    def info(self) -> Message:
        return self._message


def set_cookie_values(response: Any) -> list[str]:
    """Every Set-Cookie of one response, kept apart (requests folds them with ", ")."""
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    listed = raw_headers.getlist("Set-Cookie") if hasattr(raw_headers, "getlist") else None
    return [str(item) for item in listed] if isinstance(listed, (list, tuple)) else []


def _received(response: Any, jar: SessionJar, show_values: bool) -> list[dict[str, Any]]:
    url = str(getattr(getattr(response, "request", None), "url", None)
              or getattr(response, "url", "") or "")
    stored = {_cookie_key(c): c.value for c in jar}
    received: list[dict[str, Any]] = []
    for value in set_cookie_values(response):
        try:
            parsed = CookieJar().make_cookies(_SetCookieResponse(value), urllib.request.Request(url))
        except Exception:
            parsed = []
        if not parsed:
            # Expired on arrival (the server deleting it) or not a parsable cookie.
            name = value.split(";", 1)[0].split("=", 1)[0].strip()
            received.append({"name": name, "deleted": True, "stored": False, "url": url})
            continue
        for cookie in parsed:
            entry = describe_cookie(cookie, show_values)
            entry["stored"] = stored.get(_cookie_key(cookie), object()) == cookie.value
            entry["url"] = url
            received.append(entry)
    return received


def report(info: dict[str, Any], jar: SessionJar, before: list[Cookie], response: Any,
           show_values: bool = False) -> dict[str, Any]:
    """The ``http_session`` block of an answer: what each hop sent and received (with its url)."""
    _touch((info["agent_label"], info["name"]), jar)
    hops = list(getattr(response, "history", None) or []) + [response]
    pool = before + list(jar)
    sent: list[dict[str, Any]] = []
    seen: set[Any] = set()
    received: list[dict[str, Any]] = []
    for hop in hops:
        prepared = getattr(hop, "request", None)
        hop_url = str(getattr(prepared, "url", "") or "")
        for cookie in _sent_cookies(prepared, pool):
            key = (cookie if isinstance(cookie, str) else _cookie_key(cookie), hop_url)
            if key in seen:
                continue
            seen.add(key)
            entry = ({"name": cookie} if isinstance(cookie, str)
                     else describe_cookie(cookie, show_values))
            sent.append({**entry, "url": hop_url})
        received.extend(_received(hop, jar, show_values))
    return {**info, "cookies_in_jar": len(list(jar)), "sent_cookies": sent,
            "received_cookies": received, "values_shown": bool(show_values)}


_SET_COOKIE_VALUE = re.compile(r"^(\s*[^=;,\s]+\s*=)[^;]*")


def redact_set_cookie(value: str) -> str:
    """``sid=abc; Path=/`` -> ``sid=<redacted>; Path=/``."""
    return _SET_COOKIE_VALUE.sub(r"\1<redacted>", str(value), count=1)


def redact_headers(headers: dict[str, Any], set_cookies: list[str]) -> None:
    """Hide cookie values in the answer's own header copies (in place)."""
    set_cookies[:] = [redact_set_cookie(item) for item in set_cookies]
    for name in list(headers):
        if str(name).lower() == "set-cookie":
            headers[name] = ", ".join(set_cookies) or redact_set_cookie(headers[name])
