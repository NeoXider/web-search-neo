"""The windows (tabs and popups) of one Selenium-driven session's browser.

A click on a ``target=_blank`` link or a page calling ``window.open`` gives the
browser a new window handle. chromedriver keeps driving the window it was on,
so without this module the new page is invisible to the session: it is neither
reported nor reachable, and an owned browser keeps it open until the session ends.

Everything here takes the session and talks to its driver only; the MCP
wrappers live in ``tab_actions.py``. The user's own Chrome (current mode) has no
window handles - its tabs are the companion's business - and is never touched.

chromedriver's window handles are Chrome's target ids, so ``Target.getTargets``
describes every window (URL, title, opener) without switching to it. Where that
is refused, the fallback switches to each window and back.
"""
from __future__ import annotations

import time
from typing import Any

_BLANK_URLS = ("", "about:blank")


def uses_windows(driver: Any) -> bool:
    """A Selenium driver of a browser this session reaches directly (not the companion)."""
    return not getattr(driver, "is_extension_bridge", False) and hasattr(driver, "window_handles")


def handles(driver: Any) -> list[str]:
    return [str(handle) for handle in (driver.window_handles or [])]


def current_handle(driver: Any) -> str | None:
    """The window chromedriver drives, or ``None`` when that window was closed."""
    try:
        return str(driver.current_window_handle)
    except Exception:
        return None


def _targets(driver: Any) -> dict[str, dict[str, Any]]:
    try:
        infos = (driver.execute_cdp_cmd("Target.getTargets", {}) or {}).get("targetInfos") or []
    except Exception:
        return {}
    return {str(info.get("targetId")): info for info in infos if info.get("type") == "page"}


def _read_by_switching(driver: Any, wanted: list[str]) -> dict[str, dict[str, Any]]:
    back = current_handle(driver)
    found: dict[str, dict[str, Any]] = {}
    try:
        for handle in wanted:
            try:
                driver.switch_to.window(handle)
                found[handle] = {"url": driver.current_url, "title": driver.title}
            except Exception:
                continue
    finally:
        if back:
            try:
                driver.switch_to.window(back)
            except Exception:
                pass
    return found


def describe(driver: Any, wanted: list[str], settle_seconds: float = 0.0) -> dict[str, dict[str, Any]]:
    """URL, title and opener of each handle in ``wanted``.

    A window a page has just opened shows ``about:blank`` until its first
    document commits; ``settle_seconds`` waits that out (a window opened on
    about:blank on purpose simply costs the wait).
    """
    deadline = time.monotonic() + max(0.0, settle_seconds)
    while True:
        targets = _targets(driver)
        if not targets:
            return {handle: {**info, "opener": None}
                    for handle, info in _read_by_switching(driver, wanted).items()}
        pending = [h for h in wanted if str((targets.get(h) or {}).get("url") or "") in _BLANK_URLS]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    return {handle: {"url": targets[handle].get("url"), "title": targets[handle].get("title"),
                     "opener": targets[handle].get("openerId") or None}
            for handle in wanted if handle in targets}


def prime(session: Any) -> str | None:
    """Record the windows the session already knows, once; return the current handle.

    An owned browser holds nothing but this session's pages, so only the window
    it drives is known and every other one is a tab its pages opened. An attached
    browser is somebody else's: all of its windows count as known.
    """
    driver = session.driver
    current = current_handle(driver)
    if session.known_windows is None:
        session.known_windows = (set(handles(driver)) if session.profile_mode == "attach"
                                 else {current} if current else set())
    return current


def detect_new(session: Any, opener_hint: str | None, settle_seconds: float = 1.5) -> list[dict[str, Any]]:
    """Windows that appeared since the session last looked, described and remembered."""
    if session.known_windows is None:
        prime(session)
        return []
    driver = session.driver
    fresh = [handle for handle in handles(driver) if handle not in session.known_windows]
    if not fresh:
        return []
    session.known_windows.update(fresh)
    session.opened_windows.extend(handle for handle in fresh if handle not in session.opened_windows)
    infos = describe(driver, fresh, settle_seconds)
    rows = []
    for handle in fresh:
        info = infos.get(handle) or {}
        opener = info.get("opener") or opener_hint
        if opener:
            session.window_openers[handle] = str(opener)
        rows.append({"handle": handle, "url": info.get("url"), "title": info.get("title"),
                     "opener": opener})
    return rows


def listing(session: Any) -> list[dict[str, Any]]:
    """Every window of the session's browser, in chromedriver's order."""
    driver = session.driver
    every = handles(driver)
    current = current_handle(driver)
    infos = describe(driver, every)
    return [{"index": index, "handle": handle, "url": (infos.get(handle) or {}).get("url"),
             "title": (infos.get(handle) or {}).get("title"),
             "opener": (infos.get(handle) or {}).get("opener") or session.window_openers.get(handle),
             "current": handle == current, "opened_by_page": handle in session.opened_windows}
            for index, handle in enumerate(every)]


def _save_state(session: Any, handle: str | None) -> None:
    if handle:
        session.window_state[handle] = {"dialog_script_id": session.dialog_script_id,
                                        "render_bootstrap_registered": session.render_bootstrap_registered}


def _load_state(session: Any, handle: str) -> bool:
    """Restore ``handle``'s script record; true when that window was never set up."""
    state = session.window_state.get(handle)
    session.dialog_script_id = (state or {}).get("dialog_script_id")
    session.render_bootstrap_registered = bool((state or {}).get("render_bootstrap_registered"))
    return state is None


def switch(session: Any, handle: str) -> bool:
    """Drive ``handle`` from now on; true when that window had never been set up.

    The new-document scripts a session registers (dialog answerer, render gate,
    console hook) belong to one window, so each window keeps its own record of them.
    """
    driver = session.driver
    _save_state(session, current_handle(driver))
    driver.switch_to.window(handle)
    return _load_state(session, handle)


def fallback_handle(session: Any, closed: str, remaining: list[str]) -> str | None:
    """Where a session goes when its window closed: the window that opened it, else the first."""
    opener = session.window_openers.get(closed)
    return opener if opener in remaining else (remaining[0] if remaining else None)


def close(session: Any, handle: str) -> str | None:
    """Close one window; returns the handle the session drives afterwards."""
    driver = session.driver
    current = current_handle(driver)
    _save_state(session, current)
    driver.switch_to.window(handle)
    driver.close()
    session.window_state.pop(handle, None)
    remaining = handles(driver)
    target = current if current and current != handle and current in remaining else fallback_handle(
        session, handle, remaining)
    if target:
        driver.switch_to.window(target)
        _load_state(session, target)
    return target


def recover_closed_current(session: Any) -> dict[str, Any] | None:
    """The page closed the window the session drove (a popup finishing): go back to its opener."""
    driver = session.driver
    if session.known_windows is None or current_handle(driver) is not None:
        return None
    remaining = handles(driver)
    gone = next((h for h in session.known_windows if h not in remaining and h in session.window_openers), None)
    target = fallback_handle(session, gone, remaining) if gone else (remaining[0] if remaining else None)
    if not target:
        return None
    driver.switch_to.window(target)
    _load_state(session, target)
    return {"closed": gone, "returned_to": target}


def close_opened(session: Any) -> list[str]:
    """Close the windows pages opened during the session; what could not be closed.

    For a browser the session does not own (attach), where ending the session
    stops only chromedriver: the tabs its pages opened would otherwise stay in
    somebody else's browser. An owned browser quits with every window anyway.
    """
    driver = session.driver
    problems: list[str] = []
    if not session.opened_windows or not uses_windows(driver):
        return problems
    try:
        alive = set(handles(driver))
    except Exception as exc:
        return [f"the windows could not be listed ({type(exc).__name__})"]
    for handle in [h for h in session.opened_windows if h in alive]:
        try:
            driver.switch_to.window(handle)
            driver.close()
        except Exception as exc:
            problems.append(f"window {handle} could not be closed ({type(exc).__name__})")
    session.opened_windows = []
    return problems
