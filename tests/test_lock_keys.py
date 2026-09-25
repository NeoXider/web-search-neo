"""CapsLock, NumLock and ScrollLock through CDP (1.20; 1.19 refused them).

Both input paths run in a real headless Chrome of a temporary session against a
local fixture page: the Selenium one through ``press_keys``, and the companion
one, whose exact CDP parameters are replayed through the same Chrome.
"""

from __future__ import annotations

import pytest
from selenium.webdriver.common.action_chains import ActionChains

from web_search_neo import browser_tools
from web_search_neo.actions import lock_keys
from web_search_neo.chrome_bridge import ChromeBridgeDriver


def _open_or_skip(local_site, session_id):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(
            f"{local_site.base_url}/fixtures/perception/key_events.html", session_id=session_id,
            width=1024, height=768, headless=True, profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    driver = browser_tools._get_session(session_id).driver
    driver.execute_script(
        "const t=document.getElementById('ta'); t.value=''; t.focus(); window.__events.length = 0;"
    )
    return driver


def _downs(driver):
    return [e for e in driver.execute_script("return window.__events.slice()") if e["t"] == "keydown"]


def _value(driver):
    return driver.execute_script("return document.getElementById('ta').value")


def test_selenium_lock_keys_reach_the_page_and_capslock_changes_letters(local_site):
    driver = _open_or_skip(local_site, "locks-selenium")

    def press(key):
        browser_tools.press_keys([key], session_id="locks-selenium", focus_mode="none")

    for key, code in (("CapsLock", 20), ("NumLock", 144), ("ScrollLock", 145)):
        driver.execute_script("window.__events.length = 0;")
        press(key)
        downs = _downs(driver)
        assert [(d["key"], d["code"], d["keyCode"], d["location"]) for d in downs] == [(key, key, code, 0)]
    # NumLock and ScrollLock only flip their tracked state; switch them back off.
    press("NUM_LOCK")
    press("scroll lock")
    assert lock_keys.locks_on(driver) == ["CapsLock"]

    driver.execute_script("window.__events.length = 0;")
    press("a")
    press("SHIFT+b")
    down_a = next(d for d in _downs(driver) if d["code"] == "KeyA")
    assert (down_a["key"], down_a["keyCode"]) == ("A", 65)
    assert _value(driver) == "Ab"  # CapsLock on: upper case, lower case under Shift

    # type_text types what it was given, whatever the lock.
    browser_tools.type_text("xY", session_id="locks-selenium", mode="keys")
    assert _value(driver) == "AbxY"

    press("CapsLock")
    assert lock_keys.locks_on(driver) == []
    press("c")
    assert _value(driver) == "AbxYc"


def test_companion_path_sends_the_same_lock_key_events(local_site):
    driver = _open_or_skip(local_site, "locks-bridge")
    bridge = ChromeBridgeDriver.__new__(ChromeBridgeDriver)
    bridge._modifier_mask = 0
    bridge.execute_cdp_cmd = lambda method, params, timeout=None: driver.execute_cdp_cmd(method, params)

    def tap(key):
        name = browser_tools._normalize_game_key(key)
        lock_keys.perform(bridge, [{"type": "down", "key": name}, {"type": "up", "key": name}], ActionChains)

    tap("caps_lock")
    tap("q")
    downs = _downs(driver)
    assert [(d["key"], d["code"], d["keyCode"]) for d in downs] == [("CapsLock", "CapsLock", 20), ("Q", "KeyQ", 81)]
    assert _value(driver) == "Q"
    assert lock_keys.locks_on(bridge) == ["CapsLock"]


def test_a_driver_without_cdp_refuses_a_lock_key_before_sending_anything():
    sent = []

    class _Chain:
        def __init__(self, _driver):
            pass

        def key_down(self, key):
            sent.append(("down", key))

        def key_up(self, key):
            sent.append(("up", key))

        def pause(self, _seconds):
            pass

        def perform(self):
            pass

    class _NoCdpDriver:
        pass

    driver = _NoCdpDriver()
    with pytest.raises(ValueError, match="CapsLock"):
        lock_keys.perform(driver, [{"type": "down", "key": "a"}, {"type": "down", "key": "CapsLock"}], _Chain)
    assert sent == []
    # Without any lock the stream goes to WebDriver unchanged.
    lock_keys.perform(driver, [{"type": "down", "key": "a"}, {"type": "up", "key": "a"}], _Chain)
    assert sent == [("down", "a"), ("up", "a")]
