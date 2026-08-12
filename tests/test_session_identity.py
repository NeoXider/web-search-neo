"""A session must not outlive the browser run it was opened in.

Chrome hands out tab ids from a counter that restarts with the browser, so a
session that survived a restart would keep addressing "tab 42" - and tab 42 in
the new run belongs to whoever opened it, very possibly the user. The companion
mints an id per browser run for exactly this comparison; these tests cover what
the server does with it.
"""

from __future__ import annotations

import pytest

import browser_tools


class _Companion:
    """A stand-in for the bridge client, answering only what the check reads."""

    def __init__(self, run: str | None, connected: bool = True) -> None:
        self.browser_run = run
        self.connected = connected

    def status(self, wait_seconds: float = 0.0) -> dict:
        """The shape :meth:`ChromeBridge.status` returns; `get_status` reads it."""
        return {
            "connected": self.connected,
            "host": "127.0.0.1",
            "port": 0,
            "startup_error": None,
            "browser": {"browser_run": self.browser_run},
            "daemon": {"linked": self.connected, "version": None, "pid": None},
        }

    def release_tab(self, tab_id: int, timeout: float = 5.0) -> dict:
        # The daemon drops the whole claim registry when the run changes, so a
        # release aimed at the old id could only hit a claim made since, in the
        # new run - somebody else's.
        raise AssertionError(
            f"tab {tab_id} was released against a browser that never granted it"
        )


class _SwitchTo:
    def default_content(self) -> None:
        return None


class _Tab:
    """A driver that records everything sent to Chrome, so a test can prove
    nothing was."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tab_id = 42
        self.actual_tab_group = browser_tools.DEFAULT_TAB_GROUP
        self.is_extension_bridge = True
        self.switch_to = _SwitchTo()

    def quit(self) -> None:
        self.calls.append("quit")

    def close_tab(self) -> dict[str, bool]:
        self.calls.append("close_tab")
        return {"removed": True}

    def execute_script(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("execute_script")
        return None

    def execute_cdp_cmd(self, *_args: object, **_kwargs: object) -> dict:
        self.calls.append("execute_cdp_cmd")
        return {}


def _companion(monkeypatch, run: str | None, connected: bool = True) -> None:
    monkeypatch.setattr(
        browser_tools, "get_chrome_bridge", lambda: _Companion(run, connected)
    )


def _register(session_id: str, **overrides) -> _Tab:
    driver = _Tab()
    fields = {
        "driver": driver,
        "headless": False,
        "profile_mode": "current",
        "current_tab_id": 42,
        "browser_run": "run-one",
        "owns_browser": True,
        "owns_tab": True,
    }
    fields.update(overrides)
    browser_tools._sessions[session_id] = browser_tools.BrowserSession(**fields)
    return driver


def test_a_session_survives_a_service_worker_restart(monkeypatch):
    """The run id is stable across worker evictions, so nothing should happen."""
    driver = _register("stable")
    _companion(monkeypatch, "run-one")

    assert browser_tools._get_session("stable").driver is driver


def test_a_session_from_a_previous_chrome_is_refused_and_dropped(monkeypatch):
    _register("stale")
    _companion(monkeypatch, "run-two")

    with pytest.raises(ValueError) as failure:
        browser_tools._get_session("stale")

    message = str(failure.value)
    assert "no longer running" in message
    assert "names a different tab" in message
    assert '"action":"open"' in message
    assert "stale" not in browser_tools._sessions


def test_dropping_a_stale_session_sends_nothing_to_the_new_browser(monkeypatch):
    """The dangerous part: its tab id now names a tab the user owns."""
    driver = _register("stale")
    _companion(monkeypatch, "run-two")

    with pytest.raises(ValueError):
        browser_tools._get_session("stale")

    assert driver.calls == []


@pytest.mark.parametrize(
    ("run", "connected"),
    [(None, True), ("run-one", False), (None, False)],
    ids=["companion-too-old", "companion-not-connected", "neither"],
)
def test_an_unknowable_run_id_never_invalidates_a_session(monkeypatch, run, connected):
    """Absence of evidence is not evidence of a restart."""
    driver = _register("unknown")
    _companion(monkeypatch, run, connected)

    assert browser_tools._get_session("unknown").driver is driver


def test_a_bridge_that_cannot_start_never_invalidates_a_session(monkeypatch):
    def explode():
        raise RuntimeError("no daemon and no way to spawn one")

    driver = _register("offline")
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", explode)

    assert browser_tools._get_session("offline").driver is driver


def test_selenium_backed_sessions_are_not_checked(monkeypatch):
    """They own their own browser; the companion's run id says nothing about it."""
    driver = _register(
        "own", profile_mode="temporary", current_tab_id=None, browser_run=None
    )
    _companion(monkeypatch, "run-two")

    assert browser_tools._get_session("own").driver is driver


def test_reopening_after_a_restart_replaces_the_session_instead_of_refusing(monkeypatch):
    """`open` under a name whose browser is gone is a reopen, not a conflict."""
    replacement = _Tab()
    replacement.tab_id = 7
    _register("demo")
    _companion(monkeypatch, "run-two")
    monkeypatch.setattr(
        browser_tools, "create_driver", lambda *args, **kwargs: replacement
    )

    session = browser_tools._create_session(
        "demo", 1280, 800, None, profile_mode="current"
    )

    assert session.driver is replacement
    assert session.current_tab_id == 7
    assert session.browser_run == "run-two"


def test_a_new_session_records_the_run_it_was_opened_in(monkeypatch):
    monkeypatch.setattr(browser_tools, "create_driver", lambda *args, **kwargs: _Tab())
    _companion(monkeypatch, "run-one")

    session = browser_tools._create_session(
        "fresh", 1280, 800, None, profile_mode="current"
    )

    assert session.browser_run == "run-one"


# Teardown asks the same question, because it is the path that sends the most to
# Chrome: closing a session aims tabs.remove and debugger.detach at a tab id, and
# after a restart that id belongs to whoever now has it.


def test_closing_a_stale_session_sends_nothing_to_the_new_browser(monkeypatch):
    driver = _register("stale")
    _companion(monkeypatch, "run-two")

    result = browser_tools.close_session("stale")

    assert driver.calls == []
    assert result["closed"] is True
    assert result["tab_closed"] is False
    assert result["browser_gone"] is True
    assert "no longer running" in result["note"]
    assert "stale" not in browser_tools._sessions


def test_close_all_leaves_the_new_browsers_tabs_alone(monkeypatch):
    """This is also what exiting the server does: the atexit hook is close_all."""
    stale = _register("stale")
    _companion(monkeypatch, "run-two")

    result = browser_tools.close_all_sessions()

    assert stale.calls == []
    assert result["tabs_closed"] == 0
    assert result["browser_gone"] == ["stale"]
    # Nothing was left behind, because nothing of ours was there to leave.
    assert result["closed_all"] is True
    assert "warnings" not in result


def test_close_all_still_closes_the_sessions_whose_browser_is_there(monkeypatch):
    live = _register("live")
    stale = _register("stale", browser_run="run-zero")
    _companion(monkeypatch, "run-one")

    result = browser_tools.close_all_sessions()

    assert "close_tab" in live.calls and "quit" in live.calls
    assert stale.calls == []
    assert result["tabs_closed"] == 1
    assert result["browser_gone"] == ["stale"]


def test_the_cap_sweep_frees_a_stale_slot_without_touching_chrome(monkeypatch):
    """The sweep is a teardown path too: it quits the driver and releases the
    claim, both aimed at a tab id that belongs to somebody else now."""
    drivers = [_register(f"stale{index}") for index in range(browser_tools.MAX_SESSIONS)]
    _companion(monkeypatch, "run-two")

    browser_tools._drop_sessions_whose_tab_is_gone()

    assert browser_tools._sessions == {}
    assert all(driver.calls == [] for driver in drivers)


def test_status_does_not_report_on_a_stranger_s_tab(monkeypatch):
    """It ran a page summary against the tab and answered session_open: true."""
    driver = _register("stale")
    _companion(monkeypatch, "run-two")

    status = browser_tools.get_status("stale")

    assert driver.calls == []
    assert status["session_open"] is False
    assert status["browser_gone"] is True
    assert "no longer running" in status["next"]
    assert "stale" not in browser_tools._sessions


def test_status_on_a_live_session_still_reads_the_page(monkeypatch):
    driver = _register("live")
    _companion(monkeypatch, "run-one")

    status = browser_tools.get_status("live")

    assert status["session_open"] is True
    assert "browser_gone" not in status
    assert "execute_script" in driver.calls


# A run that was never knowable is not a run that will never be knowable.


def test_a_session_with_no_run_of_its_own_adopts_the_one_in_front_of_it(monkeypatch):
    """Opened against a companion older than 1.3.2, or while the link was down.
    Left unadopted it stays uncheckable for good, and goes on driving tab 42
    across every later restart."""
    _register("old", browser_run=None)
    _companion(monkeypatch, "run-one")

    browser_tools._get_session("old")

    assert browser_tools._sessions["old"].browser_run == "run-one"


def test_an_adopted_run_then_catches_the_next_restart(monkeypatch):
    driver = _register("old", browser_run=None)
    _companion(monkeypatch, "run-one")
    browser_tools._get_session("old")

    _companion(monkeypatch, "run-two")
    with pytest.raises(ValueError, match="no longer running"):
        browser_tools._get_session("old")

    assert driver.calls == []


def test_nothing_is_adopted_while_the_run_is_still_unknowable(monkeypatch):
    _register("old", browser_run=None)
    _companion(monkeypatch, None)

    browser_tools._get_session("old")

    assert browser_tools._sessions["old"].browser_run is None
