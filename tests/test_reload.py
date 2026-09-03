"""reload: canned-driver tests for the in-place page reload.

Before this the only reload was `open` on the same URL or `run_script` with
`location.reload()`; both are covered by what this action replaces.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from web_search_neo import browser_tools
from web_search_neo import main


class _ReloadDriver:
    is_extension_bridge = False

    def __init__(self, probe):
        self.probe = dict(probe)
        self.cdp_calls: list[tuple[str, dict]] = []
        self.refreshed = 0
        self.ready_probes = 0

    def execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))
        return {}

    def execute_script(self, script, *args):
        if script.strip() == "return document.readyState":
            self.ready_probes += 1
            return "complete"
        return dict(self.probe)

    def refresh(self):
        self.refreshed += 1

    def quit(self):
        return None


class _NoCdpDriver(_ReloadDriver):
    """A backend without CDP must still reload, via WebDriver's own refresh."""

    execute_cdp_cmd = None  # type: ignore[assignment]


_PROBE = {
    "url": "https://example.test/page",
    "title": "Fixture page",
    "viewport_width": 1440,
    "viewport_height": 900,
    "page_width": 1440,
    "page_height": 2000,
    "ready_state": "complete",
    "challenge": {},
}


def _register(driver, session_id="reload-case") -> None:
    browser_tools._sessions[session_id] = browser_tools.BrowserSession(
        driver=driver, headless=True
    )


def test_reload_returns_the_page_envelope_with_a_normal_reload_by_default():
    driver = _ReloadDriver(_PROBE)
    _register(driver)
    result = browser_tools.reload_page(session_id="reload-case", wait_seconds=0)
    assert driver.cdp_calls == [("Page.reload", {"ignoreCache": False})]
    assert driver.refreshed == 0
    assert result["session_id"] == "reload-case"
    assert result["url"] == "https://example.test/page"
    assert result["title"] == "Fixture page"
    assert result["ready_state"] == "complete"
    assert result["hard"] is False


def test_reload_hard_bypasses_the_cache():
    driver = _ReloadDriver(_PROBE)
    _register(driver, "reload-hard")
    result = browser_tools.reload_page(
        session_id="reload-hard", hard=True, wait_seconds=0
    )
    assert driver.cdp_calls == [("Page.reload", {"ignoreCache": True})]
    assert result["hard"] is True
    assert result["url"] == "https://example.test/page"


def test_reload_falls_back_to_refresh_without_cdp():
    driver = _NoCdpDriver(_PROBE)
    _register(driver, "reload-nocdp")
    result = browser_tools.reload_page(session_id="reload-nocdp", wait_seconds=0)
    assert driver.refreshed == 1
    assert result["url"] == "https://example.test/page"


def test_reload_waits_for_readiness():
    driver = _ReloadDriver(_PROBE)
    _register(driver, "reload-wait")
    browser_tools.reload_page(session_id="reload-wait", wait_seconds=0.1)
    assert driver.ready_probes >= 1


def test_reload_on_an_unknown_session_raises():
    with pytest.raises(ValueError, match="does not exist"):
        browser_tools.reload_page(session_id="reload-missing", wait_seconds=0)


def test_reload_is_registered_as_a_page_action():
    spec = main._ACTIONS["reload"]
    assert spec.group == "page"
    assert spec.handler is main.browser_reload
    assert "hard" in spec.summary
    params = inspect.signature(main.browser_reload).parameters
    assert set(params) == {"session_id", "hard", "wait_seconds"}


def test_reload_reaches_the_capabilities_and_actions_topics():
    document = asyncio.run(main.web_info("capabilities"))
    assert document["actions"]["reload"]["summary"] == main._ACTIONS["reload"].summary
    assert "reload" in document["action_groups"]["page"]
    index = asyncio.run(main.web_info("actions", {"group": "page"}))
    assert "reload" in index["actions"]
    assert "reload" in index["action_groups"]["page"]
