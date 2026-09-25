"""Shared, retrying HTTP client utilities."""

from __future__ import annotations

import copy
import ipaddress
import os
import secrets
import socket
import threading
import time
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3 import exceptions as urllib3_errors
from urllib3.response import BaseHTTPResponse
from urllib3.util.retry import Retry


DEFAULT_TIMEOUT_SECONDS = 20.0
# Per-call ceiling for any user-supplied timeout; a larger value is clamped.
MAX_TIMEOUT_SECONDS = 120.0
# The streamed body read gets this multiple of the (clamped) timeout as a total
# wall-clock budget, so a server trickling one byte per read cannot hold a
# worker thread forever even though each individual read is on time.
TOTAL_DEADLINE_FACTOR = 2.0
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


def clamp_timeout(value: Any, default: float = DEFAULT_TIMEOUT_SECONDS) -> float:
    """Bound a caller-supplied timeout to 1..MAX_TIMEOUT_SECONDS seconds."""
    try:
        seconds = float(default if value is None else value)
    except (TypeError, ValueError):
        seconds = float(default)
    if seconds != seconds:  # NaN
        seconds = float(default)
    return max(1.0, min(seconds, MAX_TIMEOUT_SECONDS))


# Cloud instance-metadata endpoints hand out credentials to anything that can
# reach them, so no fetch may target them, not even an explicit local one.
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "169.254.0.0/16",  # IPv4 link-local, incl. 169.254.169.254 (AWS/GCP/Azure)
        "fe80::/10",  # IPv6 link-local
        "100.100.100.200/32",  # Alibaba Cloud metadata
        "fd00:ec2::254/128",  # AWS IMDS over IPv6
        "168.63.129.16/32",  # Azure WireServer
    )
)

DESTINATION_PUBLIC = "public"
DESTINATION_PRIVATE = "private"


# IPv6 prefixes that carry an IPv4 address a gateway will dial: NAT64
# (RFC 6052, the address in the low 32 bits) and 6to4 (RFC 3056).
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address.sixtofour


def _address_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    embedded = _embedded_ipv4(address)
    if embedded is not None and _address_blocked(embedded):
        return True
    return any(
        address in network for network in _BLOCKED_NETWORKS if network.version == address.version
    )


def _address_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    embedded = _embedded_ipv4(address)
    if embedded is not None and _address_private(embedded):
        return True
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    )


def _resolve(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address a name resolves to; empty when it does not resolve here.

    An unresolvable name is not refused: requests fails on it anyway, and with
    WEB_SEARCH_NEO_PROXY set the proxy, not this machine, resolves it.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    addresses = []
    for info in infos:
        literal = _ip_literal(str(info[4][0]).split("%", 1)[0])
        if literal is not None:
            addresses.append(literal)
    return addresses


def classify_destination(url: str) -> str:
    """Return ``public`` or ``private`` for a URL's host, or raise if it is forbidden.

    Names are resolved, so a public name pointing at 127.0.0.1 counts as
    private. Metadata and link-local destinations raise ValueError for every
    fetch. The check runs before connecting; a resolver that answers
    differently on the connect lookup (DNS rebinding) is not covered.
    """
    hostname = (urlparse(url).hostname or "").strip().strip("[]").rstrip(".").lower()
    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Requests to the cloud metadata host '{hostname}' are blocked")
    literal = _ip_literal(hostname)
    addresses = [literal] if literal is not None else _resolve(hostname)
    for address in addresses:
        if _address_blocked(address):
            raise ValueError(
                f"Requests to the link-local/metadata address {address} ('{hostname}') are blocked"
            )
    if any(_address_private(address) for address in addresses):
        return DESTINATION_PRIVATE
    if not addresses and is_local_host(hostname):
        return DESTINATION_PRIVATE
    return DESTINATION_PUBLIC


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
            # Hand back the last 5xx instead of a body-less RetryError, so
            # http_request can report the server's error payload.
            raise_on_status=False,
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

# Headers that carry no credential and may follow a redirect to another
# origin. Everything else a caller set (X-Api-Key, custom tokens) is dropped.
CROSS_ORIGIN_SAFE_HEADERS = frozenset({
    "accept",
    "accept-charset",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "content-language",
    "content-type",
    "dnt",
    "pragma",
    "range",
    "user-agent",
})


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), parsed.hostname, parsed.port or (
        443 if parsed.scheme.lower() == "https" else 80
    )


class _NoRedirectAuth(requests.auth.AuthBase):
    """Suppress both Session.auth and automatic netrc authentication."""

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        return request


def _follow_redirects(
    session: requests.Session,
    response: requests.Response,
    *,
    method: str,
    timeout: tuple[float, float],
    origin_class: str = DESTINATION_PRIVATE,
    plain_http_hosts: Any = frozenset(),
    follow: Any = None,
    **kwargs: Any,
) -> requests.Response:
    """Follow redirects manually so every hop is validated before it is requested.

    ``origin_class`` classifies the first URL: once a chain started on a public
    host, no hop may land on a private or loopback one. ``plain_http_hosts`` may
    be reached over http:// even when the process refuses public plain http;
    ``follow(url) -> bool`` can stop the chain before a hop, which is then left
    unrequested and named in ``response.wsn_not_followed``.
    """
    history: list[requests.Response] = []
    # The hop URL already carries the query the server chose. Passing ``params``
    # again would append the original query to it, so ``/search?q=kittens`` ->
    # ``/results?q=kittens&form=CANON`` would be re-requested with a duplicate
    # ``q``.
    kwargs.pop("params", None)
    for _ in range(MAX_REDIRECTS):
        if not response.is_redirect or not response.headers.get("location"):
            break
        try:
            location = urljoin(response.url, response.headers["location"])
            plain_ok = (plain_http_hosts(location) if callable(plain_http_hosts)
                        else (urlparse(location).hostname or "").lower() in plain_http_hosts)
            try:
                target = validate_http_url(location, allow_plain_http=True if plain_ok else None)
            except ValueError as refused:
                if follow is None:
                    raise
                # A caller that decides hop by hop (the security report) keeps the chain
                # so far and learns where it would have gone, instead of losing it all.
                response.wsn_not_followed = location
                response.wsn_not_followed_reason = str(refused)
                break
            if follow is not None and not follow(target):
                response.wsn_not_followed = target
                break
            try:
                hop_class = classify_destination(target)
                if origin_class == DESTINATION_PUBLIC and hop_class != DESTINATION_PUBLIC:
                    raise ValueError(
                        f"Redirect from a public host to the private address {target!r} is blocked"
                    )
            except ValueError as refused:
                if follow is None:
                    raise
                response.wsn_not_followed = target
                response.wsn_not_followed_reason = str(refused)
                break
            cross_origin = _origin(response.url) != _origin(target)
        except Exception:
            response.close()
            raise
        if cross_origin:
            # Manual redirect requests bypass requests' normal auth stripping.
            # Mask session defaults too, and never reintroduce credentials if a
            # later hop returns to the original origin. An empty Cookie header
            # also prevents the shared session jar from supplying host cookies
            # to another port on the same host.
            headers = requests.structures.CaseInsensitiveDict(
                {
                    name: value
                    for name, value in (kwargs.get("headers") or {}).items()
                    if str(name).lower() in CROSS_ORIGIN_SAFE_HEADERS
                }
            )
            for name in ("Authorization", "Proxy-Authorization", "Host"):
                headers[name] = None
            headers["Cookie"] = ""
            kwargs["headers"] = headers
            kwargs["auth"] = _NoRedirectAuth()
            kwargs.pop("cookies", None)
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


def _body_chunks(response: requests.Response, chunk_size: int = 65_536):
    """Yield the body as it arrives, returning after every socket read.

    ``iter_content`` blocks until a whole chunk is buffered, so a server
    trickling bytes could keep it waiting far past any deadline. urllib3's
    ``read1`` returns whatever one read produced, which lets the caller check
    its wall clock between reads. Errors are mapped the way requests maps them.
    """
    raw = getattr(response, "raw", None)
    if not isinstance(raw, BaseHTTPResponse) or not hasattr(raw, "read1"):
        yield from response.iter_content(chunk_size=chunk_size)
        return
    try:
        while True:
            chunk = raw.read1(chunk_size, decode_content=True)
            if not chunk:
                return
            yield chunk
    except urllib3_errors.ProtocolError as exc:
        raise requests.exceptions.ChunkedEncodingError(exc) from exc
    except urllib3_errors.DecodeError as exc:
        raise requests.exceptions.ContentDecodingError(exc) from exc
    except urllib3_errors.ReadTimeoutError as exc:
        raise requests.exceptions.ConnectionError(exc) from exc
    except urllib3_errors.SSLError as exc:
        raise requests.exceptions.SSLError(exc) from exc


def _seed_cookies(target: CookieJar, jar: CookieJar | None) -> dict[tuple, Any]:
    """Copy a caller's jar into the call's jar; the copies tell unchanged cookies apart."""
    seeded: dict[tuple, Any] = {}
    for cookie in jar if jar is not None else ():
        clone = copy.copy(cookie)
        target.set_cookie(clone)
        seeded[(cookie.domain, cookie.path, cookie.name)] = clone
    return seeded


def _merge_cookies_back(source: CookieJar, jar: CookieJar | None, seeded: dict) -> None:
    """Store what this call set or deleted in the caller's jar, then empty the call's jar."""
    if jar is None:
        return
    current = {(cookie.domain, cookie.path, cookie.name): cookie for cookie in source}
    for key in seeded.keys() - current.keys():
        try:
            jar.clear(*key)
        except KeyError:
            pass
    for key, cookie in current.items():
        if seeded.get(key) is not cookie:
            jar.set_cookie(copy.copy(cookie))
    source.clear()


def request(
    url: str,
    *,
    method: str = "GET",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int | None = None,
    allow_plain_http: bool | None = None,
    plain_http_hosts: Any = frozenset(),
    follow: Any = None,
    cookie_jar: CookieJar | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Send a bounded HTTP request and raise a useful error for bad responses.

    The body (up to ``max_response_bytes``) is read before the status is
    checked, so an HTTPError still carries the server's error payload on
    ``exc.response``. Explicit loopback/private URLs are allowed (local API
    testing); metadata/link-local hosts and public-to-private redirects are not.
    ``allow_plain_http=True`` admits a public http:// start URL for this one call
    (the security report reads how a site answers plain http); redirect hops are
    still validated with the process-wide rule, except hops to ``plain_http_hosts``
    (a set of host names, or a predicate on the hop URL such as a scope's ``allows``).
    ``follow(url) -> bool`` stops the redirect chain before a hop it refuses.
    ``cookie_jar`` opts into a caller-owned jar (http_request's http_session): it
    seeds this call, still under cookie matching and cross-origin redirect
    stripping, and what the responses set or deleted is merged back into it.
    """
    normalized = validate_http_url(url, allow_plain_http=allow_plain_http)
    origin_class = classify_destination(normalized)
    timeout = clamp_timeout(timeout_seconds)
    total_budget = timeout * TOTAL_DEADLINE_FACTOR
    deadline = time.monotonic() + total_budget
    if max_response_bytes is not None:
        kwargs["stream"] = True
    session = _session()
    # Every call starts with an empty jar. The session is per worker thread, so
    # cookies one call received used to ride along on a later, unrelated call -
    # another path, another agent - depending on which thread picked it up.
    # Redirect hops within this call still carry the cookies set along the way.
    session.cookies.clear()
    seeded = _seed_cookies(session.cookies, cookie_jar)
    timeouts = (min(5.0, timeout), timeout)
    try:
        response = session.request(
            method=method,
            url=normalized,
            timeout=timeouts,
            allow_redirects=False,
            **kwargs,
        )
        response = _follow_redirects(
            session, response, method=method, timeout=timeouts, origin_class=origin_class,
            plain_http_hosts=plain_http_hosts, follow=follow, **kwargs
        )
    finally:
        _merge_cookies_back(session.cookies, cookie_jar, seeded)
    try:
        if max_response_bytes is not None:
            limit = max(1024, min(int(max_response_bytes), 20_000_000))
            chunks: list[bytes] = []
            size = 0
            for chunk in _body_chunks(response):
                if time.monotonic() > deadline:
                    raise requests.Timeout(
                        f"Response body was not received within {total_budget:.0f} s"
                    )
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    # Keep the head and say so - for a success as for an error
                    # status: a big page is still a page, never an exception.
                    chunks.append(chunk[: len(chunk) - (size - limit)])
                    response.wsn_truncated = True
                    response.wsn_byte_limit = limit
                    break
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
        response.raise_for_status()
    except Exception:
        response.close()
        raise
    return response
