"""Tab activity badge, busy-tab warnings, and the debugger-banner note.

Canned drivers only: no Chrome, no network.
"""

from __future__ import annotations

import threading
import time

import pytest

from web_search_neo import browser_tools


class _ProbeDriver:
    is_extension_bridge = False

    def __init__(self, probe=None):
        self.probe = dict(
            probe
            or {
                "url": "https://example.test/page",
                "title": "Fixture page",
                "viewport_width": 1440,
                "viewport_height": 900,
                "page_width": 1440,
                "page_height": 2000,
                "ready_state": "complete",
                "challenge": {},
            }
        )
        self.cdp_calls: list[tuple[str, dict]] = []
        self.scripts: list[str] = []

    def execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))
        if command == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "activity-script-1"}
        return {}

    def execute_script(self, script, *args):
        self.scripts.append(script)
        return dict(self.probe)

    def quit(self):
        return None


def _register(driver, session_id, **kwargs):
    kwargs.setdefault("headless", False)
    session = browser_tools.BrowserSession(driver=driver, **kwargs)
    browser_tools._sessions[session_id] = session
    return session


# --- activity badge lifecycle -------------------------------------------------


def test_activity_badge_installs_and_pings_live():
    driver = _ProbeDriver()
    session = _register(driver, "activity-install", profile_mode="current")
    browser_tools._apply_tab_activity(session, "activity-install")
    assert session.tab_activity_script_id == "activity-script-1"
    add_scripts = [
        params
        for command, params in driver.cdp_calls
        if command == "Page.addScriptToEvaluateOnNewDocument"
    ]
    assert len(add_scripts) == 1
    assert "__wsnActivity" in add_scripts[0]["source"]
    assert any("__wsnActivity" in script for script in driver.scripts)
    # Idempotent: a second apply while installed costs nothing.
    before = (len(driver.cdp_calls), len(driver.scripts))
    browser_tools._apply_tab_activity(session, "activity-install")
    assert (len(driver.cdp_calls), len(driver.scripts)) == before


def test_activity_badge_skipped_when_headless_or_opted_out():
    driver = _ProbeDriver()
    headless = _register(
        driver, "activity-headless", profile_mode="temporary", headless=True
    )
    browser_tools._apply_tab_activity(headless, "activity-headless")
    assert headless.tab_activity_script_id is None
    assert driver.cdp_calls == [] and driver.scripts == []

    driver2 = _ProbeDriver()
    opted_out = _register(driver2, "activity-optout", profile_mode="current")
    browser_tools._apply_tab_activity(opted_out, "activity-optout", label_tab=False)
    assert opted_out.tab_activity_script_id is None
    assert driver2.cdp_calls == []


def test_activity_ping_is_throttled_and_removal_restores():
    driver = _ProbeDriver()
    session = _register(driver, "activity-ping", profile_mode="current")
    browser_tools._apply_tab_activity(session, "activity-ping")

    def _pings():
        return [script for script in driver.scripts if "__wsnActivity" in script]

    pings_after_install = len(_pings())
    assert pings_after_install == 1
    # A fresh install just pinged: the summary right after adds no round-trip.
    browser_tools._page_summary(driver, "activity-ping")
    assert len(_pings()) == pings_after_install
    # An hour-old ping re-arms the badge exactly once, then throttles again.
    session.activity_pinged_at = time.monotonic() - 3600
    browser_tools._page_summary(driver, "activity-ping")
    assert len(_pings()) == pings_after_install + 1
    assert "__wsnActivity" in driver.scripts[-1]
    browser_tools._page_summary(driver, "activity-ping")
    assert len(_pings()) == pings_after_install + 1

    browser_tools._remove_tab_activity(session)
    assert session.tab_activity_script_id is None
    removals = [
        command
        for command, _ in driver.cdp_calls
        if command == "Page.removeScriptToEvaluateOnNewDocument"
    ]
    assert removals == ["Page.removeScriptToEvaluateOnNewDocument"]
    assert any("stop" in script for script in driver.scripts)


def test_badge_source_is_self_contained_and_defensive():
    source = browser_tools._TAB_ACTIVITY_SOURCE
    assert "setTimeout(restore, IDLE_MS)" in source or "setTimeout(restore" in source
    assert "5 * 60 * 1000" in source
    assert "delete window[KEY]" in source or "delete window" in source


# --- roster activity + busy warnings ------------------------------------------


def test_roster_marks_fresh_sessions_active_and_old_ones_idle():
    fresh = _register(_ProbeDriver(), "activity-fresh", profile_mode="current")
    stale = _register(_ProbeDriver(), "activity-stale", profile_mode="current")
    stale.last_used = time.monotonic() - 400
    overview = browser_tools.sessions_overview()
    rows = {row["session_id"]: row for row in overview["sessions"]}
    assert rows["activity-fresh"]["agent_active"] is True
    assert "activity_note" in rows["activity-fresh"]
    assert "favicon" in rows["activity-fresh"]["activity_note"]
    assert rows["activity-stale"]["agent_active"] is False
    assert "activity_note" not in rows["activity-stale"]
    assert fresh is not None


def test_shared_session_note_names_busy_and_read_only_path():
    driver = _ProbeDriver()
    session = _register(driver, "activity-shared", profile_mode="current")
    assert browser_tools._shared_session_note("activity-shared", session) == {}
    held: list[bool] = []
    release = threading.Event()

    def _hold():
        with session.lock:
            held.append(True)
            release.wait(timeout=10)

    worker = threading.Thread(target=_hold)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            time.sleep(0.01)
        assert held, "worker never took the session lock"
        note = browser_tools._shared_session_note("activity-shared", session)
    finally:
        release.set()
        worker.join(timeout=10)
    assert note["shared_session"] is True
    assert note["session_busy"] is True
    assert "read-only" in note["shared_session_warning"]
    assert "its own session_id" in note["shared_session_warning"]


def test_claim_refusal_tells_the_second_agent_to_open_its_own_tab():
    class _RefusingBridge:
        def claim_tab(self, tab_id, timeout=5.0):
            return {"status": "refused"}

        def release_tab(self, tab_id, timeout=5.0):
            return {"released": True}

    import web_search_neo.browser_tools as tools

    original = tools.get_chrome_bridge
    tools.get_chrome_bridge = lambda: _RefusingBridge()
    try:
        with pytest.raises(RuntimeError) as failure:
            tools._claim_tab(41)
    finally:
        tools.get_chrome_bridge = original
    assert "fresh session_id" in str(failure.value)


# --- debugger banner ----------------------------------------------------------


def test_companion_status_names_the_banner_and_its_silence():
    class _Bridge:
        def status(self, timeout=0.0):
            return {"connected": True, "browser": {"extension_version": "1.10.1"}}

    import web_search_neo.browser_tools as tools

    original = tools.get_chrome_bridge
    tools.get_chrome_bridge = lambda: _Bridge()
    try:
        status = tools._companion_status()
    finally:
        tools.get_chrome_bridge = original
    banner = status["debug_banner"]
    assert "--silent-debugger-extension-api" in banner["silence"]
    assert "Cancel" in banner["what"]


def test_companion_status_without_connection_has_no_banner():
    class _Bridge:
        def status(self, timeout=0.0):
            return {"connected": False}

    import web_search_neo.browser_tools as tools

    original = tools.get_chrome_bridge
    tools.get_chrome_bridge = lambda: _Bridge()
    try:
        status = tools._companion_status()
    finally:
        tools.get_chrome_bridge = original
    assert "debug_banner" not in status
