"""Agent presence: the favicon badge, the last-action flash, and the ghost cursor.

Three signals aimed at the person watching the browser rather than at the
caller. Every action a session runs pings the page: the tab's favicon gets a
badge while the agent is working and for five minutes after it stops, the
element the action touched flashes for a quarter of a second, and a ghost
cursor follows the agent's virtual pointer - synthetic CDP input that never
moves the operating system's mouse - with a fading ring on every press.

Neither may ever cost a caller an action, so the whole path swallows failures -
which is exactly why it needs tests: a signal that silently stops working looks
identical to an idle agent.
"""

from __future__ import annotations

import re

import asyncio

import pytest

from web_search_neo import agent_presence
from web_search_neo import browser_tools
from web_search_neo import main


class _PresenceDriver:
    """A driver double that records scripts and answers the ping deliberately.

    ``installed`` is the page's side of the contract: the ping script returns
    false until the installer has run in that document, and true afterwards.
    """

    is_extension_bridge = True

    def __init__(self, installed: bool = False, cdp: bool = True):
        self.installed = installed
        self.scripts: list[tuple[str, tuple]] = []
        self.cdp_calls: list[tuple[str, dict]] = []
        self._cdp = cdp
        if cdp:
            self.execute_cdp_cmd = self._execute_cdp_cmd  # type: ignore[assignment]

    def _execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))
        if command == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "presence-1"}
        return {}

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        if "presence.note(" in script:
            return self.installed
        if "typeof presence.restore" in script:
            return True
        self.installed = True
        return True

    def quit(self):
        return None


def _register(driver, session_id="presence-case", **kwargs) -> browser_tools.BrowserSession:
    session = browser_tools.BrowserSession(
        driver=driver, headless=False, profile_mode="temporary", **kwargs
    )
    browser_tools._sessions[session_id] = session
    return session


@pytest.fixture(autouse=True)
def _clean_sessions():
    yield
    for name in [key for key in browser_tools._sessions if key.startswith("presence-")]:
        browser_tools._sessions.pop(name, None)


def _presence_env(monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv(agent_presence.PRESENCE_ENV, raising=False)
    else:
        monkeypatch.setenv(agent_presence.PRESENCE_ENV, value)


# --- what one action says about itself --------------------------------------


def test_payload_prefers_the_selector_the_action_aimed_at():
    payload = agent_presence.payload_for(
        "click", {"selector": "#go", "text": "Send", "session_id": "s"}
    )
    assert payload["selector"] == "#go"
    assert payload["text"] == "Send"
    assert payload["action"] == "click"
    assert payload["flash"] is True


def test_payload_carries_coordinates_when_there_is_no_selector():
    payload = agent_presence.payload_for("pointer", {"x": 120, "y": 48.5})
    assert (payload["x"], payload["y"]) == (120.0, 48.5)
    assert "selector" not in payload


def test_payload_ignores_half_a_coordinate_pair():
    assert "x" not in agent_presence.payload_for("pointer", {"x": 10})


def test_payload_trims_a_long_text_target():
    payload = agent_presence.payload_for("click_text", {"text": "x" * 500})
    assert len(payload["text"]) == 80


def test_quiet_actions_ping_the_badge_but_do_not_flash():
    # A wait or a cookie read is still "the agent is in this tab", and still
    # not worth painting a box over the page for.
    for action in ("wait", "cookies", "close"):
        assert agent_presence.payload_for(action, {})["flash"] is False


def test_payload_reports_a_refused_action():
    assert agent_presence.payload_for("click", {}, ok=False)["ok"] is False


def test_payload_carries_the_owner_label():
    payload = agent_presence.payload_for("click", {}, label="ag-mail")
    assert payload["label"] == "ag-mail"


# --- the scripts themselves --------------------------------------------------


def test_the_installer_hides_everything_it_adds_from_the_reading_topics():
    # aria-hidden is the one marker page_text, page_outline and inspect all
    # skip, so an overlay can never turn up in what an agent reads back.
    assert 'setAttribute("aria-hidden", "true")' in agent_presence.install_source()


def test_the_installer_never_creates_a_stylesheet():
    # A page with a strict style-src CSP would drop a <style> element and leave
    # the overlay unstyled and opaque over the content.
    assert 'createElement("style")' not in agent_presence.install_source()


def test_the_installer_carries_this_build_s_timings():
    source = agent_presence.install_source()
    assert f'"recentMs": {5 * 60 * 1000}' in source
    assert f'"flashMs": {agent_presence.FLASH_MS}' in source
    assert agent_presence.FLASH_MS <= 300


def test_the_ping_refuses_a_stale_installation():
    # A page still running an older build must be reinstalled, not pinged.
    assert f"presence.version !== {agent_presence.PRESENCE_VERSION}" in (
        agent_presence.ping_script()
    )


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_the_environment_kill_switch(monkeypatch, value):
    _presence_env(monkeypatch, value)
    assert agent_presence.enabled() is False


# --- arming a session --------------------------------------------------------


def test_apply_registers_the_installer_for_every_future_document(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver()
    session = _register(driver)
    with session.lock:
        browser_tools._apply_agent_presence(session, "presence-case")
    assert session.presence_script_id == "presence-1"
    command, params = driver.cdp_calls[0]
    assert command == "Page.addScriptToEvaluateOnNewDocument"
    assert "__wsnPresence" in params["source"]


def test_apply_is_idempotent(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver()
    session = _register(driver)
    with session.lock:
        browser_tools._apply_agent_presence(session, "presence-case")
        browser_tools._apply_agent_presence(session, "presence-case")
    adds = [call for call in driver.cdp_calls if call[0].startswith("Page.addScript")]
    assert len(adds) == 1


def test_headless_sessions_are_left_alone(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver()
    session = _register(driver, "presence-headless")
    session.headless = True
    with session.lock:
        browser_tools._apply_agent_presence(session, "presence-headless")
    assert session.presence_script_id is None
    assert not [call for call in driver.cdp_calls if call[0].startswith("Page.addScript")]


def test_a_backend_without_cdp_is_not_an_error(monkeypatch):
    # The per-action ping installs the script itself; losing the registration
    # only costs it its free survival across a navigation.
    _presence_env(monkeypatch)
    driver = _PresenceDriver(cdp=False)
    session = _register(driver, "presence-nocdp")
    with session.lock:
        browser_tools._apply_agent_presence(session, "presence-nocdp")
    assert session.presence_script_id is None


# --- marking one action ------------------------------------------------------


def test_note_pings_a_page_that_already_has_the_script(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    _register(driver, agent_label="ag-mail")
    assert browser_tools.note_agent_activity(
        "presence-case", "click", {"selector": "#go"}
    )
    assert len(driver.scripts) == 1
    _script, args = driver.scripts[0]
    assert args[0]["selector"] == "#go"
    assert args[0]["label"] == "ag-mail"


def test_note_installs_the_script_when_the_document_has_none(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=False)
    _register(driver)
    assert browser_tools.note_agent_activity("presence-case", "click", {})
    # ping (false) -> install -> ping again
    assert len(driver.scripts) == 3
    assert "__wsnPresence" in driver.scripts[1][0]


def test_note_falls_back_to_the_session_id_as_the_label(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    _register(driver)
    browser_tools.note_agent_activity("presence-case", "click", {})
    assert driver.scripts[0][1][0]["label"] == "presence-case"


def test_note_says_nothing_to_an_unknown_session(monkeypatch):
    _presence_env(monkeypatch)
    assert browser_tools.note_agent_activity("presence-missing", "click", {}) is False


def test_note_never_raises_when_the_driver_is_gone(monkeypatch):
    _presence_env(monkeypatch)

    class _DeadDriver(_PresenceDriver):
        def execute_script(self, script, *args):
            raise RuntimeError("target closed")

    _register(_DeadDriver(), "presence-dead")
    assert browser_tools.note_agent_activity("presence-dead", "click", {}) is False


def test_note_honours_the_kill_switch(monkeypatch):
    _presence_env(monkeypatch, "0")
    driver = _PresenceDriver(installed=True)
    _register(driver)
    assert browser_tools.note_agent_activity("presence-case", "click", {}) is False
    assert driver.scripts == []


def test_a_stepped_page_is_left_alone(monkeypatch):
    # Step and render modes freeze the clock the fade and the removal run on,
    # and step is called once per frame.
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    session = _register(driver, "presence-stepped")
    session.render_mode = "step"
    assert browser_tools.note_agent_activity("presence-stepped", "click", {}) is False
    assert driver.scripts == []


def test_actions_that_draw_nothing_are_throttled(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    _register(driver, "presence-quiet")
    assert browser_tools.note_agent_activity("presence-quiet", "wait", {}) is True
    assert browser_tools.note_agent_activity("presence-quiet", "wait", {}) is False
    # A step that has something to show is never throttled.
    assert browser_tools.note_agent_activity("presence-quiet", "click", {}) is True
    assert len(driver.scripts) == 2


def test_note_skips_headless(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    session = _register(driver, "presence-headless2")
    session.headless = True
    assert browser_tools.note_agent_activity("presence-headless2", "click", {}) is False


# --- staying out of what the agent reads back --------------------------------


def test_a_screenshot_clears_the_overlays_first(monkeypatch):
    # Otherwise a capture taken inside the 250 ms flash hands the agent a
    # picture of a box its own click drew, to be read as the page's doing.
    _presence_env(monkeypatch)

    class _ShotDriver(_PresenceDriver):
        def get_screenshot_as_png(self):
            return b"png-bytes"

    driver = _ShotDriver(installed=True)
    _register(driver, "presence-shot")
    assert browser_tools.screenshot("presence-shot") == b"png-bytes"
    assert any("hideFlashes" in script for script, _args in driver.scripts)


# --- giving the tab back -----------------------------------------------------


def test_remove_restores_the_favicon_and_drops_the_registration(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    session = _register(driver)
    with session.lock:
        browser_tools._apply_agent_presence(session, "presence-case")
    browser_tools.note_agent_activity("presence-case", "click", {})
    with session.lock:
        browser_tools._remove_agent_presence(session)
    assert session.presence_script_id is None
    assert (
        "Page.removeScriptToEvaluateOnNewDocument",
        {"identifier": "presence-1"},
    ) in driver.cdp_calls
    assert any("restore" in script for script, _args in driver.scripts)


def test_a_tab_that_was_never_marked_is_handed_back_untouched(monkeypatch):
    # _leave_claimed_tab gives the user's tab back. A session that armed the
    # script but never painted anything has nothing to undo, and running a
    # script in that tab on the way out is itself touching it.
    _presence_env(monkeypatch)
    driver = _PresenceDriver()
    session = _register(driver)
    with session.lock:
        browser_tools._apply_agent_presence(session, "presence-case")
        browser_tools._remove_agent_presence(session)
    assert driver.scripts == []


def test_a_handed_back_tab_is_cleaned(monkeypatch):
    # _clear_injected_state runs for a borrowed tab that outlives the session;
    # a badge left on it would claim an agent is still working in the user's tab.
    _presence_env(monkeypatch)
    driver = _PresenceDriver()
    session = _register(driver)
    with session.lock:
        browser_tools._apply_agent_presence(session, "presence-case")
        browser_tools._clear_injected_state(session)
    assert session.presence_script_id is None


# --- wiring into the dispatcher ---------------------------------------------


def test_every_action_with_a_session_is_marked(monkeypatch):
    # Hooked in the dispatcher, not in each handler: a signal that is only as
    # complete as the last handler someone instrumented would make the tab look
    # idle during whichever actions were forgotten.
    seen: list[tuple] = []
    monkeypatch.setattr(
        browser_tools,
        "note_agent_activity",
        lambda *args: seen.append(args) or True,
    )
    asyncio.run(main._execute_actions([{"action": "close", "session_id": "presence-none"}]))
    assert seen == [("presence-none", "close", {"session_id": "presence-none"}, True)]


def test_a_refused_action_is_marked_as_a_failure(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        browser_tools,
        "note_agent_activity",
        lambda *args: seen.append(args) or True,
    )
    asyncio.run(
        main._execute_actions([{"action": "click", "session_id": "presence-missing"}])
    )
    assert len(seen) == 1
    assert seen[0][0] == "presence-missing"
    assert seen[0][3] is False


def test_an_action_without_a_session_is_not_marked(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        browser_tools,
        "note_agent_activity",
        lambda *args: seen.append(args) or True,
    )
    asyncio.run(main._execute_actions([{"action": "fetch_text", "url": "not a url"}]))
    assert seen == []


def test_a_broken_signal_never_fails_the_action(monkeypatch):
    def _explode(*_args):
        raise RuntimeError("presence is broken")

    monkeypatch.setattr(browser_tools, "note_agent_activity", _explode)
    result = asyncio.run(
        main._execute_actions([{"action": "close", "session_id": "presence-none"}])
    )
    assert result["success"] is True


def test_favicon_mark_is_a_translucent_slime_over_the_page_icon():
    source = agent_presence.install_source()
    # The page's own icon is drawn first and the slime goes on top of it in the
    # bottom-right quarter, partly see-through: the tab keeps its identity.
    assert "context.drawImage(baseImage, 0, 0, 32, 32)" in source
    assert "context.translate(12, 12)" in source
    assert "context.scale(1.25, 1.25)" in source
    alphas = [float(value) for value in re.findall(r"globalAlpha = active \? ([\d.]+) : ([\d.]+)", source)[0]]
    assert all(0 < alpha < 1 for alpha in alphas)
    # An unreadable (tainted) favicon is left alone instead of being replaced by
    # an opaque tile, and the older activity dot defers to this script.
    assert "#20272e" not in source
    assert "window.__wsnPresence" in browser_tools._TAB_ACTIVITY_SOURCE


# --- the ghost cursor ----------------------------------------------------------


def test_cursor_payload_follows_coordinate_actions():
    payload = agent_presence.payload_for(
        "pointer", {"action": "move", "x": 10, "y": 20}, pointer=(110.0, 48.0)
    )
    assert payload["cursor"] == {"x": 110.0, "y": 48.0, "tap": False}
    acquire = agent_presence.payload_for(
        "pointer_lock", {"action": "acquire"}, pointer=(5.0, 6.0)
    )
    assert acquire["cursor"] == {"x": 5.0, "y": 6.0, "tap": True}


def test_cursor_payload_marks_presses_but_not_key_batches():
    click = agent_presence.payload_for(
        "pointer", {"action": "click", "x": 1, "y": 2}, pointer=(5.0, 6.0)
    )
    assert click["cursor"]["tap"] is True
    batch = agent_presence.payload_for(
        "input",
        {"pointer_actions": [
            {"action": "hover", "x": 1, "y": 1},
            {"action": "press", "x": 2, "y": 2},
        ]},
        pointer=(2.0, 2.0),
    )
    assert batch["cursor"] == {"x": 2.0, "y": 2.0, "tap": True}
    keys = agent_presence.payload_for(
        "input", {"key_actions": [{"key": "a", "action": "tap"}]}, pointer=(2.0, 2.0)
    )
    assert "cursor" not in keys


def test_cursor_payload_ignores_reads_and_dom_clicks():
    # A synthetic DOM click moves no pointer - nor would the OS cursor - so the
    # cursor stays where the last coordinate action left it.
    assert "cursor" not in agent_presence.payload_for(
        "click", {"selector": "#go"}, pointer=(1.0, 2.0)
    )
    assert "cursor" not in agent_presence.payload_for(
        "press_keys", {"keys": ["a"]}, pointer=(1.0, 2.0)
    )
    assert "cursor" not in agent_presence.payload_for(
        "pointer_lock", {"action": "status"}, pointer=(1.0, 2.0)
    )
    assert "cursor" not in agent_presence.payload_for("pointer", {"action": "move"})


def test_cursor_payload_refuses_non_finite_positions():
    assert "cursor" not in agent_presence.payload_for(
        "pointer", {"action": "move"}, pointer=(float("inf"), 1.0)
    )
    assert "cursor" not in agent_presence.payload_for(
        "pointer", {"action": "move"}, pointer=(float("nan"), 1.0)
    )


def test_the_installer_draws_a_ghost_cursor_and_press_rings():
    source = agent_presence.install_source()
    assert 'setAttribute("data-wsn-presence", "cursor")' in source
    assert 'setAttribute("data-wsn-presence", "ripple")' in source
    # An inline SVG arrow: no external image a page CSP could refuse.
    assert "createElementNS" in source
    assert "moveCursor" in source
    assert "showEphemeral" in source


def test_the_installer_carries_the_cursor_timings():
    source = agent_presence.install_source()
    assert f'"glideMs": {agent_presence.CURSOR_GLIDE_MS}' in source
    assert f'"rippleMs": {agent_presence.RIPPLE_MS}' in source
    assert f'"chipMs": {agent_presence.CURSOR_CHIP_MS}' in source
    assert agent_presence.RIPPLE_MS <= 1000


def test_note_moves_the_cursor_after_a_pointer_action(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    session = _register(driver, "presence-cursor")
    session.pointer_x, session.pointer_y = 111.0, 47.0
    assert browser_tools.note_agent_activity(
        "presence-cursor", "pointer", {"action": "click", "x": 1, "y": 2}
    )
    assert driver.scripts[0][1][0]["cursor"] == {"x": 111.0, "y": 47.0, "tap": True}


def test_note_leaves_the_cursor_alone_for_dom_clicks(monkeypatch):
    _presence_env(monkeypatch)
    driver = _PresenceDriver(installed=True)
    session = _register(driver, "presence-cursor-dom")
    session.pointer_x, session.pointer_y = 111.0, 47.0
    assert browser_tools.note_agent_activity(
        "presence-cursor-dom", "click", {"selector": "#go"}
    )
    assert "cursor" not in driver.scripts[0][1][0]


def test_a_screenshot_brings_the_cursor_back(monkeypatch):
    # The capture hides the cursor so the agent never photographs its own
    # arrow, then shows it again: hiding without showing would strand the tab
    # cursorless until the next pointer action.
    _presence_env(monkeypatch)

    class _ShotDriver(_PresenceDriver):
        def get_screenshot_as_png(self):
            return b"png-bytes"

    driver = _ShotDriver(installed=True)
    _register(driver, "presence-shot-cursor")
    assert browser_tools.screenshot("presence-shot-cursor") == b"png-bytes"
    kinds = [script for script, _args in driver.scripts]
    assert any("hideFlashes" in script for script in kinds)
    assert any("showEphemeral" in script for script in kinds)
    assert max(
        index for index, script in enumerate(kinds) if "showEphemeral" in script
    ) > max(index for index, script in enumerate(kinds) if "hideFlashes" in script)
