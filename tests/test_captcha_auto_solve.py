"""Auto-solving a captcha that blocks work on the tab the agent is already on.

Canned callables only: no Chrome, no network, no image decoding.
"""

from __future__ import annotations

import time

import pytest

from web_search_neo import browser_tools


class _StubDriver:
    """Enough of a driver for the session bookkeeping; the patched parts never use it."""

    is_extension_bridge = False

    def execute_script(self, script, *args):
        return {"widgets": [], "markers": [], "heading": "", "body_length": 100, "body": ""}

    def quit(self):
        pass


@pytest.fixture
def session(monkeypatch):
    """A registered session whose driver is never touched by the patched parts."""
    fake = browser_tools.BrowserSession(driver=_StubDriver(), headless=True)
    browser_tools._sessions["auto-captcha"] = fake
    monkeypatch.setattr(
        browser_tools, "_challenge_status",
        lambda driver: {"challenge_detected": True, "invisible_challenge_pending": False},
    )
    monkeypatch.setattr(
        browser_tools, "_page_summary",
        lambda driver, session_id: {"session_id": session_id},
    )
    return fake


def _stalled_result() -> dict:
    return {
        "session_id": "auto-captcha",
        "invisible_challenge_pending": True,
        "invisible_challenge": {"vendor": "tcaptcha"},
    }


# --- the switch and its reach --------------------------------------------------


def test_the_environment_can_put_every_captcha_back_on_a_person(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_AUTO_ON_TAB", "0")
    assert browser_tools._auto_solve_enabled() is False
    seen = []
    monkeypatch.setattr(
        browser_tools, "_slider_solve", lambda *args: seen.append(args) or {"solved": True}
    )
    assert browser_tools._auto_solve_challenge("auto-captcha") == {"attempted": False}
    assert seen == []


def test_the_slider_kill_switch_stops_the_auto_drag_too(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_LOCAL_SOLVE", "0")
    seen = []
    monkeypatch.setattr(
        browser_tools, "_slider_solve", lambda *args: seen.append(args) or {"solved": True}
    )
    assert browser_tools._auto_solve_challenge("auto-captcha") == {"attempted": False}
    assert seen == []


def test_a_solve_that_crashes_is_reported_as_a_failed_attempt(monkeypatch):
    def explode(session_id, budget):
        raise ValueError("the widget vanished mid-drag")

    monkeypatch.setattr(browser_tools, "_slider_solve", explode)
    outcome = browser_tools._auto_solve_challenge("auto-captcha")
    assert outcome["attempted"] is True and outcome["solved"] is False
    assert "vanished mid-drag" in outcome["reason"]


# --- a submit that sent nothing ------------------------------------------------


def test_a_stalled_submit_is_unblocked_by_the_free_drag(monkeypatch, session):
    monkeypatch.setattr(browser_tools, "_requests_since", lambda session, started: [])
    monkeypatch.setattr(
        browser_tools, "_auto_solve_challenge",
        lambda session_id: {"attempted": True, "solved": True},
    )
    out = browser_tools._note_stalled_submit(session, _stalled_result(), time.time() * 1000 - 1000)
    assert "submit_blocked_by_challenge" not in out
    assert out["challenge_cleared_by"] == "local_slider"
    assert "clicking once more submits once" in out["challenge_cleared_note"]


def test_an_unsure_measurement_still_names_the_block_and_says_it_tried(monkeypatch, session):
    monkeypatch.setattr(browser_tools, "_requests_since", lambda session, started: [])
    monkeypatch.setattr(
        browser_tools, "_auto_solve_challenge",
        lambda session_id: {"attempted": True, "solved": False, "reason": "the hole was not measurable"},
    )
    out = browser_tools._note_stalled_submit(session, _stalled_result(), time.time() * 1000 - 1000)
    assert out["submit_blocked_by_challenge"] is True
    assert out["captcha_auto_solve"] == {"solved": False, "reason": "the hole was not measurable"}
    assert "clear the challenge with the captcha action first" in out["submit_block_reason"]


def test_a_submit_that_reached_the_server_is_left_alone(monkeypatch, session):
    monkeypatch.setattr(browser_tools, "_requests_since", lambda session, started: [{"id": "1"}])
    calls = []
    monkeypatch.setattr(
        browser_tools, "_auto_solve_challenge", lambda session_id: calls.append(session_id)
    )
    out = browser_tools._note_stalled_submit(session, _stalled_result(), time.time() * 1000 - 1000)
    assert "submit_blocked_by_challenge" not in out and calls == []


def test_a_request_that_was_actually_sent_is_never_called_a_block(monkeypatch, session):
    # The silence check runs before any of this: a fast page simply has not got
    # round to its request yet, and dragging its widget would be an intrusion.
    monkeypatch.setattr(browser_tools, "_requests_since", lambda session, started: [])
    calls = []
    monkeypatch.setattr(
        browser_tools, "_auto_solve_challenge", lambda session_id: calls.append(session_id)
    )
    result = _stalled_result()
    out = browser_tools._note_stalled_submit(session, result, time.time() * 1000)
    assert out is result and calls == []


# --- a wait for a human --------------------------------------------------------


def test_the_wait_solves_the_slider_before_it_starts_waiting_for_a_person(monkeypatch, session):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_AUTO_ON_TAB", "1")
    monkeypatch.setattr(
        browser_tools, "_slider_solve",
        lambda session_id, budget: {"attempted": True, "solved": True, "distance_px": 174},
    )
    out = browser_tools.wait_for_challenge_resolution("auto-captcha", timeout_seconds=5)
    assert out["resolved"] is True and out["success"] is True
    assert out["solved_by"] == "local_slider" and out["waited_seconds"] == 0.0
    assert out["slider"]["distance_px"] == 174
    # The solve reports the page it ended on, not a promise about one.
    assert out["session_id"] == "auto-captcha"


def test_a_slider_that_refused_falls_back_to_the_wait_it_always_had(monkeypatch, session):
    monkeypatch.setattr(
        browser_tools, "_slider_solve",
        lambda session_id, budget: {"attempted": True, "solved": False, "reason": "unsure"},
    )
    out = browser_tools.wait_for_challenge_resolution("auto-captcha", timeout_seconds=0.2)
    assert out["resolved"] is False and out["timed_out"] is True
    assert "solved_by" not in out


def test_a_page_with_no_captcha_on_it_is_not_probed_for_a_slider(monkeypatch, session):
    monkeypatch.setattr(
        browser_tools, "_challenge_status",
        lambda driver: {"challenge_detected": False, "invisible_challenge_pending": False},
    )
    seen = []
    monkeypatch.setattr(
        browser_tools, "_slider_solve", lambda *args: seen.append(args) or {"solved": True}
    )
    out = browser_tools.wait_for_challenge_resolution("auto-captcha", timeout_seconds=1)
    assert out["resolved"] is True and out["challenge_seen"] is False
    assert seen == []
