"""Captcha: identification, waiting a human out, and the solving-service path."""

from __future__ import annotations

import pytest

import browser_tools
import captcha


class _CannedDriver:
    """A driver whose challenge verdict can change between polls, as a real one does.

    Both page-script routes land in ``execute_script``: the challenge probe and
    the captcha identify/apply scripts. They are told apart by what the script
    asks for, because that is the only thing that distinguishes them in the
    caller too.
    """

    is_extension_bridge = False

    def __init__(self, probes, identity=None, applied=None):
        self.probes = list(probes)
        self.identity = identity
        self.applied = applied or {"applied": True, "fields": 1, "callbacks": 1}
        self.scripts = []

    def execute_script(self, script, *args):
        self.scripts.append((script, list(args)))
        if "vendors" in script and "sitekey" in script:
            return self.identity
        if "spec.field" in script or "callbacks" in script:
            return self.applied
        return self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]

    def quit(self):
        pass


def _register(driver, session_id="default"):
    session = browser_tools.BrowserSession(driver=driver, headless=False)
    browser_tools._sessions[session_id] = session
    return session


CLEAR = {"widgets": [], "markers": [], "heading": "", "body_length": 5000, "body": ""}
BLOCKED = {
    "widgets": ["div.g-recaptcha"],
    "markers": [],
    "heading": "",
    "body_length": 120,
    "body": "",
    "blocking": True,
}
IDENTIFIED = {
    "vendor": "recaptcha",
    "sitekey": "6LtestKEY",
    "url": "https://hh.ru/apply",
    "task": "RecaptchaV2TaskProxyless",
}
NAMELESS = {"vendor": None, "sitekey": None, "url": "https://example.com", "task": None}


@pytest.fixture(autouse=True)
def no_configured_solver(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_NEO_CAPTCHA_KEY", raising=False)
    monkeypatch.delenv("WEB_SEARCH_NEO_CAPTCHA_HOST", raising=False)
    # execute_js decorates every answer with a page summary, which would run the
    # probe script an extra time and consume the queued verdicts.
    monkeypatch.setattr(
        browser_tools, "_page_summary", lambda driver, session_id: {"session_id": session_id}
    )


# --- configuration ----------------------------------------------------------


def test_solver_is_unconfigured_without_a_key():
    assert captcha.solver_config()["configured"] is False


def test_solver_reads_key_and_default_host(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    config = captcha.solver_config()
    assert config["configured"] is True
    assert config["host"] == "api.2captcha.com"


def test_solver_host_is_overridable(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_HOST", "api.anti-captcha.com")
    assert captcha.solver_config()["host"] == "api.anti-captcha.com"


def test_solving_without_a_key_is_refused_before_any_network_call():
    with pytest.raises(ValueError, match="WEB_SEARCH_NEO_CAPTCHA_KEY"):
        captcha.solve_remotely("RecaptchaV2TaskProxyless", "key", "https://example.com")


# --- detection --------------------------------------------------------------


def test_a_clear_page_reports_no_captcha():
    _register(_CannedDriver([CLEAR]))
    result = browser_tools.solve_captcha(mode="detect")
    assert result["success"] is True
    assert result["captcha_present"] is False


def test_detect_reports_a_blocking_widget_without_waiting():
    _register(_CannedDriver([BLOCKED]))
    result = browser_tools.solve_captcha(mode="detect")
    assert result["captcha_present"] is True
    assert result["challenge_detected"] is True
    assert result["challenge_type"] == "captcha"


def test_detect_mode_never_calls_a_solver(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("detect must not reach the solving service")

    monkeypatch.setattr(captcha, "solve_remotely", explode)
    _register(_CannedDriver([BLOCKED]))
    assert browser_tools.solve_captcha(mode="detect")["captcha_present"] is True


# --- waiting ----------------------------------------------------------------


def test_wait_returns_as_soon_as_the_challenge_clears():
    # Blocked when first probed, cleared by the time the poll comes round: this
    # is a human clicking the box while the call is parked.
    driver = _CannedDriver([BLOCKED, CLEAR, CLEAR], IDENTIFIED)
    _register(driver)
    result = browser_tools.solve_captcha(mode="wait", timeout_seconds=10, poll_seconds=0.5)
    assert result["success"] is True
    assert result["captcha_present"] is False
    assert result["mode"] == "wait"
    assert result["waited_seconds"] < 5


def test_wait_gives_up_with_an_actionable_message():
    driver = _CannedDriver([BLOCKED], IDENTIFIED)
    _register(driver)
    result = browser_tools.solve_captcha(mode="wait", timeout_seconds=5, poll_seconds=0.5)
    assert result["success"] is False
    assert result["captcha_present"] is True
    assert "WEB_SEARCH_NEO_CAPTCHA_KEY" in result["error"]


def test_auto_falls_back_to_waiting_when_no_service_is_configured():
    driver = _CannedDriver([BLOCKED, CLEAR], IDENTIFIED)
    _register(driver)
    result = browser_tools.solve_captcha(mode="auto", timeout_seconds=10, poll_seconds=0.5)
    assert result["mode"] == "wait"
    assert result["success"] is True


def test_unknown_mode_is_refused():
    _register(_CannedDriver([BLOCKED], IDENTIFIED))
    with pytest.raises(ValueError, match="detect, wait, solve, or auto"):
        browser_tools.solve_captcha(mode="bogus", timeout_seconds=1)


# --- solving ----------------------------------------------------------------


def test_solve_asks_the_service_and_applies_the_token(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    asked = {}

    def fake_solve(task_type, sitekey, page_url, timeout_seconds=180.0, poll_seconds=5.0):
        asked.update({"task": task_type, "sitekey": sitekey, "url": page_url})
        return {"token": "03AGdBq26...", "task_id": 7, "cost": "0.0029"}

    monkeypatch.setattr(captcha, "solve_remotely", fake_solve)
    driver = _CannedDriver([BLOCKED], IDENTIFIED)
    _register(driver)

    result = browser_tools.solve_captcha(mode="solve", timeout_seconds=30)
    assert result["success"] is True
    assert result["vendor"] == "recaptcha"
    assert result["cost"] == "0.0029"
    assert asked == {
        "task": "RecaptchaV2TaskProxyless",
        "sitekey": "6LtestKEY",
        "url": "https://hh.ru/apply",
    }
    # The token has to reach the page, not just the caller.
    assert any("03AGdBq26..." in str(args) for _, args in driver.scripts)


def test_auto_prefers_the_service_once_a_key_exists(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    monkeypatch.setattr(
        captcha,
        "solve_remotely",
        lambda *a, **k: {"token": "tok", "task_id": 1, "cost": "0.001"},
    )
    driver = _CannedDriver([BLOCKED], IDENTIFIED)
    _register(driver)
    assert browser_tools.solve_captcha(mode="auto")["mode"] == "solve"


def test_a_captcha_without_a_sitekey_cannot_be_sent_to_a_service(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    _register(_CannedDriver([BLOCKED], NAMELESS))
    with pytest.raises(ValueError, match="no sitekey"):
        browser_tools.solve_captcha(mode="solve")


def test_every_known_vendor_has_a_field_and_a_task_type():
    for name, spec in captcha._VENDORS.items():
        assert spec["detect"] and spec["field"] and spec["task"], name
