"""Regression bundle for the eight session-reported bugs (fixes 1-8).

Canned drivers only: no Chrome, no network. Each fix gets at least one test
pinning the reported failure mode.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from selenium.common.exceptions import TimeoutException, WebDriverException

from web_search_neo import browser_tools
from web_search_neo import main


class _ProbeDriver:
    """Minimal driver answering page summaries and recording CDP/scripts."""

    is_extension_bridge = False

    def __init__(self, probe=None):
        self.probe = dict(
            probe
            or {
                "url": "https://example.test/page",
                "title": "Fixture page",
                "viewport_width": 1440,
                "viewport_height": 900,
                "page_width": 1440,
                "page_height": 2000,
                "ready_state": "complete",
                "challenge": {},
            }
        )
        self.cdp_calls: list[tuple[str, dict]] = []
        self.scripts: list[tuple[str, list]] = []

    def execute_cdp_cmd(self, command, params, timeout=None):
        self.cdp_calls.append((command, params))
        if command == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "stub-script-1"}
        return {}

    def execute_script(self, script, *args):
        self.scripts.append((script, list(args)))
        return dict(self.probe)

    def quit(self):
        return None


def _register(driver, session_id="bundle-case", **kwargs) -> browser_tools.BrowserSession:
    session = browser_tools.BrowserSession(driver=driver, headless=True, **kwargs)
    browser_tools._sessions[session_id] = session
    return session


# --- Fix 1: reload drift ------------------------------------------------------


class _StaleCompanionDriver(_ProbeDriver):
    """A companion that predates Page.reload refuses it by name."""

    def __init__(self, probe=None):
        super().__init__(probe)
        self.navigated: list[str] = []

    @property
    def current_url(self):
        return "https://example.test/page"

    def get(self, url):
        self.navigated.append(url)

    def execute_cdp_cmd(self, command, params, timeout=None):
        self.cdp_calls.append((command, params))
        if command == "Page.reload":
            raise Exception(
                "Refused DevTools method 'Page.reload': the companion forwards "
                "only the 24 methods Web Search Neo uses."
            )
        return {}


def test_reload_falls_back_to_navigation_on_a_stale_companion():
    driver = _StaleCompanionDriver()
    _register(driver, "bundle-reload-stale")
    result = browser_tools.reload_page(session_id="bundle-reload-stale", wait_seconds=0)
    assert result["url"] == "https://example.test/page"
    assert result["hard"] is False
    assert result["reload_fallback"] == "navigate"
    assert driver.navigated == ["https://example.test/page"]
    assert "chrome://extensions" in result["companion_note"]


def test_companion_status_publishes_the_shipped_allowlist():
    class _Bridge:
        def status(self, timeout=0.0):
            return {"connected": True, "browser": {"extension_version": "1.10.1"}}

    import web_search_neo.browser_tools as tools

    original = tools.get_chrome_bridge
    tools.get_chrome_bridge = lambda: _Bridge()
    try:
        status = tools._companion_status()
    finally:
        tools.get_chrome_bridge = original
    assert status["allowed_cdp_methods_size"] == len(status["allowed_cdp_methods"])
    assert "Page.reload" in status["allowed_cdp_methods"]
    assert status["allowed_cdp_methods_hash"]


# --- Fix 2: run_script retry --------------------------------------------------


class _FlakyScriptDriver(_ProbeDriver):
    def __init__(self, failures_before_success=1):
        super().__init__()
        self.failures_left = failures_before_success
        self.attempts = 0

    def execute_script(self, script, *args):
        if script.strip() == "return document.readyState":
            return "complete"
        if script == "return 42;":
            self.attempts += 1
            if self.failures_left > 0:
                self.failures_left -= 1
                raise WebDriverException("Uncaught")
            return 42
        return dict(self.probe)


def test_execute_js_retries_a_post_navigation_uncaught():
    driver = _FlakyScriptDriver(failures_before_success=1)
    _register(driver, "bundle-retry")
    result = browser_tools.execute_js("return 42;", session_id="bundle-retry", retry_on_uncaught=True)
    assert result["success"] is True
    assert result["value"] == 42
    assert result["attempts"] == 2


def test_execute_js_single_shot_when_retry_disabled():
    driver = _FlakyScriptDriver(failures_before_success=5)
    _register(driver, "bundle-no-retry")
    result = browser_tools.execute_js(
        "return 42;", session_id="bundle-no-retry", retry_on_uncaught=False
    )
    assert result["success"] is False
    assert result["attempts"] == 1


def test_execute_js_does_not_retry_a_genuine_page_error():
    class _SyntaxDriver(_ProbeDriver):
        def execute_script(self, script, *args):
            if script == ";;;":
                raise WebDriverException("SyntaxError: Unexpected token ';'")
            return dict(self.probe)

    driver = _SyntaxDriver()
    _register(driver, "bundle-syntax")
    result = browser_tools.execute_js(";;;", session_id="bundle-syntax")
    assert result["success"] is False
    assert result["attempts"] == 1


# --- Fix 3: universal wait ----------------------------------------------------


class _ConditionDriver(_ProbeDriver):
    def __init__(self, values):
        super().__init__()
        self.values = list(values)

    def execute_script(self, script, *args):
        self.scripts.append((script, list(args)))
        if script.startswith("return ("):
            if self.values:
                return self.values.pop(0)
            return None
        return dict(self.probe)


def test_wait_for_condition_polls_until_truthy():
    driver = _ConditionDriver([None, None, "hydrated"])
    _register(driver, "bundle-wait-cond")
    result = browser_tools.wait_for_condition(
        "window.__hydrated === true",
        session_id="bundle-wait-cond",
        timeout_seconds=5,
        poll_ms=10,
    )
    assert result["success"] is True
    assert result["value"] == "hydrated"
    assert result["waited_seconds"] >= 0


def test_wait_for_condition_times_out_falsy():
    driver = _ConditionDriver([None, 0, "", False])
    _register(driver, "bundle-wait-timeout")
    with pytest.raises(TimeoutException):
        browser_tools.wait_for_condition(
            "window.__never === true",
            session_id="bundle-wait-timeout",
            timeout_seconds=0.2,
            poll_ms=10,
        )


def test_wait_refuses_selector_and_script_together():
    driver = _ProbeDriver()
    _register(driver, "bundle-wait-both")
    with pytest.raises(ValueError, match="either selector or script"):
        browser_tools.wait_for_element(
            "#app", session_id="bundle-wait-both", script="window.x"
        )
    # A present-but-empty script is still a passed script: with no selector it
    # cannot poll and must say so instead of sleeping.
    with pytest.raises(ValueError, match="must not be empty"):
        browser_tools.wait_for_element("", session_id="bundle-wait-both", script="")


def test_wait_without_selector_or_script_is_a_plain_sleep():
    driver = _ProbeDriver()
    _register(driver, "bundle-wait-sleep")
    started = time.monotonic()
    result = browser_tools.wait_for_element(
        "", session_id="bundle-wait-sleep", timeout_seconds=0.2
    )
    assert result["success"] is True
    assert result["state"] == "sleep"
    assert result["selector"] == ""
    assert 0.15 <= time.monotonic() - started < 5
    # The sleep floor is honoured in the answer, not only in real time.
    clamped = browser_tools.wait_for_element(
        "", session_id="bundle-wait-sleep", timeout_seconds=0.0
    )
    assert clamped["timeout_seconds"] == 0.1


def test_wait_with_script_routes_to_condition():
    driver = _ConditionDriver(["yes"])
    _register(driver, "bundle-wait-route")
    result = browser_tools.wait_for_element(
        "", session_id="bundle-wait-route", script="window.ready", poll_ms=10
    )
    assert result["success"] is True
    assert result["value"] == "yes"


# --- Fix 4: fill focus --------------------------------------------------------


def test_keep_focus_read_script_does_not_blur():
    assert "blur" in browser_tools._FIELD_STATE_SCRIPT
    assert "blur" not in browser_tools._FIELD_STATE_KEEP_FOCUS_SCRIPT


def test_typing_emulation_inserts_per_character_with_focus_kept():
    driver = _ProbeDriver()
    element = object()
    browser_tools._type_text_like_typing(driver, element, "hey")
    texts = [
        params["text"]
        for command, params in driver.cdp_calls
        if command == "Input.insertText"
    ]
    assert texts == ["h", "e", "y"]
    assert driver.scripts and "focus" in driver.scripts[0][0]


class _FillElement:
    def __init__(self):
        self.cleared = 0
        self.keys: list[str] = []

    def clear(self):
        self.cleared += 1

    def send_keys(self, *keys):
        self.keys.extend(keys)


class _FillDriver(_ProbeDriver):
    def __init__(self, element):
        super().__init__()
        self.element = element
        self.read_script: str | None = None

    def execute_script(self, script, *args):
        self.scripts.append((script, list(args)))
        if "_FIELD_PREPARE" in script or "scrollIntoView" in script:
            return {"tag": "input", "type": "text"}
        self.read_script = script
        return {"kind": "text", "type": "text", "value": "typed"}


def _fill_monkeypatch(monkeypatch, driver, element):
    monkeypatch.setattr(
        browser_tools, "_resolve_element", lambda driver_, selector: element
    )
    monkeypatch.setattr(
        browser_tools,
        "_held_value",
        lambda state: state.get("value"),
    )
    monkeypatch.setattr(browser_tools, "_field_rejection", lambda state, exp: None)
    monkeypatch.setattr(
        browser_tools, "_release_locator_frame", lambda driver_, locator: None
    )


def test_fill_reports_blur_and_typing_modes(monkeypatch):
    driver = _FillDriver(_FillElement())
    _register(driver, "bundle-fill")
    _fill_monkeypatch(monkeypatch, driver, driver.element)
    result = browser_tools.fill_fields(
        {"#name": "typed"},
        session_id="bundle-fill",
        blur_after=False,
        typing=True,
    )
    assert result["success"] is True
    assert result["blur_after"] is False
    assert result["typing"] is True
    assert driver.read_script is not None and "blur" not in driver.read_script


# --- Fix 5: isolated contexts -------------------------------------------------


def test_isolated_profile_mode_resolves():
    mode, profile, address, key = browser_tools._profile_configuration(
        "s", "isolated", None, None
    )
    assert (mode, profile, address, key) == ("isolated", None, None, None)
    assert browser_tools._resolve_profile_mode("isolated", None) == "isolated"


def test_context_overrides_refused_on_current():
    driver = _ProbeDriver()
    with pytest.raises(ValueError, match="isolated"):
        browser_tools._apply_context_overrides(
            driver, "current", user_agent="Custom/1.0"
        )


def test_context_overrides_applied_on_owned_browser():
    driver = _ProbeDriver()
    applied = browser_tools._apply_context_overrides(
        driver, "temporary", user_agent="Custom/1.0", timezone="Europe/Berlin"
    )
    assert applied == {"user_agent": "Custom/1.0", "timezone": "Europe/Berlin"}
    commands = [command for command, _ in driver.cdp_calls]
    assert "Network.setUserAgentOverride" in commands
    assert "Emulation.setTimezoneOverride" in commands


def test_context_overrides_reject_bad_geolocation():
    driver = _ProbeDriver()
    with pytest.raises(ValueError, match="geolocation"):
        browser_tools._apply_context_overrides(
            driver, "temporary", geolocation={"latitude": "north"}
        )


def test_open_signature_offers_isolated_and_overrides():
    import inspect

    params = inspect.signature(main.browser_open_page).parameters
    assert "isolated" in str(params["profile_mode"].annotation)
    for name in ("user_agent", "timezone", "locale", "geolocation"):
        assert name in params


# --- Fix 6: fetch raw/download ------------------------------------------------


class _FakeFetchResponse:
    url = "https://example.test/bundle.js"
    text = "<html><head><script>var mining = true;</script></head><body>hi</body></html>"
    content = text.encode("utf-8")


def test_fetch_mode_validation_and_raw(monkeypatch):
    monkeypatch.setattr(main, "request", lambda *a, **k: _FakeFetchResponse())
    with pytest.raises(ValueError, match="mode must be"):
        main._fetch_url_text("https://example.test/x", mode="bogus")
    raw = main._fetch_url_text("https://example.test/x", mode="raw")
    assert "var mining = true;" in raw
    text = main._fetch_url_text("https://example.test/x", mode="text")
    assert "var mining" not in text
    assert "hi" in text


def test_fetch_custom_headers_forwarded(monkeypatch):
    seen: dict = {}

    def _fake_request(url, **kwargs):
        seen.update(kwargs)
        return _FakeFetchResponse()

    monkeypatch.setattr(main, "request", _fake_request)
    main._fetch_url_text(
        "https://example.test/x", headers={"Authorization": "Bearer secret"}
    )
    assert seen["headers"] == {"Authorization": "Bearer secret"}


def test_fetch_save_to_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "request", lambda *a, **k: _FakeFetchResponse())
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    target = tmp_path / "bundle.js"
    message = main._fetch_url_text(
        "https://example.test/bundle.js", save_to=str(target)
    )
    assert "Saved" in message and target.read_bytes() == _FakeFetchResponse.content
    with pytest.raises(ValueError, match="inside the download directory"):
        main._fetch_url_text(
            "https://example.test/x", save_to=str(tmp_path.parent / "elsewhere.js")
        )


# --- Fix 7: schema enums ------------------------------------------------------


def test_local_storage_schema_exposes_enums():
    schema = asyncio.run(main.web_info("action_schema", {"action": "local_storage"}))
    op = schema["input_schema"]["properties"]["op"]
    kind = schema["input_schema"]["properties"]["kind"]
    assert op["enum"] == ["read", "write", "delete"]
    assert kind["enum"] == ["local", "session"]


def test_capabilities_full_schemas_is_opt_in():
    plain = asyncio.run(main.web_info("capabilities"))
    assert "schemas" not in plain
    full = asyncio.run(main.web_info("capabilities", {"full_schemas": True}))
    assert full["schemas"]["local_storage"]["properties"]["op"]["enum"] == [
        "read",
        "write",
        "delete",
    ]
    with pytest.raises(ValueError, match="full_schemas"):
        asyncio.run(main.web_info("capabilities", {"bogus": True}))


# --- Fix 8: request mocks -----------------------------------------------------


def test_mock_validation():
    with pytest.raises(ValueError, match="url_pattern"):
        browser_tools._validate_mock("", 200, None, "")
    with pytest.raises(ValueError, match="200-599"):
        browser_tools._validate_mock("*", 99, None, "")
    with pytest.raises(ValueError, match="byte limit"):
        browser_tools._validate_mock("*", 200, None, "x" * (browser_tools._MOCK_BODY_LIMIT + 1))
    entry = browser_tools._validate_mock("https://api.test/*", 201, {"X-A": "b"}, "{}")
    assert entry["status"] == 201 and entry["pattern"] == "https://api.test/*"


def test_mock_add_list_clear_on_selenium_backend():
    driver = _ProbeDriver()
    _register(driver, "bundle-mock")
    added = browser_tools.mock_add_request(
        "https://api.test/*",
        session_id="bundle-mock",
        status=200,
        headers={"Content-Type": "application/json"},
        body='{"ok": true}',
    )
    assert added["success"] is True and added["mocks"] == 1
    sources = [params["source"] for command, params in driver.cdp_calls
               if command == "Page.addScriptToEvaluateOnNewDocument"]
    assert any(browser_tools._MOCK_STUB_SOURCE in source and "https://api.test/*" in source
               for source in sources)
    listed = browser_tools.mock_list_requests(session_id="bundle-mock")
    assert listed["count"] == 1
    assert listed["mocks"][0]["pattern"] == "https://api.test/*"
    cleared = browser_tools.mock_clear_requests(session_id="bundle-mock")
    assert cleared["success"] is True and cleared["cleared"] == 1
    removals = [
        command
        for command, _ in driver.cdp_calls
        if command == "Page.removeScriptToEvaluateOnNewDocument"
    ]
    assert removals


def test_mock_add_on_bridge_backend_uses_fetch_commands():
    calls: list[tuple[str, dict]] = []

    class _FakeBridge:
        def request(self, method, params, timeout=None):
            calls.append((method, params))
            if method == "mock.add":
                return {"mocked": True, "mocks": 1}
            raise AssertionError(method)

    class _BridgeDriver(_ProbeDriver):
        is_extension_bridge = True

        def __init__(self):
            super().__init__()
            self.tab_id = 7
            self.bridge = _FakeBridge()

    driver = _BridgeDriver()
    _register(driver, "bundle-mock-bridge")
    result = browser_tools.mock_add_request(
        "https://third.test/*", session_id="bundle-mock-bridge", body="{}"
    )
    assert result["success"] is True
    assert calls[0][0] == "mock.add"
    assert calls[0][1]["pattern"] == "https://third.test/*"


def test_mock_actions_registered_in_contract():
    assert "mock" in main._ACTIONS
    assert main._ACTIONS["mock"].group == "page"
    import inspect

    params = inspect.signature(main.browser_mock).parameters
    assert set(params) == {
        "op",
        "url_pattern",
        "session_id",
        "status",
        "headers",
        "body",
    }


def test_mock_dispatcher_validates_op():
    with pytest.raises(ValueError, match="url_pattern"):
        asyncio.run(main.browser_mock(op="add", url_pattern="  "))
    with pytest.raises(ValueError, match="mock op must be"):
        asyncio.run(main.browser_mock(op="bogus"))  # type: ignore[arg-type]
