"""Building the headers a plain fetch sends when the caller named none.

WHY at all: an un-headed python fetch announces itself (python-requests UA, no
Accept-Language), and more sites answer that with a 403 or a consent shell than
answer a browser. The builder dresses the request like the browser this server
drives; it only fires when the caller passed no headers, because a caller who
named headers meant exactly those.

The pick is sticky per host on purpose: a UA that changes every request to the
same site is its own tell, and a site that watches the visitor across a session
would see a different client each hop. Within one host the identity is stable;
across hosts it rotates, which is what spreads the fingerprint.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from web_search_neo.web_client import USER_AGENTS

# Accept-Language the builder sends when nothing about the host says otherwise.
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
# Hosts whose public TLDs sit in Cyrillic-writing countries: an English-only
# Accept-Language there is a weaker match for what the operator is after.
CYRILLIC_TLDS = frozenset({
    "ru", "su", "ua", "by", "kz", "md", "ge", "am", "az", "kg", "tj", "tm", "uz",
})
CYRILLIC_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.6,en;q=0.5"


def host_of(url: str) -> str:
    """Lowercase hostname of a URL, exactly as requests would read it."""
    return (urlparse(str(url or "")).hostname or "").lower()


def ua_for_host(host: str, pool: tuple[str, ...] = USER_AGENTS) -> str:
    """The pool member this host gets, the same one every time.

    WHY hashlib and not hash(): the built-in hash is salted per process, so a
    sticky pick would not survive a restart; a sha256 of the host does.
    """
    key = str(host or "").strip().lower()
    if not key:
        return pool[0]
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return pool[digest[0] % len(pool)]


def accept_language_for_host(host: str) -> str:
    """Default English; the Cyrillic variant for a Cyrillic-writing TLD."""
    labels = str(host or "").strip().lower().rstrip(".").split(".")
    if len(labels) > 1 and labels[-1] in CYRILLIC_TLDS:
        return CYRILLIC_ACCEPT_LANGUAGE
    return DEFAULT_ACCEPT_LANGUAGE


def browser_headers(url: str, *, referer: str | None = None) -> dict[str, str]:
    """The header set a plain fetch sends when the caller named none.

    ``referer`` is added only when given: a first hit on a page has no referer,
    and inventing one would be a lie the site can check.
    """
    host = host_of(url)
    headers = {
        "User-Agent": ua_for_host(host),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": accept_language_for_host(host),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    # WHY no Accept-Encoding here: web_client owns the session and advertises
    # its own encoding set; naming a second one here would only invite a
    # compressed body the shared decoder was not asked to promise.
    if referer:
        headers["Referer"] = str(referer)
    return headers
