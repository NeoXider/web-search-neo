"""inject_script, cookies, local_storage, and gesture scripts: canned-CDP driver tests."""

from __future__ import annotations

import json

import pytest

from web_search_neo import browser_tools


class _CannedDriver:
    is_extension_bridge = False

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.scripts = []

    def execute_cdp_cmd(self, command, params, timeout=None):
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


@pytest.mark.parametrize("fail", [False, True])
def test_selenium_promise_timeout_is_scoped_and_restored(monkeypatch, fail):
    """Use Selenium's actual CDP method, without starting a browser or socket."""
    from selenium.webdriver.chromium.webdriver import ChromiumDriver
    from selenium.webdriver.remote.client_config import ClientConfig
    from selenium.webdriver.remote.errorhandler import ErrorHandler
    from selenium.webdriver.remote.remote_connection import RemoteConnection

    _summary_free(monkeypatch)
    driver = object.__new__(ChromiumDriver)
    driver.session_id = "timeout-fixture"
    # Selenium 4.49 checks the negotiated browser before dispatching CDP.
    driver.caps = {"browserName": "chrome"}
    driver.error_handler = ErrorHandler()
    config = ClientConfig(remote_server_addr="http://127.0.0.1:4444", timeout=11)
    executor = RemoteConnection(client_config=config)
    driver.command_executor = executor
    calls = []

    def execute(command, params):
        calls.append((command, params, config.timeout))
        if fail:
            raise RuntimeError("Transport failed")
        return {"value": {"result": {"value": 42}}}

    monkeypatch.setattr(executor, "execute", execute)
    _register_session(driver, "selenium-timeout")
    try:
        result = browser_tools.execute_js(
            "return Promise.resolve(42)", session_id="selenium-timeout",
            await_promise=True, timeout_seconds=65,
        )
        assert config.timeout == 11
        assert len(calls) == 1, result  # A failure must never replay a script.
        assert calls[0][0] == "executeCdpCommand"
        assert calls[0][1]["cmd"] == "Runtime.evaluate"
        assert calls[0][1]["params"]["awaitPromise"] is True
        assert calls[0][2] == 65
        assert result["success"] is not fail
        if fail:
            assert "Transport failed" in result["error"]
        else:
            assert result["value"] == 42
    finally:
        browser_tools._sessions.pop("selenium-timeout", None)
        executor.close()


def test_bridge_promise_timeout_stays_a_per_call_argument(monkeypatch):
    _summary_free(monkeypatch)

    class BridgeDriver(_CannedDriver):
        def execute_cdp_cmd(self, command, params, timeout=None):
            self.timeout = timeout
            return super().execute_cdp_cmd(command, params)

    driver = BridgeDriver({"Runtime.evaluate": {"result": {"value": 42}}})
    _register_session(driver)
    result = browser_tools.execute_js(
        "return Promise.resolve(42)", await_promise=True, timeout_seconds=65,
    )
    assert result["value"] == 42
    assert driver.timeout == 65


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


# --- execute_js result contract ------------------------------------------------
#
# value is always plain JSON and value_json always its string form, so a caller
# never has to guess between an object and content parts, and a DOM node never
# reaches the MCP layer as a live handle.


def test_execute_js_object_value_has_matching_value_json(monkeypatch):
    _summary_free(monkeypatch)
    payload = {"user": {"name": "Ada", "tags": ["a", "b"]}}
    driver = _CannedDriver({"execute_script": payload})
    _register_session(driver, "js-obj")
    result = browser_tools.execute_js("return state;", session_id="js-obj")
    assert result["success"] is True
    assert result["value"] == payload
    assert json.loads(result["value_json"]) == payload


def test_execute_js_dom_element_becomes_a_descriptor(monkeypatch):
    _summary_free(monkeypatch)

    class _LiveElement:
        tag_name = "IFRAME"

    driver = _CannedDriver({"execute_script": _LiveElement()})
    _register_session(driver, "js-el")
    result = browser_tools.execute_js("return frame;", session_id="js-el")
    assert result["success"] is True
    assert result["value"]["element"] == "iframe"
    assert "query their properties" in result["value"]["note"]
    assert json.loads(result["value_json"])["element"] == "iframe"


def test_execute_js_nested_unserialisable_is_sanitised(monkeypatch):
    _summary_free(monkeypatch)
    driver = _CannedDriver({"execute_script": {"items": [object(), 1]}})
    _register_session(driver, "js-nested")
    result = browser_tools.execute_js("return mixed;", session_id="js-nested")
    assert result["success"] is True
    assert result["value"]["items"][1] == 1
    assert "unserialisable" in result["value"]["items"][0]
    json.loads(result["value_json"])  # Must not raise.


# --- execute_js frame_selector -------------------------------------------------


class _FrameSwitch:
    def __init__(self):
        self.calls: list = []

    def default_content(self):
        self.calls.append("top")

    def frame(self, element):
        self.calls.append(("frame", element))


def _frame_driver(count):
    driver = _CannedDriver({"execute_script": count})
    driver.switch_to = _FrameSwitch()
    driver.find_calls = []

    def find_element(by, selector):
        found = {"by": by, "selector": selector}
        driver.find_calls.append(found)
        return found

    driver.find_element = find_element
    return driver


def test_execute_js_frame_selector_runs_inside_and_returns_on_top(monkeypatch):
    _summary_free(monkeypatch)
    driver = _frame_driver(1)
    _register_session(driver, "js-frame")
    result = browser_tools.execute_js(
        "return location.href;", session_id="js-frame", frame_selector="#fr"
    )
    assert result["success"] is True
    assert driver.find_calls == [{"by": "css selector", "selector": "#fr"}]
    # Entered the frame, ran the script there, handed back at the top document.
    assert driver.switch_to.calls[0] == "top"
    assert driver.switch_to.calls[1][0] == "frame"
    assert driver.switch_to.calls[-1] == "top"


def test_execute_js_ambiguous_frame_refuses_before_running(monkeypatch):
    _summary_free(monkeypatch)
    driver = _frame_driver(2)
    _register_session(driver, "js-amb")
    with pytest.raises(ValueError, match="matches 2"):
        browser_tools.execute_js(
            "return 42;", session_id="js-amb", frame_selector="iframe"
        )
    # Only the frame count probe ran; the script itself never did.
    assert driver.find_calls == []
    assert all("return 42" not in script for script, _args in driver.scripts)


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
    assert browser_tools.cookies(op="get", domain="tracker.net", name="tracking")["count"] == 1
    assert browser_tools.cookies(op="get", domain="example", name="tracking")["count"] == 0
    # A domain filter means the domain and its subdomains - never a substring.
    assert browser_tools.cookies(op="get", domain="tracker")["count"] == 0
    assert browser_tools.cookies(op="get", domain="le.com")["count"] == 0


def test_cookies_set_passes_list_through_and_requires_it():
    driver = _CannedDriver({})
    _register_session(driver)
    to_set = [{"name": "a", "value": "1", "domain": "example.com", "path": "/"}]
    result = browser_tools.cookies(op="set", set_cookies=to_set)
    assert result["count"] == 1
    assert _last_call(driver) == ("Storage.setCookies", {"cookies": to_set})

    with pytest.raises(ValueError):
        browser_tools.cookies(op="set")


def test_cookies_clear_deletes_only_the_matching_cookies():
    # Storage.clearCookies has no filter: it used to receive name/domain it
    # ignores and wiped every cookie of the user's profile. A filtered clear now
    # deletes exactly the matches, and never calls clearCookies at all.
    jar = [dict(c) for c in COOKIE_SAMPLE]

    def delete(_command, params):
        jar[:] = [c for c in jar if (c["name"], c["domain"]) != (params["name"], params["domain"])]
        return {}

    driver = _CannedDriver({
        "Storage.getCookies": lambda *_: {"cookies": [dict(c) for c in jar]},
        "Network.deleteCookies": delete,
    })
    _register_session(driver)
    result = browser_tools.cookies(op="clear", name="theme", domain="example.com")
    assert result["deleted"] == 1 and result["success"] is True
    assert ("Network.deleteCookies", {"name": "theme", "domain": "example.com", "path": "/"}) in driver.calls
    assert not any(command == "Storage.clearCookies" for command, _ in driver.calls)

    by_domain = browser_tools.cookies(op="clear", domain="example.com")
    assert [c["name"] for c in by_domain["deleted_cookies"]] == ["session"]
    # A domain is matched as a domain, not a substring: "le.com" names nobody here.
    assert browser_tools.cookies(op="clear", domain="le.com")["deleted"] == 0


def test_an_unfiltered_cookie_clear_needs_explicit_confirmation():
    driver = _CannedDriver({})
    _register_session(driver)
    with pytest.raises(ValueError, match="confirm_clear_all"):
        browser_tools.cookies(op="clear")
    # A name alone would hit that cookie name on every site.
    with pytest.raises(ValueError, match="every 'sid' cookie of every site"):
        browser_tools.cookies(op="clear", name="sid")
    assert driver.calls == []
    assert browser_tools.cookies(op="clear", confirm_clear_all=True)["cleared"] == "all"
    assert _last_call(driver) == ("Storage.clearCookies", {})


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


def test_cookies_offset_pages_through_the_tail():
    many = [dict(COOKIE_SAMPLE[0], name=f"c{index}") for index in range(250)]
    driver = _CannedDriver({"Storage.getCookies": {"cookies": many}})
    _register_session(driver, "cookies-pages")

    first = browser_tools.cookies(op="get", session_id="cookies-pages", limit=100)
    assert first["count"] == 250
    assert first["offset"] == 0
    assert first["returned"] == 100
    assert first["truncated"] is True
    assert first["cookies"][0]["name"] == "c0"

    # The tail past the old hard window is reachable now.
    tail = browser_tools.cookies(op="get", session_id="cookies-pages", limit=100, offset=200)
    assert tail["offset"] == 200
    assert tail["returned"] == 50
    assert tail["truncated"] is False
    assert tail["cookies"][0]["name"] == "c200"
    assert tail["cookies"][-1]["name"] == "c249"

    beyond = browser_tools.cookies(op="get", session_id="cookies-pages", offset=9999)
    assert beyond["returned"] == 0
    assert beyond["cookies"] == []
    assert beyond["truncated"] is False


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


def test_a_partitioned_cookie_is_deleted_with_its_partition_or_reported_kept():
    partition = {"topLevelSite": "https://shop.test", "hasCrossSiteAncestor": False}
    jar = [{"name": "chips", "domain": "widget.test", "path": "/", "partitionKey": partition},
           {"name": "stubborn", "domain": "widget.test", "path": "/"}]

    def delete(_command, params):
        if params["name"] == "chips" and params.get("partitionKey") == partition:
            jar[:] = [c for c in jar if c["name"] != "chips"]
        return {}

    driver = _CannedDriver({
        "Storage.getCookies": lambda *_: {"cookies": [dict(c) for c in jar]},
        "Network.deleteCookies": delete,
    })
    _register_session(driver)
    result = browser_tools.cookies(op="clear", domain="widget.test")
    assert [c["name"] for c in result["deleted_cookies"]] == ["chips"]
    assert result["success"] is False and result["not_deleted"][0]["name"] == "stubborn"
