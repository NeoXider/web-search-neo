from __future__ import annotations

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from web_search_neo import browser_tools


GAMEPAD_FIXTURE = "/fixtures/games/gamepad.html"


def _open_or_skip(url: str, session_id: str):
    try:
        return browser_tools.open_page(
            url,
            session_id=session_id,
            headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def test_virtual_gamepad_connects_with_state_and_disconnects(local_site):
    session_id = "gamepad-events"
    _open_or_skip(f"{local_site.base_url}{GAMEPAD_FIXTURE}", session_id)
    session = browser_tools._get_session(session_id)
    buttons = [
        {"touched": True, "pressed": True, "value": 1.0},
        {"touched": True, "pressed": False, "value": 0.35},
        {"touched": False, "pressed": False, "value": 0.0},
    ]
    axes = [-0.75, 0.25, 1.0, -1.0]

    try:
        try:
            browser_tools.gamepad(session_id, True, buttons, axes)
        except WebDriverException as exc:
            if "Emulation.sendGamepadEvents' wasn't found" in str(exc):
                pytest.skip("Installed Chrome does not support virtual gamepad events")
            raise
        report = WebDriverWait(session.driver, 2).until(
            lambda driver: driver.execute_script("return window.__report();") or False
        )
        assert report[-1]["type"] == "connected"
        assert len(report[-1]["gamepads"]) == 1
        observed = report[-1]["gamepads"][0]
        assert observed["buttons"] == buttons
        assert observed["axes"] == axes

        browser_tools.gamepad(session_id, False, [], [])
        report = WebDriverWait(session.driver, 2).until(
            lambda driver: (
                driver.execute_script("return window.__report();")
                if driver.execute_script("return window.__report().at(-1)?.type;")
                == "disconnected"
                else False
            )
        )
        assert report[-1] == {"type": "disconnected", "gamepads": []}
        assert session.driver.execute_script(
            "return Array.from(navigator.getGamepads()).filter(Boolean).length;"
        ) == 0
    finally:
        browser_tools.close_session(session_id)
