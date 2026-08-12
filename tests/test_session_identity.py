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
