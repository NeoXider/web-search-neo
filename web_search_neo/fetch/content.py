"""HTTP extraction without MCP registration or browser state."""

import logging
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from web_search_neo.fetch.safety import redact_url, write_download
from web_search_neo.web_client import clamp_timeout

log = logging.getLogger("web_search_neo")


def _fetch_url_text(
    url: str,
    max_chars: int = 50_000,
    timeout_seconds: float = 20.0,
    mode: str = "text",
    headers: dict[str, str] | None = None,
    save_to: str | None = None,
    overwrite: bool = False,
    *, request_client: Any,
) -> str:
    """Fetch a URL as readable text, raw source, or straight to a file.

    ``mode="text"`` (default) strips scripts/styles and returns readable text.
    ``mode="html"``/``"raw"`` return the raw response body (for mining JS
    bundles and markup without a browser). ``headers`` sends custom request
    headers. ``save_to`` writes the raw bytes to a file inside the download
    directory (fetch.safety) and returns a short confirmation instead of the
    body; an existing file is only replaced with ``overwrite=True``.
    """
    normalized_mode = str(mode or "text").strip().lower()
    if normalized_mode not in {"text", "html", "raw"}:
        raise ValueError("mode must be 'text', 'html', or 'raw'")
    log.info("Fetching text from %s", redact_url(url))
    byte_limit = min(max(1_000_000, int(max_chars) * 8), 10_000_000)
    extra: dict[str, Any] = {}
    if headers:
        if not isinstance(headers, dict):
            raise ValueError("headers must be a {name: value} map")
        extra["headers"] = {str(k): str(v) for k, v in headers.items()}
    response = request_client(
        url, timeout_seconds=clamp_timeout(timeout_seconds), max_response_bytes=byte_limit, **extra
    )
    if save_to:
        body = response.content
        resolved = write_download(str(save_to), body, overwrite=bool(overwrite))
        return f"Saved {len(body)} bytes from {response.url} to {resolved}"
    if normalized_mode in {"html", "raw"}:
        limit = max(1, min(int(max_chars), 500_000))
        return response.text[:limit]
    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    text = soup.get_text(separator="\n", strip=True)
    limit = max(1, min(int(max_chars), 500_000))
    return text[:limit]


def _fetch_page_links(
    url: str,
    limit: int = 500,
    timeout_seconds: float = 20.0,
    headers: dict[str, str] | None = None,
    *, request_client: Any,
) -> list[str]:
    log.info("Fetching links from %s", redact_url(url))
    extra: dict[str, Any] = {}
    if headers:
        if not isinstance(headers, dict):
            raise ValueError("headers must be a {name: value} map")
        extra["headers"] = {str(k): str(v) for k, v in headers.items()}
    response = request_client(
        url, timeout_seconds=clamp_timeout(timeout_seconds), max_response_bytes=5_000_000, **extra
    )
    soup = BeautifulSoup(response.text, "html.parser")
    maximum = max(1, min(int(limit), 5000))
    links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        link = urljoin(response.url, str(anchor["href"]))
        if not link.startswith(("http://", "https://")) or link in seen:
            continue
        seen.add(link)
        links.append(link)
        if len(links) >= maximum:
            break
    return links
