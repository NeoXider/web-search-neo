"""set_extra_headers, stealth, and replay_request: canned-driver tests."""

from __future__ import annotations

import pytest

from web_search_neo import browser_tools


class _CannedDriver:
    is_extension_bridge = False

    def __init__(self, cdp=None, script_result=None):
        self.cdp = cdp or {}
        self.script_result = script_result
        self.cdp_calls = []
        self.scripts = []

    def execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))
        response = self.cdp.get(command)
        return response(command, params) if callable(response) else (response or {})

    def execute_script(self, script, *args):
        self.scripts.append((script, list(args)))
        return self.script_result

    def quit(self):
        pass


def _register(driver, session_id="default"):
    session = browser_tools.BrowserSession(driver=driver, headless=False)
    browser_tools._sessions[session_id] = session
    return session


@pytest.fixture(autouse=True)
def summary_free(monkeypatch):
    monkeypatch.setattr(
        browser_tools, "_page_summary", lambda driver, session_id: {"session_id": session_id}
    )


# --- set_extra_headers ------------------------------------------------------


def test_extra_headers_are_enabled_and_stored():
    driver = _CannedDriver()
    session = _register(driver)
    result = browser_tools.set_extra_headers({"Authorization": "Bearer x", "X-AB": "1"})
    assert result["success"] is True
    assert result["cleared"] is False
    assert session.extra_headers == {"Authorization": "Bearer x", "X-AB": "1"}
    commands = [call[0] for call in driver.cdp_calls]
    assert "Network.enable" in commands
    sent = next(params for cmd, params in driver.cdp_calls if cmd == "Network.setExtraHTTPHeaders")
    assert sent["headers"]["Authorization"] == "Bearer x"


def test_empty_headers_clear_the_override():
    driver = _CannedDriver()
    session = _register(driver)
    browser_tools.set_extra_headers({"X": "1"})
    cleared = browser_tools.set_extra_headers()
    assert cleared["cleared"] is True
    assert session.extra_headers == {}
    # It still sends the (empty) set, because that is what actually clears it.
    last = driver.cdp_calls[-1]
    assert last == ("Network.setExtraHTTPHeaders", {"headers": {}})


# --- stealth ----------------------------------------------------------------


def test_stealth_on_registers_the_override_and_remembers_it():
    driver = _CannedDriver({"Page.addScriptToEvaluateOnNewDocument": {"identifier": "s-1"}})
    session = _register(driver)
    result = browser_tools.stealth(op="on")
    assert result["enabled"] is True
    assert result["identifier"] == "s-1"
    assert session.stealth_identifier == "s-1"
    # The registered source is what a detector reads first.
    added = next(
        params for cmd, params in driver.cdp_calls if cmd == "Page.addScriptToEvaluateOnNewDocument"
    )
    assert "navigator" in added["source"] and "webdriver" in added["source"]
    assert "s-1" in session.injected_scripts


def test_stealth_off_forgets_the_registration():
    driver = _CannedDriver({"Page.addScriptToEvaluateOnNewDocument": {"identifier": "s-1"}})
    session = _register(driver)
    browser_tools.stealth(op="on")
    off = browser_tools.stealth(op="off")
    assert off["enabled"] is False
    assert off["removed"] is True
    assert session.stealth_identifier is None
    assert session.injected_scripts == []


def test_stealth_on_twice_does_not_leak_a_registration():
    # A second on used to register a duplicate and forget the first, leaving it
    # running after off with no handle to remove it.
    ids = iter(["s-1", "s-2"])
    driver = _CannedDriver(
        {"Page.addScriptToEvaluateOnNewDocument": lambda c, p: {"identifier": next(ids)}}
    )
    session = _register(driver)
    browser_tools.stealth(op="on")
    browser_tools.stealth(op="on")
    assert session.stealth_identifier == "s-2"
    assert session.injected_scripts == ["s-2"]
    browser_tools.stealth(op="off")
    assert session.injected_scripts == []


def test_stealth_off_when_never_on_is_harmless():
    _register(_CannedDriver())
    off = browser_tools.stealth(op="off")
    assert off["removed"] is False


def test_stealth_unknown_op_raises():
    _register(_CannedDriver())
    with pytest.raises(ValueError, match="on or off"):
        browser_tools.stealth(op="bogus")


# --- replay_request ---------------------------------------------------------


CANNED_RESPONSE = {
    "ok": True,
    "status": 200,
    "url": "https://host/api",
    "headers": {"content-type": "application/json"},
    "body": '{"ok":true}',
    "truncated": False,
    "ms": 12,
}


def test_the_replay_script_is_valid_runnable_javascript():
    # The canned driver never parses the script, so a syntax/scope error like a
    # top-level await in a non-async wrapper would pass every mock. Assert the
    # shape that keeps it valid: the awaits live inside an async IIFE that the
    # wrapper returns, not at the top level.
    script = browser_tools._REPLAY_SCRIPT
    assert "return (async () =>" in script
    # No await outside that IIFE - the only awaits are indented inside it.
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("await ") or stripped.startswith("return await"):
            raise AssertionError(f"top-level await would break the wrapper: {stripped!r}")


def test_replay_by_explicit_url_runs_a_page_fetch():
    driver = _CannedDriver(script_result=CANNED_RESPONSE)
    _register(driver)
    result = browser_tools.replay_request(
        url="https://host/api", method="post", body='{"a":1}', headers={"X": "1"}
    )
    assert result["success"] is True
    assert result["response"]["status"] == 200
    # The method is normalised and the spec is what crossed into the page.
    spec = driver.scripts[-1][1][0]
    assert spec["method"] == "POST"
    assert spec["url"] == "https://host/api"
    assert spec["body"] == '{"a":1}'
    assert spec["credentials"] == "include"


def test_replay_by_request_id_takes_url_and_method_from_the_row():
    driver = _CannedDriver(script_result=CANNED_RESPONSE)
    session = _register(driver)
    session.network_rows = [
        {"id": "REQ-9", "url": "https://host/replayed", "method": "GET"},
    ]
    result = browser_tools.replay_request(request_id="REQ-9")
    assert result["success"] is True
    spec = driver.scripts[-1][1][0]
    assert spec["url"] == "https://host/replayed"
    assert spec["method"] == "GET"


def test_replay_by_unknown_request_id_is_a_clear_error():
    _register(_CannedDriver(script_result=CANNED_RESPONSE))
    with pytest.raises(ValueError, match="REQ-X"):
        browser_tools.replay_request(request_id="REQ-X")


def test_replay_needs_a_target():
    _register(_CannedDriver(script_result=CANNED_RESPONSE))
    with pytest.raises(ValueError, match="request_id or an explicit url"):
        browser_tools.replay_request()


def test_replay_reports_a_page_side_fetch_failure():
    driver = _CannedDriver(script_result={"ok": False, "status": 0, "error": "NetworkError"})
    _register(driver)
    result = browser_tools.replay_request(url="https://host/down")
    # The fetch resolved to an error object; the call still succeeds and hands it back.
    assert result["success"] is True
    assert result["response"]["ok"] is False
    assert result["response"]["error"] == "NetworkError"


def test_teardown_clears_injected_state_on_a_tab_that_is_handed_back():
    # A borrowed tab survives close, so headers and scripts we set on it must be
    # undone or the next session to claim it inherits them.
    driver = _CannedDriver({"Page.addScriptToEvaluateOnNewDocument": {"identifier": "s-1"}})
    session = _register(driver)
    session.owns_tab = False  # a borrowed tab, handed back rather than closed
    browser_tools.set_extra_headers({"Authorization": "Bearer tok"})
    browser_tools.stealth(op="on")

    browser_tools._clear_injected_state(session)
    assert session.extra_headers == {}
    assert session.injected_scripts == []
    assert session.stealth_identifier is None
    commands = [cmd for cmd, _ in driver.cdp_calls]
    assert "Page.removeScriptToEvaluateOnNewDocument" in commands
    # The last header call cleared the set.
    header_calls = [p for c, p in driver.cdp_calls if c == "Network.setExtraHTTPHeaders"]
    assert header_calls[-1] == {"headers": {}}


def test_remove_actually_calls_the_cdp_removal():
    driver = _CannedDriver({"Page.addScriptToEvaluateOnNewDocument": {"identifier": "x-1"}})
    _register(driver)
    browser_tools.inject_script(op="add", source="void 0;")
    browser_tools.inject_script(op="remove", identifier="x-1")
    commands = [(cmd, params) for cmd, params in driver.cdp_calls]
    assert ("Page.removeScriptToEvaluateOnNewDocument", {"identifier": "x-1"}) in commands


def test_all_three_actions_are_registered():
    from web_search_neo import main

    for name in ("set_extra_headers", "stealth", "replay_request"):
        assert name in main._ACTIONS
        assert main._ACTIONS[name].group == "page"
