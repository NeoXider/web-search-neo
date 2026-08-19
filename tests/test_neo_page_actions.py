"""inject_script, cookies, local_storage, and gesture scripts: canned-CDP driver tests."""

from __future__ import annotations

import pytest

import browser_tools


class _CannedDriver:
    is_extension_bridge = False

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.scripts = []

    def execute_cdp_cmd(self, command, params):
        self.calls.append((command, params))
        response = self.responses.get(command)
        if callable(response):
            return response(command, params)
        return response or {}

    def execute_script(self, script, *args):
        """The WebDriver route execute_js takes when no gesture is asked for."""
        self.scripts.append((script, list(args)))
        return self.responses.get("execute_script")

    def quit(self) -> None:
        self.calls.append(("quit", {}))


def _register_session(driver, session_id="default") -> browser_tools.BrowserSession:
    session = browser_tools.BrowserSession(driver=driver, headless=False)
    browser_tools._sessions[session_id] = session
    return session


def _last_call(driver):
    return driver.calls[-1]


# --- run_script under a user gesture ----------------------------------------
#
# The plain route is covered elsewhere; what is new is user_gesture=True, which
# is the only way to reach the APIs Chrome gates behind a real click.


def _summary_free(monkeypatch):
    """execute_js decorates its answer with a page summary; stub it out here."""
    monkeypatch.setattr(
        browser_tools, "_page_summary", lambda driver, session_id: {"session_id": session_id}
    )


def test_gesture_script_goes_through_cdp_with_user_gesture(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({"Runtime.evaluate": {"result": {"value": 42}}})
    _register_session(driver)
    result = browser_tools.execute_js("return 40 + 2;", user_gesture=True)
    assert result["success"] is True
    assert result["value"] == 42
    command, params = _last_call(driver)
    assert command == "Runtime.evaluate"
    assert params["returnByValue"] is True
    assert params["userGesture"] is True
    assert "return 40 + 2;" in params["expression"]


def test_gesture_script_applies_args_as_the_arguments_array(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({"Runtime.evaluate": {"result": {"value": "ok"}}})
    _register_session(driver)
    browser_tools.execute_js(
        "return arguments[0] + arguments[1];", args=["a", 7], user_gesture=True
    )
    _, params = _last_call(driver)
    # Applied, not declared: `arguments` cannot be assigned inside a function.
    assert params["expression"].endswith('.apply(null, ["a", 7])')
    assert "arguments[0] + arguments[1]" in params["expression"]


def test_gesture_script_reports_a_page_side_throw(monkeypatch):
    _summary_free(monkeypatch)
    exception = {
        "text": "Uncaught ReferenceError",
        "lineNumber": 3,
        "exception": {"description": "ReferenceError: boom is not defined"},
    }
    driver = _CannedDriver({"Runtime.evaluate": {"exceptionDetails": exception}})
    _register_session(driver)
    result = browser_tools.execute_js("boom();", user_gesture=True)
    assert result["success"] is False
    assert "boom is not defined" in result["error"]


def test_without_a_gesture_the_webdriver_route_is_used(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({"execute_script": 7})
    _register_session(driver)
    assert browser_tools.execute_js("return 7;", args=[1])["value"] == 7
    assert driver.scripts == [("return 7;", [1])]
    assert driver.calls == []  # no CDP round trip at all


def test_script_on_an_unknown_session_raises():
    with pytest.raises(ValueError):
        browser_tools.execute_js("return 1;")


# --- inject_script ----------------------------------------------------------


def test_inject_script_add_list_remove():
    driver = _CannedDriver(
        {"Page.addScriptToEvaluateOnNewDocument": {"identifier": "id-1"}}
    )
    session = _register_session(driver)
    added = browser_tools.inject_script(op="add", source="window.marked = true;")
    assert added["identifier"] == "id-1"
    assert session.injected_scripts == ["id-1"]
    command, params = _last_call(driver)
    assert command == "Page.addScriptToEvaluateOnNewDocument"
    assert params["source"] == "window.marked = true;"

    assert browser_tools.inject_script(op="list")["identifiers"] == ["id-1"]

    removed = browser_tools.inject_script(op="remove", identifier="id-1")
    assert removed["removed"] is True
    assert session.injected_scripts == []
    assert browser_tools.inject_script(op="list")["identifiers"] == []


def test_inject_script_remove_unknown_is_best_effort():
    driver = _CannedDriver({})
    _register_session(driver)
    removed = browser_tools.inject_script(op="remove", identifier="never-added")
    assert removed["removed"] is False
    assert removed["identifier"] == "never-added"


def test_inject_script_add_requires_source_and_unknown_op_raises():
    driver = _CannedDriver({})
    _register_session(driver)
    with pytest.raises(ValueError):
        browser_tools.inject_script(op="add")
    with pytest.raises(ValueError):
        browser_tools.inject_script(op="bogus")


# --- cookies ----------------------------------------------------------------


COOKIE_SAMPLE = [
    {"name": "session", "value": "abc", "domain": ".example.com", "path": "/",
     "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": 123456},
    {"name": "theme", "value": "dark", "domain": "example.com", "path": "/",
     "secure": False, "httpOnly": False, "sameSite": "Strict", "expires": -1},
    {"name": "tracking", "value": "x", "domain": ".tracker.net", "path": "/",
     "secure": False, "httpOnly": False, "sameSite": "None", "expires": 0},
]


def test_cookies_get_filters_by_domain_and_name():
    driver = _CannedDriver({"Storage.getCookies": {"cookies": COOKIE_SAMPLE}})
    _register_session(driver)
    result = browser_tools.cookies(op="get")
    assert result["count"] == 3
    assert result["cookies"] == COOKIE_SAMPLE
    assert _last_call(driver) == ("Storage.getCookies", {})

    assert browser_tools.cookies(op="get", domain="example.com")["count"] == 2
    by_name = browser_tools.cookies(op="get", name="theme")
    assert by_name["count"] == 1
    assert by_name["cookies"][0]["name"] == "theme"
    assert browser_tools.cookies(op="get", domain="tracker", name="tracking")["count"] == 1
    assert browser_tools.cookies(op="get", domain="example", name="tracking")["count"] == 0


def test_cookies_set_passes_list_through_and_requires_it():
    driver = _CannedDriver({})
    _register_session(driver)
    to_set = [{"name": "a", "value": "1", "domain": "example.com", "path": "/"}]
    result = browser_tools.cookies(op="set", set_cookies=to_set)
    assert result["count"] == 1
    assert _last_call(driver) == ("Storage.setCookies", {"cookies": to_set})

    with pytest.raises(ValueError):
        browser_tools.cookies(op="set")


def test_cookies_clear_passes_filters():
    driver = _CannedDriver({})
    _register_session(driver)
    browser_tools.cookies(op="clear")
    assert _last_call(driver) == ("Storage.clearCookies", {})

    browser_tools.cookies(op="clear", name="theme", domain="example.com")
    assert _last_call(driver) == ("Storage.clearCookies", {"name": "theme", "domain": "example.com"})


def test_cookies_get_is_capped_but_still_counts_everything():
    # A real profile holds thousands; returning them all buries the answer and
    # blows the caller's budget, so the count is honest and the list is short.
    many = [dict(COOKIE_SAMPLE[0], name=f"c{index}") for index in range(250)]
    driver = _CannedDriver({"Storage.getCookies": {"cookies": many}})
    _register_session(driver)

    capped = browser_tools.cookies(op="get")
    assert capped["count"] == 250
    assert capped["truncated"] is True
    assert len(capped["cookies"]) == 100

    asked = browser_tools.cookies(op="get", limit=10)
    assert len(asked["cookies"]) == 10
    assert browser_tools.cookies(op="get", limit=500)["truncated"] is False


def test_cookies_unknown_op_raises():
    driver = _CannedDriver({})
    _register_session(driver)
    with pytest.raises(ValueError):
        browser_tools.cookies(op="bogus")


# --- local_storage ----------------------------------------------------------


def _last_script(driver):
    return driver.scripts[-1][0]


def test_local_storage_read_all_returns_map(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({"execute_script": {"a": "1", "b": "2"}})
    _register_session(driver)
    result = browser_tools.local_storage(op="read")
    assert result["success"] is True
    assert result["value"] == {"a": "1", "b": "2"}
    assert result["key"] is None
    script = _last_script(driver)
    assert "localStorage" in script
    assert "getItem" in script


def test_local_storage_read_one_key(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({"execute_script": "dark"})
    _register_session(driver)
    result = browser_tools.local_storage(op="read", key="theme")
    assert result["value"] == "dark"
    assert result["key"] == "theme"
    assert 'getItem("theme")' in _last_script(driver)


def test_local_storage_write_and_delete(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({})
    _register_session(driver)
    written = browser_tools.local_storage(op="write", key="theme", value="dark")
    assert written["value"] == "dark"
    assert 'setItem("theme", "dark")' in _last_script(driver)

    deleted = browser_tools.local_storage(op="delete", key="theme")
    assert deleted["key"] == "theme"
    assert 'removeItem("theme")' in _last_script(driver)


def test_local_storage_session_kind_targets_session_storage(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({})
    _register_session(driver)
    browser_tools.local_storage(op="read", key="k", kind="session")
    script = _last_script(driver)
    assert "sessionStorage" in script
    assert "localStorage" not in script


def test_local_storage_requires_arguments_and_rejects_unknown_op():
    driver = _CannedDriver({})
    _register_session(driver)
    with pytest.raises(ValueError):
        browser_tools.local_storage(op="write", key="theme")
    with pytest.raises(ValueError):
        browser_tools.local_storage(op="delete")
    with pytest.raises(ValueError):
        browser_tools.local_storage(op="bogus")