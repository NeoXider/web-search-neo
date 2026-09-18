"""Web Search Neo: API-free search, fetch, and rendered browser MCP server."""

import asyncio
from dataclasses import dataclass
import functools
import json
import os
import sys
from typing import Any, Literal
from mcp.server.fastmcp import FastMCP, Image
from pydantic import ValidationError

from web_search_neo import bridge_daemon
from web_search_neo import browser_tools
from web_search_neo import chrome_bridge
from web_search_neo import macros
from web_search_neo import msp_date_time
from web_search_neo import msp_search
from web_search_neo import plugins
from web_search_neo.log_setup import configure_server_log
from web_search_neo.mcp_compat import ReportingFastMCP, registered_tool
from web_search_neo.web_client import clamp_timeout, request
from web_search_neo.fetch import api as fetch_api
from web_search_neo.fetch import content as fetch_content


__version__ = "1.16.3"

log = configure_server_log()  # per-user state dir; see log_setup.py


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
    url: str,
    max_chars: int = 50_000,
    timeout_seconds: float = 20.0,
    mode: str = "text",
    headers: dict[str, str] | None = None,
    save_to: str | None = None,
    overwrite: bool = False,
) -> str:
    return fetch_content._fetch_url_text(url, max_chars, timeout_seconds, mode, headers, save_to, overwrite, request_client=request)

@mcp.tool()
async def fetch_url_text(
    url: str,
    max_chars: int = 50_000,
    timeout_seconds: float = 20.0,
    mode: Literal["text", "html", "raw"] = "text",
    headers: dict[str, str] | None = None,
    save_to: str | None = None,
    overwrite: bool = False,
) -> str:
    """Download an HTTP(S) page without blocking parallel MCP tool calls.

    mode='raw'/'html' returns the raw source (JS bundles, markup); headers
    sends custom request headers; save_to writes the body to a file under
    WEB_SEARCH_NEO_DOWNLOAD_DIR (default ./downloads) and never replaces an
    existing file unless overwrite=true. timeout_seconds is capped at 120.
    """
    return await asyncio.to_thread(
        _fetch_url_text, url, max_chars, timeout_seconds, mode, headers, save_to, overwrite
    )


def _fetch_page_links(
    url: str,
    limit: int = 500,
    timeout_seconds: float = 20.0,
    headers: dict[str, str] | None = None,
) -> list[str]:
    return fetch_content._fetch_page_links(url, limit, timeout_seconds, headers, request_client=request)

@mcp.tool()
async def fetch_page_links(
    url: str,
    limit: int = 500,
    timeout_seconds: float = 20.0,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """Return de-duplicated absolute links without blocking parallel calls."""
    return await asyncio.to_thread(_fetch_page_links, url, limit, timeout_seconds, headers)


@mcp.tool()
async def fetch_urls_text(
    urls: list[str],
    max_chars_per_page: int = 20_000,
    timeout_seconds: float = 20.0,
    mode: Literal["text", "html", "raw"] = "text",
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch up to 16 pages concurrently and return text or an error per URL."""
    if not urls:
        raise ValueError("urls must not be empty")
    if len(urls) > 16:
        raise ValueError("At most 16 URLs can be fetched in one call")

    async def fetch_one(url: str) -> dict[str, Any]:
        try:
            text = await asyncio.to_thread(
                _fetch_url_text, url, max_chars_per_page, timeout_seconds, mode, headers, None
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
async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    body: str | None = None,
    body_json: Any | None = None,
    timeout_seconds: float = 20.0,
    max_chars: int = 20_000,
    save_to: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Send any HTTP request without a browser; 4xx/5xx return status and body, not errors.

    save_to is confined to WEB_SEARCH_NEO_DOWNLOAD_DIR (default ./downloads) and
    needs overwrite=true to replace a file; timeout_seconds is capped at 120.
    """
    return await asyncio.to_thread(
        fetch_api.http_request,
        url,
        method,
        headers,
        query,
        body,
        body_json,
        timeout_seconds,
        max_chars,
        save_to,
        overwrite,
    )


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
        clamp_timeout(timeout_seconds),
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

    manual_timeout = min(300.0, max(10.0, float(manual_timeout_seconds)))
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
        "auto", "current", "temporary", "isolated", "persistent", "attach"
    ] = "current",
    profile_id: str | None = None,
    debugger_address: str | None = None,
    current_tab_id: int | None = None,
    tab_group: str = chrome_bridge.DEFAULT_TAB_GROUP,
    agent_label: str | None = None,
    label_tab: bool = True,
    user_agent: str | None = None,
    timezone: str | None = None,
    locale: str | None = None,
    geolocation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open in the current Chrome's agent tab group by default; auto falls back to Selenium.

    profile_mode='isolated' opens a disposable separate browser profile;
    user_agent/timezone/locale/geolocation override per session
    (owned browsers only, refused on current/attach).
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.open_page,
            url,
            session_id=session_id,
            width=width,
            height=height,
            timeout_seconds=timeout_seconds,
            headless=headless,
            profile_mode=profile_mode,
            profile_id=profile_id,
            debugger_address=debugger_address,
            current_tab_id=current_tab_id,
            tab_group=tab_group,
            agent_label=agent_label,
            label_tab=label_tab,
            user_agent=user_agent,
            timezone=timezone,
            locale=locale,
            geolocation=geolocation,
        )
    )


@mcp.tool()
async def browser_context(
    session_id: str = "default",
    user_agent: str | None = None,
    timezone: str | None = None,
    locale: str | None = None,
    geolocation: dict[str, Any] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Change a live session's fingerprint overrides without reopening it.

    Owned browsers only (temporary/isolated/persistent); refused on
    current/attach with an explanation.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.apply_context_overrides,
            session_id=session_id,
            user_agent=user_agent,
            timezone=timezone,
            locale=locale,
            geolocation=geolocation,
            width=width,
            height=height,
        )
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
        "auto", "current", "temporary", "isolated", "persistent", "attach"
    ] = "current",
    tab_group: str = chrome_bridge.DEFAULT_TAB_GROUP,
    label_tab: bool = True,
) -> dict[str, Any]:
    """Open up to four pages, using the current Chrome's agent tab group by default."""
    cap = browser_tools.max_sessions()
    if not urls or len(urls) > cap:
        raise ValueError(f"Provide 1-{cap} URLs")
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
                None,
                label_tab,
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
        *(open_one(url, session_id) for url, session_id in zip(urls, ids, strict=True))
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
async def browser_close_tabs(
    tab_ids: list[int],
    include_pinned: bool = False,
    include_claimed: bool = False,
    wait_seconds: float = 1.0,
) -> dict[str, Any]:
    """Close named tabs in the user's Chrome; ids come from web_info browser_tabs."""
    return await asyncio.to_thread(
        browser_tools.close_tabs, tab_ids, include_pinned, include_claimed, wait_seconds
    )


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
    agent_label: str | None = None,
    label_tab: bool = True,
) -> dict[str, Any]:
    """Attach a reusable MCP session to one existing Chrome tab without navigating it."""
    return await asyncio.to_thread(
        browser_tools.attach_current_tab, tab_id, session_id, agent_label, label_tab
    )


@mcp.tool()
async def browser_get_page_elements(
    session_id: str = "default",
    include_links: bool = True,
    include_forms: bool = True,
    include_buttons: bool = True,
    limit: int = 200,
    offset: int = 0,
    max_chars: int = browser_tools.DEFAULT_RESPONSE_CHAR_BUDGET,
    href_pattern: str | None = None,
    text_pattern: str | None = None,
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
        max_chars,
        href_pattern,
        text_pattern,
    )


@mcp.tool()
async def browser_page_outline(
    session_id: str = "default",
    limit: int = 200,
    include_occlusion: bool = True,
    output: Literal["text", "json"] = "text",
    frame_selector: str | None = None,
    max_chars: int = browser_tools.DEFAULT_RESPONSE_CHAR_BUDGET,
    scope: Literal["auto", "page"] = "auto",
) -> dict[str, Any]:
    """Outline the page: roles, names, states, refs, and boxes, including shadow DOM.

    Overlays come first, and an overlay that declares itself modal is the whole
    answer - `scoped_to_modal` says so. Nothing behind a modal can be clicked, so it
    is left out rather than offered. Use scope="page" to look behind one on purpose.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.get_page_outline,
            session_id=session_id,
            limit=limit,
            include_occlusion=include_occlusion,
            output=output,
            frame_selector=frame_selector,
            max_chars=max_chars,
            scope=scope,
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
async def browser_element_text(
    selector: str,
    session_id: str = "default",
    mode: Literal["text", "html", "outer", "both"] = "text",
    full_text: bool = False,
    max_chars: int = 20_000,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Extract one element's whole content - text, innerHTML, or outerHTML.

    Unlike page_text, the answer is not clipped by overflow: the element's full
    DOM subtree is read, so a scrolled code block or collapsed panel gives up
    its tail. full_text=true switches to textContent (everything in the DOM,
    including overflow-hidden parts); mode='html'/'outer'/'both' return markup.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.get_element_text,
            session_id=session_id,
            selector=selector,
            mode=mode,
            full_text=full_text,
            max_chars=max_chars,
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
    max_chars: int = browser_tools.DEFAULT_RESPONSE_CHAR_BUDGET,
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
            max_chars=max_chars,
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
    selector: str = "",
    session_id: str = "default",
    state: Literal["present", "visible", "clickable"] = "visible",
    timeout_seconds: float = 10.0,
    frame_selector: str | None = None,
    script: str | None = None,
    poll_ms: int = 150,
) -> dict[str, Any]:
    """Wait for dynamic content: an element state, or a JS condition.

    Pass selector for present/visible/clickable, or script (a JS expression,
    e.g. "window.__hydrated === true") to poll atomically server-side.
    Selector and script are mutually exclusive.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.wait_for_element,
            selector,
            session_id=session_id,
            state=state,
            timeout_seconds=timeout_seconds,
            frame_selector=frame_selector,
            script=script,
            poll_ms=poll_ms,
        )
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
    blur_after: bool = True,
    typing: bool = False,
) -> dict[str, Any]:
    """Fill rendered form fields; map CSS selectors to values or local file paths.

    blur_after=false leaves focus in the last control; typing=true emulates
    keystroke-by-keystroke input for masked/autocomplete fields.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.fill_fields,
            fields,
            files,
            session_id=session_id,
            frame_selector=frame_selector,
            blur_after=blur_after,
            typing=typing,
        )
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
    selector: str | None = None,
    session_id: str = "default",
    wait_seconds: float = 0.5,
    frame_selector: str | None = None,
    trusted: bool = False,
    text: str | None = None,
    role: str | None = None,
    exact: bool = True,
    x: float | None = None,
    y: float | None = None,
    selector_must_be_unique: bool = False,
) -> dict[str, Any]:
    """Click one thing: a button/element, text, or a viewport point.

    Provide exactly one target: ``selector`` (CSS, ref handle, or a 'a >>> b'
    piercing path) clicks that element; ``text`` (with optional ``role`` and
    ``exact``) clicks the one visible interactive element matching that rendered
    text and refuses ambiguity; ``x``/``y`` (both required) click the viewport
    point in CSS pixels. ``trusted=true`` dispatches a real trusted mouse
    sequence for the selector form. ``selector_must_be_unique=true`` accepts
    plain CSS only and refuses zero or multiple matches immediately before click.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.click,
            selector,
            session_id=session_id,
            wait_seconds=wait_seconds,
            frame_selector=frame_selector,
            trusted=trusted,
            text=text,
            role=role,
            exact=exact,
            x=x,
            y=y,
            selector_must_be_unique=selector_must_be_unique,
        )
    )


@mcp.tool()
async def browser_run_script(
    script: str,
    args: list[Any] | None = None,
    session_id: str = "default",
    await_promise: bool = False,
    user_gesture: bool = False,
    retry_on_uncaught: bool = False,
    retries: int = 2,
    retry_delay_ms: int = 300,
    wait_ready: bool = False,
) -> dict[str, Any]:
    """Execute a JavaScript snippet in a session's page and return its value.

    Use for state the DOM reads do not expose (localStorage, virtualised lists,
    framework state) and for mutations without an input-shaped equivalent.
    Runs once by default. Enable retry_on_uncaught only for scripts safe to repeat:
    a thrown exception may follow an already completed mutation.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.execute_js,
            script,
            args=args,
            session_id=session_id,
            await_promise=await_promise,
            user_gesture=user_gesture,
            retry_on_uncaught=retry_on_uncaught,
            retries=retries,
            retry_delay_ms=retry_delay_ms,
            wait_ready=wait_ready,
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
async def browser_click_text(
    text: str,
    session_id: str = "default",
    exact: bool = True,
    role: str | None = None,
    selector: str | None = None,
    wait_seconds: float = 0.5,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Click one visible interactive element by rendered text and optional role."""
    return await asyncio.to_thread(
        browser_tools.click_text,
        text,
        session_id,
        exact,
        role,
        selector,
        wait_seconds,
        frame_selector,
    )


@mcp.tool()
async def browser_type_text(
    text: str,
    session_id: str = "default",
    selector: str | None = None,
) -> dict[str, Any]:
    """Type text into the focused element or a CSS target via CDP insert-text."""
    return await asyncio.to_thread(browser_tools.type_text, text, session_id, selector)


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
    selector: str | None = None,
    frame_selector: str | None = None,
    wait_seconds: float = 0.1,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Scroll down with positive delta_y or up with negative delta_y.

    Omit x/y to use the viewport centre; provide both to choose the scrollable
    container under that point. Pass selector (CSS, ref handle, or a 'a >>> b'
    piercing path) to scroll the container that holds that element: it is
    brought into view first, then the wheel lands on its centre. selector and
    frame_selector are mutually exclusive.
    """
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.scroll_page,
            delta_y,
            session_id=session_id,
            delta_x=delta_x,
            x=x,
            y=y,
            selector=selector,
            frame_selector=frame_selector,
            wait_seconds=wait_seconds,
            include_summary=include_summary,
        )
    )


@mcp.tool()
async def browser_reload(
    session_id: str = "default",
    hard: bool = False,
    wait_seconds: float = 0.5,
) -> dict[str, Any]:
    """Reload the page in place; hard=true bypasses the cache."""
    return await asyncio.to_thread(
        functools.partial(
            browser_tools.reload_page,
            session_id=session_id,
            hard=hard,
            wait_seconds=wait_seconds,
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
async def browser_automation_skill(section: str | None = None) -> dict[str, Any]:
    """Return the built-in automation playbook, or one detailed section of it."""
    if section is None or not str(section).strip():
        # Names only. Each section already opens with its own summary, and
        # repeating eleven of them here would cost the compact playbook a fifth
        # of its size to describe pages nobody has asked for yet.
        return {
            **_AUTOMATION_SKILL,
            "sections": sorted(_SKILL_SECTIONS),
            "read_a_section": (
                "web_info(topic='skill', params={'section': '<name>'}) opens one in "
                "full: when it applies, the calls in order, the rules, what to avoid."
            ),
        }
    chosen = str(section).strip().lower()
    detail = _SKILL_SECTIONS.get(chosen)
    if detail is None:
        raise ValueError(
            f"Unknown skill section: '{chosen}'. Available: {sorted(_SKILL_SECTIONS)}"
        )
    return {"section": chosen, **detail}


@mcp.tool()
async def browser_action_index(group: str | None = None) -> dict[str, Any]:
    """List every web_action action with its summary and required parameters."""
    groups: dict[str, list[str]] = {}
    for name, spec in _ACTIONS.items():
        groups.setdefault(spec.group, []).append(name)
    index = _action_index()
    if group is not None and str(group).strip():
        chosen = str(group).strip().lower()
        if chosen not in groups:
            raise ValueError(
                f"Unknown action group: '{chosen}'. Available: {sorted(groups)}"
            )
        index = {name: index[name] for name in sorted(groups[chosen])}
    return {
        "actions": index,
        "action_groups": {key: sorted(value) for key, value in sorted(groups.items())},
        "info_topics": _INFO_TOPICS,
        "next": (
            "Optional parameter names, types, and defaults are not here. Read one with "
            "web_info(topic='action_schema', params={'action': '<action or topic>'})."
        ),
    }


@mcp.tool()
async def browser_show(session_id: str = "default") -> dict[str, Any]:
    """Explicitly put one browser session in the foreground."""
    return await asyncio.to_thread(browser_tools.show_session, session_id)


@mcp.tool()
async def browser_get_status(session_id: str = "default") -> dict[str, Any]:
    """Check Chrome support and whether a named browser session is open."""
    return await asyncio.to_thread(browser_tools.get_status, session_id)


@mcp.tool()
async def browser_close(
    session_id: str = "default", close_tab: bool | None = None
) -> dict[str, Any]:
    """Close a browser session; close_tab decides the tab's fate explicitly.

    The default is unchanged and deliberate: a tab the server opened closes with
    the session, a tab claimed with attach_tab is handed back. close_tab=True
    overrides that for a claimed tab, which is the only way a caller could ask
    for the tab itself to go - the capability was already in browser_tools and
    simply had no route out through the action.
    """
    return await asyncio.to_thread(browser_tools.close_session, session_id, close_tab)


@mcp.tool()
async def browser_close_all(
    agent_label: str | None = None,
    scope: Literal["mine", "all"] = "mine",
    include_foreign: bool = False,
) -> dict[str, Any]:
    """Close the sessions this agent_label owns; scope='all' closes every agent's."""
    return await asyncio.to_thread(
        browser_tools.close_all_sessions, agent_label, scope, include_foreign
    )


# The session an action means when the caller does not name one. It is also the
# default of every session_id parameter, so a macro replayed without an explicit
# session drives exactly the tab the agent has been driving.
_DEFAULT_SESSION = "default"


def _named_session(session_id: str | None) -> str:
    return str(session_id).strip() if str(session_id or "").strip() else _DEFAULT_SESSION


def _step_problems(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check each resolved step against the schema its action publishes.

    Macro files are written by hand, so the mistake in one is usually a wrong
    parameter name rather than a wrong click path - and without this it surfaces
    mid-replay, after the steps before it have already dispatched. The same
    validator the dispatcher uses runs here, against nothing.
    """
    problems: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        arguments = dict(step)
        action_name = str(arguments.pop("action", "")).strip().lower()
        spec = _ACTIONS.get(action_name)
        if spec is None:
            problems.append(
                {"index": index, "error": _unsupported_action_error(action_name, arguments)}
            )
            continue
        try:
            _validate_arguments(spec.tool_name, f"action '{action_name}'", arguments)
        except ValueError as exc:
            problems.append({"index": index, "action": action_name, "error": str(exc)})
    return problems


# A placeholder inside a script is the expensive mistake, and the reason this
# checker exists at all. A `{{body}}` is substituted as raw text into JavaScript,
# so a value with a newline, a quote or a backslash - which is most real values -
# produces a syntactically broken program, and the step fails with an opaque
# "WebDriverException: Uncaught" from inside the page. Variables belong in the
# script's `args`, where they cross as data.
_SCRIPT_FIELDS = ("script", "source")

# The steps that read something back. A macro whose last act is to send, click or
# submit reports whatever the dispatcher happened to return and calls it done;
# one that ends by waiting for a success marker or reading state can be believed.
_VERIFYING_ACTIONS = frozenset(
    {"wait", "wait_challenge", "run_script", "fetch_text", "fetch_links", "search"}
)
# Steps that end a macro without being its point, so the "does it check itself"
# question is asked of what came before them.
_EPILOGUE_ACTIONS = frozenset({"close", "close_all", "release_inputs", "render"})


def _declared_variables(payload: Any) -> dict[str, Any] | None:
    """What the file itself declares, or None when it declares nothing at all."""
    if isinstance(payload, dict) and isinstance(payload.get("variables"), dict):
        return dict(payload["variables"])
    return None


def _unreadable(name: str, path: Any, error: str, fix: str) -> dict[str, Any]:
    """One shape for every reason a file could not be checked at all."""
    return {
        "success": False,
        "macro": name,
        "valid": False,
        "path": str(path),
        "errors": [{"error": error, "fix": fix}],
        "warnings": [],
        "executed": False,
    }


def _validate_macro_file(name: str, project_root: str | None = None) -> dict[str, Any]:
    """Check a macro file without running a single step of it.

    A macro is a JSON file a person or a generator writes, so the mistakes in one
    are writing mistakes: a misspelled action, a required parameter left out, a
    placeholder nobody declared, a session id that drifted halfway through. Every
    one of those used to surface mid-replay, on a live page, after the steps
    before it had already clicked something.

    Errors are things that cannot work. Warnings are things that usually mean a
    typo but might be deliberate, so they never make a macro invalid.
    """
    validated_name = macros.validate_name(name)
    path = macros.macro_file(validated_name, project_root)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not path.exists():
        return _unreadable(
            validated_name,
            path,
            f"No macro file at {path}.",
            "Check op='list' for the store in use, and the file name.",
        )
    try:
        payload = macros.raw_payload(validated_name, project_root)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _unreadable(
            validated_name,
            path,
            f"{path} is not readable JSON: {exc}",
            "Fix the JSON syntax: a macro file is a JSON object or a JSON array of steps.",
        )
    except OSError as exc:
        return _unreadable(
            validated_name,
            path,
            f"{path} could not be read: {exc}",
            "Check that the file exists and can be read.",
        )

    declared = _declared_variables(payload)
    try:
        record = macros.record_from_payload(payload, validated_name, path)
    except ValueError as exc:
        return _unreadable(
            validated_name,
            path,
            str(exc),
            'A macro file is {"name": ..., "steps": [...]} or the bare list of steps.',
        )

    steps = record["steps"]
    sessions: list[str] = []
    for index, step in enumerate(steps):
        arguments = dict(step)
        action_name = str(arguments.pop("action", "")).strip().lower()
        spec = _ACTIONS.get(action_name)
        if spec is None:
            errors.append(
                {
                    "index": index,
                    "action": action_name or None,
                    "error": _unsupported_action_error(action_name, arguments),
                    "fix": "Use a published action name; web_info(topic='actions') lists them.",
                }
            )
            continue
        required, optional = _parameter_names(spec.tool_name)
        known = set(required) | set(optional)
        missing = [item for item in required if item not in arguments]
        schema_hint = (
            "web_info(topic='action_schema', params={'action': '" + action_name + "'})"
        )
        if missing:
            errors.append(
                {
                    "index": index,
                    "action": action_name,
                    "error": f"required parameter(s) {missing} are missing",
                    "fix": f"Add them to step {index}; {schema_hint} has the types.",
                }
            )
        unknown = sorted(key for key in arguments if key not in known)
        if unknown:
            errors.append(
                {
                    "index": index,
                    "action": action_name,
                    "error": (
                        f"parameter(s) {unknown} are not part of this action and would "
                        "be refused at dispatch"
                    ),
                    "fix": f"Remove them, or read {schema_hint} for the right names.",
                }
            )
        for field in _SCRIPT_FIELDS:
            value = arguments.get(field)
            inside = macros.placeholders_in(value) if isinstance(value, str) else set()
            if inside:
                errors.append(
                    {
                        "index": index,
                        "action": action_name,
                        "error": (
                            f"{sorted(inside)} are substituted as raw text into "
                            f"'{field}'. A value containing a newline, a quote or a "
                            "backslash breaks the JavaScript, and the step fails with "
                            "an opaque error from inside the page."
                        ),
                        "fix": (
                            "Pass the value through args instead, and read it inside "
                            "the script from arguments[0]."
                        ),
                    }
                )
        session = arguments.get("session_id")
        if isinstance(session, str) and session.strip():
            sessions.append(session.strip())

    used = macros.placeholders_in(steps)
    if declared is None:
        if used:
            warnings.append(
                {
                    "error": f"the file declares no 'variables', but its steps use {sorted(used)}",
                    "fix": (
                        'Add "variables": {"<name>": null} for each, so op=show says '
                        "what the macro wants before anyone runs it."
                    ),
                }
            )
    else:
        undeclared = sorted(used - set(declared))
        unused = sorted(set(declared) - used)
        if undeclared:
            errors.append(
                {
                    "error": f"placeholder(s) {undeclared} are used but not declared in 'variables'",
                    "fix": "Declare each one, with a default value or null.",
                }
            )
        if unused:
            warnings.append(
                {
                    "error": f"variable(s) {unused} are declared but no step uses them",
                    "fix": "Remove them, or check how the placeholder is spelled in the steps.",
                }
            )

    distinct = sorted(set(sessions))
    if len(distinct) > 1:
        warnings.append(
            {
                "error": f"the steps drive {len(distinct)} different sessions: {distinct}",
                "fix": (
                    "Almost always a typo. Use one session_id throughout, or pass "
                    "session_id to op='run' to point the whole macro at one tab."
                ),
            }
        )

    # Paired with the step it is about, not with the end of the file: a macro
    # that ends `submit, close_all` is judged on the submit, and pointing the
    # finding at the close would send the reader to the wrong line.
    meaningful = [
        (index, step)
        for index, step in enumerate(steps)
        if str(step.get("action") or "").strip().lower() not in _EPILOGUE_ACTIONS
    ]
    if meaningful:
        last_index, last_step = meaningful[-1]
        last = str(last_step.get("action") or "").strip().lower()
        if last not in _VERIFYING_ACTIONS:
            warnings.append(
                {
                    "index": last_index,
                    "error": f"the macro ends on '{last}' and never reads the result back",
                    "fix": (
                        "End with a wait on a success marker, or a run_script that reads "
                        "the page state. A macro that sends something without checking "
                        "reports success for a submit that failed."
                    ),
                }
            )

    return {
        "success": True,
        "macro": validated_name,
        "valid": not errors,
        "path": str(path),
        "step_count": len(steps),
        "declared_variables": sorted(declared) if declared is not None else [],
        "used_placeholders": sorted(used),
        "errors": errors,
        "warnings": warnings,
        "executed": False,
        "note": (
            "Nothing was dispatched. "
            + (
                "No errors; run op='preview' with the variables to see the final steps."
                if not errors
                else f"{len(errors)} error(s) would fail this macro as written."
            )
        ),
    }


def _retarget_session(steps: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    """Point every step of a replay at one session, or refuse to guess.

    A macro recorded against one tab carries that tab's id in every step, so
    running it beside the original - a second agent, a second account - otherwise
    means editing the file. A macro that already drives two sessions means two
    tabs on purpose, and collapsing them into one would quietly change what it
    does, so that is refused and the caller is pointed at a placeholder instead.
    """
    target = str(session_id).strip()
    if not target:
        return steps
    sessions = {
        found
        for step in steps
        if (spec := _ACTIONS.get(str(step.get("action") or "").strip().lower())) is not None
        and (found := _step_session(spec.tool_name, step)) is not None
    }
    if len(sessions) > 1:
        raise ValueError(
            f"session_id cannot retarget this macro: its steps already use {sorted(sessions)}, "
            "and running them all in one tab would change what the macro does. Put a "
            "{{placeholder}} in the session_id of the steps that should vary instead."
        )
    retargeted: list[dict[str, Any]] = []
    for step in steps:
        spec = _ACTIONS.get(str(step.get("action") or "").strip().lower())
        if spec is None or _step_session(spec.tool_name, step) is None:
            retargeted.append(step)
            continue
        retargeted.append({**step, "session_id": target})
    return retargeted


async def _macro_store(project_root: str | None) -> dict[str, Any]:
    """Where this call read and wrote, reported on every macro answer.

    The commonest macro failure is not a bad step list: it is a save into one
    store and a run against the other, which from the caller's side looks exactly
    like a macro that vanished. Naming the store in the answer that caused it
    makes the mistake visible where it happens.
    """
    return await asyncio.to_thread(macros.store_info, project_root)


@mcp.tool()
async def browser_macro(
    op: str = "list",
    name: str | None = None,
    variables: dict[str, Any] | None = None,
    continue_on_error: bool = False,
    guard: dict[str, Any] | None = None,
    checkpoint: str | None = None,
    project_root: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Check and replay named macro files from this project's or the user's store."""
    op = str(op or "").strip().lower()

    if op == "validate":
        if not name:
            raise ValueError("macro op 'validate' requires name")
        return {
            **await asyncio.to_thread(_validate_macro_file, name, project_root),
            **await _macro_store(project_root),
        }

    if op == "run":
        if not name:
            raise ValueError("macro op 'run' requires name")
        record = await asyncio.to_thread(macros.load, name, project_root)
        resolved = macros.resolve(record["steps"], record.get("variables"), variables)
        if session_id:
            resolved = _retarget_session(resolved, session_id)
        # A replay is one logical call, so it does not inherit web_action's
        # hand-written 32-action ceiling.
        outcome = await _execute_actions(resolved, continue_on_error)
        return {
            **outcome,
            "macro": record["name"],
            "step_count": len(resolved),
            **({"session_override": _named_session(session_id)} if session_id else {}),
            **await _macro_store(project_root),
        }

    if op == "guarded_stage":
        if not name:
            raise ValueError("macro op 'guarded_stage' requires name")
        record = await asyncio.to_thread(macros.load, name, project_root)
        resolved = macros.resolve(record["steps"], record.get("variables"), variables)
        checked_guard = await asyncio.to_thread(macros.validate_guard, guard, resolved)
        staged_steps, terminal_step = macros.split_terminal_action(resolved)
        outcome = await _execute_actions(staged_steps, continue_on_error=False)
        if not outcome.get("success"):
            return {
                **outcome,
                "macro": record["name"],
                "executed_terminal_action": False,
                "executed_submit": False,
                "executed_click": False,
                "checkpoint": None,
                "note": "Staging failed; terminal consequential action was not dispatched.",
            }
        assertions = macros.evaluate_assertions(outcome, checked_guard["assertions"])
        reserved = await asyncio.to_thread(
            macros.reserve_checkpoint, checked_guard, terminal_step, project_root
        )
        return {
            **outcome,
            "macro": record["name"],
            "executed_terminal_action": False,
            "executed_submit": False,
            "executed_click": False,
            "terminal_action": terminal_step["action"],
            "checkpoint": reserved,
            "identity": checked_guard["identity"],
            "resource_sha256": checked_guard["resource_sha256"],
            "assertions": assertions,
            "note": (
                "Live semantic assertions passed. Review this result, then call "
                "op='guarded_commit' once with the checkpoint to attempt the terminal action."
            ),
            **await _macro_store(project_root),
        }

    if op == "guarded_commit":
        if not checkpoint:
            raise ValueError("macro op 'guarded_commit' requires checkpoint")
        # Consume before dispatch: a timeout or lost response is ambiguous, so the
        # same terminal consequential action must never be replayed automatically.
        reserved = await asyncio.to_thread(macros.consume_checkpoint, checkpoint, project_root)
        terminal_step = reserved.get("terminal_step") or reserved.get("submit_step")
        if not isinstance(terminal_step, dict):
            raise ValueError("guarded checkpoint has no terminal action; refusing dispatch")
        terminal_action = str(reserved.get("terminal_action") or terminal_step.get("action"))
        outcome = await _execute_actions([terminal_step], False)
        return {
            **outcome,
            "executed_terminal_action": True,
            "executed_submit": terminal_action == "submit",
            "executed_click": terminal_action == "click",
            "terminal_action": terminal_action,
            "checkpoint": checkpoint,
            "identity": reserved["identity"],
            "resource_sha256": reserved.get("resource_sha256"),
            "note": "Checkpoint consumed before dispatch; this terminal action cannot be replayed.",
            **await _macro_store(project_root),
        }

    if op == "preview":
        if not name:
            raise ValueError("macro op 'preview' requires name")
        record = await asyncio.to_thread(macros.load, name, project_root)
        resolved = macros.resolve(record["steps"], record.get("variables"), variables)
        if session_id:
            resolved = _retarget_session(resolved, session_id)
        problems = _step_problems(resolved)
        return {
            "success": True,
            "macro": record["name"],
            "description": record.get("description") or "",
            "step_count": len(resolved),
            "steps": resolved,
            "executed": False,
            **({"session_override": _named_session(session_id)} if session_id else {}),
            "steps_valid": not problems,
            **({"problems": problems} if problems else {}),
            "note": (
                "Preview only: no action was dispatched and no browser state changed."
                if not problems
                else "Preview only: nothing was dispatched, and these steps would fail "
                "as written. Fix the macro file before running it."
            ),
            **await _macro_store(project_root),
        }

    if op == "list":
        return {
            "success": True,
            "macros": await asyncio.to_thread(macros.list_macros, project_root),
            **await _macro_store(project_root),
        }

    if op == "show":
        if not name:
            raise ValueError("macro op 'show' requires name")
        record = await asyncio.to_thread(macros.load, name, project_root)
        return {"success": True, **record, **await _macro_store(project_root)}

    raise ValueError(
        "macro op must be list, show, validate, preview, run, guarded_stage or "
        f"guarded_commit, not '{op}'. Macros are JSON files: create, edit, rename "
        "and delete them with ordinary file operations, then check the result with "
        "op='validate'."
    )


@mcp.tool()
async def browser_captcha(
    mode: str = "auto",
    session_id: str = "default",
    timeout_seconds: float = 180.0,
    poll_seconds: float = 3.0,
) -> dict[str, Any]:
    """Detect a captcha and clear it: wait for a human by default; mode='solve' uses a configured paid service."""
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
async def browser_mock(
    op: Literal["add", "list", "clear"] = "list",
    url_pattern: str | None = None,
    session_id: str = "default",
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: str = "",
) -> dict[str, Any]:
    """Stub third-party responses in the page: add/list/clear URL-pattern mocks.

    add needs url_pattern (wildcard, '*' matches anything) and answers the
    match with status/headers/body; list shows live stubs; clear drops one
    pattern or every stub. Non-matching requests reach the network untouched.
    """
    if op == "add":
        if not (url_pattern or "").strip():
            raise ValueError("mock op 'add' requires url_pattern")
        return await asyncio.to_thread(
            functools.partial(
                browser_tools.mock_add_request,
                url_pattern,
                session_id=session_id,
                status=status,
                headers=headers,
                body=body,
            )
        )
    if op == "list":
        return await asyncio.to_thread(browser_tools.mock_list_requests, session_id)
    if op == "clear":
        return await asyncio.to_thread(
            functools.partial(
                browser_tools.mock_clear_requests,
                session_id=session_id,
                url_pattern=url_pattern,
            )
        )
    raise ValueError(f"mock op must be add, list, or clear, not '{op}'")


@mcp.tool()
async def browser_inject_script(
    op: Literal["add", "list", "remove"] = "add",
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
    op: Literal["get", "set", "clear"] = "get",
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
    op: Literal["read", "write", "delete"] = "read",
    session_id: str = "default",
    key: str | None = None,
    value: str | None = None,
    kind: Literal["local", "session"] = "local",
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
mcp = ReportingFastMCP(  # failed web_action batches come back with isError=true
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
        _action("http_request", http_request, "fetch", "Send any HTTP request without a browser; 4xx/5xx return status."),
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
        _action(
            "context",
            browser_context,
            "session",
            "Retarget a live session's fingerprint.",
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
        _action("type_text", browser_type_text, "page", "Type text into the focused control or a CSS target via CDP insert-text."),
        _action(
            "click_text",
            browser_click_text,
            "page",
            "Click the one visible interactive element matching text and optional role.",
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
            "reload",
            browser_reload,
            "page",
            "Reload the current page in place; hard=true bypasses the cache.",
        ),
        _action(
            "inject_script", browser_inject_script, "page", "Run code before each document's scripts."
        ),
        _action("cookies", browser_cookies, "page", "Read, write, or clear cookies with their flags."),
        _action(
            "local_storage", browser_local_storage, "page", "Read or write local/session storage."
        ),
        _action(
            "macro",
            browser_macro,
            "macro",
            "Check and replay named macro files from this project's macro store.",
        ),
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
            "mock",
            browser_mock,
            "page",
            "Stub third-party responses.",
        ),
        _action(
            "close",
            browser_close,
            "session",
            "Close one session; a claimed current-Chrome tab stays open unless close_tab=true.",
        ),
        _action(
            "close_tabs",
            browser_close_tabs,
            "session",
            "Close named tabs in the user's Chrome by id; pinned and other agents' tabs are refused.",
        ),
        _action(
            "close_all",
            browser_close_all,
            "session",
            "Close the sessions your agent_label owns; scope='all' closes every agent's.",
        ),
    )
}


def _argument_model(tool_name: str) -> Any:
    """Return the pydantic model FastMCP generated for one wrapper function."""
    return registered_tool(legacy_mcp, tool_name).fn_metadata.arg_model


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


from web_search_neo.contract.playbook import (_AUTOMATION_SKILL, _SKILL_SECTIONS)


from web_search_neo.contract.notes import (_INFO_TOPICS, _ACTION_NOTES)

from web_search_neo.contract.examples import (_RECIPES, _PITFALLS, _EXAMPLES, _CONTRACT_EXAMPLE_NAMES, _RUNTIME_REQUIREMENTS)


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
    original = registered_tool(legacy_mcp, handler.__name__).parameters
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


def _capabilities(action_name: str | None = None, full_schemas: bool = False) -> dict[str, Any]:
    """Return the whole agent-facing contract, one action's schema, or one topic's."""
    if action_name is not None:
        selected = action_name.strip().lower()
        spec = _ACTIONS.get(selected)
        if spec is None:
            return _topic_schema(selected)
        original = registered_tool(legacy_mcp, spec.tool_name).parameters
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
    # The cap alone was never the useful number: an agent that reads "8" and
    # finds all eight taken has learned nothing it could not have learned by
    # failing. How many are free is the answer to the question it was asking.
    occupancy = browser_tools.sessions_overview()
    document: dict[str, Any] = {
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
        "examples": {name: _EXAMPLES[name] for name in _CONTRACT_EXAMPLE_NAMES},
        "limits": {
            "ordered_actions_per_call": 32,
            "parallel_browser_sessions": occupancy["max_sessions"],
            "browser_sessions_open": occupancy["sessions_open"],
            "browser_sessions_free": occupancy["sessions_free"],
            "browser_sessions_busy": len(occupancy["sessions_in_use"]),
            "max_sessions_source": occupancy["max_sessions_source"],
            "response_char_budget_default": browser_tools.DEFAULT_RESPONSE_CHAR_BUDGET,
            "input_actions_per_batch": "16 key + 16 pointer",
            "automatic_captcha": False,
            "captcha_modes": ["fallback", "manual"],
        },
        "discovery": {
            "next_call": "web_info",
            "topic": "action_schema",
            "params_example": {"action": "input"},
            "list_actions": "web_info(topic='actions') is the action index alone; params.group narrows it.",
            "playbook": "web_info(topic='skill') is the loop plus a section index; params.section='<name>' opens one in full (start, loop, locators, forms, macros, guarded, parallel, search, diagnostics, games, troubleshooting).",
            "note": "params.action names an action or an info topic; a topic's parameters are published nowhere else, and any key it does not list is refused.",
            "parameters": (
                "actions[name].required lists parameters you must always send; "
                "also_required is a condition a list cannot express and is just as "
                "mandatory. Optional names, types, and defaults exist only in "
                "action_schema; capabilities full_schemas=true embeds them all at once."
            ),
        },
    }
    if full_schemas:
        document["schemas"] = {
            name: _capabilities(name)["input_schema"] for name in _ACTIONS
        }
    return document


_TOPIC_HANDLERS = {
    "skill": browser_automation_skill,
    "actions": browser_action_index,
    "search_status": get_search_engines_status,
    "browser_status": browser_get_status,
    "browser_tabs": browser_list_tabs,
    "page_outline": browser_page_outline,
    "page_text": browser_page_text,
    "element_text": browser_element_text,
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
        "actions",
        "search_status",
        "browser_status",
        "browser_tabs",
        "page_outline",
        "page_text",
        "element_text",
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
        unknown = set(arguments) - {"full_schemas"}
        if unknown:
            raise ValueError(
                f"capabilities accepts only params.full_schemas; unknown: {sorted(unknown)}"
            )
        full_schemas = arguments.get("full_schemas", False)
        if not isinstance(full_schemas, bool):
            raise ValueError("full_schemas must be a boolean")
        return _stamp_now(_capabilities(full_schemas=full_schemas))
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


async def _mark_agent_presence(
    tool_name: str,
    action_name: str,
    arguments: dict[str, Any],
    *,
    ok: bool,
) -> None:
    """Let the human watching the tab see that this step happened.

    Hooked here rather than inside each handler for one reason: there are
    dozens of handlers and one dispatcher, and a signal that is only as
    complete as the last action someone remembered to instrument is worse
    than none - the tab would look idle precisely during the actions nobody
    thought about.

    Never raises and never blocks the result. A step that has no session, or
    whose session the step itself just closed, is simply not marked.
    """
    session_id = _step_session(tool_name, arguments)
    if not session_id:
        return
    try:
        await asyncio.to_thread(
            browser_tools.note_agent_activity, session_id, action_name, arguments, ok
        )
    except Exception:
        pass


async def _execute_actions(
    actions: list[dict[str, Any]],
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Run an ordered action list, validating each against its published schema.

    ``web_action`` and a macro replay share this loop rather than each having
    their own: a macro that ran its steps down a second, laxer path would drift
    from the calls its file was validated against, which is the one thing a
    saved click path cannot afford.
    """
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
            await _mark_agent_presence(
                spec.tool_name, action_name, validated, ok=not reported_failure
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
            # A refused step is worth showing too, in the failure colour: a
            # burst where one click never landed is exactly what a watching
            # human wants to catch.
            await _mark_agent_presence(spec.tool_name, action_name, arguments, ok=False)
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


def _step_session(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """The session an action actually acted on, its schema default included.

    ``exclude_unset`` keeps a default out of the recorded step, which is right -
    but the action still ran against that default tab, and attributing it to
    "no session" would hand it to whichever recording happened to be open.
    """
    fields = _argument_model(tool_name).model_fields
    if "session_id" not in fields:
        return None
    value = arguments.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    default = fields["session_id"].default
    return default if isinstance(default, str) and default else None


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
    # Plugins (WEB_SEARCH_NEO_PLUGINS or entry points) may add actions, info
    # topics, and search providers before the surface is published; the bridge
    # daemon above publishes no MCP surface and never loads them.
    plugins.load_plugins()
    browser_tools.start_current_chrome_bridge()
    active_mcp = (
        legacy_mcp
        if os.getenv("WEB_SEARCH_NEO_LEGACY_TOOLS", "").strip() == "1"
        else mcp
    )
    active_mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
