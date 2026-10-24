"""The page's own favicon for the tab mark, fetched by the server when the page cannot read it.

The mark is painted on a canvas over the page's icon, and a canvas may only read an image
the page is allowed to read: an icon on another origin without CORS headers - most sites,
VK's included - taints it. Left there, such a tab got no mark at all. So the page asks
once (``needBase`` in a note's answer) and the server fetches that one URL itself, plain
http(s), no cookies, a few hundred kilobytes at most, and hands it back as a data URL,
which the canvas reads freely; the robot then sits in its corner over the real icon.
"""
from __future__ import annotations

import base64
import logging
import threading
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

MAX_BYTES = 256 * 1024
TIMEOUT_SECONDS = 6.0
_TYPES = {b"\x89PNG": "image/png", b"\x00\x00\x01\x00": "image/x-icon", b"GIF8": "image/gif",
          b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}
_SET_BASE = "const p = window.__wsnPresence; if (p && p.setBase) p.setBase(arguments[0]); return true;"


def _sniff(data: bytes, declared: str) -> str | None:
    for magic, kind in _TYPES.items():
        if data.startswith(magic):
            return kind
    head = data[:300].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return declared if declared.startswith("image/") else None


def fetch_data_url(href: str) -> str | None:
    """The icon at ``href`` as a data URL, or None (not http(s), too big, not an image, unreachable)."""
    if not href.lower().startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(href, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome",
        "Accept": "image/avif,image/webp,image/svg+xml,image/*,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(MAX_BYTES + 1)
            declared = (response.headers.get_content_type() or "").lower()
    except Exception as exc:
        logger.debug("favicon fetch failed for %s: %s", href, exc)
        return None
    if not data or len(data) > MAX_BYTES:
        return None
    kind = _sniff(data, declared)
    return f"data:{kind};base64,{base64.b64encode(data).decode()}" if kind else None


def deliver(session: Any, href: str) -> None:
    """Fetch in the background and hand the icon (or None) to the page; never blocks an action."""
    def run() -> None:
        data_url = fetch_data_url(href)
        try:
            with session.lock:
                session.driver.execute_script(_SET_BASE, data_url)
        except Exception as exc:
            logger.debug("favicon hand-over failed: %s", exc)

    threading.Thread(target=run, name="wsn-favicon", daemon=True).start()
