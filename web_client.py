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
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa")

_local = threading.local()


def _plain_http_allowed() -> bool:
    """Report whether unencrypted http:// to public hosts is explicitly enabled."""
    return os.getenv(ALLOW_PLAIN_HTTP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def is_local_host(host: str) -> bool:
    """Report whether a hostname points at this machine or a private network.

    Local development servers stay reachable over plain http; only public hosts
    are required to use https.
    """
    hostname = (host or "").strip().strip("[]").lower()
    if not hostname:
        return False
    if hostname == "localhost" or hostname.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


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
                "Loopback and private-network addresses are always allowed."
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
    timeout = max(1.0, min(float(timeout_seconds), 60.0))
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
