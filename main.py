"""Web Search Neo: API-free search, fetch, and rendered browser MCP server."""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Any, Literal
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
        "submitting, or capturing it; reuse the same session_id for subsequent actions. "
        "For canvas/WebGL games use browser_game_probe, browser_pointer, "
        "browser_press_keys, browser_input_batch, and browser_render_control. "
        "Search challenges fall back immediately unless challenge_mode is manual. Browser "
        "profiles are temporary by default; persistent and attach modes preserve explicit "
        "user-managed authorization without sending passwords through the model."
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
    challenge_mode: Literal["fallback", "manual"] = "fallback",
    manual_timeout_seconds: float = 180.0,
) -> dict:
    """Search with immediate fallback, or allow a three-minute manual challenge handoff."""
    if challenge_mode not in {"fallback", "manual"}:
        raise ValueError("challenge_mode must be 'fallback' or 'manual'")
    if challenge_mode == "fallback":
        response = await asyncio.to_thread(
            msp_search.search_web,
            query,
            num,
            engine,
            fallback,
            timeout_seconds,
            fresh,
        )
        return {**response, "challenge_mode": "fallback"}

    manual_timeout = max(10.0, min(float(manual_timeout_seconds), 300.0))
    initial = await asyncio.to_thread(
        msp_search.search_web,
        query,
        num,
        engine,
        False,
        timeout_seconds,
        fresh,
    )
    if initial.get("success"):
        return {**initial, "challenge_mode": "manual", "manual_challenge": None}

    recoveries = initial.get("challenge_recoveries") or []
    if not recoveries:
        if not fallback:
            return {**initial, "challenge_mode": "manual", "manual_challenge": None}
        response = await asyncio.to_thread(
            msp_search.search_web,
            query,
            num,
            engine,
            True,
            timeout_seconds,
            True,
        )
        return {**response, "challenge_mode": "manual", "manual_challenge": None}

    recovery = recoveries[0]
    suggested = recovery["suggested_arguments"]
    arguments = (
        suggested["actions"][0]
        if isinstance(suggested, dict) and suggested.get("actions")
        else suggested
    )
    session_id = arguments["session_id"]
    manual: dict[str, Any] = {
        "provider": recovery["provider"],
        "session_id": session_id,
        "browser_url": recovery["browser_url"],
        "timeout_seconds": manual_timeout,
        "opened": False,
        "resolved": False,
        "timed_out": False,
        "session_open": False,
        "fallback_continued": False,
    }
    try:
        await asyncio.to_thread(
            browser_tools.open_page,
            recovery["browser_url"],
            session_id,
            1440,
            900,
            min(timeout_seconds, 20.0),
            False,
        )
        manual["opened"] = True
        waited = await asyncio.to_thread(
            browser_tools.wait_for_challenge_resolution,
            session_id,
            manual_timeout,
        )
        manual.update(
            resolved=bool(waited["resolved"]),
            timed_out=bool(waited["timed_out"]),
            challenge_seen=bool(waited["challenge_seen"]),
            waited_seconds=waited["waited_seconds"],
            session_open=True,
            url=waited["url"],
            title=waited["title"],
        )
    except Exception as exc:
        manual["error"] = f"{type(exc).__name__}: {exc}"

    if manual["resolved"]:
        return {
            **initial,
            "success": True,
            "engine_used": recovery["provider"],
            "challenge_mode": "manual",
            "outcome": "manual_browser_ready",
            "result_source": "browser_session",
            "browser_session_ready": True,
            "manual_challenge": manual,
            "next_tools": ["web_info", "web_action"],
            "next_calls": [
                {
                    "tool": "web_info",
                    "arguments": {
                        "topic": "page_elements",
                        "params": {"session_id": session_id},
                    },
                },
                {
                    "tool": "web_info",
                    "arguments": {
                        "topic": "screenshot",
                        "params": {"session_id": session_id},
                    },
                },
            ],
        }

    if manual["opened"]:
        closed = await asyncio.to_thread(browser_tools.close_session, session_id)
        manual["session_open"] = False
        manual["closed"] = bool(closed["closed"])
    if not fallback:
        return {
            **initial,
            "challenge_mode": "manual",
            "outcome": "manual_challenge_failed",
            "manual_challenge": manual,
        }

    response = await asyncio.to_thread(
        msp_search.search_web,
        query,
        num,
        engine,
        True,
        timeout_seconds,
        True,
    )
    manual["fallback_continued"] = True
    return {
        **response,
        "challenge_mode": "manual",
        "outcome": "fallback_after_manual_timeout",
        "manual_challenge": manual,
    }


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
    headless: bool | None = None,
    profile_mode: Literal[
        "auto", "current", "temporary", "persistent", "attach"
    ] = "current",
    profile_id: str | None = None,
    debugger_address: str | None = None,
    current_tab_id: int | None = None,
    tab_group: str = "AI",
) -> dict[str, Any]:
    """Open in the current Chrome AI group by default; use auto for Selenium fallback."""
    return await asyncio.to_thread(
        browser_tools.open_page,
        url,
        session_id,
        width,
        height,
        timeout_seconds,
        headless,
        profile_mode,
        profile_id,
        debugger_address,
        current_tab_id,
        tab_group,
    )


@mcp.tool()
async def browser_open_pages(
    urls: list[str],
    session_ids: list[str] | None = None,
    width: int = 1440,
    height: int = 900,
    timeout_seconds: float = 20.0,
    headless: bool | None = None,
    profile_mode: Literal[
        "auto", "current", "temporary", "persistent", "attach"
    ] = "current",
    tab_group: str = "AI",
) -> dict[str, Any]:
    """Open up to four pages, using the current Chrome AI group by default."""
    if not urls or len(urls) > browser_tools.MAX_SESSIONS:
        raise ValueError(f"Provide 1-{browser_tools.MAX_SESSIONS} URLs")
    ids = session_ids or [f"page-{index + 1}" for index in range(len(urls))]
    if len(ids) != len(urls) or len(set(ids)) != len(ids):
        raise ValueError("session_ids must be unique and match the number of URLs")

    resolved_profile_mode = await asyncio.to_thread(
        browser_tools.resolve_profile_mode, profile_mode, headless
    )

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
                resolved_profile_mode,
                None,
                None,
                None,
                tab_group,
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
async def browser_list_tabs(wait_seconds: float = 1.0) -> dict[str, Any]:
    """List web tabs in the user's already-open Chrome, including tab group names."""
    return await asyncio.to_thread(browser_tools.get_current_tabs, wait_seconds)


@mcp.tool()
async def browser_setup_current_chrome(
    confirm_install: bool = False,
    timeout_seconds: float = 30.0,
    window_title: str | None = None,
) -> dict[str, Any]:
    """Install/enable the companion in current Chrome; requires explicit consent."""
    return await asyncio.to_thread(
        browser_tools.setup_current_chrome_companion,
        confirm_install,
        timeout_seconds,
        window_title,
    )


@mcp.tool()
async def browser_attach_tab(
    tab_id: int,
    session_id: str = "default",
) -> dict[str, Any]:
    """Attach a reusable MCP session to one existing Chrome tab without navigating it."""
    return await asyncio.to_thread(
        browser_tools.attach_current_tab, tab_id, session_id
    )


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
async def browser_wait_for_challenge(
    session_id: str = "default",
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Wait up to three minutes for a human to clear a visible browser challenge."""
    return await asyncio.to_thread(
        browser_tools.wait_for_challenge_resolution,
        session_id,
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
async def browser_press_keys(
    keys: list[str],
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    hold_seconds: float = 0.05,
    repeat: int = 1,
    wait_seconds: float = 0.2,
    action: Literal["tap", "hold", "release"] = "tap",
) -> dict[str, Any]:
    """Tap, hold, or release one or more keys as a single input batch."""
    return await asyncio.to_thread(
        browser_tools.press_keys,
        keys,
        session_id,
        target_selector,
        frame_selector,
        hold_seconds,
        repeat,
        wait_seconds,
        action,
    )


@mcp.tool()
async def browser_pointer(
    action: Literal[
        "click", "double_click", "move", "hover", "drag", "press", "release"
    ],
    x: float,
    y: float,
    session_id: str = "default",
    end_x: float | None = None,
    end_y: float | None = None,
    button: Literal["left", "right", "middle"] = "left",
    duration_seconds: float = 0.3,
    frame_selector: str | None = None,
    wait_seconds: float = 0.2,
    coordinate_mode: Literal["absolute", "delta"] = "absolute",
) -> dict[str, Any]:
    """Click, hover, drag, press, or release using absolute or delta coordinates."""
    return await asyncio.to_thread(
        browser_tools.pointer_action,
        action,
        x,
        y,
        session_id,
        end_x,
        end_y,
        button,
        duration_seconds,
        frame_selector,
        wait_seconds,
        coordinate_mode,
    )


@mcp.tool()
async def browser_input_batch(
    key_actions: list[dict[str, str]] | None = None,
    pointer_actions: list[dict[str, Any]] | None = None,
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    wait_seconds: float = 0.2,
) -> dict[str, Any]:
    """Mix per-key and pointer actions, then advance exactly one step-mode frame."""
    return await asyncio.to_thread(
        browser_tools.input_batch,
        key_actions,
        pointer_actions,
        session_id,
        target_selector,
        frame_selector,
        wait_seconds,
    )


@mcp.tool()
async def browser_game_probe(
    session_id: str = "default",
    frame_selector: str | None = None,
    sample_seconds: float = 1.0,
    include_console: bool = True,
) -> dict[str, Any]:
    """Report canvas/WebGL readiness, sampled FPS, focus, frames, and console issues."""
    return await asyncio.to_thread(
        browser_tools.game_probe,
        session_id,
        frame_selector,
        sample_seconds,
        include_console,
    )


@mcp.tool()
async def browser_render_control(
    mode: Literal["normal", "throttled", "step"],
    session_id: str = "default",
    target_fps: float = 10.0,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Run normally, throttle requestAnimationFrame, or advance frames only on command/input."""
    return await asyncio.to_thread(
        browser_tools.set_render_control,
        mode,
        session_id,
        target_fps,
        frame_selector,
    )


@mcp.tool()
async def browser_render_step(
    frames: int = 1,
    session_id: str = "default",
) -> dict[str, Any]:
    """Advance an active step-mode game by an exact bounded number of animation frames."""
    return await asyncio.to_thread(browser_tools.render_step, frames, session_id)


@mcp.tool()
async def browser_release_inputs(session_id: str = "default") -> dict[str, Any]:
    """Release every keyboard key and mouse button held by a browser session."""
    return await asyncio.to_thread(browser_tools.release_inputs, session_id)


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


# Keep the narrow Python wrappers above for compatibility and direct testing, but expose
# only a compact self-documenting MCP surface to models.
legacy_mcp = mcp
mcp = FastMCP(
    "Web Search Neo",
    instructions=(
        "Use web_info for discovery and observation. Start with topic=capabilities when "
        "the compact contract is not already known. Use web_action for one or many "
        "ordered mutations. Reuse session_id across browser actions. In step render "
        "mode an input action applies all mixed keyboard and pointer changes before "
        "advancing exactly one frame."
    ),
)


_ACTION_HANDLERS = {
    "search": search_web,
    "fetch_text": fetch_url_text,
    "fetch_links": fetch_page_links,
    "fetch_many": fetch_urls_text,
    "open": browser_open_page,
    "open_many": browser_open_pages,
    "attach_tab": browser_attach_tab,
    "setup_current_chrome": browser_setup_current_chrome,
    "wait": browser_wait_for,
    "wait_challenge": browser_wait_for_challenge,
    "fill": browser_fill_fields,
    "upload": browser_upload_file,
    "click": browser_click,
    "input": browser_input_batch,
    "render": browser_render_control,
    "step": browser_render_step,
    "release_inputs": browser_release_inputs,
    "submit": browser_submit_form,
    "close": browser_close,
    "close_all": browser_close_all,
}

_ACTION_TOOL_NAMES = {
    "search": "search_web",
    "fetch_text": "fetch_url_text",
    "fetch_links": "fetch_page_links",
    "fetch_many": "fetch_urls_text",
    "open": "browser_open_page",
    "open_many": "browser_open_pages",
    "attach_tab": "browser_attach_tab",
    "setup_current_chrome": "browser_setup_current_chrome",
    "wait": "browser_wait_for",
    "wait_challenge": "browser_wait_for_challenge",
    "fill": "browser_fill_fields",
    "upload": "browser_upload_file",
    "click": "browser_click",
    "input": "browser_input_batch",
    "render": "browser_render_control",
    "step": "browser_render_step",
    "release_inputs": "browser_release_inputs",
    "submit": "browser_submit_form",
    "close": "browser_close",
    "close_all": "browser_close_all",
}


def _capabilities(action_name: str | None = None) -> dict[str, Any]:
    document = {
        "public_tools": ["web_info", "web_action"],
        "info_topics": {
            "capabilities": "This compact contract and examples.",
            "action_schema": "Detailed parameters for one action type; pass params.action.",
            "search_status": "Configured/live search providers, latency, cooldowns, challenges.",
            "browser_status": "Chrome availability and named session state.",
            "browser_tabs": "Open tabs in the user's current Chrome with IDs and groups.",
            "page_elements": "Rendered links, forms, fields, and buttons with CSS selectors.",
            "game_probe": "Canvas/WebGL/iframe surfaces, FPS, focus, console, held input.",
            "screenshot": "PNG Image response for a browser session.",
            "time": "Current local date, time, and UTC offset.",
        },
        "action_types": {
            "search": {
                "required": ["query"],
                "optional": [
                    "num",
                    "engine",
                    "fallback",
                    "timeout_seconds",
                    "fresh",
                    "challenge_mode",
                    "manual_timeout_seconds",
                ],
            },
            "fetch_text": {"required": ["url"], "optional": ["max_chars", "timeout_seconds"]},
            "fetch_links": {"required": ["url"], "optional": ["limit", "timeout_seconds"]},
            "fetch_many": {
                "required": ["urls"],
                "optional": ["max_chars_per_page", "timeout_seconds"],
            },
            "open": {
                "required": ["url"],
                "optional": [
                    "session_id",
                    "width",
                    "height",
                    "timeout_seconds",
                    "headless",
                    "profile_mode",
                    "profile_id",
                    "debugger_address",
                    "current_tab_id",
                    "tab_group",
                ],
            },
            "open_many": {"required": ["urls"], "optional": ["session_ids", "width", "height", "timeout_seconds", "headless", "profile_mode", "tab_group"]},
            "attach_tab": {"required": ["tab_id"], "optional": ["session_id"]},
            "setup_current_chrome": {
                "optional": ["confirm_install", "timeout_seconds", "window_title"],
                "confirmation": "Call first without confirmation; proceed only after the user explicitly approves extension installation.",
            },
            "wait": {"required": ["selector"], "optional": ["session_id", "state", "timeout_seconds"]},
            "wait_challenge": {"optional": ["session_id", "timeout_seconds"]},
            "fill": {"required": ["fields"], "optional": ["files", "session_id"]},
            "upload": {"required": ["selector", "file_paths"], "optional": ["session_id"]},
            "click": {"required": ["selector"], "optional": ["session_id", "wait_seconds"]},
            "input": {
                "optional": [
                    "key_actions",
                    "pointer_actions",
                    "session_id",
                    "target_selector",
                    "frame_selector",
                    "wait_seconds",
                ],
                "key_action": {"key": "W|SPACE|...", "action": "tap|hold|release"},
                "pointer_action": {
                    "action": "click|double_click|hover|move|drag|press|release",
                    "coordinates": "x/y absolute, or deltas when coordinate_mode=delta",
                },
            },
            "render": {"required": ["mode"], "optional": ["session_id", "target_fps", "frame_selector"], "modes": ["normal", "throttled", "step"]},
            "step": {"optional": ["frames", "session_id"]},
            "release_inputs": {"optional": ["session_id"]},
            "submit": {"required": ["form_selector"], "optional": ["session_id", "submit_selector", "wait_seconds"]},
            "close": {"optional": ["session_id"]},
            "close_all": {},
        },
        "examples": {
            "search": {
                "actions": [
                    {
                        "action": "search",
                        "query": "free browser automation MCP",
                        "engine": "duckduckgo",
                        "fallback": True,
                    }
                ]
            },
            "atomic_game_frame": {
                "actions": [
                    {
                        "action": "render",
                        "mode": "step",
                        "session_id": "game",
                        "frame_selector": "#game-frame",
                    },
                    {
                        "action": "input",
                        "session_id": "game",
                        "frame_selector": "#game-frame",
                        "key_actions": [
                            {"key": "W", "action": "hold"},
                            {"key": "S", "action": "release"},
                            {"key": "SPACE", "action": "tap"},
                            {"key": "E", "action": "tap"},
                        ],
                        "pointer_actions": [
                            {"action": "hover", "x": 640, "y": 360},
                            {
                                "action": "move",
                                "x": 20,
                                "y": -5,
                                "coordinate_mode": "delta",
                            },
                        ],
                    },
                ]
            },
        },
        "limits": {
            "ordered_actions_per_call": 32,
            "parallel_browser_sessions": browser_tools.MAX_SESSIONS,
            "input_actions_per_batch": 16,
            "automatic_captcha": False,
            "captcha_modes": ["fallback", "manual"],
        },
    }
    if action_name is not None:
        selected = action_name.strip().lower()
        notes = document["action_types"].get(selected)
        legacy_name = _ACTION_TOOL_NAMES.get(selected)
        if notes is None or legacy_name is None:
            raise ValueError(f"Unknown action schema: {selected}")
        original = legacy_mcp._tool_manager._tools[legacy_name].parameters
        input_schema = {
            **original,
            "properties": {
                "action": {
                    "const": selected,
                    "description": "Dispatcher action name.",
                    "type": "string",
                },
                **original.get("properties", {}),
            },
            "required": ["action", *original.get("required", [])],
            "title": f"{selected}Action",
        }
        response = {"action": selected, "input_schema": input_schema, "notes": notes}
        if selected == "input":
            response["example"] = document["examples"]["atomic_game_frame"]
        elif selected == "search":
            response["example"] = document["examples"]["search"]
        return response
    document.pop("action_types")
    document["action_groups"] = {
        "search": ["search"],
        "fetch": ["fetch_text", "fetch_links", "fetch_many"],
        "session": ["setup_current_chrome", "open", "open_many", "attach_tab", "close", "close_all"],
        "page": ["wait", "wait_challenge", "fill", "upload", "click", "submit"],
        "game": ["input", "render", "step", "release_inputs"],
    }
    document["discovery"] = {
        "next_call": "web_info",
        "topic": "action_schema",
        "params_example": {"action": "input"},
        "note": "Describe only the action you need, then call it through web_action.",
    }
    return document


@mcp.tool()
async def web_info(
    topic: Literal[
        "capabilities",
        "action_schema",
        "search_status",
        "browser_status",
        "browser_tabs",
        "page_elements",
        "game_probe",
        "screenshot",
        "time",
    ] = "capabilities",
    params: dict[str, Any] | None = None,
) -> Any:
    """Discover the compact contract or read search, page, browser, game, image, or time state."""
    arguments = dict(params or {})
    if topic == "capabilities":
        if arguments:
            raise ValueError("capabilities does not accept params")
        return _capabilities()
    if topic == "action_schema":
        action_name = str(arguments.pop("action", ""))
        if arguments:
            raise ValueError("action_schema accepts only params.action")
        if not action_name:
            raise ValueError("action_schema requires params.action")
        return _capabilities(action_name)
    if topic == "search_status":
        return await get_search_engines_status(**arguments)
    if topic == "browser_status":
        return await browser_get_status(**arguments)
    if topic == "browser_tabs":
        return await browser_list_tabs(**arguments)
    if topic == "page_elements":
        return await browser_get_page_elements(**arguments)
    if topic == "game_probe":
        return await browser_game_probe(**arguments)
    if topic == "screenshot":
        return await browser_screenshot(**arguments)
    if topic == "time":
        if arguments:
            raise ValueError("time does not accept params")
        return get_current_time_and_region()
    raise ValueError(f"Unsupported info topic: {topic}")


@mcp.tool()
async def web_action(
    actions: list[dict[str, Any]],
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Execute 1-32 ordered search, fetch, browser, form, input, render, or close actions."""
    if not actions or len(actions) > 32:
        raise ValueError("Provide 1-32 actions")
    results: list[dict[str, Any]] = []
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, dict):
            raise ValueError(f"Action {index} must be an object")
        arguments = dict(raw_action)
        action_name = str(arguments.pop("action", "")).strip().lower()
        handler = _ACTION_HANDLERS.get(action_name)
        if handler is None:
            error = {
                "index": index,
                "action": action_name or None,
                "success": False,
                "error": f"Unsupported action: {action_name or '<missing>'}",
            }
            results.append(error)
            if not continue_on_error:
                break
            continue
        try:
            data = await handler(**arguments)
            reported_failure = (
                isinstance(data, dict) and data.get("success") is False
            )
            results.append(
                {
                    "index": index,
                    "action": action_name,
                    "success": not reported_failure,
                    "data": data,
                    **(
                        {"error": str(data.get("error") or "Action reported success=false")}
                        if reported_failure
                        else {}
                    ),
                }
            )
            if reported_failure and not continue_on_error:
                break
        except Exception as exc:
            results.append(
                {
                    "index": index,
                    "action": action_name,
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if not continue_on_error:
                break
    failures = sum(not item["success"] for item in results)
    return {
        "success": failures == 0 and len(results) == len(actions),
        "requested_count": len(actions),
        "completed_count": len(results),
        "failure_count": failures,
        "stopped_early": len(results) < len(actions),
        "results": results,
    }


def main() -> None:
    browser_tools.start_current_chrome_bridge()
    active_mcp = (
        legacy_mcp
        if os.getenv("WEB_SEARCH_NEO_LEGACY_TOOLS", "").strip() == "1"
        else mcp
    )
    active_mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
