"""Behavior regressions missed by the v1.11.0 canned-driver bundle."""
from __future__ import annotations

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools as b


def register(monkeypatch, driver):
    monkeypatch.setitem(b._sessions, "audit", b.BrowserSession(
        driver=driver, headless=True, profile_mode="isolated"
    ))
    monkeypatch.setattr(b, "_page_summary", lambda *_: {})


def test_script_error_does_not_replay_mutation_by_default(monkeypatch):
    class Driver:
        writes = 0

        def execute_script(self, *_):
            self.writes += 1
            raise WebDriverException("Uncaught: TypeError: after side effect")

    driver = Driver()
    register(monkeypatch, driver)
    result = b.execute_js("window.counter++; throw new Error('later')", session_id="audit")
    assert result["success"] is False
    assert driver.writes == result["attempts"] == 1


@pytest.mark.parametrize("value", [{}, [], [0], "ready", 1])
def test_wait_uses_javascript_truthiness(monkeypatch, value):
    class Driver:
        def execute_script(self, *_):
            return value

    register(monkeypatch, Driver())
    result = b.wait_for_condition("window.ready", session_id="audit", timeout_seconds=.1)
    assert result["success"] is True
    assert result["value"] == value


def test_promise_uses_cdp_without_granting_user_gesture(monkeypatch):
    class Driver:
        params = None

        def execute_cdp_cmd(self, command, params, timeout=None):
            assert command == "Runtime.evaluate"
            self.params = params
            return {"result": {"value": 42}}

        def execute_script(self, *_):
            raise AssertionError("promise must go through awaitPromise")

    driver = Driver()
    register(monkeypatch, driver)
    result = b.execute_js("return Promise.resolve(arguments[0]);", args=[42],
                          session_id="audit", await_promise=True)
    assert result["value"] == 42
    assert driver.params["awaitPromise"] is True
    assert driver.params["userGesture"] is False


def test_reload_does_not_repeat_failed_fallback_navigation(monkeypatch):
    class Driver:
        current_url = "https://example.test/"
        navigations = 0

        def execute_cdp_cmd(self, *_):
            raise WebDriverException("Refused DevTools method 'Page.reload'")

        def get(self, *_):
            self.navigations += 1
            raise WebDriverException("timeout after navigation started")

    driver = Driver()
    register(monkeypatch, driver)
    with pytest.raises(WebDriverException, match="timeout"):
        b.reload_page(session_id="audit")
    assert driver.navigations == 1


def test_viewport_update_preserves_other_dimension(monkeypatch):
    class Driver:
        def execute_script(self, *_):
            return {"width": 800, "height": 600}

    register(monkeypatch, Driver())
    seen = []
    monkeypatch.setattr(b, "_set_viewport", lambda _, w, h: seen.append((w, h)))
    b.apply_context_overrides(session_id="audit", width=1000)
    assert seen == [(1000, 600)]


def test_locale_updates_languages_and_retains_user_agent(monkeypatch):
    class Driver:
        calls = []

        def execute_script(self, *_):
            return "Existing/1.0"

        def execute_cdp_cmd(self, method, params, timeout=None):
            self.calls.append((method, params))

    driver = Driver()
    b._apply_context_overrides(driver, "isolated", locale="de-DE")
    assert ("Network.setUserAgentOverride", {
        "userAgent": "Existing/1.0", "acceptLanguage": "de-DE"
    }) in driver.calls


def test_live_isolated_focus_locale_wait_and_promise():
    try:
        driver = b.create_driver(headless=True, profile_mode="isolated")
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    b._sessions["audit-live"] = b.BrowserSession(driver=driver, headless=True, profile_mode="isolated")
    try:
        driver.get("data:text/html,<input id=x>")
        b._apply_context_overrides(driver, "isolated", locale="de-DE")
        assert driver.execute_script("return navigator.language") == "de-DE"
        assert driver.execute_script("return Intl.DateTimeFormat().resolvedOptions().locale") == "de-DE"
        result = b.fill_fields({"#x": "hello"}, session_id="audit-live", typing=True, blur_after=False)
        assert result["success"] is True
        assert driver.execute_script("return document.activeElement.id") == "x"
        assert b.wait_for_condition("({})", session_id="audit-live")["value"] == {}
        result = b.execute_js("return new Promise(r=>setTimeout(()=>r(42),20));",
                              session_id="audit-live", await_promise=True)
        assert result["value"] == 42
    finally:
        b._sessions.pop("audit-live", None)
        driver.quit()
