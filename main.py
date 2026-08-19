"""Web Search Neo: API-free search, fetch, and rendered browser MCP server."""

import asyncio
from dataclasses import dataclass
import functools
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
from typing import Any, Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP, Image
from pydantic import ValidationError

import bridge_daemon
import browser_tools
import chrome_bridge
import macros
import msp_date_time
import msp_search
from web_client import request


__version__ = "1.3.5"

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
    recoveries = initial.get("challenge_recoveries") or []
    # An engine answering "no hits" is a success now, not a failure. That is right
    # for the caller, but in manual mode it must not swallow the handoff: a run
    # that came back empty *and* hit a challenge is exactly the run the user asked
    # to solve by hand, and returning it as a clean empty answer would strand them.
    stranded = bool(recoveries) and not initial.get("results")
    if initial.get("success") and not stranded:
        return {**initial, "challenge_mode": "manual", "manual_challenge": None}

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
        # "Nothing blocking here" and "the scan gave up early" have to stay apart
        # in the one place a person is watching the browser: this branch is the
        # human handoff, and a caller told the challenge is gone would close it.
        if waited.get("captcha_scan_incomplete"):
            manual["captcha_scan_incomplete"] = True
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
    tab_group: str = chrome_bridge.DEFAULT_TAB_GROUP,
) -> dict[str, Any]:
    """Open in the current Chrome's agent tab group by default; auto falls back to Selenium."""
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
    tab_group: str = chrome_bridge.DEFAULT_TAB_GROUP,
) -> dict[str, Any]:
    """Open up to four pages, using the current Chrome's agent tab group by default."""
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
async def browser_setup_current_chrome(wait_seconds: float = 1.0) -> dict[str, Any]:
    """Publish the bridge secret, update a stale companion, and return what is left."""
    return await asyncio.to_thread(
        browser_tools.setup_current_chrome_companion, wait_seconds
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
    offset: int = 0,
) -> dict[str, Any]:
    """Get rendered links, forms, fields, and buttons with CSS selectors."""
    return await asyncio.to_thread(
        browser_tools.get_page_elements,
        session_id,
        include_links,
        include_forms,
        include_buttons,
        limit,
        offset,
    )


@mcp.tool()
async def browser_page_outline(
    session_id: str = "default",
    limit: int = 200,
    include_occlusion: bool = True,
    output: Literal["text", "json"] = "text",
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Outline the page: roles, names, states, refs, and boxes, including shadow DOM."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.get_page_outline,
            session_id=session_id,
            limit=limit,
            include_occlusion=include_occlusion,
            output=output,
            frame_selector=frame_selector,
        )
    )


@mcp.tool()
async def browser_page_text(
    session_id: str = "default",
    max_chars: int = 20_000,
    mode: Literal["main", "full"] = "main",
    include_links: bool = False,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Read the rendered page as text, including content that only exists after JS."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.get_page_text,
            session_id=session_id,
            max_chars=max_chars,
            mode=mode,
            include_links=include_links,
            frame_selector=frame_selector,
        )
    )


@mcp.tool()
async def browser_find(
    query: str,
    session_id: str = "default",
    role: str | None = None,
    limit: int = 5,
    visible_only: bool = True,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Find elements by meaning and get refs back, instead of reading the whole page."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.find_elements,
            query,
            session_id=session_id,
            role=role,
            limit=limit,
            visible_only=visible_only,
            frame_selector=frame_selector,
        )
    )


@mcp.tool()
async def browser_console(
    session_id: str = "default",
    levels: list[str] | None = None,
    contains: str | None = None,
    kinds: list[str] | None = None,
    limit: int = 50,
    since_seq: int = 0,
    clear: bool = False,
) -> dict[str, Any]:
    """Read console output and uncaught page errors with stack traces."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.get_console,
            session_id=session_id,
            levels=levels,
            contains=contains,
            kinds=kinds,
            limit=limit,
            since_seq=since_seq,
            clear=clear,
        )
    )


@mcp.tool()
async def browser_network(
    session_id: str = "default",
    url_pattern: str | None = None,
    types: list[str] | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    only_errors: bool = False,
    limit: int = 50,
    output: Literal["text", "json"] = "text",
) -> dict[str, Any]:
    """List the page's HTTP requests with status, type, duration, and size."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.get_network,
            session_id=session_id,
            url_pattern=url_pattern,
            types=types,
            status_min=status_min,
            status_max=status_max,
            only_errors=only_errors,
            limit=limit,
            output=output,
        )
    )


@mcp.tool()
async def browser_network_body(
    request_id: str,
    session_id: str = "default",
    max_chars: int = 20_000,
) -> dict[str, Any]:
    """Fetch one response body by the request_id reported by the network topic."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.get_network_body,
            request_id,
            session_id=session_id,
            max_chars=max_chars,
        )
    )


@mcp.tool()
async def browser_wait_for(
    selector: str,
    session_id: str = "default",
    state: str = "visible",
    timeout_seconds: float = 10.0,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Wait for dynamic content to become present, visible, or clickable."""
    return await asyncio.to_thread(
        browser_tools.wait_for_element,
        selector,
        session_id,
        state,
        timeout_seconds,
        frame_selector,
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
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Fill rendered form fields; map CSS selectors to values or local file paths."""
    return await asyncio.to_thread(
        browser_tools.fill_fields, fields, files, session_id, frame_selector
    )


@mcp.tool()
async def browser_upload_file(
    selector: str,
    file_paths: list[str],
    session_id: str = "default",
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Upload one or more local files into a rendered input[type=file]."""
    return await asyncio.to_thread(
        browser_tools.upload_file, selector, file_paths, session_id, frame_selector
    )


@mcp.tool()
async def browser_click(
    selector: str,
    session_id: str = "default",
    wait_seconds: float = 0.5,
    frame_selector: str | None = None,
    trusted: bool = False,
) -> dict[str, Any]:
    """Click one rendered page element using a CSS selector.

    trusted=true dispatches a real trusted mouse sequence at the element's
    centre, for pages that reject synthetic clicks or read pointer position.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.click,
            selector,
            session_id=session_id,
            wait_seconds=wait_seconds,
            frame_selector=frame_selector,
            trusted=trusted,
        )
    )


@mcp.tool()
async def browser_run_script(
    script: str,
    args: list[Any] | None = None,
    session_id: str = "default",
    await_promise: bool = False,
    user_gesture: bool = False,
) -> dict[str, Any]:
    """Execute a JavaScript snippet in a session's page and return its value.

    Use for state the DOM reads do not expose (localStorage, virtualised lists,
    framework state) and for mutations without an input-shaped equivalent.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.execute_js,
            script,
            args=args,
            session_id=session_id,
            await_promise=await_promise,
            user_gesture=user_gesture,
        )
    )


@mcp.tool()
async def browser_execute_js(
    script: str,
    args: list[Any] | None = None,
    session_id: str = "default",
    await_promise: bool = False,
) -> dict[str, Any]:
    """Run a JavaScript snippet and report what it returns (info-topic form)."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.execute_js,
            script,
            args=args,
            session_id=session_id,
            await_promise=await_promise,
        )
    )


@mcp.tool()
async def browser_press_keys(
    keys: list[str],
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    hold_seconds: float = 0.05,
    repeat: int = 1,
    wait_seconds: float = 0.0,
    key_action: Literal["tap", "hold", "release"] = "tap",
    hold_frames: int = 1,
    focus_mode: Literal["focus", "click", "none"] = "focus",
    include_summary: bool = True,
) -> dict[str, Any]:
    """Tap, hold, or release one or more keys as a single input batch.

    ``key_action`` is the keyboard verb; the dispatcher already owns ``action``.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.press_keys,
            keys,
            session_id=session_id,
            target_selector=target_selector,
            frame_selector=frame_selector,
            hold_seconds=hold_seconds,
            repeat=repeat,
            wait_seconds=wait_seconds,
            action=key_action,
            hold_frames=hold_frames,
            focus_mode=focus_mode,
            include_summary=include_summary,
        )
    )


@mcp.tool()
async def browser_pointer(
    pointer_action: Literal[
        "click", "double_click", "move", "hover", "drag", "press", "release", "wheel"
    ],
    x: float,
    y: float,
    session_id: str = "default",
    end_x: float | None = None,
    end_y: float | None = None,
    button: Literal["left", "right", "middle"] = "left",
    duration_seconds: float = 0.3,
    frame_selector: str | None = None,
    wait_seconds: float = 0.0,
    coordinate_mode: Literal["absolute", "delta", "relative"] = "absolute",
    delta_x: float = 0.0,
    delta_y: float = 0.0,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Click, hover, drag, scroll the wheel, or hold a mouse button.

    Use coordinate_mode='relative' while pointer lock is held: the cursor cannot
    move, so only the movement delta reaches the game.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.pointer_action,
            pointer_action,
            x,
            y,
            session_id=session_id,
            end_x=end_x,
            end_y=end_y,
            button=button,
            duration_seconds=duration_seconds,
            frame_selector=frame_selector,
            wait_seconds=wait_seconds,
            coordinate_mode=coordinate_mode,
            delta_x=delta_x,
            delta_y=delta_y,
            include_summary=include_summary,
        )
    )


@mcp.tool()
async def browser_scroll(
    delta_y: float,
    session_id: str = "default",
    delta_x: float = 0.0,
    x: float | None = None,
    y: float | None = None,
    frame_selector: str | None = None,
    wait_seconds: float = 0.1,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Scroll down with positive delta_y or up with negative delta_y.

    Omit x/y to use the viewport centre; provide both to choose the scrollable
    container under that point.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.scroll_page,
            delta_y,
            session_id=session_id,
            delta_x=delta_x,
            x=x,
            y=y,
            frame_selector=frame_selector,
            wait_seconds=wait_seconds,
            include_summary=include_summary,
        )
    )


@mcp.tool()
async def browser_touch(
    touch_action: Literal["tap", "press", "move", "release", "swipe", "cancel"],
    points: list[dict[str, Any]] | None = None,
    session_id: str = "default",
    frame_selector: str | None = None,
    steps: int = 8,
    duration_seconds: float = 0.2,
    wait_seconds: float = 0.0,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Send touch input: tap, multi-finger press/move/release, or a swipe."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.touch_action,
            touch_action,
            points=points,
            session_id=session_id,
            frame_selector=frame_selector,
            steps=steps,
            duration_seconds=duration_seconds,
            wait_seconds=wait_seconds,
            include_summary=include_summary,
        )
    )


@mcp.tool()
async def browser_touch_emulation(
    session_id: str = "default",
    enabled: bool = True,
    max_touch_points: int = 5,
    reload_page: bool = True,
) -> dict[str, Any]:
    """Present the page as a touch device so mobile code paths actually run."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.set_touch_emulation,
            session_id=session_id,
            enabled=enabled,
            max_touch_points=max_touch_points,
            reload_page=reload_page,
        )
    )


@mcp.tool()
async def browser_pointer_lock(
    operation: Literal["acquire", "release", "status"] = "status",
    session_id: str = "default",
    selector: str | None = None,
    frame_selector: str | None = None,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Acquire, release, or read pointer lock for first-person style games."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.pointer_lock,
            operation,
            session_id=session_id,
            selector=selector,
            frame_selector=frame_selector,
            timeout_seconds=timeout_seconds,
        )
    )


@mcp.tool()
async def browser_input_batch(
    key_actions: list[dict[str, str]] | None = None,
    pointer_actions: list[dict[str, Any]] | None = None,
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    wait_seconds: float = 0.0,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Mix per-key and pointer actions, then advance exactly one step-mode frame."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.input_batch,
            key_actions=key_actions,
            pointer_actions=pointer_actions,
            session_id=session_id,
            target_selector=target_selector,
            frame_selector=frame_selector,
            wait_seconds=wait_seconds,
            include_summary=include_summary,
        )
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
    frame_delta_ms: float = 1000 / 60,
    freeze_time: bool = True,
    gate_timers: bool = True,
) -> dict[str, Any]:
    """Run normally, throttle requestAnimationFrame, or advance frames only on command/input."""
    result = await asyncio.to_thread(
        functools.partial(
            browser_tools.set_render_control,
            mode,
            session_id=session_id,
            target_fps=target_fps,
            frame_selector=frame_selector,
            frame_delta_ms=frame_delta_ms,
            freeze_time=freeze_time,
            gate_timers=gate_timers,
        )
    )
    # Say what to call next: "step mode is on" is not actionable on its own, and
    # a caller that does not know can sit here re-selecting the same mode.
    result["next"] = (
        'The page is frozen. Send {"action": "step", "frames": N, "session_id": '
        f'"{session_id}"}} to advance, or an input action, which advances one frame.'
        if mode == "step"
        else "Animation runs on its own; no step calls are needed."
    )
    return result


@mcp.tool()
async def browser_render_step(
    frames: int = 1,
    session_id: str = "default",
    include_summary: bool = True,
) -> dict[str, Any]:
    """Advance an active step-mode game by an exact bounded number of animation frames."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.render_step,
            frames,
            session_id=session_id,
            include_summary=include_summary,
        )
    )


@mcp.tool()
async def browser_release_inputs(session_id: str = "default") -> dict[str, Any]:
    """Release every key, mouse button and touch point held by a browser session."""
    return await asyncio.to_thread(browser_tools.release_inputs, session_id)


@mcp.tool()
async def browser_submit_form(
    form_selector: str,
    session_id: str = "default",
    submit_selector: str | None = None,
    wait_seconds: float = 0.5,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Submit a rendered form, preserving browser validation and submit events."""
    return await asyncio.to_thread(
        browser_tools.submit_form,
        form_selector,
        session_id,
        submit_selector,
        wait_seconds,
        frame_selector,
    )


@mcp.tool()
async def browser_screenshot(
    session_id: str = "default",
    width: int | None = None,
    height: int | None = None,
    full_page: bool = False,
    mode: Literal["viewport", "full_page", "region"] | None = None,
    x: float | None = None,
    y: float | None = None,
) -> Image:
    """Return a viewport, full-page, or page-region PNG screenshot."""
    png = await asyncio.to_thread(
        browser_tools.screenshot, session_id, width, height, full_page, mode, x, y
    )
    return Image(data=png, format="png")


@mcp.tool()
async def browser_automation_skill() -> dict[str, Any]:
    """Return the built-in compact browser automation playbook."""
    return _AUTOMATION_SKILL


@mcp.tool()
async def browser_show(session_id: str = "default") -> dict[str, Any]:
    """Explicitly put one browser session in the foreground."""
    return await asyncio.to_thread(browser_tools.show_session, session_id)


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
    return await asyncio.to_thread(browser_tools.close_all_sessions)


# One recording at a time, because there is one hand driving the browser: a
# second concurrent recording could only capture the same steps twice.
_RECORDING: dict[str, Any] = {"active": False, "name": "", "steps": []}

# A batch holds this for as long as it is dispatching, so two batches sent at
# once are recorded one after the other instead of interleaving into a script
# whose steps never ran in that order. It guards the recorder only: batches that
# are not being recorded never touch it and stay fully concurrent.
_RECORDING_LOCK = asyncio.Lock()


@mcp.tool()
async def browser_macro(
    op: str = "list",
    name: str | None = None,
    steps: list[dict[str, Any]] | None = None,
    variables: dict[str, Any] | None = None,
    description: str = "",
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Save, record, run, inspect, or delete a named action script with {{placeholders}}."""
    op = str(op or "").strip().lower()

    if op == "record":
        if not name:
            raise ValueError("macro op 'record' requires name")
        # Starting over silently would throw away a task already driven by hand,
        # which is the most expensive thing in the whole feature.
        if _RECORDING["active"] and _RECORDING["steps"]:
            raise ValueError(
                f"A recording of '{_RECORDING['name']}' is already open with "
                f"{len(_RECORDING['steps'])} step(s). Save it with op='save', or "
                "throw it away with op='cancel', before recording another."
            )
        _RECORDING.update({"active": True, "name": macros.validate_name(name), "steps": []})
        return {
            "success": True,
            "recording": True,
            "name": name,
            "note": (
                "Every action that dispatches from now on is captured. Drive the "
                "task once, then call macro op='save' to keep it under this name; "
                "op='cancel' throws the recording away."
            ),
        }

    if op == "cancel":
        was = _RECORDING["name"]
        _RECORDING.update({"active": False, "name": "", "steps": []})
        return {"success": True, "recording": False, "discarded": was}

    if op == "save":
        captured = list(_RECORDING["steps"])
        # An explicit step list is its own macro and must be named as one:
        # borrowing the open recording's name would overwrite the macro that
        # recording is going to be saved as, destroying work already done.
        if steps is not None and not name:
            raise ValueError(
                "macro op 'save' with explicit steps requires name; it will not "
                "borrow the name of the recording that is open."
            )
        target = name or _RECORDING["name"]
        if steps is None and not captured:
            raise ValueError(
                "macro op 'save' needs either explicit steps or an open recording "
                "that captured at least one action."
            )
        if not target:
            raise ValueError("macro op 'save' requires name")
        record = await asyncio.to_thread(
            macros.save, target, steps if steps is not None else captured, description, variables
        )
        if steps is None:
            _RECORDING.update({"active": False, "name": "", "steps": []})
        return {
            "success": True,
            "name": record["name"],
            "step_count": record["step_count"],
            "variables": sorted(record["variables"]),
            "recorded": steps is None,
        }

    if op == "run":
        if not name:
            raise ValueError("macro op 'run' requires name")
        record = await asyncio.to_thread(macros.load, name)
        resolved = macros.resolve(record["steps"], record.get("variables"), variables)
        # A replay is one logical call, so it does not re-enter the recorder and
        # does not inherit web_action's hand-written 32-action ceiling.
        outcome = await _execute_actions(resolved, continue_on_error, record=False)
        return {**outcome, "macro": record["name"], "step_count": len(resolved)}

    if op == "list":
        return {"success": True, "macros": await asyncio.to_thread(macros.list_macros)}

    if op == "show":
        if not name:
            raise ValueError("macro op 'show' requires name")
        record = await asyncio.to_thread(macros.load, name)
        return {"success": True, **record}

    if op == "delete":
        if not name:
            raise ValueError("macro op 'delete' requires name")
        return {
            "success": True,
            "deleted": await asyncio.to_thread(macros.delete, name),
            "name": name,
        }

    raise ValueError(
        f"macro op must be record, save, run, list, show, delete, or cancel, not '{op}'"
    )


@mcp.tool()
async def browser_captcha(
    mode: str = "auto",
    session_id: str = "default",
    timeout_seconds: float = 180.0,
    poll_seconds: float = 3.0,
) -> dict[str, Any]:
    """Detect a captcha and clear it: wait for a human, or use a configured solving service."""
    return await asyncio.to_thread(
        browser_tools.solve_captcha, mode, session_id, timeout_seconds, poll_seconds
    )


@mcp.tool()
async def browser_set_extra_headers(
    headers: dict[str, str] | None = None,
    session_id: str = "default",
) -> dict[str, Any]:
    """Send extra HTTP headers with every request this session makes; empty map clears them."""
    return await asyncio.to_thread(browser_tools.set_extra_headers, headers, session_id)


@mcp.tool()
async def browser_stealth(
    op: str = "on",
    session_id: str = "default",
) -> dict[str, Any]:
    """Hide common automation tells (navigator.webdriver, plugins) before page scripts run."""
    return await asyncio.to_thread(browser_tools.stealth, op, session_id)


@mcp.tool()
async def browser_replay_request(
    request_id: str | None = None,
    session_id: str = "default",
    url: str | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    credentials: str = "include",
) -> dict[str, Any]:
    """Re-send a captured or explicit request from the page's context; return the full response."""
    return await asyncio.to_thread(
        browser_tools.replay_request,
        request_id,
        session_id,
        url,
        method,
        headers,
        body,
        credentials,
    )


@mcp.tool()
async def browser_inject_script(
    op: str = "add",
    source: str | None = None,
    identifier: str | None = None,
    session_id: str = "default",
) -> dict[str, Any]:
    """Register, list, or drop page code that runs before every document's own scripts."""
    return await asyncio.to_thread(
        browser_tools.inject_script, op, source, identifier, session_id
    )


@mcp.tool()
async def browser_cookies(
    op: str = "get",
    session_id: str = "default",
    domain: str | None = None,
    name: str | None = None,
    set_cookies: list[dict[str, Any]] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read, write, or clear cookies as full objects with flags (secure, httpOnly, sameSite)."""
    return await asyncio.to_thread(
        browser_tools.cookies, op, session_id, domain, name, set_cookies, limit
    )


@mcp.tool()
async def browser_local_storage(
    op: str = "read",
    session_id: str = "default",
    key: str | None = None,
    value: str | None = None,
    kind: str = "local",
) -> dict[str, Any]:
    """Read, write, or delete localStorage or sessionStorage entries for the open page."""
    return await asyncio.to_thread(
        browser_tools.local_storage, op, session_id, key, value, kind
    )


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


@dataclass(frozen=True)
class ActionSpec:
    """One dispatcher action, described once and reused everywhere."""

    name: str
    handler: Any
    tool_name: str
    group: str
    summary: str


def _action(name: str, handler: Any, group: str, summary: str) -> ActionSpec:
    return ActionSpec(name, handler, handler.__name__, group, summary)


_ACTIONS: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        _action("search", search_web, "search", "Web search with automatic multi-engine fallback."),
        _action(
            "fetch_text", fetch_url_text, "fetch", "Read one page as text without opening a browser."
        ),
        _action("fetch_links", fetch_page_links, "fetch", "List the links of one page without a browser."),
        _action("fetch_many", fetch_urls_text, "fetch", "Read several pages concurrently as text."),
        _action("open", browser_open_page, "session", "Open a URL in a named browser session."),
        _action(
            "open_many", browser_open_pages, "session", "Open several URLs in independent sessions at once."
        ),
        _action(
            "attach_tab",
            browser_attach_tab,
            "session",
            "Claim an existing Chrome tab by id without navigating or moving it.",
        ),
        _action(
            "setup_current_chrome",
            browser_setup_current_chrome,
            "session",
            "Publish the bridge secret and return the manual steps Chrome still requires.",
        ),
        _action(
            "show",
            browser_show,
            "session",
            "Explicitly bring one session to the foreground; this may interrupt the user.",
        ),
        _action("wait", browser_wait_for, "page", "Wait until an element is present, visible, or clickable."),
        _action(
            "wait_challenge",
            browser_wait_for_challenge,
            "page",
            "Hand the visible browser to the user so they can solve a challenge.",
        ),
        _action("fill", browser_fill_fields, "page", "Set values on form fields by CSS selector."),
        _action("upload", browser_upload_file, "page", "Attach local files to a file input."),
        _action("click", browser_click, "page", "Click one element by CSS selector."),
        _action(
            "run_script",
            browser_run_script,
            "page",
            "Execute a JavaScript snippet in a session's page and return its value.",
        ),
        _action(
            "input",
            browser_input_batch,
            "game",
            "Apply mixed keyboard and pointer input atomically; releases one frame in step mode.",
        ),
        _action(
            "press_keys",
            browser_press_keys,
            "game",
            "Keyboard-only input: tap, hold, or release keys, optionally across N frames.",
        ),
        _action(
            "pointer",
            browser_pointer,
            "game",
            "One pointer event: click, hover, drag, wheel, or a held button.",
        ),
        _action(
            "scroll",
            browser_scroll,
            "page",
            "Scroll the page or the container under a viewport point; positive delta_y moves down.",
        ),
        _action(
            "touch",
            browser_touch,
            "game",
            "Touch input: tap, swipe, or multi-finger press/move/release.",
        ),
        _action(
            "touch_emulation",
            browser_touch_emulation,
            "game",
            "Present the page as a touch device so mobile code paths run.",
        ),
        _action(
            "pointer_lock",
            browser_pointer_lock,
            "game",
            "Acquire, release, or read pointer lock for first-person games.",
        ),
        _action("render", browser_render_control, "game", "Set the animation gate: normal, throttled, or step."),
        _action("step", browser_render_step, "game", "Release an explicit number of animation frames."),
        _action(
            "release_inputs", browser_release_inputs, "game", "Release every held key and pointer button."
        ),
        _action("submit", browser_submit_form, "page", "Submit a form."),
        _action(
            "inject_script", browser_inject_script, "page", "Run code before each document's scripts."
        ),
        _action("cookies", browser_cookies, "page", "Read, write, or clear cookies with their flags."),
        _action(
            "local_storage", browser_local_storage, "page", "Read or write local/session storage."
        ),
        _action("macro", browser_macro, "macro", "Record a task once; replay it by name."),
        _action("captcha", browser_captcha, "page", "Detect a captcha and wait it out or solve it."),
        _action(
            "set_extra_headers",
            browser_set_extra_headers,
            "page",
            "Send extra HTTP headers with every request; empty clears.",
        ),
        _action("stealth", browser_stealth, "page", "Hide automation tells before page scripts run."),
        _action(
            "replay_request",
            browser_replay_request,
            "page",
            "Re-send a captured or explicit request from the page context.",
        ),
        _action(
            "close", browser_close, "session", "Close one session; a claimed current-Chrome tab stays open."
        ),
        _action("close_all", browser_close_all, "session", "Close every session owned by this server."),
    )
}


def _argument_model(tool_name: str) -> Any:
    """Return the pydantic model FastMCP generated for one wrapper function."""
    return legacy_mcp._tool_manager._tools[tool_name].fn_metadata.arg_model


def _parameter_names(tool_name: str) -> tuple[list[str], list[str]]:
    """Split a wrapper's parameters into required and optional names."""
    fields = _argument_model(tool_name).model_fields
    required = [name for name, field in fields.items() if field.is_required()]
    optional = [name for name, field in fields.items() if not field.is_required()]
    return required, optional


_ACTION_KEY_ALIASES = ("type", "name", "tool", "command", "op", "operation", "method")


def _unsupported_action_error(action_name: str, arguments: dict[str, Any]) -> str:
    """Explain a bad action well enough that the caller fixes it on the next try.

    A weaker model that writes ``{"type": "open"}`` will otherwise repeat the
    same call forever, because "unsupported action" does not say what to change.
    """
    if not action_name:
        misplaced = [key for key in _ACTION_KEY_ALIASES if key in arguments]
        if misplaced:
            key = misplaced[0]
            return (
                f"Every action object needs an \"action\" key; this one used "
                f"\"{key}\": {arguments[key]!r}. Rename it to \"action\". "
                f"Available actions: {sorted(_ACTIONS)}."
            )
        return (
            "Every action object needs an \"action\" key, for example "
            '{"action": "open", "url": "https://example.com"}. '
            f"Available actions: {sorted(_ACTIONS)}."
        )
    close = [name for name in _ACTIONS if name.startswith(action_name[:3])]
    suggestion = f" Did you mean {close}?" if close else ""
    return (
        f"Unsupported action: {action_name}.{suggestion} "
        f"Available actions: {sorted(_ACTIONS)}."
    )


def _validate_arguments(tool_name: str, label: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce caller arguments against the published schema.

    ``web_action`` and ``web_info`` dispatch to plain functions, so without this
    the advertised JSON Schema and the accepted input would drift apart and a
    typo would surface as an internal ``TypeError``.
    """
    model = _argument_model(tool_name)
    allowed = list(model.model_fields)
    unknown = [key for key in arguments if key not in allowed]
    if unknown:
        raise ValueError(
            f"{label}: unknown parameter(s) {sorted(unknown)}. "
            f"Allowed: {allowed}. "
            "Call web_info(topic='action_schema', params={'action': '<action or topic>'}) "
            "for the full schema."
        )
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        required, optional = _parameter_names(tool_name)
        raise ValueError(
            f"{label}: {problems}. Required: {required}. Optional: {optional}."
        ) from None
    return validated.model_dump(exclude_unset=True)


_AUTOMATION_SKILL = {
    "name": "web-search-neo-browser",
    "goal": "Drive one named browser session with an inspect -> act -> verify loop.",
    "schema": {
        "rule": "Before guessing any optional parameter, call web_info(topic='action_schema', params={'action': name}).",
        "timeouts": "timeout_ms does not exist. The exact schema may name timeout_seconds for a wait or wait_seconds for post-action settling; unknown keys are refused.",
    },
    "loop": [
        {
            "step": "inspect",
            "calls": ["page_outline or page_elements", "page_text when content matters"],
            "rule": "Read the current DOM immediately before choosing a selector or final value.",
        },
        {
            "step": "act",
            "calls": ["fill/click/scroll/submit through web_action"],
            "rule": "Reuse session_id; after navigation or rerender discard old selectors and refs.",
        },
        {
            "step": "verify",
            "calls": ["page_text/page_elements, screenshot when pixels matter"],
            "rule": "Tool success means the event ran, not that the user's outcome happened.",
        },
    ],
    "current_chrome": {
        "setup": "Call setup_current_chrome if the companion is unavailable; show manual_steps verbatim.",
        "existing_tab": "browser_tabs -> attach_tab(tab_id, session_id) claims without navigating.",
        "new_tab": "open defaults to profile_mode=current and creates a background AI-group tab.",
        "session_rule": "Reusing session_id navigates that controlled page; use a different session_id when a second reference page must stay open.",
        "action_locators": "In current Chrome every action locator is plain CSS. Never send ref: or >>> to click, fill, wait, upload, submit.form_selector, or input; they are observation-only there.",
        "ownership": "close removes an agent-created tab but only detaches a claimed user tab.",
    },
    "focus": {
        "default": "Open, attach, navigate, input, and screenshot stay background; new temporary/persistent browsers default headless.",
        "opt_in": "Only web_action show(session_id) may request foreground focus.",
        "window_state": "show activates the tab or calls Page.bringToFront; it never minimizes, maximizes, restores, or resizes.",
    },
    "elements": {
        "scope": "page_elements counts the whole existing DOM, not only the viewport, including open shadow roots and same-origin frames.",
        "pagination": "Use limit plus offset per category; follow range.<category>.next_offset until null.",
        "filtering": "page_elements has no selector filter: get its lists and filter the returned links/fields/buttons yourself.",
        "duplicates": "When text repeats, match the exact links.href, value, or stable attribute from a fresh read; never choose by list index or a brittle nth-child path alone.",
        "refs": "Temporary/persistent/attach actions may use fresh refs and piercing paths where their schema says so. Current-Chrome actions may not. Every ref goes stale after its DOM epoch/rerender, so reread.",
        "dynamic": "Lazy/infinite/virtualized items do not exist yet: scroll, wait, then reread from offset=0 because the DOM may have changed.",
        "safety_cap": "collector_truncated.<category>=true means the 20000-item collector cap hid a tail; found is then not the true total.",
    },
    "scroll": {
        "direction": "Positive delta_y moves down; negative delta_y moves up.",
        "point": "Omit x/y for viewport centre, or provide both to choose a nested scroll container.",
        "after": "Wait, then reread page_elements from offset=0 when scrolling may have changed the DOM.",
    },
    "screenshots": {
        "viewport": "Default; omit width/height to preserve actual size. Explicit size is Selenium-only and refused in current Chrome.",
        "full_page": "mode=full_page (or full_page=true); errors above 3840x10000 instead of returning a partial image.",
        "region": "mode=region requires page-CSS x/y/width/height and never resizes the browser.",
        "semantics": "Use DOM/text for labels and values. A background current-Chrome screenshot can be slow or unavailable when Chrome is not painting; that is not page failure.",
    },
    "visual_click": {
        "call": "Take a fresh viewport screenshot, then web_action pointer with pointer_action='click' and x/y in viewport CSS pixels.",
        "example": {"action": "pointer", "pointer_action": "click", "x": 640, "y": 360},
        "mapping": "Only viewport maps directly. If PNG size differs from reported viewport_width/height, scale each axis. Full-page/region can include offscreen pixels: scroll into view and recapture first.",
        "stale_guard": "After scroll, zoom, resize, navigation, animation, or rerender, recapture before clicking. Verify from fresh DOM/text.",
    },
    "forms": {
        "prepare": "Inspect fields/options, fill, then reread the form and its selected/current values.",
        "resume_rule": "Never infer a selected resume/account/option from a URL parameter, remembered default, or prior page; verify the visible selected value in the live form.",
        "choice_widget": "Open it, reread its options, match exact visible text/value, click the visible option row rather than a hidden radio/input, then reread the collapsed control after rerender.",
        "question_rule": "A heading such as 'answer questions' is boilerplate, not proof that questions exist. Treat only live enabled form controls as questions; after a success message, stop.",
        "final_submit_guard": [
            "Keep submit_attempted=false. From a fresh DOM confirm the exact target and every critical selected value; ambiguity means do not submit.",
            "Set submit_attempted=true as the terminal submit is clicked exactly once.",
            "After any result or timeout, never click it again: inspect URL, text, elements, console/network. Stop on terminal success; only a clearly separate questionnaire may have its own later final submit.",
        ],
    },
}


_INFO_TOPICS = {
    "capabilities": "This contract: topics, actions, recipes, and pitfalls.",
    "skill": "Built-in browser automation playbook: inspect, act, verify, and guard final submits.",
    "action_schema": "Full JSON Schema for one action or topic; pass params.action.",
    "page_outline": "Roles, names, states, refs, and boxes - start looking here.",
    "page_text": "Readable text of the rendered page; params.mode=main|full.",
    "find": "Find an element by meaning: params.query='submit application'.",
    "page_elements": "Links, forms, fields, buttons by selector: CSS, '#host >>> #leaf' in a shadow root or frame, or '' when none is unique.",
    "console": "console.log/warn/error and uncaught errors; params.levels, params.contains.",
    "network": "HTTP requests with status, type, ms, size; params.only_errors=true.",
    "network_body": "One response body; params.request_id is the id from a network read with output='json'.",
    "execute_js": "Run a JavaScript snippet in a session's page and read its return value.",
    "screenshot": "PNG viewport, full-page, or exact page-region image.",
    "game_probe": "Canvas/WebGL/iframe surfaces, FPS, focus, console, held input.",
    "browser_status": "Chrome availability and named session state.",
    "browser_tabs": "Tabs open in the user's Chrome, with ids and groups.",
    "search_status": "Search providers, live availability, latency, cooldowns.",
}

# Repeated verbatim on every hot-path action, because a model reads one schema.
_HOT_PATH_SPEED = (
    "wait_seconds=0 (the default) and include_summary=false skip the post-action "
    "page read; both matter when you drive a game frame by frame."
)

# step is the one hot-path action without wait_seconds, and the argument check
# refuses an unknown key - so the shared note above advertised a hard error.
_STEP_SPEED = (
    "include_summary=false skips the post-action page read; step takes no "
    "wait_seconds and refuses one."
)

# frame_selector always names exactly one frame - ambiguous CSS is refused with
# the count everywhere. Only the accepted form differs: whatever aims by
# coordinate needs the frame's box in the top document, so it takes CSS alone.
_FRAME_ANY = "Frame to work inside: CSS, a ref: handle, or '#host >>> #inner'; ambiguous CSS is refused with the count."
_FRAME_CSS = "Frame to work inside: plain CSS only - a ref or a '>>>' path is refused before any event is sent; ambiguous CSS is refused with the count."

# Keyed by action name or info topic name; both are described from here.
_ACTION_NOTES = {
    "fill": {
        "results": "filled took your value, field_values answers for every selector you sent including the failures, errors maps selector to the driver's own message, success=false if errors is non-empty.",
        "field_values": "null means nothing could be read back - the selector matched nothing, or the control is gone. A refused control reports what it still holds, so you can see the write did not land.",
        "checkbox": "1|yes|y|on|check|checked or 0|no|n|off|uncheck|unchecked|''; anything else is refused, and field_values reports a JSON boolean.",
        "multi_select": "A <select multiple> reads back as a list and only it takes a list of values; a scalar replaces its whole selection rather than adding to it.",
        "sanitisation": "The browser's own tidying is accepted - trimmed whitespace on email/url, CRLF, a handler's case folding - while maxlength truncation and a rewritten value still fail.",
        "typed_controls": "date/time/datetime-local/month/week/range/color are set, not typed: an unparseable value is refused without touching the control and the error names the format.",
        "contenteditable": "TipTap/ProseMirror/Slate/Quill editors are written like a real edit: the whole content is selected and the text inserted through the browser's input channel, so the editor's own model updates and its change handler fires. The read-back is the editor's textContent.",
        "files": "A file input is refused in fields; pass files={selector: path}, which replaces the input's selection rather than adding to it.",
        "blur": "Every control written is blurred, which is how the last field fires its change event - so focus ends on the body and a following press_keys needs target_selector to reach a field.",
        "frame_selector": _FRAME_ANY,
    },
    "upload": {
        "replaces": "The input is cleared first: this sets its selection to exactly file_paths. Two files means one call with two paths; a second call discards the first file.",
        "files_uploaded": "{selector: [names]}, read back off the input - the same shape fill returns.",
        "frame_selector": _FRAME_ANY,
    },
    "submit": {
        "validation": "validation_passed=false means native validation blocked it and nothing was sent; validation_errors names the fields by id/name, never match on the message.",
        "proof": "submit_triggered is submit_event_fired or navigation_observed; submit_default_prevented marks a handler that cancelled the navigation on purpose. Neither says the server accepted it.",
        "evidence": "submit_evidence is a sentence naming what the verdict rests on; new_tab_opened=true means a target='_blank' result is in a tab this session does not own, so the url and title here are still this page's.",
        "submit_selector": "Plain CSS only, unlike form_selector.",
        "frame_selector": _FRAME_ANY,
    },
    "click": {
        "choice": "Prefer a fresh exact CSS/href-backed target over repeated visible text or document order. In current Chrome selector must be plain CSS, never ref: or >>>.",
        "trusted": "trusted=true sends a real trusted mouse sequence at the element's centre (scrolled into view first), so pages that require isTrusted events or read pointer position behave as if a user clicked. Use it when a synthetic click is ignored. It lands on whatever is at that point, like a human pointer.",
        "no_box": "trusted=true needs a visible box; an element with zero size refuses with a clear error instead of falling back silently.",
        "frame_selector": _FRAME_ANY,
    },
    "run_script": {
        "scope": "Runs in the top document of the session's current tab; there is no frame_selector - address a frame from inside the script if needed.",
        "args": "args arrive as arguments[0..n]; only JSON-serialisable values can cross into the page.",
        "result": "value is the JSON-serialisable return value; a promise is awaited when await_promise=true (Chrome bridge driver). Long strings are clipped at 200k characters and reported as {clipped, length, head}.",
        "safety": "This is raw page-side JavaScript: it can navigate, mutate, or delete state. Prefer fill/click/pointer for input-shaped work and reserve scripts for state only the page holds (localStorage, virtualised rows, framework stores).",
    },
    "wait": {
        "state": "present|visible|clickable; timeout_seconds is clamped to 30 and the result reports the effective value, so a requested 120 comes back as 30.",
        "frame_selector": _FRAME_ANY,
    },
    "find": {
        "scores": "match_score is how well the query matched that element alone; score adds ranking context. low_confidence is derived from match_score only.",
        "role": "A filter, not a nudge: another role is dropped. Under low_confidence the guesses come from the unfiltered set, so a wrong role can reappear there.",
        "ambiguous": "The top two matched and ranked equally, so document order alone chose; say which you mean instead of taking matches[0].",
        "limits": "visible_only=true by default; limit is clamped to 25.",
    },
    "page_elements": {
        "selector": "CSS in the top document, '#host >>> #leaf' inside an open shadow root or same-origin frame, and '' when nothing addresses the element uniquely.",
        "scope": "Always the whole page: this topic takes no frame_selector, and it is the only read topic reporting challenge_detected/captcha_widgets.",
        "duplicates": "For repeated labels, compare each returned link href or stable value/attribute. Never choose by array index or nth-child alone.",
        "captcha_scan_incomplete": "true means the captcha walk stopped early, so an empty captcha_widgets is not proof there is none. Every page summary carries this key.",
        "contenteditable": "Only [contenteditable=\"true\"] is listed; a bare contenteditable attribute is a field to page_outline and invisible here.",
        "pagination": "The whole existing DOM is counted before each category is sliced. Use offset plus limit, then follow range.<category>.next_offset until null; reread after scrolling a lazy/infinite page.",
        "limits": "limit is clamped to 1000 per top-level category and offset to 0-20000. collector_truncated.<category>=true means the 20000-item safety cap was hit and found is only the collected prefix. include_forms=false also omits fields.",
    },
    "page_text": {
        "fallback": "mode='main' on a page that is one big form would be empty, so it re-reads the whole body and says so with fallback_used=true and mode_used='full'.",
    },
    "network": {
        "id": "The default output='text' carries no ids. Pass output='json' and hand that row's id to network_body as request_id.",
    },
    "execute_js": {
        "scope": "Top document of the session's current tab only; reach into a frame from inside the script when you must.",
        "result": "value is the JSON-serialisable return value, promise-awaited on the Chrome bridge driver; strings over 200k characters come back as {clipped, length, head}.",
        "prefer_actions": "Use fill/click/pointer for anything a user gesture should do; a script cannot simulate a trusted interaction.",
    },
    "game_probe": {
        "frame_selector": _FRAME_CSS,
        "why_strict": "It sends nothing, but the canvas rects it reports are aimed at with this same string, so it is as strict as the input actions.",
    },
    "open": {
        "profile_mode": {
            "current": "the user's signed-in Chrome through the companion extension (default)",
            "auto": "current, falling back to a headless temporary profile",
            "temporary": "clean disposable profile",
            "persistent": "durable server-owned profile, keeps logins",
            "attach": "a Chrome you started with a DevTools port",
        },
        "headless": "temporary/persistent default headless; headless=false explicitly opens a visible window. attach preserves the launcher's window mode when omitted. headless=true is refused with current and makes auto resolve straight to temporary.",
        "claimed_tab": "open on a session claimed by attach_tab does not navigate the user's tab: it takes a new one and reports the released id as left_claimed_tab.",
    },
    "show": {
        "only_foreground": "This is the only action allowed to request browser or OS foreground focus; never call it unless foreground was explicitly requested.",
        "window_state": "Current Chrome activates its tab/window; Selenium and attach use Page.bringToFront. No minimize, maximize, restore, resize, or other window-state request is sent.",
        "result": "focus_requested=true confirms the explicit request was sent; warning names the user interruption risk.",
    },
    "attach_tab": {
        "ownership": "Refused, naming the holder, when another agent is already driving that tab. Pick another tab or open your own; do not retry.",
        "capture": "Console and network are recorded from the claim onwards; whatever the tab did before it was claimed is unrecoverable.",
    },
    "close": {
        "tab": "Closes a tab the agent opened, reported as tab_closed; an attach_tab tab is only detached and stays open.",
        "browser_gone": "browser_gone=true with a note means the Chrome it was opened in is gone: nothing was sent, because the tab id now names someone else's tab. Open again, do not retry close.",
    },
    "close_all": {
        "browser_gone": "A list of session ids left alone because their Chrome is gone; closed_all stays true, since nothing of ours was left to leak.",
    },
    "browser_status": {
        "browser_gone": "session_open=false with browser_gone=true means the session was dropped because its Chrome restarted; follow the 'next' field and open the page again.",
    },
    "input": {
        "key_action": {"key": "W|SPACE|ARROW_LEFT|F5|NUMPAD1|...", "action": "tap|hold|release"},
        "pointer_action": {
            "action": "click|double_click|hover|move|drag|press|release|wheel",
            "coordinates": "x/y absolute, deltas when coordinate_mode=delta or relative",
            "wheel": "pass delta_x/delta_y; x/y is where the wheel is scrolled",
        },
        "atomicity": "Every change lands before the single released frame; taps stay down for it.",
        "limits": "At most 16 key and 16 pointer entries.",
        "refusals": "Raised before anything is sent: tapping a key this session already holds (release it first), and a point that maps outside the window or onto another element, which is named.",
        "held_keys": "After a failed batch this over-reports on purpose - any event may have landed - so call release_inputs rather than trusting the list.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
    "press_keys": {
        "key_action": "tap|hold|release",
        "note": "The dispatcher key is 'action'; the keyboard verb is 'key_action'.",
        "hold_frames": "With key_action='tap' in render=step, the key stays down for N released frames - so one call releases hold_frames per repeat, not one. Read frames_advanced.",
        "limits": "1-8 keys, repeat 1-50, hold_frames 1-30.",
        "refusals": "Tapping a key this session already holds is refused before anything is sent; release it first, or drop the tap.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
    "render": {
        "modes": ["normal", "throttled", "step"],
        "determinism": (
            "step freezes performance.now()/Date.now() and queues timers, so each "
            "released frame is a fixed frame_delta_ms. Without it a game measures "
            "your thinking time as its frame delta."
        ),
        "monotonic": "Stepping carries page time ahead of wall time and returning to normal keeps the gap, so the page clock never goes backwards - and never matches yours again.",
        "frame_selector": _FRAME_ANY,
    },
    "pointer": {
        "pointer_action": "click|double_click|hover|move|drag|press|release|wheel",
        "note": "The dispatcher key is 'action'; the pointer verb is 'pointer_action'.",
        "visual_click": "For image-guided clicking, take a fresh viewport screenshot and send pointer_action='click' with viewport CSS x/y. If PNG dimensions differ from viewport_width/height, scale both axes. Full-page/region coordinates do not directly map to the viewport.",
        "stale_image": "After scroll, zoom, resize, navigation, animation, or rerender, recapture before using image coordinates; then verify with fresh DOM/text.",
        "refusals": "A point that maps outside the window, or onto an element covering the frame there, is refused with the blocker named - never clamped or dropped.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
    "scroll": {
        "direction": "positive delta_y scrolls down; negative delta_y scrolls up",
        "point": "omit x/y for viewport centre; provide both to scroll the container painted under that point",
        "result": "before/after are selected document window metrics; a nested container can move while those page metrics stay unchanged",
        "lazy_pages": "page_elements already sees offscreen controls in the existing DOM. Scroll only to materialise lazy/infinite content, then read page_elements again.",
        "frame_selector": _FRAME_CSS,
    },
    "screenshot": {
        "modes": "viewport (default), full_page, region; full_page=true remains an alias for mode='full_page'",
        "viewport": "omit width/height to preserve the actual viewport. An explicit pair resizes Selenium sessions exactly and is refused in current Chrome.",
        "region": "requires x/y/width/height in page CSS pixels, captures without resizing, and works in current Chrome and Selenium",
        "full_page": "captures the whole current layout up to 3840x10000; an oversize page errors instead of returning an unlabelled partial image",
        "background": "A current-Chrome screenshot can wait or fail while its window is obscured because Chrome is not painting pixels. DOM/actions still work; do not use pixels to prove labels or selected values.",
    },
    "pointer_lock": {
        "operation": "acquire|release|status",
        "note": "After acquire, move with coordinate_mode='relative'.",
        "movement": "Each move reports exactly the delta you sent; nothing recentres.",
        "frame_selector": _FRAME_CSS,
    },
    "touch": {
        "touch_action": "tap|press|move|release|swipe|cancel",
        "points": [{"x": 0, "y": 0, "id": 0, "end_x": 0, "end_y": 0}],
        "partial_release": (
            "release with points=[{id}] lifts only those fingers and leaves the "
            "rest down; release with no points lifts all of them."
        ),
        "refusals": "Pressing an id that is already down is refused - Chrome ignores it - so move that finger or release it first; a point landing outside the window or on another element is refused and the blocker named.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
    "step": {
        "frames": "1-120. step has no frame_selector - it reuses the one render stored - and fails unless render mode=step is active.",
        "speed": _STEP_SPEED,
    },
    "setup_current_chrome": {
        "opens_no_page": (
            "It publishes the shared secret and reads state. Its one browser effect "
            "is reloading a stale companion; self_update reports done, unsupported "
            "or timeout. Show manual_steps to the user verbatim when they are "
            "present; they contain the absolute folder to pick."
        ),
        "why": (
            "No program can add an unpacked extension to a Chrome that is already "
            "open, so the first install belongs to the user. Updates after that do "
            "not: the companion re-reads its own folder when asked."
        ),
        "wait_seconds": "Raise it right after the user pressed Load unpacked.",
    },
}

_RECIPES = {
    "lookup": ["search {query}", "open {url}", "page_elements", "read the answer"],
    "form": [
        "open {url}",
        "fresh page_elements: exact href/target plus live controls",
        "fill/select, then reread every consequential value",
        "set submit_attempted=true and terminal submit exactly once",
        "verify URL/text/elements; after timeout never submit again",
    ],
    "visual_click": [
        "viewport screenshot plus reported viewport_width/height",
        "scale image point to viewport CSS x/y if dimensions differ",
        "pointer {pointer_action: click, x, y}",
        "verify with fresh DOM/text; recapture after any layout change",
    ],
    "existing_tab": ["browser_tabs", "attach_tab {tab_id}", "page_elements", "act"],
    "lazy_page": [
        "page_elements (already includes offscreen existing DOM)",
        "scroll {delta_y: positive}",
        "page_elements again; paginate offset until next_offset=null",
    ],
    "game": [
        "open {url}",
        "game_probe (read frame_selector and canvas rect)",
        "render mode=step",
        "input {key_actions/pointer_actions} or step {frames}",
        "screenshot or game_probe between batches",
        "release_inputs, then render mode=normal",
    ],
}

_PITFALLS = [
    "web_action success=true is not task success: check failure_count and every results[i].success.",
    "Never guess optional names: call action_schema. timeout_ms does not exist; the exact action may use timeout_seconds or wait_seconds.",
    "page_elements takes no selector filter: read its category lists and filter the returned objects yourself.",
    "Selectors and screenshots die when the page changes; reread or recapture after navigation, rerender, scroll, zoom, resize, or animation.",
    "In current Chrome every action locator is plain CSS: never send ref: from page_outline or >>> to click/fill/wait/upload/submit/input. Other modes accept them only where action_schema says so; refs expire after rerender/navigation.",
    "Repeated text is not identity: compare the exact href/value/stable attribute from a fresh page_elements read, never array index or nth-child alone.",
    "challenge_detected means a CAPTCHA is in the way: use captcha, never hammer clicks. captcha_widgets lists ones merely present; ignore those.",
    "find low_confidence=true means it is guessing: re-query with other words or a role, do not click matches[0].",
    "profile_mode=current drives the user's real Chrome; close closes a tab the agent opened and leaves an attach_tab tab open.",
    "Automation stays background-only by default and never changes window state. show is the sole foreground opt-in; call it only when the user explicitly asks to see the session.",
    "In render=step nothing moves until input or step runs, so a screenshot taken first shows the old frame.",
    "Always release_inputs after hold, and return render to normal before you finish.",
    "Pointer coordinates are viewport-local; inside an iframe pass frame_selector and frame-local x/y.",
    "For image-guided clicks use a fresh viewport screenshot and scale image pixels to reported viewport CSS size. Full-page/region pixels are not direct pointer coordinates.",
    "For a consequential submit verify all live choices, click once, and never retry after a timeout; inspect page state first because the first click may have succeeded.",
    "scroll delta_y is positive to move down and negative to move up; reread page_elements after scrolling a lazy/infinite page.",
    "Plain http:// to public hosts is refused; use https. Loopback and private addresses stay allowed.",
]


_EXAMPLES = {
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
    "input": {
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
                ],
                "pointer_actions": [
                    {"action": "hover", "x": 640, "y": 360},
                    {"action": "wheel", "x": 640, "y": 360, "delta_y": -240},
                    {"action": "move", "x": 20, "y": -5, "coordinate_mode": "delta"},
                ],
            },
        ]
    },
    "pointer_lock": {
        "actions": [
            {"action": "pointer_lock", "operation": "acquire", "session_id": "fps"},
            {
                "action": "input",
                "session_id": "fps",
                "pointer_actions": [
                    {"action": "move", "x": 400, "y": 0, "coordinate_mode": "relative"}
                ],
            },
        ]
    },
    "script": {
        "actions": [
            {
                "action": "run_script",
                "session_id": "s",
                "script": "return JSON.parse(localStorage.getItem('state'))",
            }
        ]
    },
}


# Requirements no Python signature can carry: these parameters have a default,
# so the schema calls them optional, yet the handler refuses the call without
# them. Left unsaid, the most-used action ("input") looks like it takes nothing
# and a small model learns the real contract only from a runtime error.
_RUNTIME_REQUIREMENTS = {
    "input": "at least one of key_actions=[{key,action}] or pointer_actions=[{action,x,y}]",
    "touch": (
        "points=[{x,y}], swipe adds end_x/end_y; only release and cancel need "
        "none, and release may name ids instead to lift just those fingers"
    ),
}


def _action_index() -> dict[str, dict[str, Any]]:
    """Summaries plus required parameter names, which is what a caller must guess.

    Optional names stay out on purpose: they would double the contract, and the
    caller has one reliable place for them, topic=action_schema.
    """
    index: dict[str, dict[str, Any]] = {}
    for name, spec in _ACTIONS.items():
        required, _ = _parameter_names(spec.tool_name)
        entry: dict[str, Any] = {"summary": spec.summary}
        if required:
            entry["required"] = required
        if name in _RUNTIME_REQUIREMENTS:
            entry["also_required"] = _RUNTIME_REQUIREMENTS[name]
        index[name] = entry
    return index


def _action_documentation() -> dict[str, dict[str, Any]]:
    """Describe every action from the single registry, never a parallel list."""
    document = {}
    for name, spec in _ACTIONS.items():
        required, optional = _parameter_names(spec.tool_name)
        entry: dict[str, Any] = {"summary": spec.summary}
        if required:
            entry["required"] = required
        if name in _RUNTIME_REQUIREMENTS:
            entry["also_required"] = _RUNTIME_REQUIREMENTS[name]
        if optional:
            entry["optional"] = optional
        entry.update(_ACTION_NOTES.get(name, {}))
        document[name] = entry
    return document


def _topic_schema(topic: str) -> dict[str, Any]:
    """Publish the parameters of one web_info topic.

    A topic's params are validated exactly as strictly as an action's, so an
    unlisted key is refused - and until now nothing published the list, which
    left ``output``, ``limit``, ``since_seq`` and the rest mandatory to know and
    written down nowhere. Every topic is backed by the same kind of wrapper
    function an action is, so the same generated schema answers for both.
    """
    handler = _TOPIC_HANDLERS.get(topic)
    if handler is None:
        raise ValueError(
            f"Unknown action schema: {topic}. Available actions: {sorted(_ACTIONS)}. "
            f"Available info topics: {sorted(_TOPIC_HANDLERS)}"
        )
    original = legacy_mcp._tool_manager._tools[handler.__name__].parameters
    response: dict[str, Any] = {
        "topic": topic,
        "summary": _INFO_TOPICS[topic],
        "params_schema": {**original, "title": f"{topic}Params"},
        "call": {"tool": "web_info", "arguments": {"topic": topic, "params": {}}},
    }
    notes = _ACTION_NOTES.get(topic)
    if notes:
        response["notes"] = notes
    return response


def _capabilities(action_name: str | None = None) -> dict[str, Any]:
    """Return the whole agent-facing contract, one action's schema, or one topic's."""
    if action_name is not None:
        selected = action_name.strip().lower()
        spec = _ACTIONS.get(selected)
        if spec is None:
            return _topic_schema(selected)
        original = legacy_mcp._tool_manager._tools[spec.tool_name].parameters
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
        response = {
            "action": selected,
            "input_schema": input_schema,
            "notes": _action_documentation()[selected],
        }
        example = _EXAMPLES.get(selected)
        if example is not None:
            response["example"] = example
        return response

    groups: dict[str, list[str]] = {}
    for name, spec in _ACTIONS.items():
        groups.setdefault(spec.group, []).append(name)
    return {
        "server": "Web Search Neo",
        "version": __version__,
        "public_tools": ["web_info", "web_action"],
        "how": (
            "web_info(topic=...) reads state; web_action(actions=[...]) performs 1-32 "
            "ordered actions. One session_id is one page - reuse it. This document "
            "is the whole contract; no external skill is required."
        ),
        "info_topics": _INFO_TOPICS,
        "actions": _action_index(),
        "action_groups": groups,
        "recipes": _RECIPES,
        "pitfalls": _PITFALLS,
        "examples": _EXAMPLES,
        "limits": {
            "ordered_actions_per_call": 32,
            "parallel_browser_sessions": browser_tools.MAX_SESSIONS,
            "input_actions_per_batch": "16 key + 16 pointer",
            "automatic_captcha": False,
            "captcha_modes": ["fallback", "manual"],
        },
        "discovery": {
            "next_call": "web_info",
            "topic": "action_schema",
            "params_example": {"action": "input"},
            "note": "params.action names an action or an info topic; a topic's parameters are published nowhere else, and any key it does not list is refused.",
            "parameters": (
                "actions[name].required lists parameters you must always send; "
                "also_required is a condition a list cannot express and is just as "
                "mandatory. Optional names, types, and defaults exist only in "
                "action_schema."
            ),
        },
    }


_TOPIC_HANDLERS = {
    "skill": browser_automation_skill,
    "search_status": get_search_engines_status,
    "browser_status": browser_get_status,
    "browser_tabs": browser_list_tabs,
    "page_outline": browser_page_outline,
    "page_text": browser_page_text,
    "find": browser_find,
    "page_elements": browser_get_page_elements,
    "console": browser_console,
    "network": browser_network,
    "network_body": browser_network_body,
    "execute_js": browser_execute_js,
    "game_probe": browser_game_probe,
    "screenshot": browser_screenshot,
}


def _stamp_now(payload: Any) -> Any:
    """Attach the current local time to a web_info result (dicts only).

    Every web_info answer carries the current local date/time and UTC-offset
    region string under the top-level ``now`` key, so a model never needs a
    separate time call. Non-dict payloads (e.g. screenshot images) pass through.
    """
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["now"] = msp_date_time.get_current_time_and_region()
    return payload


@mcp.tool()
async def web_info(
    topic: Literal[
        "capabilities",
        "action_schema",
        "skill",
        "search_status",
        "browser_status",
        "browser_tabs",
        "page_outline",
        "page_text",
        "find",
        "page_elements",
        "console",
        "network",
        "network_body",
        "execute_js",
        "game_probe",
        "screenshot",
    ] = "capabilities",
    params: dict[str, Any] | None = None,
) -> Any:
    """Read the contract, the page, the console, the network, or search/browser state.

    Called with no arguments it returns the whole contract, including recipes and
    common mistakes, so no external skill file is needed. Every result (dict
    payloads) also carries the current local date/time and UTC-offset region
    under the top-level ``now`` key.
    """
    arguments = dict(params or {})
    if topic == "capabilities":
        if arguments:
            raise ValueError("capabilities does not accept params")
        return _stamp_now(_capabilities())
    if topic == "action_schema":
        # params.topic is accepted as an alias: what is being described may be an
        # info topic, and naming one under the key "action" reads as a mistake.
        named = str(arguments.pop("action", "") or "")
        alias = str(arguments.pop("topic", "") or "")
        if arguments:
            raise ValueError("action_schema accepts only params.action or params.topic")
        if not named and not alias:
            raise ValueError(
                "action_schema requires params.action: an action name for web_action, "
                f"or an info topic name. Topics: {sorted(_TOPIC_HANDLERS)}"
            )
        return _stamp_now(_capabilities(named or alias))
    handler = _TOPIC_HANDLERS.get(topic)
    if handler is None:
        raise ValueError(
            f"Unsupported info topic: {topic}. Available: {sorted(_INFO_TOPICS)}"
        )
    validated = _validate_arguments(handler.__name__, f"topic '{topic}'", arguments)
    return _stamp_now(await handler(**validated))


@mcp.tool()
async def web_action(
    actions: list[dict[str, Any]],
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Execute 1-32 ordered search, fetch, browser, form, input, render, or close actions."""
    if not actions or len(actions) > 32:
        raise ValueError("Provide 1-32 actions")
    return await _execute_actions(actions, continue_on_error)


async def _execute_actions(
    actions: list[dict[str, Any]],
    continue_on_error: bool = False,
    record: bool = True,
) -> dict[str, Any]:
    """Run an ordered action list, validating each against its published schema.

    ``web_action`` and a macro replay share this loop rather than each having
    their own: a macro that ran its steps down a second, laxer path would drift
    from the calls it was recorded from, which is the one thing a saved click
    path cannot afford. ``record=False`` keeps a replay out of the recorder, so
    running a macro while recording does not inline its steps.

    While a recording is open, the batches being recorded run one at a time: two
    sent at once would otherwise interleave into a script whose steps never ran
    in that order. Nothing is serialised when no recording is open.
    """
    if record and _RECORDING["active"]:
        async with _RECORDING_LOCK:
            return await _dispatch_actions(actions, continue_on_error, record)
    return await _dispatch_actions(actions, continue_on_error, record)


async def _dispatch_actions(
    actions: list[dict[str, Any]],
    continue_on_error: bool,
    record: bool,
) -> dict[str, Any]:
    """Validate and run each action of one batch in order, reporting every one."""
    results: list[dict[str, Any]] = []
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, dict):
            raise ValueError(f"Action {index} must be an object")
        arguments = dict(raw_action)
        action_name = str(arguments.pop("action", "")).strip().lower()
        spec = _ACTIONS.get(action_name)
        if spec is None:
            error = {
                "index": index,
                "action": action_name or None,
                "success": False,
                "error": _unsupported_action_error(action_name, arguments),
                "example": {
                    "actions": [{"action": "open", "url": "https://example.com", "session_id": "s"}]
                },
            }
            results.append(error)
            if not continue_on_error:
                break
            continue
        try:
            validated = _validate_arguments(spec.tool_name, f"action '{action_name}'", arguments)
            data = await spec.handler(**validated)
            reported_failure = (
                isinstance(data, dict) and data.get("success") is False
            )
            # Record what actually dispatched, not what was typed: the step kept
            # is the validated one, so a macro replays the call the schema
            # accepted rather than a shorthand that happened to work today.
            if record and not reported_failure:
                _record_step(action_name, validated)
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


def _record_step(action_name: str, arguments: dict[str, Any]) -> None:
    """Append one dispatched action to the open recording, if there is one.

    A ``macro`` call is never captured. Recording one would build a script the
    saver then refuses - a macro cannot run a macro - leaving the recording
    unsaveable and the only way out the one that throws the work away. Managing
    macros while recording a task is a normal thing to do, so it stays silent.
    """
    if _RECORDING["active"] and action_name != "macro":
        _RECORDING["steps"].append({"action": action_name, **arguments})


def stop_bridge_daemon() -> int:
    """Ask a running daemon to exit; the answer to "why is my edit not live?"."""
    # Not the shared instance: stopping a daemon must never start one first.
    bridge = chrome_bridge.ChromeBridge(spawn=False)
    try:
        if bridge.stop_daemon("stopped from the command line"):
            print("The bridge daemon was asked to stop.")
            return 0
        print(f"No bridge daemon is listening on {bridge.host}:{bridge.port}.")
        return 0
    finally:
        bridge.shutdown()


def main() -> None:
    arguments = sys.argv[1:]
    if "--bridge" in arguments:
        if "--stop" in arguments:
            raise SystemExit(stop_bridge_daemon())
        # The daemon behind the companion port, run from the same file every MCP
        # config already points at. It speaks no MCP and touches no stdio: an
        # agent's browser calls reach it over the loopback bridge instead.
        raise SystemExit(bridge_daemon.run_forever(__version__))
    browser_tools.start_current_chrome_bridge()
    active_mcp = (
        legacy_mcp
        if os.getenv("WEB_SEARCH_NEO_LEGACY_TOOLS", "").strip() == "1"
        else mcp
    )
    active_mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
