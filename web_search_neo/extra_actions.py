"""MCP wrappers for the 1.19 actions, registered on the legacy tool server.

``main.py`` is a compatibility facade held to a size ratchet, so new wrappers
live here and ``register`` adds them to the same FastMCP instance main's action
table resolves tools on. Each wrapper is a plain async function that runs its
work off the event loop, exactly like the ones in main.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from web_search_neo import audit_actions, browser_tools, frame_capture, frame_health, page_guards
from web_search_neo import owned_parking, tab_actions
from web_search_neo.sessions import parking as current_parking


# The mode of a session opened without profile_mode (1.20: isolated, was current).
DEFAULT_NEW_SESSION_MODE = "isolated"


def _owned_session(session_id: str) -> Any:
    return browser_tools._get_session(session_id)


async def browser_dialogs(
    session_id: str = "default",
    policy: Literal["accept", "dismiss"] | None = None,
    prompt_text: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Read logged alert/confirm/prompt dialogs; set how the next ones are answered.

    policy='accept' makes confirm() return true and prompt() return prompt_text;
    'dismiss' (the default) returns false/null. Applies to this document and later ones.
    """
    def run() -> dict[str, Any]:
        session = _owned_session(session_id)
        with session.lock:
            return {"session_id": session_id,
                    **page_guards.dialogs_action(session, policy, prompt_text, clear)}
    return await asyncio.to_thread(run)


async def browser_downloads(session_id: str = "default", wait_seconds: float = 0.0) -> dict[str, Any]:
    """List files this session's browser downloaded (its own folder, never ~/Downloads)."""
    def run() -> dict[str, Any]:
        return {"session_id": session_id,
                **page_guards.downloads_action(_owned_session(session_id), wait_seconds)}
    return await asyncio.to_thread(run)


def inherited_open_options(session_id: str, profile_mode: str | None, profile_id: str | None,
                           debugger_address: str | None, current_tab_id: int | None = None,
                           persist: bool = False) -> dict[str, Any]:
    """``open`` without profile_mode keeps an existing session's browser (else isolated).

    Before, the wrapper's default 'current' made a second open of a temporary or
    isolated session fail with "different browser/profile options". Since 1.20 a
    new session opens ``isolated``: driving the user's own Chrome is opt-in.
    ``persist=true`` without a mode continues what is parked under the session_id
    in its own mode (a parked tab of the user's Chrome: ``current``; a parked
    temporary/isolated browser: that mode). With nothing parked, a new session
    must name its mode - ``current`` keeps a tab of the user's Chrome,
    ``isolated``/``temporary`` keeps the server's browser - because guessing the
    user's Chrome is exactly what 1.20 stopped doing.
    """
    with browser_tools._sessions_lock:
        session = browser_tools._sessions.get(session_id)
    if profile_mode is None and session is not None:
        return {"profile_mode": session.profile_mode,
                "profile_id": profile_id if profile_id is not None else session.profile_id,
                "debugger_address": debugger_address if debugger_address is not None else session.debugger_address}
    if profile_mode is None and persist and current_tab_id is None:
        parked = owned_parking.read(session_id)  # a parked owned browser keeps its own mode
        if parked and parked.get("profile_mode") in owned_parking.OWNED_MODES:
            profile_mode = str(parked["profile_mode"])
        elif current_parking.lookup(session_id):  # a parked tab of the user's Chrome
            profile_mode = "current"
        elif not debugger_address and not profile_id:
            raise ValueError(
                f"persist=true on the new session '{session_id}' needs profile_mode: 'current' keeps a tab of "
                "the user's Chrome open after this server exits, 'isolated' (or 'temporary') keeps the "
                "server's own browser. Nothing is parked under this session_id to continue.")
    if profile_mode is None:  # an option that only one mode has names that mode
        # current_tab_id exists only for the user's Chrome.
        profile_mode = ("current" if current_tab_id is not None else "attach" if debugger_address
                        else "persistent" if profile_id else DEFAULT_NEW_SESSION_MODE)
    return {"profile_mode": profile_mode, "profile_id": profile_id,
            "debugger_address": debugger_address}


async def browser_navigate(
    url: str,
    session_id: str = "default",
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Navigate an open session to a URL, keeping its browser, profile and options."""
    def run() -> dict[str, Any]:
        session = _owned_session(session_id)
        size: dict[str, int] = {}
        if session.profile_mode not in {"current", "attach"}:  # keep the viewport it has
            with session.lock:
                box = session.driver.execute_script("return [innerWidth, innerHeight]") or []
            if len(box) == 2 and all(isinstance(n, (int, float)) and n > 0 for n in box):
                size = {"width": int(box[0]), "height": int(box[1])}
        return browser_tools.open_page(
            url, session_id=session_id, timeout_seconds=timeout_seconds, **size,
            profile_mode=session.profile_mode, headless=None if session.profile_mode == "current" else session.headless,
            profile_id=session.profile_id, debugger_address=session.debugger_address,
            tab_group=session.tab_group or browser_tools.DEFAULT_TAB_GROUP,
            label_tab=session.label_tab,
        )
    return await asyncio.to_thread(run)


def image_to_viewport(session_id: str, x: float, y: float, end_x: float | None = None,
                      end_y: float | None = None, frame_selector: str | None = None,
                      ) -> tuple[float, float, float | None, float | None]:
    """pointer coordinate_space='image': last capture's pixels -> viewport CSS pixels."""
    if frame_selector:
        raise ValueError("coordinate_space='image' works on the top document's viewport; with "
                         "frame_selector give frame-local CSS pixels (coordinate_space='viewport').")
    session = _owned_session(session_id)
    x, y = frame_capture.to_viewport(session, x, y)
    if end_x is not None and end_y is not None:
        end_x, end_y = frame_capture.to_viewport(session, end_x, end_y)
    return x, y, end_x, end_y


async def browser_wait_frames(session_id: str = "default", frames: int = 2) -> dict[str, Any]:
    """Let `frames` animation frames render (1-120), e.g. between a click and keys.

    A game applies focus and input on its next frames; keys sent before those
    frames are lost. raf_stalled=true means the page renders no frames at all
    (see frame_health / unthrottle).
    """
    def run() -> dict[str, Any]:
        session = _owned_session(session_id)
        with session.lock:
            waited = frame_capture.wait_frames(session.driver, max(1, int(frames))) or {}
            if waited.get("raf_stalled"):
                waited["frame_health"] = frame_health.health(session.driver, None, session.render_mode)
        return {"success": not waited.get("raf_stalled"), "session_id": session_id, **waited}
    return await asyncio.to_thread(run)


async def browser_unthrottle(session_id: str = "default") -> dict[str, Any]:
    """Ask Chrome to treat the tab as focused and active; measure raf_fps before and after."""
    def run() -> dict[str, Any]:
        session = _owned_session(session_id)
        with session.lock:
            return {"session_id": session_id, **frame_health.unthrottle(session.driver, session.render_mode)}
    return await asyncio.to_thread(run)


async def browser_look(
    dx: float,
    dy: float,
    session_id: str = "default",
    steps: int = 8,
    duration_ms: int = 160,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Turn a pointer-locked (FPS) camera smoothly: dx/dy split over `steps` relative moves.

    One big jump arrives as one event, which many engines clamp per frame; a
    series spread over duration_ms reads like a hand on a mouse. The sum of the
    moves is exactly dx/dy. Acquire the lock first (pointer_lock acquire).
    """
    def run() -> dict[str, Any]:
        count = max(1, min(int(steps), 60))
        pause = max(0.0, min(float(duration_ms), 5000.0)) / 1000.0 / count
        sent_x = sent_y = 0.0
        started = time.monotonic()
        for index in range(1, count + 1):
            step_x = round(float(dx) * index / count) - sent_x
            step_y = round(float(dy) * index / count) - sent_y
            if step_x or step_y:
                browser_tools.pointer_action("move", step_x, step_y, session_id=session_id,
                                             coordinate_mode="relative", include_summary=False,
                                             frame_selector=frame_selector)
                sent_x, sent_y = sent_x + step_x, sent_y + step_y
            if index < count and pause:
                time.sleep(pause)
        session = _owned_session(session_id)
        return {"success": True, "session_id": session_id, "moved_x": sent_x, "moved_y": sent_y,
                "steps": count, "elapsed_ms": round((time.monotonic() - started) * 1000),
                "pointer_locked": bool(session.pointer_locked)}
    return await asyncio.to_thread(run)


ACTION_SPECS = (
    ("navigate", browser_navigate, "session", "Go to a URL in an open session, keeping its browser and profile."),
    ("dialogs", browser_dialogs, "page", "Read alert/confirm/prompt texts; accept or dismiss the next ones."),
    ("downloads", browser_downloads, "page", "List files the session downloaded into its own folder."),
    ("wait_frames", browser_wait_frames, "game", "Let N animation frames render, e.g. between a click and keys."),
    ("unthrottle", browser_unthrottle, "game", "Un-throttle a background tab; reports raf_fps before/after."),
    ("look", browser_look, "game", "Turn a pointer-locked camera smoothly by dx/dy in steps."),
) + audit_actions.ACTION_SPECS + tab_actions.ACTION_SPECS  # 1.20: site checks; tabs of Selenium sessions


def register(server: Any, facade: Any = None) -> None:
    """Register the wrappers as tools on ``server`` (main's legacy FastMCP).

    ``facade`` is main's own module: test_run replays its steps through that
    module's dispatcher, and main may be running as ``__main__``.
    """
    for _name, wrapper, _group, _summary in ACTION_SPECS:
        server.tool()(wrapper)
    if facade is not None:
        audit_actions.bind(facade)


__all__ = ["ACTION_SPECS", "browser_dialogs", "browser_downloads", "browser_look", "browser_navigate",
           "browser_unthrottle", "browser_wait_frames", "image_to_viewport", "inherited_open_options", "register"]
