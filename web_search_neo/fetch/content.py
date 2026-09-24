"""HTTP extraction without MCP registration or browser state."""

import logging
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from web_search_neo.fetch.safety import redact_url, write_download
from web_search_neo.web_client import clamp_timeout

log = logging.getLogger("web_search_neo")

# A static fetch cannot run JavaScript, so a JS-rendered (SPA) page arrives as
# its shell: a title, an empty mount node, and bundled scripts. Below this
# much visible text there is nothing worth quoting, so the fetch says so
# instead of handing back a title-only page as if it were the content.
SPA_VISIBLE_TEXT_LIMIT = 300
# A mount node only counts as a shell marker while it is itself (almost)
# empty: a server-rendered page may legitimately mount into #root/#app and
# still carry real text, which the visible-text check above already let pass.
SPA_MOUNT_TEXT_LIMIT = 50
# Visible text left once the title is removed, below which a page that loads
# external scripts is a shell no matter how its bundle is named: the body said
# nothing at all, which a static page with even one sentence never does.
SPA_TITLE_ONLY_LIMIT = 3
SPA_MOUNT_IDS = frozenset({"root", "app", "__next"})
# Substrings typical of app bundles (webpack/Vite/Next chunks). Deliberately
# not a generic ".min.js": a tiny static page pulling jQuery is still a page,
# not an app shell.
BUNDLE_SRC_PARTS = (
    "bundle",
    "chunk",
    "_next/static",
    "/assets/",
    "static/js",
    "main.",
    "app.",
    "index.",
)


def is_spa_shell(html: str, text: str) -> bool:
    """Report whether ``html`` looks like a JS app shell with almost no content.

    All of these have to hold at once: the extracted visible text (scripts,
    styles, and noscript already removed) is below SPA_VISIBLE_TEXT_LIMIT,
    and the markup carries an empty mount node (``<div id="root">`` and
    friends) or a bundled-JS script. Either signal alone misfires - tiny
    static pages exist, and so do large bundled ones - which is why short
    text by itself never flags.
    """
    if len((text or "").strip()) >= SPA_VISIBLE_TEXT_LIMIT:
        return False
    if not html or "<" not in html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(id=True):
        if not isinstance(node, Tag):
            continue
        if str(node.get("id", "")).strip() not in SPA_MOUNT_IDS:
            continue
        # WHY an emptiness check, not just the id: the id alone also matches
        # server-rendered pages that mount real text into #root/#app.
        if len(node.get_text(strip=True)) < SPA_MOUNT_TEXT_LIMIT:
            return True
    for script in soup.find_all("script", src=True):
        if not isinstance(script, Tag):
            continue
        if str(script.get("type", "") or "").strip().lower() == "module":
            # WHY: bundler-less ESM entry points (Vite's /src/main.tsx) carry
            # no bundle-ish filename, but type=module + src is still an app.
            return True
        lowered_src = str(script.get("src", "") or "").lower()
        if lowered_src and any(part in lowered_src for part in BUNDLE_SRC_PARTS):
            return True
    # Title-only text: once the <title> is taken away nothing is left, and the
    # page loads external scripts - the rest is drawn by JavaScript whatever the
    # bundle happens to be called.
    title_node = soup.find("title")
    title = title_node.get_text(strip=True) if isinstance(title_node, Tag) else ""
    remainder = (text or "").replace(title, "", 1).strip() if title else (text or "").strip()
    return len(remainder) < SPA_TITLE_ONLY_LIMIT and soup.find("script", src=True) is not None


def _is_html_response(response: Any) -> bool:
    """Skip SPA detection for payloads that are not HTML (JSON APIs, files).

    A missing content-type means "try HTML": hand-rolled fakes in the test
    suite carry none, and a real HTML server always sends one, so only an
    explicitly non-HTML type opts out.
    """
    headers = getattr(response, "headers", None) or {}
    try:
        content_type = headers.get("content-type", "") or headers.get("Content-Type", "")
    except AttributeError:
        return True
    if not content_type:
        return True
    lowered = str(content_type).lower()
    return "html" in lowered or lowered.startswith("text/")


def _spa_notice(url: str) -> str:
    """The pointer appended to a title-only shell instead of silent content."""
    return (
        "\n\n[spa_suspected=true] SPA: use browser session - this page renders "
        "its content with JavaScript, so the static fetch only sees the shell "
        "(usually just the title). Render it in a browser session instead: "
        f'web_action [{{"action":"open","url":"{url}","session_id":"<id>"}}] then '
        "web_info(topic='page_text', params={'session_id':'<id>'})."
    )


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
    When ``mode="text"`` sees an SPA shell (title-only text plus an empty
    mount node or bundled JS), the shell text is kept and a
    ``[spa_suspected=true] SPA: use browser session`` pointer is appended,
    since a static fetch cannot render the real content.
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
    raw_html = response.text
    soup = BeautifulSoup(raw_html, "html.parser")
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    full_text = soup.get_text(separator="\n", strip=True)
    # WHY detect before truncating: a long page fetched with a small max_chars
    # would otherwise look "almost empty" and earn a notice it does not need.
    spa_suspected = _is_html_response(response) and is_spa_shell(raw_html, full_text)
    limit = max(1, min(int(max_chars), 500_000))
    text = full_text[:limit]
    if spa_suspected:
        # WHY appended after the limit: the notice is fetch metadata, not page
        # content, so it must never be cut off by max_chars - and the original
        # shell text stays first, keeping the str return a superset of before.
        final_url = getattr(response, "url", None) or url
        text += _spa_notice(str(final_url))
    return text


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
