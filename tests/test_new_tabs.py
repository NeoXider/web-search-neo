"""New tabs and popups of Selenium sessions (G10), live in headless Chrome.

A click on ``target=_blank`` or a page calling ``window.open`` gives the browser a
new window that chromedriver never switches to. These tests drive real pages from
``tests/fixtures/tabs/`` through ``web_action`` and check what the agent is told
(``new_tabs``), where the session goes (``follow_new_tab``, ``tabs switch``), what
is closed (``tabs close``, a popup that closes itself, the session itself) and that
no chromedriver or Chrome process of a closed session is left running.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools, main, process_probe, tab_actions
from web_search_neo.sessions import process_tree, windows


def _run(*steps: dict) -> list[dict]:
    answer = asyncio.run(main.web_action(list(steps), continue_on_error=True))
    return answer["results"]


def _one(step: dict) -> dict:
    result = _run(step)[0]
    assert result["success"], result.get("error")
    return result["data"]


def _open(local_site, session_id: str, **extra) -> dict:
    url = f"{local_site.base_url}/fixtures/tabs/opener.html"
    result = _run({"action": "open", "url": url, "session_id": session_id,
                   "profile_mode": "temporary", "headless": True, **extra})[0]
    if not result["success"]:
        pytest.skip(f"Chrome/Selenium is unavailable: {result.get('error')}")
    return result["data"]


def _tree(pid: int) -> dict[int, tuple[str, str | None]]:
    """chromedriver and every real descendant: pid -> (executable, start stamp).

    Strangers behind a reused parent pid (older than their listed parent) are
    left out, as the server itself leaves them out when it kills a tree.
    """
    names = {pid: "chromedriver"}
    frontier = [pid]
    while frontier:
        for child, name in process_tree.children(frontier.pop()):
            if child not in names:
                names[child] = name
                frontier.append(child)
    return {member: (names[member], process_tree.started_at(member)) for member in names}


def _wait_dead(family: dict[int, tuple[str, str | None]], seconds: float = 10.0) -> set[int]:
    """The members still running as the same process (a pid handed to a new program does not count)."""
    def running() -> set[int]:
        return {pid for pid, (_name, stamp) in family.items()
                if (process_tree.still_running(pid, stamp) if stamp else process_probe.process_alive(pid))}
    deadline = time.monotonic() + seconds
    alive = running()
    while alive and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = running()
    return alive


def test_a_blank_target_link_is_reported_switched_to_read_and_closed(local_site):
    _open(local_site, "tabs-link")
    session = browser_tools._sessions["tabs-link"]
    opener = session.driver.current_window_handle

    clicked = _one({"action": "click", "selector": "#blank-link", "session_id": "tabs-link"})
    [tab] = clicked["new_tabs"]
    assert tab["url"].endswith("/fixtures/tabs/child.html?via=link")
    assert tab["title"] == "Tabs child" and tab["opener"] == opener
    # The click had an effect even though its own page did not change.
    assert clicked["effect_detected"] is True and clicked["no_observable_change"] is False
    assert clicked["url"].endswith("/opener.html"), "the session stays on the opener"

    # Reported once: the next action does not report the same tab again.
    again = _one({"action": "click", "selector": "#blank-link", "session_id": "tabs-link"})
    assert [row["handle"] for row in again["new_tabs"]] != [tab["handle"]]

    listed = _one({"action": "tabs", "session_id": "tabs-link"})
    assert listed["count"] == 3 and listed["current_handle"] == opener
    assert {row["handle"]: row["opened_by_page"] for row in listed["tabs"]}[tab["handle"]] is True

    switched = _one({"action": "tabs", "op": "switch", "handle": tab["handle"], "session_id": "tabs-link"})
    assert switched["switched_to"] == tab["handle"] and switched["title"] == "Tabs child"
    read = _one({"action": "run_script", "script": "return location.href", "session_id": "tabs-link"})
    assert read["value"].endswith("/child.html?via=link")

    closed = _one({"action": "tabs", "op": "close", "handle": tab["handle"], "session_id": "tabs-link"})
    assert closed["closed"] == tab["handle"] and closed["current_handle"] == opener
    assert closed["url"].endswith("/opener.html")
    assert tab["handle"] not in session.driver.window_handles
    assert _one({"action": "tabs", "session_id": "tabs-link"})["count"] == 2


def test_follow_new_tab_moves_the_session_to_a_window_open_page(local_site):
    _open(local_site, "tabs-follow")
    clicked = _one({"action": "click", "selector": "#open-window", "session_id": "tabs-follow",
                    "follow_new_tab": True})
    [tab] = clicked["new_tabs"]
    assert clicked["followed_new_tab"] == tab["handle"]
    assert clicked["tab"]["url"].endswith("/child.html?via=window-open")
    text = _one({"action": "run_script", "script": "return document.getElementById('via').textContent",
                 "session_id": "tabs-follow"})
    assert text["value"] == "?via=window-open"

    back = _one({"action": "tabs", "op": "switch", "index": 0, "session_id": "tabs-follow"})
    assert back["url"].endswith("/opener.html")
    by_text = _one({"action": "click_text", "text": "Open in new tab", "session_id": "tabs-follow",
                    "follow_new_tab": True})
    assert by_text["followed_new_tab"] == by_text["new_tabs"][0]["handle"]
    assert by_text["tab"]["url"].endswith("/child.html?via=link")

    # Nothing opened: the session stays and says why.
    nothing = _one({"action": "click", "selector": "#child-marker", "session_id": "tabs-follow",
                    "follow_new_tab": True})
    assert nothing["followed_new_tab"] is None and "No new tab" in nothing["follow_note"]


def test_scripts_and_waits_report_tabs_too(local_site):
    _open(local_site, "tabs-script")
    opened = _one({"action": "run_script", "session_id": "tabs-script",
                   "script": "window.open('/fixtures/tabs/child.html?via=script'); return 1"})
    assert opened["new_tabs"][0]["url"].endswith("?via=script")
    late = _one({"action": "run_script", "session_id": "tabs-script",
                 "script": "setTimeout(() => window.open('/fixtures/tabs/child.html?via=late'), 200); return 1"})
    waited = _one({"action": "wait", "seconds": 1, "session_id": "tabs-script"})
    # The 200 ms timer races the end of run_script: the tab is reported by whichever action saw it.
    reported = [tab["url"] for answer in (late, waited) for tab in answer.get("new_tabs") or []]
    assert sum(url.endswith("?via=late") for url in reported) == 1, reported


def test_a_popup_that_closes_itself_hands_the_session_back(local_site):
    _open(local_site, "tabs-closer")
    session = browser_tools._sessions["tabs-closer"]
    opener = session.driver.current_window_handle
    followed = _one({"action": "click", "selector": "#open-closer", "session_id": "tabs-closer",
                     "follow_new_tab": True})
    popup = followed["followed_new_tab"]
    assert followed["tab"]["title"] == "Tabs closer"
    finished = _one({"action": "click", "selector": "#finish", "session_id": "tabs-closer"})
    assert finished["tab_closed_by_page"] == {"closed": popup, "returned_to": opener}
    assert finished["url"].endswith("/opener.html")
    assert _one({"action": "run_script", "script": "return document.title",
                 "session_id": "tabs-closer"})["value"] == "Tabs opener"


def test_the_last_window_is_not_closed_and_unknown_handles_are_named(local_site):
    _open(local_site, "tabs-last")
    only = _run({"action": "tabs", "op": "close", "index": 0, "session_id": "tabs-last"})[0]
    assert only["success"] is False and "last window" in only["error"]
    unknown = _run({"action": "tabs", "op": "switch", "handle": "nope", "session_id": "tabs-last"})[0]
    assert unknown["success"] is False and "No window 'nope'" in unknown["error"]


def test_closing_the_session_closes_its_tabs_and_leaves_no_process(local_site):
    _open(local_site, "tabs-gone")
    _one({"action": "click", "selector": "#blank-link", "session_id": "tabs-gone"})
    _one({"action": "click", "selector": "#open-window", "session_id": "tabs-gone", "follow_new_tab": True})
    session = browser_tools._sessions["tabs-gone"]
    assert len(session.driver.window_handles) == 3
    family = _tree(session.driver.service.process.pid)
    assert sum("chrome" in name.lower() for name, _stamp in family.values()) >= 2, family  # Chrome and children
    closed = _one({"action": "close", "session_id": "tabs-gone"})
    assert closed["closed"] is True
    assert not _wait_dead(family), "a chromedriver/Chrome process of the closed session is still running"


def test_an_attached_browser_loses_only_the_tabs_its_pages_opened(local_site):
    owner = _open(local_site, "tabs-owner")
    assert owner["profile_mode"] == "temporary"
    host = browser_tools._sessions["tabs-owner"]
    address = (host.driver.capabilities.get("goog:chromeOptions") or {}).get("debuggerAddress")
    try:
        attached = _run({"action": "open", "url": f"{local_site.base_url}/fixtures/tabs/opener.html",
                         "session_id": "tabs-guest", "profile_mode": "attach",
                         "debugger_address": address.replace("localhost", "127.0.0.1")})[0]
    except WebDriverException as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"attach is unavailable: {exc}")
    assert attached["success"], attached.get("error")
    clicked = _one({"action": "click", "selector": "#blank-link", "session_id": "tabs-guest"})
    [tab] = clicked["new_tabs"]
    refused = _run({"action": "tabs", "op": "close", "index": 0, "session_id": "tabs-guest"})[0]
    assert refused["success"] is False and "attached browser" in refused["error"]
    assert tab["handle"] in host.driver.window_handles
    _one({"action": "close", "session_id": "tabs-guest"})
    deadline = time.monotonic() + 5
    while tab["handle"] in host.driver.window_handles and time.monotonic() < deadline:
        time.sleep(0.1)
    assert tab["handle"] not in host.driver.window_handles, "the tab the guest's page opened is closed"
    assert len(host.driver.window_handles) == 1, "the host browser and its own window stay"


def test_current_chrome_sessions_are_refused_by_name_and_never_observed():
    class _Companion:
        is_extension_bridge = True

    session = browser_tools.BrowserSession(driver=_Companion(), headless=False, profile_mode="current")
    with browser_tools._sessions_lock:
        browser_tools._sessions["tabs-current"] = session
    try:
        refused = asyncio.run(tab_actions.browser_session_tabs("tabs-current"))
        assert refused["success"] is False and "browser_tabs" in refused["error"]
        assert not windows.uses_windows(session.driver)

        async def work():
            return {"success": True}

        answer = asyncio.run(tab_actions.observed("tabs-current", True, work()))
        assert answer["followed_new_tab"] is None and "attach_tab" in answer["follow_note"]
        assert session.known_windows is None
    finally:
        with browser_tools._sessions_lock:
            browser_tools._sessions.pop("tabs-current", None)


def test_attach_mode_knows_every_window_it_found_and_closes_only_its_own():
    class _Switch:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.current = handle

    class _Driver:
        def __init__(self):
            self.window_handles = ["user-1", "user-2"]
            self.current = "user-1"
            self.switch_to = _Switch(self)

        @property
        def current_window_handle(self):
            return self.current

        def close(self):
            self.window_handles.remove(self.current)

        def execute_cdp_cmd(self, *_args):
            raise WebDriverException("no CDP here")

        current_url, title = "about:blank", ""

    session = SimpleNamespace(driver=_Driver(), profile_mode="attach", known_windows=None,
                              window_openers={}, opened_windows=[], window_state={},
                              dialog_script_id=None, render_bootstrap_registered=False)
    assert windows.prime(session) == "user-1"
    assert session.known_windows == {"user-1", "user-2"}
    session.driver.window_handles.append("page-opened")
    assert [row["handle"] for row in windows.detect_new(session, "user-1", 0)] == ["page-opened"]
    assert windows.close_opened(session) == []
    assert session.driver.window_handles == ["user-1", "user-2"]
