"""Who owns which tab: the user's, ours, and another agent's.

`attach_tab` claims a tab the user already had open so the agent can read what
is on it. An `open` on that session used to call `driver.get()` on it, which
took the user's page away in a tab they still believed was theirs. It opens the
agent's own tab in the agent's group instead, and hands the borrowed one back.

The second half is about other agents. The in-process guard only sees this
server's sessions, so ownership across the several MCP clients that can now
drive one Chrome lives in the bridge daemon; these cover how this side asks.
"""

from __future__ import annotations

import pytest

import browser_tools


class _Companion:
    """The bridge client, answering only what tab ownership asks of it."""

    def __init__(self, refuse: int | None = None, available: bool = True) -> None:
        self.refuse = refuse
        self.available = available
        self.claimed: list[int] = []
        self.released: list[int] = []
        self.browser_run = "run-one"
        self.connected = True

    def claim_tab(self, tab_id: int, timeout: float = 5.0) -> dict:
        if not self.available:
            return {"status": "unavailable", "granted": True, "tab_id": tab_id}
        if tab_id == self.refuse:
            return {
                "status": "refused",
                "granted": False,
                "tab_id": tab_id,
                "reason": (
                    f"Chrome tab {tab_id} is already being driven by another agent "
                    "on this machine (msp_server.py#8123, for 47s)."
                ),
            }
        self.claimed.append(tab_id)
        return {
            "status": "granted",
            "granted": True,
            "tab_id": tab_id,
            "browser_run": self.browser_run,
        }

    def release_tab(self, tab_id: int, timeout: float = 5.0) -> dict:
        self.released.append(tab_id)
        return {"released": True, "tab_id": tab_id}


@pytest.fixture
def companion(monkeypatch) -> _Companion:
    """Nothing here may reach the machine's real daemon."""
    bridge = _Companion()
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: bridge)
    return bridge


class _SwitchTo:
    def default_content(self) -> None:
        return None


class _Tab:
    def __init__(self, tab_id: int) -> None:
        self.tab_id = tab_id
        self.actual_tab_group = browser_tools.DEFAULT_TAB_GROUP
        self.is_extension_bridge = True
        self.switch_to = _SwitchTo()
        self.calls: list[str] = []
        self.url = "about:blank"

    def get(self, url: str) -> None:
        self.calls.append(f"get {url}")
        self.url = url

    def quit(self) -> None:
        self.calls.append("quit")

    def close_tab(self) -> dict[str, bool]:
        self.calls.append("close_tab")
        return {"removed": True}

    def execute_script(self, script: str, *_args: object) -> object:
        self.calls.append("execute_script")
        if script.strip() == "return document.readyState":
            return "complete"
        return {"url": self.url, "title": "Agent tab"}

    def execute_cdp_cmd(self, *_args: object, **_kwargs: object) -> dict:
        self.calls.append("execute_cdp_cmd")
        return {}


def _borrowed(session_id: str = "reader", tab_id: int = 42) -> _Tab:
    driver = _Tab(tab_id)
    browser_tools._sessions[session_id] = browser_tools.BrowserSession(
        driver=driver,
        headless=False,
        profile_mode="current",
        current_tab_id=tab_id,
        tab_group=browser_tools.DEFAULT_TAB_GROUP,
        browser_run="run-one",
        owns_browser=True,
        owns_tab=False,
    )
    return driver


def test_the_borrowed_tab_is_handed_back_untouched(monkeypatch, companion):
    borrowed = _borrowed()
    session = browser_tools._sessions["reader"]
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(7))

    released = browser_tools._leave_claimed_tab(
        session, 1280, 800, browser_tools.DEFAULT_TAB_GROUP
    )

    assert released == 42
    # Detached, and nothing else: not closed, not navigated, not activated.
    assert borrowed.calls == ["quit"]


def test_the_session_moves_to_a_tab_it_owns(monkeypatch, companion):
    _borrowed()
    session = browser_tools._sessions["reader"]
    replacement = _Tab(7)
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: replacement)

    browser_tools._leave_claimed_tab(session, 1280, 800, browser_tools.DEFAULT_TAB_GROUP)

    assert session.driver is replacement
    assert session.current_tab_id == 7
    assert session.owns_tab is True
    assert session.tab_group == browser_tools.DEFAULT_TAB_GROUP


def test_the_new_tab_is_opened_in_the_agents_group(monkeypatch, companion):
    _borrowed()
    session = browser_tools._sessions["reader"]
    asked: dict[str, object] = {}

    def record(*args, **kwargs):
        asked["args"] = args
        return _Tab(7)

    monkeypatch.setattr(browser_tools, "create_driver", record)
    browser_tools._leave_claimed_tab(session, 1280, 800, browser_tools.DEFAULT_TAB_GROUP)

    # mode 'current', no tab id (so a new one is created), the agent's group.
    assert asked["args"][3] == "current"
    assert asked["args"][6] is None
    assert asked["args"][7] == browser_tools.DEFAULT_TAB_GROUP


def test_the_old_tabs_history_does_not_follow_the_session(monkeypatch, companion):
    """Otherwise `console` and `network` would explain the new page with the old
    page's traffic."""
    _borrowed()
    session = browser_tools._sessions["reader"]
    session.network_rows = [{"url": "https://user-was-reading.test/"}]
    session.network_dropped = 12
    session.browser_log = [{"message": "from the borrowed tab"}]
    session.probe_console_seen = [{"text": "old"}]
    session.console.seq = 99
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(7))

    browser_tools._leave_claimed_tab(session, 1280, 800, browser_tools.DEFAULT_TAB_GROUP)

    assert session.network_rows == []
    assert session.network_dropped == 0
    assert session.browser_log == []
    assert session.probe_console_seen == []
    assert session.console.seq == 0
    # The gate and console hook were installed in the tab we just left.
    assert session.render_bootstrap_registered is False


def test_a_session_that_owns_its_tab_is_left_alone(monkeypatch, companion):
    driver = _borrowed("own")
    session = browser_tools._sessions["own"]
    session.owns_tab = True
    monkeypatch.setattr(
        browser_tools,
        "create_driver",
        lambda *a, **k: pytest.fail("a tab we opened is ours to navigate"),
    )

    assert (
        browser_tools._leave_claimed_tab(
            session, 1280, 800, browser_tools.DEFAULT_TAB_GROUP
        )
        is None
    )
    assert session.driver is driver


def test_open_navigates_the_new_tab_and_names_the_one_it_freed(monkeypatch, companion):
    borrowed = _borrowed()
    replacement = _Tab(7)
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: replacement)

    result = browser_tools.open_page(
        "https://example.test/report", session_id="reader", profile_mode="current"
    )

    assert result["left_claimed_tab"] == 42
    assert result["current_tab_id"] == 7
    assert "get https://example.test/report" in replacement.calls
    assert not any(call.startswith("get ") for call in borrowed.calls)


def test_a_tab_another_agent_is_driving_is_refused_in_its_own_words(monkeypatch, companion):
    companion.refuse = 41
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(41))

    with pytest.raises(RuntimeError) as failure:
        browser_tools.attach_current_tab(41, session_id="second")

    # The daemon's sentence is the one worth showing a person: it names the
    # holder and how long it has held the tab.
    assert "msp_server.py#8123" in str(failure.value)
    assert "second" not in browser_tools._sessions


def test_a_refused_claim_costs_the_other_agent_nothing(monkeypatch, companion):
    """No debugger attach, so the tab it is refusing to share is not disturbed."""
    companion.refuse = 41
    monkeypatch.setattr(
        browser_tools,
        "create_driver",
        lambda *a, **k: pytest.fail("the claim must be settled before Chrome is touched"),
    )

    with pytest.raises(RuntimeError):
        browser_tools.attach_current_tab(41, session_id="second")


def test_a_daemon_that_cannot_answer_does_not_lock_us_out(monkeypatch, companion):
    """No daemon means nobody is guarding the browser, not that it is busy."""
    companion.available = False
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(41))

    result = browser_tools.attach_current_tab(41, session_id="lonely")

    assert result["success"] is True
    assert companion.claimed == []


def test_a_tab_we_opened_is_claimed_too(monkeypatch, companion):
    """Otherwise another agent's attach_tab could take the page we just opened."""
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(7))

    browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")

    assert companion.claimed == [7]


def test_the_run_the_claim_was_granted_in_is_the_one_recorded(monkeypatch, companion):
    companion.browser_run = "run-from-the-daemon"
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(7))

    session = browser_tools._create_session(
        "fresh", 1280, 800, None, profile_mode="current"
    )

    assert session.browser_run == "run-from-the-daemon"


def test_a_session_that_never_opened_releases_the_tab_it_claimed(monkeypatch, companion):
    def fail_to_attach(*_args, **_kwargs):
        raise RuntimeError("the debugger would not attach")

    monkeypatch.setattr(browser_tools, "create_driver", fail_to_attach)

    with pytest.raises(RuntimeError):
        browser_tools._create_session(
            "doomed", 1280, 800, None, profile_mode="current", current_tab_id=41
        )

    assert companion.released == [41]


def test_closing_a_session_gives_its_tab_back(companion):
    _borrowed("done")

    browser_tools.close_session("done")

    assert companion.released == [42]


def test_moving_off_a_borrowed_tab_swaps_the_claim(monkeypatch, companion):
    _borrowed()
    session = browser_tools._sessions["reader"]
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(7))

    browser_tools._leave_claimed_tab(session, 1280, 800, browser_tools.DEFAULT_TAB_GROUP)

    assert companion.claimed == [7]
    assert companion.released == [42]


def test_open_on_a_session_of_our_own_reports_no_freed_tab(monkeypatch, companion):
    _borrowed("own")
    browser_tools._sessions["own"].owns_tab = True
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(7))

    result = browser_tools.open_page(
        "https://example.test/report", session_id="own", profile_mode="current"
    )

    assert "left_claimed_tab" not in result
