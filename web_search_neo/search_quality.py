"""Judging what a search engine answered: tracking links and off-topic rows.

Two engines answered in ways that looked like success and were not. Bing wraps
every result in a ``bing.com/ck/a`` redirect, so the caller got click-tracking
links instead of the pages; and for some queries (Cyrillic ones reliably) Bing
serves a page of rows about something else entirely. ``unwrap_bing`` recovers the
real URL, and ``relevance`` counts how many rows mention the query at all, so
the search loop can treat an all-off-topic answer like an empty one and ask the
next engine, saying so in the response instead of passing the rows on.
"""
from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, urlparse

_WORD = re.compile(r"\w{3,}", re.UNICODE)


def unwrap_bing(url: str) -> str:
    """The page behind a ``bing.com/ck/a?...&u=a1<base64url>`` link; others unchanged."""
    parsed = urlparse(url)
    if not parsed.netloc.endswith("bing.com") or not parsed.path.startswith("/ck/"):
        return url
    token = (parse_qs(parsed.query).get("u") or [""])[0]
    if not token.startswith("a1"):
        return url
    payload = token[2:] + "=" * (-len(token[2:]) % 4)
    try:
        target = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return url
    return target if target.startswith(("http://", "https://")) else url


def query_stems(query: str) -> list[str]:
    """Word stems to look for: the first four letters of every word of three or more."""
    return sorted({word.casefold()[:4] for word in _WORD.findall(query) if not word.isdigit()})


def relevance(query: str, results: list[dict[str, str]]) -> tuple[int, int] | None:
    """``(rows mentioning a query word, rows)``; None when the query has no words to check."""
    stems = query_stems(query)
    if not stems or not results:
        return None
    hits = 0
    for row in results:
        text = " ".join(str(row.get(key) or "") for key in ("title", "snippet", "url")).casefold()
        if any(stem in text for stem in stems):
            hits += 1
    return hits, len(results)


def off_topic(query: str, results: list[dict[str, str]]) -> bool:
    """True when not one row mentions any word of the query."""
    measured = relevance(query, results)
    return measured is not None and measured[0] == 0
