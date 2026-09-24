"""The window of page text one answer carries, and where the next one starts.

page_text used to clip at ``max_chars`` (itself capped at 200 000) and advise
"raise max_chars for the rest" - which could not help past the cap, while
``offset`` was refused as an unknown parameter. So the tail of a long page was
unreachable. A read now starts at ``offset`` and reports ``next_offset`` whenever
text is left, so any page can be read to the end in windows.
"""
from __future__ import annotations

import re
from typing import Any

MARKER_PATTERN = re.compile(r"\[(\d+)\]")
_OPEN_MARKER = re.compile(r"\[\d*$")
MIN_WINDOW = 200


def clip(text: str, limit: int) -> tuple[str, int, bool]:
    """``(kept, consumed, truncated)``: cut at a paragraph boundary when there is one.

    ``consumed`` counts the characters of ``text`` this window used up, the
    paragraph break included, so the next window starts on the next paragraph.
    """
    if len(text) <= limit:
        return text, len(text), False
    window = text[:limit]
    boundary = window.rfind("\n\n")
    if boundary > limit // 4:
        return window[:boundary].rstrip(), boundary + 2, True
    cut = _OPEN_MARKER.search(window)  # never split a link marker "[12]" in two
    if cut and cut.start() > 0 and MARKER_PATTERN.match(text, cut.start()):
        window = window[:cut.start()]
    return window.rstrip(), len(window), True


def clip_on_boundary(text: str, limit: int) -> tuple[str, bool]:
    kept, _consumed, truncated = clip(text, limit)
    return kept, truncated


def link_listing(text: str, links: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    kept = {int(marker) for marker in MARKER_PATTERN.findall(text)}
    selected = [
        link
        for link in links
        if isinstance(link, dict) and int(link.get("index", 0)) in kept
    ]
    listing = "\n".join(
        f"[{link['index']}] {link.get('text') or ''} -> {link.get('url') or ''}"
        for link in selected
    )
    return listing, selected


def window(full_text: str, raw_links: list[Any], limit: int, include_links: bool,
           offset: int = 0) -> dict[str, Any]:
    """The text from ``offset`` that fits ``limit``, its link index, and the next offset.

    The link index is part of what the caller receives, so it is paid for out of
    the same budget instead of silently overflowing it; the largest text that
    still fits beside its index is found by bisection.
    """
    start = max(0, min(int(offset or 0), len(full_text)))
    limit = max(MIN_WINDOW, int(limit))
    rest = full_text[start:]
    text, consumed, truncated = clip(rest, limit)
    links: list[dict[str, Any]] = []
    if include_links:
        listing = ""
        low, high = 0, limit
        text, consumed = "", 0
        while low <= high:
            middle = (low + high) // 2
            candidate, used, _ = clip(rest, middle)
            candidate_listing, candidate_links = link_listing(candidate, raw_links)
            spent = len(candidate) + (len(candidate_listing) + 2 if candidate_listing else 0)
            if spent <= limit:
                text, consumed, listing, links = candidate, used, candidate_listing, candidate_links
                low = middle + 1
            else:
                high = middle - 1
        if consumed == 0 and rest:
            # Not even one paragraph fits beside its link index: text first, no index.
            text, consumed, _ = clip(rest, limit)
            listing, links = "", []
        truncated = consumed < len(rest)
        if listing:
            text = f"{text}\n\n{listing}"
    return {"text": text, "links": links, "truncated": truncated, "offset": start,
            "next_offset": start + consumed if truncated else None}
