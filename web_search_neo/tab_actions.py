"""New tabs and popups of Selenium sessions: the ``tabs`` action and the reporting hook.

``sessions/windows.py`` does the work on one session; this module is the MCP
side of it. Two things live here:

- :func:`observed` wraps an action (click, click_text, submit, pointer, run_script
  in main; input, press_keys, touch, type_text, fill, scroll and wait through the
  dispatcher) and adds ``new_tabs`` to its answer when the page opened a window,
  follows it on ``follow_new_tab``, and brings a session back to the opener
  when the page closed the window it was driving (a popup that finished).
- ``tabs`` lists, switches to, or closes the windows of the session's browser.

The user's own Chrome (current mode) is left exactly as it was: it has no window
handles, and its tabs belong to ``browser_tabs``/``attach_tab``/``close_tabs``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Literal

from web_search_neo import browser_tools, page_guards
from web_search_neo.sessions import windows

# Steps the dispatcher observes; the page-opening ones with their own wrapper
# (click, click_text, submit, pointer, run_script) are observed there instead.
OBSERVED_STEPS = frozenset({"input", "press_keys", "touch", "type_text", "fill", "scroll", "wait"})

NEW_TABS_NOTE = ("The page opened a new tab or popup (new_tabs). The session still drives the "
                 "tab it was on; move to the new one with tabs {op:'switch', handle} or pass "
                 "follow_new_tab=true on the click. Closing the session closes it too.")
CURRENT_CHROME_NOTE = ("tabs works on the browser a Selenium session drives (temporary, isolated, "
                       "persistent, attach). In the user's own Chrome (current) a new tab is one of "
                       "the user's tabs: list them with web_info browser_tabs, claim one with "
                       "attach_tab, close them with close_tabs.")


def _session(session_id: Any) -> Any:
    """The live Selenium session, without touching it (no refresh, no error)."""
    try:
        session_id = browser_tools._validate_session_id(str(session_id))
    except Exception:
        return None
    with browser_tools._sessions_lock:
        session = browser_tools._sessions.get(session_id)
    return session if session is not None and windows.uses_windows(session.driver) else None


def _baseline(session_id: Any) -> str | None:
    """The window the session drives before the action; a window the page closed is left first."""
    session = _session(session_id)
    if session is None:
        return None
    try:
        with session.lock:
            returned = windows.recover_closed_current(session)
            if returned:  # said on this very answer, through the page summary
                session.pending_notice = {**(session.pending_notice or {}), "tab_closed_by_page": returned}
            return windows.prime(session) or ""
    except Exception:
        return None


def _closed_during(session_id: str, before: str, exc: BaseException) -> dict[str, Any] | None:
    """The action closed the very window it ran in (a popup's own "done" button).

    The step itself happened; only what came after it had no page left to read.
    The session goes back to the window that opened this one and says so.
    """
    session = _session(session_id)
    if session is None or not before:
        return None
    with session.lock:
        if windows.current_handle(session.driver) is not None or before in windows.handles(session.driver):
            return None
        returned = windows.recover_closed_current(session)
        if not returned:
            return None
        return {**browser_tools._page_summary(session.driver, session_id), "success": True,
                "window_closed_by_action": before, "tab_closed_by_page": returned,
                "action_note": (f"The page closed its own window during this step ({type(exc).__name__} "
                                "while reading it afterwards); the session is back on the window that opened it.")}


def _prepare_tab(session: Any, session_id: str, never_set_up: bool, timeout_seconds: float = 10.0) -> None:
    """A window the session just moved to: wait for its page, give it the session's scripts."""
    driver = session.driver
    try:
        browser_tools._wait_until_ready(driver, max(0.5, min(float(timeout_seconds), 60.0)))
    except Exception:
        pass
    if not never_set_up:
        return
    for step in (lambda: browser_tools._register_render_bootstrap(session),
                 lambda: page_guards.setup_owned(session, session_id)):
        try:
            step()
        except Exception:
            pass


def _pick(fresh: list[dict[str, Any]], opener: str | None) -> dict[str, Any] | None:
    own = [row for row in fresh if opener and row.get("opener") == opener]
    return (own or fresh or [None])[0]


def _report(session_id: str, before: str, result: Any, follow: bool) -> Any:
    session = _session(session_id)
    if session is None:
        return result
    with session.lock:
        fresh = windows.detect_new(session, before or None)
        returned = windows.recover_closed_current(session)
        if not fresh and not returned and not follow:
            return result
        answer = dict(result) if isinstance(result, dict) else {"result": result}
        if fresh:
            answer["new_tabs"] = fresh
            answer["new_tabs_note"] = NEW_TABS_NOTE
            # A new window is an effect of the click, even with nothing changed in its own page.
            if "effect_detected" in answer:
                answer["effect_detected"] = True
            if "no_observable_change" in answer:
                answer["no_observable_change"] = False
        if returned:
            answer["tab_closed_by_page"] = returned
        if follow:
            target = _pick(fresh, before or None)
            if target is None:
                answer["followed_new_tab"] = None
                answer["follow_note"] = "No new tab opened, so the session stayed where it was."
            else:
                never_set_up = windows.switch(session, str(target["handle"]))
                _prepare_tab(session, session_id, never_set_up)
                answer["followed_new_tab"] = target["handle"]
                answer["tab"] = browser_tools._page_summary(session.driver, session_id)
        return answer


async def observed(session_id: Any, follow: bool, work: Awaitable[Any]) -> Any:
    """Await ``work`` (an action not started yet) and report the windows it opened."""
    before = await asyncio.to_thread(_baseline, session_id)
    try:
        result = await work
    except Exception as exc:
        closed = await asyncio.to_thread(_closed_during, str(session_id), before, exc) if before else None
        if closed is None:
            raise
        return closed
    if before is None:
        if follow and isinstance(result, dict):
            return {**result, "followed_new_tab": None, "follow_note": CURRENT_CHROME_NOTE}
        return result
    return await asyncio.to_thread(_report, str(session_id), before, result, bool(follow))


async def observe_step(action_name: str, session_id: str | None, work: Awaitable[Any]) -> Any:
    """The dispatcher's hook: observe the steps in :data:`OBSERVED_STEPS`, pass the rest."""
    if action_name not in OBSERVED_STEPS or not session_id:
        return await work
    return await observed(session_id, False, work)


def _resolve(session: Any, handle: str | None, index: int | None) -> str:
    every = windows.handles(session.driver)
    if handle is not None:
        if str(handle) not in every:
            raise ValueError(f"No window {handle!r} in this session; open windows: {every}")
        return str(handle)
    if index is not None:
        if not 0 <= int(index) < len(every):
            raise ValueError(f"index must be 0-{len(every) - 1}; this session has {len(every)} window(s)")
        return every[int(index)]
    raise ValueError("Give handle (from tabs op='list' or new_tabs) or index.")


def _run_tabs(session_id: str, op: str, handle: str | None, index: int | None,
              timeout_seconds: float) -> dict[str, Any]:
    session = browser_tools._get_session(session_id)
    if not windows.uses_windows(session.driver):
        return {"success": False, "session_id": session_id, "op": op, "error": CURRENT_CHROME_NOTE}
    driver = session.driver
    with session.lock:
        before = windows.prime(session)
        fresh = windows.detect_new(session, before)
        returned = windows.recover_closed_current(session)
        answer: dict[str, Any] = {"success": True, "session_id": session_id, "op": op,
                                  **({"new_tabs": fresh} if fresh else {}),
                                  **({"tab_closed_by_page": returned} if returned else {})}
        if op == "list":
            rows = windows.listing(session)
            return {**answer, "tabs": rows, "count": len(rows),
                    "current_handle": windows.current_handle(driver)}
        target = _resolve(session, handle, index)
        if op == "switch":
            never_set_up = windows.switch(session, target)
            _prepare_tab(session, session_id, never_set_up, timeout_seconds)
            summary = browser_tools._page_summary(driver, session_id)
            return {**summary, **answer, "switched_to": target, "tabs": windows.listing(session)}
        if session.profile_mode == "attach" and target not in session.opened_windows:
            return {**answer, "success": False, "error": (
                "This is an attached browser: only windows its pages opened during this session "
                f"can be closed here, and {target} is not one of them.")}
        if len(windows.handles(driver)) <= 1:
            return {**answer, "success": False, "error": (
                "That is the session's last window; close the session instead (close).")}
        now = windows.close(session, target)
        summary = browser_tools._page_summary(driver, session_id) if now else {}
        return {**summary, **answer, "closed": target, "current_handle": now,
                "tabs": windows.listing(session)}


async def browser_session_tabs(
    session_id: str = "default",
    op: Literal["list", "switch", "close"] = "list",
    handle: str | None = None,
    index: int | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """List, switch to, or close the windows (tabs, popups) of this session's browser.

    Selenium sessions only (temporary/isolated/persistent/attach); the user's own
    Chrome tabs are web_info browser_tabs, attach_tab and close_tabs. switch moves
    the session to `handle` (or `index` from list) and waits up to timeout_seconds
    for its page; close closes one window, and a session whose own window closed
    goes back to the window that opened it. In attach mode only windows the pages
    opened during this session can be closed.
    """
    return await asyncio.to_thread(_run_tabs, session_id, op, handle, index, timeout_seconds)


ACTION_SPECS = (
    ("tabs", browser_session_tabs, "session",
     "List, switch to or close a Selenium session's tabs/popups."),
)

__all__ = ["ACTION_SPECS", "OBSERVED_STEPS", "browser_session_tabs", "observe_step", "observed"]
