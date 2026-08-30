"""Shared, retrying HTTP client utilities."""

from __future__ import annotations

import ipaddress
import threading
import os
import secrets
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_TIMEOUT_SECONDS = 20.0
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)
USER_AGENT = USER_AGENTS[0]

ALLOW_PLAIN_HTTP_ENV = "WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP"
_LOCAL_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".home.arpa",
    ".lan",
    ".home",
    ".intranet",
    ".private",
    ".corp",
)
_IPV4_LITERAL_CHARACTERS = frozenset("0123456789abcdefxX")

_local = threading.local()


def _plain_http_allowed() -> bool:
    """Report whether unencrypted http:// to public hosts is explicitly enabled."""
    return os.getenv(ALLOW_PLAIN_HTTP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _legacy_ipv4(hostname: str) -> ipaddress.IPv4Address | None:
    """Parse the inet_aton spellings the OS dials but ``ip_address`` refuses.

    ``127.1``, ``2130706433``, ``0177.0.0.1`` and ``0x7f000001`` all reach
    loopback, so they have to be classified as the address they resolve to
    rather than mistaken for hostnames.
    """
    parts = hostname.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: list[int] = []
    for part in parts:
        if not part or not part.isascii() or not set(part) <= _IPV4_LITERAL_CHARACTERS:
            return None
        try:
            if part[:2].lower() == "0x":
                value = int(part, 16)
            elif part[0] == "0":
                value = int(part, 8)
            else:
                value = int(part, 10)
        except ValueError:
            return None
        values.append(value)
    if any(value > 0xFF for value in values[:-1]):
        return None
    trailing_octets = 4 - len(values) + 1
    if not 0 <= values[-1] < 1 << (8 * trailing_octets):
        return None
    packed = 0
    for value in values[:-1]:
        packed = (packed << 8) | value
    packed = (packed << (8 * trailing_octets)) | values[-1]
    return ipaddress.IPv4Address(packed)


def _ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address a host literal dials, or None when it is a name."""
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(
            hostname
        )
    except ValueError:
        address = _legacy_ipv4(hostname)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 connects to 127.0.0.1, but only Python 3.13 says so
        # via is_loopback/is_private; unwrap it so 3.10-3.13 agree.
        return address.ipv4_mapped
    return address


def is_local_host(host: str) -> bool:
    """Report whether a hostname points at this machine or a private network.

    Local development servers stay reachable over plain http; only public hosts
    are required to use https.

    Names are classified without a DNS lookup. A single-label host (``nas``,
    ``raspberrypi``) cannot be published on the public DNS, so it can only be
    answered by the LAN resolver, mDNS, WINS or the hosts file; resolving it
    would cost a lookup on every URL and every redirect hop, and would make the
    verdict depend on whichever network the machine is on at that moment. Names
    under a public domain that resolve privately are the case this misses; they
    stay covered by the WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP opt-in.
    """
    hostname = (host or "").strip().strip("[]").rstrip(".").lower()
    if not hostname:
        return False
    address = _ip_literal(hostname)
    if address is not None:
        return bool(
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_unspecified
        )
    if hostname == "localhost" or hostname.endswith(_LOCAL_SUFFIXES):
        return True
    return "." not in hostname


def validate_http_url(url: str, *, allow_plain_http: bool | None = None) -> str:
    """Validate and normalize an HTTP(S) URL.

    Plain http:// is rejected for public hosts unless it is explicitly allowed,
    while loopback and private-network addresses stay usable without https.
    """
    normalized = url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be an absolute http:// or https:// address")
    if parsed.scheme == "http":
        permitted = _plain_http_allowed() if allow_plain_http is None else allow_plain_http
        if not permitted and not is_local_host(parsed.hostname or ""):
            raise ValueError(
                f"Unencrypted http:// is blocked for the public host '{parsed.hostname}'. "
                "Use https://, or set "
                f"{ALLOW_PLAIN_HTTP_ENV}=1 to allow plain HTTP. "
                "Loopback, private-network addresses and LAN names "
                "(single-label hosts, .local, .lan, .internal) are always allowed."
            )
    return normalized


def _session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        retries = Retry(
            total=1,
            connect=1,
            read=1,
            status=1,
            backoff_factor=0.25,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=8, pool_maxsize=8)
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": secrets.choice(USER_AGENTS),
                "Accept-Language": "en-US,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            }
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        proxy = os.getenv("WEB_SEARCH_NEO_PROXY")
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        _local.session = session
    return session


MAX_REDIRECTS = 5


def _follow_redirects(
    session: requests.Session,
    response: requests.Response,
    *,
    method: str,
    timeout: tuple[float, float],
    **kwargs: Any,
) -> requests.Response:
    """Follow redirects manually so every hop is validated before it is requested."""
    history: list[requests.Response] = []
    # The hop URL already carries the query the server chose. Passing ``params``
    # again would append the original query to it, so ``/search?q=kittens`` ->
    # ``/results?q=kittens&form=CANON`` would be re-requested with a duplicate
    # ``q``.
    kwargs.pop("params", None)
    for _ in range(MAX_REDIRECTS):
        if not response.is_redirect or not response.headers.get("location"):
            break
        target = validate_http_url(urljoin(response.url, response.headers["location"]))
        if response.status_code == 303 or (
            response.status_code in {301, 302} and method.upper() not in {"GET", "HEAD"}
        ):
            method = "GET"
            kwargs.pop("data", None)
            kwargs.pop("json", None)
        response.close()
        history.append(response)
        response = session.request(
            method=method,
            url=target,
            timeout=timeout,
            allow_redirects=False,
            **kwargs,
        )
    else:
        if response.is_redirect:
            response.close()
            raise ValueError(f"Exceeded {MAX_REDIRECTS} redirects")
    response.history = history
    return response


def request(
    url: str,
    *,
    method: str = "GET",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Send a bounded HTTP request and raise a useful error for bad responses."""
    normalized = validate_http_url(url)
    timeout = max(1.0, float(timeout_seconds))
    if max_response_bytes is not None:
        kwargs["stream"] = True
    session = _session()
    timeouts = (min(5.0, timeout), timeout)
    response = session.request(
        method=method,
        url=normalized,
        timeout=timeouts,
        allow_redirects=False,
        **kwargs,
    )
    response = _follow_redirects(
        session, response, method=method, timeout=timeouts, **kwargs
    )
    response.raise_for_status()
    if max_response_bytes is not None:
        limit = max(1024, min(int(max_response_bytes), 20_000_000))
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=65_536):
            if not chunk:
                continue
            size += len(chunk)
            if size > limit:
                response.close()
                raise ValueError(f"Response exceeded the {limit}-byte safety limit")
            chunks.append(chunk)
        response._content = b"".join(chunks)
        response._content_consumed = True
    return response
