"""Strict CDP virtual time layered over the existing JavaScript frame gate."""

from __future__ import annotations

import time

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools


TIMERS = "/fixtures/games/timers.html"


def _open_timers(local_site, session_id: str):
    try:
        browser_tools.open_page(
            f"{local_site.base_url}{TIMERS}",
            session_id=session_id,
            headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    return browser_tools._get_session(session_id)


def _report(driver) -> dict:
    return driver.execute_script("return window.__report();")


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_deterministic_steps_bound_native_time_and_normal_resumes(local_site):
    session_id = "strict-virtual-time"
    session = _open_timers(local_site, session_id)
    driver = session.driver
    frame_delta_ms = 20.0
    frames = 5
    try:
        control = browser_tools.set_render_control(
            "step",
            session_id,
            frame_delta_ms=frame_delta_ms,
            freeze_time=False,
            gate_timers=False,
            deterministic=True,
        )
        assert control["deterministic"] is True
        assert control["engine"] == "requestAnimationFrame gate + CDP virtual time"

        driver.execute_script("window.__resetTimers();")
        frozen = _report(driver)
        time.sleep(0.15)
        still_frozen = _report(driver)
        assert still_frozen["frames"] == frozen["frames"] == 0
        assert still_frozen["now"] == pytest.approx(frozen["now"], abs=1.0)

        stepped = browser_tools.render_step(frames, session_id)
        after_step = _report(driver)
        elapsed = after_step["now"] - frozen["now"]
        assert stepped["frames"] == frames
        assert after_step["frames"] == frames
        assert elapsed == pytest.approx(frames * frame_delta_ms, abs=5.0)

        normal = browser_tools.set_render_control("normal", session_id)
        assert normal["deterministic"] is False
        resumed = _report(driver)
        assert _wait_until(
            lambda: (
                _report(driver)["frames"] > resumed["frames"]
                and _report(driver)["fast"] > resumed["fast"]
            )
        ), "normal mode did not resume native timers and animation frames"
    finally:
        browser_tools.close_session(session_id)


def test_default_step_mode_keeps_existing_js_gate_behavior(local_site):
    session_id = "legacy-virtual-time"
    session = _open_timers(local_site, session_id)
    driver = session.driver
    frame_delta_ms = 25.0
    frames = 4
    try:
        control = browser_tools.set_render_control(
            "step", session_id, frame_delta_ms=frame_delta_ms
        )
        assert control["deterministic"] is False
        assert control["engine"] == "requestAnimationFrame gate"

        driver.execute_script("window.__resetTimers();")
        frozen = _report(driver)
        time.sleep(0.15)
        assert _report(driver) == frozen

        stepped = browser_tools.render_step(frames, session_id)
        after_step = _report(driver)
        assert stepped["frames"] == frames
        assert after_step["frames"] == frames
        assert after_step["now"] - frozen["now"] == pytest.approx(
            frames * frame_delta_ms, abs=0.05
        )

        browser_tools.set_render_control("normal", session_id)
        resumed = _report(driver)
        assert _wait_until(lambda: _report(driver)["frames"] > resumed["frames"])
    finally:
        browser_tools.close_session(session_id)
