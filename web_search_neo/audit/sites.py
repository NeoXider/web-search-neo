"""First party or third party: the registrable domain ("site") of a URL.

Two hosts are the same site when they share a registrable domain -
``www.example.com`` and ``cdn.example.com`` are one site, ``example.github.io``
and ``other.github.io`` are two. The public-suffix test is the one the cookie
clear already uses (a real Public Suffix List when installed, a built-in list
otherwise), so both features draw the line in the same place.
"""
from __future__ import annotations

import functools
import ipaddress
from urllib.parse import urlsplit

from web_search_neo.actions.cookie_scope import is_public_suffix


def host_of(url: str) -> str:
    """Lowercased host of an absolute URL, or '' for data:, blob:, about: and garbage."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https", "ws", "wss"}:
        return ""
    return (parts.hostname or "").rstrip(".").lower()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


@functools.lru_cache(maxsize=4096)
def site_of(host: str) -> str:
    """The registrable domain of ``host``; an IP, localhost or bare name is its own site.

    Cached: a network filter asks for the same few hosts hundreds of times, and a
    Public Suffix List lookup is not free.
    """
    name = str(host or "").strip().rstrip(".").lower()
    if not name or _is_ip(name) or "." not in name or name == "localhost":
        return name
    labels = name.split(".")
    for size in range(2, len(labels) + 1):
        candidate = ".".join(labels[-size:])
        if not is_public_suffix(candidate):
            return candidate
    return name


def same_site(url: str, page_url: str) -> bool:
    """True when ``url`` belongs to the page's own site (non-network URLs count as own)."""
    host = host_of(url)
    if not host:
        return True
    return site_of(host) == site_of(host_of(page_url))


def is_third_party(url: str, page_url: str) -> bool:
    """A network URL on another registrable domain than the page."""
    return bool(host_of(url)) and not same_site(url, page_url)
