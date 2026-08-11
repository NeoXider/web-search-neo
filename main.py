"""Web Search Neo: API-free search, fetch, and rendered browser MCP server."""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP, Image

import browser_tools
import msp_date_time
import msp_search
from web_client import request


PROJECT_DIR = Path(__file__).resolve().parent
log = logging.getLogger("web_search_neo")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = RotatingFileHandler(
        PROJECT_DIR / "msp_server.log",
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)


mcp = FastMCP(
    "Web Search Neo",
    instructions=(
        "Free web search and browser automation without API keys. DuckDuckGo is the "
        "default search engine. Open a browser page before inspecting, filling, clicking, "
        "submitting, or capturing it; reuse the same session_id for subsequent actions."
    ),
)


def _fetch_url_text(
    url: str, max_chars: int = 50_000, timeout_seconds: float = 20.0
) -> str:
    log.info("Fetching text from %s", url)
    byte_limit = min(max(1_000_000, int(max_chars) * 8), 10_000_000)
    response = request(
        url, timeout_seconds=timeout_seconds, max_response_bytes=byte_limit
    )
    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    text = soup.get_text(separator="\n", strip=True)
    limit = max(1, min(int(max_chars), 500_000))
    return text[:limit]


@mcp.tool()
async def fetch_url_text(
    url: str, max_chars: int = 50_000, timeout_seconds: float = 20.0
) -> str:
    """Download an HTTP(S) page without blocking parallel MCP tool calls."""
    return await asyncio.to_thread(_fetch_url_text, url, max_chars, timeout_seconds)


def _fetch_page_links(
    url: str, limit: int = 500, timeout_seconds: float = 20.0
) -> list[str]:
    log.info("Fetching links from %s", url)
    response = request(
        url, timeout_seconds=timeout_seconds, max_response_bytes=5_000_000
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


@mcp.tool()
async def fetch_page_links(
    url: str, limit: int = 500, timeout_seconds: float = 20.0
) -> list[str]:
    """Return de-duplicated absolute links without blocking parallel calls."""
    return await asyncio.to_thread(_fetch_page_links, url, limit, timeout_seconds)


@mcp.tool()
async def fetch_urls_text(
    urls: list[str], max_chars_per_page: int = 20_000, timeout_seconds: float = 20.0
) -> list[dict[str, Any]]:
    """Fetch up to 16 pages concurrently and return text or an error per URL."""
    if not urls:
        raise ValueError("urls must not be empty")
    if len(urls) > 16:
        raise ValueError("At most 16 URLs can be fetched in one call")

    async def fetch_one(url: str) -> dict[str, Any]:
        try:
            text = await asyncio.to_thread(
                _fetch_url_text, url, max_chars_per_page, timeout_seconds
            )
            return {"url": url, "success": True, "text": text, "error": None}
        except Exception as exc:
            return {
                "url": url,
                "success": False,
                "text": "",
                "error": f"{type(exc).__name__}: {exc}",
            }

    return await asyncio.gather(*(fetch_one(url) for url in urls))


@mcp.tool()
async def get_search_engines_status(
    check_live: bool = True,
    timeout_seconds: float = 6.0,
    force_refresh: bool = False,
) -> dict:
    """List search engines and optionally check their live availability in parallel."""
    return await asyncio.to_thread(
        msp_search.get_search_engines_status,
        check_live,
        timeout_seconds,
        force_refresh,
    )


@mcp.tool()
async def search_web(
    query: str,
    num: int = 5,
    engine: str = "duckduckgo",
    fallback: bool = True,
    timeout_seconds: float = 10.0,
    fresh: bool = False,
) -> dict:
    """Search the web without an API key; DuckDuckGo is the default engine."""
    return await asyncio.to_thread(
        msp_search.search_web, query, num, engine, fallback, timeout_seconds, fresh
    )


@mcp.tool()
async def search_duckduckgo(query: str, num: int = 5) -> list[dict[str, str]]:
    """Compatibility tool: search DuckDuckGo without browser startup."""
    return await asyncio.to_thread(
        msp_search.search_duckduckgo, query, max(1, min(int(num), 20))
    )


@mcp.tool()
async def search_bing(query: str, num: int = 5) -> list[dict[str, str]]:
    """Search Bing without an API key or browser startup."""
    return await asyncio.to_thread(
        msp_search.search_bing, query, max(1, min(int(num), 20))
    )


@mcp.tool()
async def browser_open_page(
    url: str,
    session_id: str = "default",
    width: int = 1440,
    height: int = 900,
    timeout_seconds: float = 20.0,
    headless: bool = True,
) -> dict[str, Any]:
    """Open a rendered page; set headless=false for manual challenge handoff."""
    return await asyncio.to_thread(
        browser_tools.open_page,
        url,
        session_id,
        width,
        height,
        timeout_seconds,
        headless,
    )


@mcp.tool()
async def browser_open_pages(
    urls: list[str],
    session_ids: list[str] | None = None,
    width: int = 1440,
    height: int = 900,
    timeout_seconds: float = 20.0,
    headless: bool = True,
) -> dict[str, Any]:
    """Open up to four pages concurrently, each in an independent browser session."""
    if not urls or len(urls) > browser_tools.MAX_SESSIONS:
        raise ValueError(f"Provide 1-{browser_tools.MAX_SESSIONS} URLs")
    ids = session_ids or [f"page-{index + 1}" for index in range(len(urls))]
    if len(ids) != len(urls) or len(set(ids)) != len(ids):
        raise ValueError("session_ids must be unique and match the number of URLs")

    async def open_one(url: str, session_id: str) -> dict[str, Any]:
        try:
            page = await asyncio.to_thread(
                browser_tools.open_page,
                url,
                session_id,
                width,
                height,
                timeout_seconds,
                headless,
            )
            return {"success": True, **page, "error": None}
        except Exception as exc:
            return {
                "success": False,
                "url": url,
                "session_id": session_id,
                "error": f"{type(exc).__name__}: {exc}",
            }

    pages = await asyncio.gather(
        *(open_one(url, session_id) for url, session_id in zip(urls, ids))
    )
    return {
        "success_count": sum(1 for page in pages if page["success"]),
        "failure_count": sum(1 for page in pages if not page["success"]),
        "pages": pages,
    }


@mcp.tool()
async def browser_get_page_elements(
    session_id: str = "default",
    include_links: bool = True,
    include_forms: bool = True,
    include_buttons: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Get rendered links, forms, fields, and buttons with CSS selectors."""
    return await asyncio.to_thread(
        browser_tools.get_page_elements,
        session_id,
        include_links,
        include_forms,
        include_buttons,
        limit,
    )


@mcp.tool()
async def browser_wait_for(
    selector: str,
    session_id: str = "default",
    state: str = "visible",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Wait for dynamic content to become present, visible, or clickable."""
    return await asyncio.to_thread(
        browser_tools.wait_for_element,
        selector,
        session_id,
        state,
        timeout_seconds,
    )


@mcp.tool()
async def browser_fill_fields(
    fields: dict[str, Any],
    files: dict[str, str] | None = None,
    session_id: str = "default",
) -> dict[str, Any]:
    """Fill rendered form fields; map CSS selectors to values or local file paths."""
    return await asyncio.to_thread(browser_tools.fill_fields, fields, files, session_id)


@mcp.tool()
async def browser_upload_file(
    selector: str,
    file_paths: list[str],
    session_id: str = "default",
) -> dict[str, Any]:
    """Upload one or more local files into a rendered input[type=file]."""
    return await asyncio.to_thread(
        browser_tools.upload_file, selector, file_paths, session_id
    )


@mcp.tool()
async def browser_click(
    selector: str,
    session_id: str = "default",
    wait_seconds: float = 0.5,
) -> dict[str, Any]:
    """Click one rendered page element using a CSS selector."""
    return await asyncio.to_thread(browser_tools.click, selector, session_id, wait_seconds)


@mcp.tool()
async def browser_submit_form(
    form_selector: str,
    session_id: str = "default",
    submit_selector: str | None = None,
    wait_seconds: float = 0.5,
) -> dict[str, Any]:
    """Submit a rendered form, preserving browser validation and submit events."""
    return await asyncio.to_thread(
        browser_tools.submit_form,
        form_selector,
        session_id,
        submit_selector,
        wait_seconds,
    )


@mcp.tool()
async def browser_screenshot(
    session_id: str = "default",
    width: int = 1440,
    height: int = 900,
    full_page: bool = False,
) -> Image:
    """Return a PNG screenshot of the rendered page at the requested resolution."""
    png = await asyncio.to_thread(
        browser_tools.screenshot, session_id, width, height, full_page
    )
    return Image(data=png, format="png")


@mcp.tool()
async def browser_get_status(session_id: str = "default") -> dict[str, Any]:
    """Check Chrome support and whether a named browser session is open."""
    return await asyncio.to_thread(browser_tools.get_status, session_id)


@mcp.tool()
async def browser_close(session_id: str = "default") -> dict[str, Any]:
    """Close a browser session and release its Chrome process."""
    return await asyncio.to_thread(browser_tools.close_session, session_id)


@mcp.tool()
async def browser_close_all() -> dict[str, Any]:
    """Close every browser session and release all Chrome processes."""
    await asyncio.to_thread(browser_tools.close_all_sessions)
    return {"closed_all": True, "active_sessions": []}


@mcp.tool()
def get_current_time_and_region() -> dict:
    """Return the current local date, time, and UTC-offset region string."""
    return msp_date_time.get_current_time_and_region()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
