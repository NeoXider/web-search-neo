"""What a session leaves behind when it ends badly.

Teardown runs when something has already gone wrong, so it is the least
exercised code here and the easiest place to report a clean result over a tab
that is still open, a debugger that is still attached, or a session that was
never usable in the first place.
"""

from __future__ import annotations

import pytest

from chrome_bridge import ChromeBridgeError
import browser_tools


class _SwitchTo:
    def default_content(self) -> None:
        return None


class _Bridge:
    """The companion, answering the one question eviction asks it."""

    def __init__(self, gone: bool = False, reachable: bool = True) -> None:
        self.gone = gone
        self.reachable = reachable
        self.asked = 0

    def request(self, method: str, params: dict | None = None, **_kwargs: object) -> dict:
        self.asked += 1
        if not self.reachable:
            raise ConnectionError("no daemon on 127.0.0.1:8765")
        if self.gone:
            raise ChromeBridgeError(f"No tab with id {(params or {}).get('tabId')}")
        return {"id": (params or {}).get("tabId"), "url": "https://example.test/"}


class _Tab:
    def __init__(self, tab_id: int = 42, bridge: _Bridge | None = None) -> None:
        self.tab_id = tab_id
        self.bridge = bridge or _Bridge()
        self.actual_tab_group = browser_tools.DEFAULT_TAB_GROUP
        self.is_extension_bridge = True
        self.switch_to = _SwitchTo()
        self.calls: list[str] = []
        self.closes = True
        self.detaches = True

    def get(self, url: str) -> None:
        self.calls.append(f"get {url}")

    def quit(self) -> None:
        self.calls.append("quit")
        if not self.detaches:
            raise ChromeBridgeError("the companion never answered debugger.detach")

    def close_tab(self) -> dict[str, bool]:
        self.calls.append("close_tab")
        return {"removed": self.closes}

    def execute_script(self, script: str, *_args: object) -> object:
        if script.strip() == "return document.readyState":
            return "complete"
        return {"url": "https://example.test/", "title": "Page"}

    def execute_cdp_cmd(self, *_args: object, **_kwargs: object) -> dict:
        return {}


def _register(session_id: str, driver: _Tab, **overrides) -> browser_tools.BrowserSession:
    fields = {
        "driver": driver,
        "headless": False,
        "profile_mode": "current",
        "current_tab_id": driver.tab_id,
        "owns_browser": True,
        "owns_tab": True,
    }
    fields.update(overrides)
    session = browser_tools.BrowserSession(**fields)
    browser_tools._sessions[session_id] = session
    return session


def test_a_claim_that_fails_halfway_leaves_no_session_behind(monkeypatch):
    driver = _Tab()
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: driver)
    monkeypatch.setattr(
        browser_tools,
        "_register_render_bootstrap",
        lambda _session: (_ for _ in ()).throw(ChromeBridgeError("debugger.attach failed")),
    )

    with pytest.raises(ChromeBridgeError):
        browser_tools.attach_current_tab(42, session_id="reader")

    # Otherwise it holds the tab against every other session while answering none.
    assert "reader" not in browser_tools._sessions
    assert "quit" in driver.calls


def test_a_claimed_tab_is_not_closed_when_the_claim_fails(monkeypatch):
    driver = _Tab()
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: driver)
    monkeypatch.setattr(
        browser_tools,
        "_register_render_bootstrap",
        lambda _session: (_ for _ in ()).throw(ChromeBridgeError("debugger.attach failed")),
    )

    with pytest.raises(ChromeBridgeError):
        browser_tools.attach_current_tab(42, session_id="reader")

    assert "close_tab" not in driver.calls


def test_close_says_so_when_the_tab_would_not_close():
    driver = _Tab()
    driver.closes = False
    _register("stuck", driver)

    result = browser_tools.close_session("stuck")

    assert result["closed"] is True
    assert result["tab_closed"] is False
    assert result["released"] is False
    assert "still open" in result["warning"]


def test_close_says_so_when_the_debugger_stays_attached():
    driver = _Tab()
    driver.detaches = False
    _register("attached", driver)

    result = browser_tools.close_session("attached")

    assert result["released"] is False
    assert "detaching from Chrome failed" in result["warning"]


def test_one_failed_step_does_not_skip_the_others(monkeypatch):
    """Releasing input used to be in the same try as closing the tab, so a
    hiccup there leaked the tab as well."""
    driver = _Tab()
    monkeypatch.setattr(
        browser_tools,
        "_reset_session_runtime_state",
        lambda _session: (_ for _ in ()).throw(RuntimeError("the page is gone")),
    )
    _register("clumsy", driver)

    result = browser_tools.close_session("clumsy")

    assert driver.calls == ["close_tab", "quit"]
    assert result["tab_closed"] is True


def test_closing_a_session_that_was_never_open_says_so():
    result = browser_tools.close_session("typo")

    assert result["closed"] is False
    assert "typo" in result["note"]


def test_close_all_reports_what_it_could_not_release():
    good = _Tab(1)
    bad = _Tab(2)
    bad.closes = False
    _register("good", good)
    _register("bad", bad)

    result = browser_tools.close_all_sessions()

    assert result["closed_all"] is False
    assert result["closed_sessions"] == ["bad", "good"]
    assert result["tabs_closed"] == 1
    assert "still open" in result["warnings"]["bad"]
    assert "good" not in result["warnings"]


def test_close_all_on_a_clean_run_reports_no_warnings():
    _register("one", _Tab(1))

    result = browser_tools.close_all_sessions()

    assert result["closed_all"] is True
    assert "warnings" not in result
    assert result["tabs_closed"] == 1


def test_a_tab_the_user_closed_frees_its_session_slot(monkeypatch):
    """Four tabs closed from the tab strip used to leave a server that refused
    to open a fifth."""
    for index in range(browser_tools.MAX_SESSIONS):
        _register(f"dead{index}", _Tab(index, _Bridge(gone=True)))
    replacement = _Tab(99)
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: replacement)

    session = browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")

    assert session.driver is replacement
    assert sorted(browser_tools._sessions) == ["fresh"]


def test_a_companion_that_cannot_be_asked_condemns_nothing(monkeypatch):
    """A transport failure says the daemon is down, not that the tabs are."""
    for index in range(browser_tools.MAX_SESSIONS):
        _register(f"live{index}", _Tab(index, _Bridge(reachable=False)))
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(99))

    with pytest.raises(RuntimeError, match="Maximum of"):
        browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")

    assert len(browser_tools._sessions) == browser_tools.MAX_SESSIONS


def test_the_cap_names_the_session_to_close(monkeypatch):
    for index in range(browser_tools.MAX_SESSIONS):
        session = _register(f"live{index}", _Tab(index, _Bridge()))
        session.last_used = 100.0 + index
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(99))

    with pytest.raises(RuntimeError) as failure:
        browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")

    message = str(failure.value)
    assert "Least recently used: 'live0'" in message
    assert "live3" in message


def test_selenium_sessions_are_never_asked_about_their_tab(monkeypatch):
    """They have no companion to ask, and no tab id that means anything to it."""
    bridge = _Bridge(gone=True)
    driver = _Tab(1, bridge)
    driver.is_extension_bridge = False
    _register("selenium", driver, profile_mode="temporary")

    browser_tools._drop_sessions_whose_tab_is_gone()

    assert bridge.asked == 0
    assert "selenium" in browser_tools._sessions
