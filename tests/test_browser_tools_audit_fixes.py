"""Regressions for the browser_tools/captcha/macros audit findings.

Canned drivers except where noted; the captcha apply-script test drives a real
headless Chrome against the local fixture site and skips without one.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time

import pytest
from selenium.common.exceptions import (
    InvalidSelectorException, TimeoutException, WebDriverException,
)

from web_search_neo import browser_tools, captcha, chrome_bootstrap, macros
from web_search_neo.actions import stealth, waits
from web_search_neo.cdp import request_mocks
from web_search_neo.sessions import file_lock


class _Driver:
    is_extension_bridge = False

    def __init__(self, script_error: Exception | None = None):
        self.script_error = script_error
        self.cdp_calls: list[tuple[str, dict]] = []
        self.switch_to = type("SwitchTo", (), {"default_content": staticmethod(lambda: None)})()

    def execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))
        if command == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": f"id-{len(self.cdp_calls)}"}
        return {}

    def execute_script(self, script, *args):
        if self.script_error is not None:
            raise self.script_error
        return {"url": "https://example.test/", "title": "t", "challenge": {}}

    def quit(self):
        return None


def _register(session_id, driver=None, **kwargs):
    session = browser_tools.BrowserSession(driver=driver or _Driver(), headless=True, **kwargs)
    browser_tools._sessions[session_id] = session
    return session


@pytest.fixture(autouse=True)
def _quiet_summary(monkeypatch):
    monkeypatch.setattr(
        browser_tools, "_page_summary", lambda driver, session_id: {"session_id": session_id}
    )


# --- item 5: waits do not hold the session lock, and are capped ---------------


def test_plain_sleep_does_not_hold_the_session_lock():
    session = _register("audit-sleep")
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(
            browser_tools.wait_for_element("", session_id="audit-sleep", timeout_seconds=1.0)
        )
    )
    worker.start()
    time.sleep(0.2)
    assert session.lock.acquire(timeout=0.3), "sleep kept the session lock"
    session.lock.release()
    worker.join()
    assert result["state"] == "sleep"
    assert "timeout_note" not in result


def test_wait_clamp_reports_the_cut():
    assert waits.clamp_wait(10) == (10.0, None)
    value, note = waits.clamp_wait(10_000)
    assert value == waits.MAX_WAIT_SECONDS
    assert "clamped" in note
    assert waits.clamp_wait(0)[0] == 0.1


def test_challenge_wait_releases_the_lock_between_polls(monkeypatch):
    session = _register("audit-challenge")
    probes = {"n": 0}

    def probe(driver):
        probes["n"] += 1
        return {"challenge_detected": probes["n"] < 4}

    monkeypatch.setattr(browser_tools, "_challenge_status", probe)
    got_lock = threading.Event()

    def contender():
        time.sleep(0.05)
        if session.lock.acquire(timeout=2):
            got_lock.set()
            session.lock.release()

    thread = threading.Thread(target=contender)
    thread.start()
    result = browser_tools.wait_for_challenge_resolution(
        "audit-challenge", timeout_seconds=10, poll_interval_seconds=0.2
    )
    thread.join()
    assert result["resolved"] is True and result["success"] is True
    assert got_lock.is_set()


def test_challenge_wait_timeout_is_capped(monkeypatch):
    _register("audit-challenge-cap")
    monkeypatch.setattr(browser_tools, "_challenge_status", lambda d: {"challenge_detected": True})
    clock = iter(float(n) for n in range(0, 10_000, 100))
    monkeypatch.setattr(waits.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(waits.time, "sleep", lambda s: None)
    result = browser_tools.wait_for_challenge_resolution(
        "audit-challenge-cap", timeout_seconds=100_000, poll_interval_seconds=1
    )
    assert result["timed_out"] is True and result["resolved"] is False
    assert result["waited_seconds"] <= waits.MAX_WAIT_SECONDS + 100
    assert "clamped" in result["timeout_note"]


class _CountingDriver(_Driver):
    """``execute_script`` is falsy until the ``ready_after``-th call."""

    def __init__(self, ready_after):
        super().__init__()
        self.calls = 0
        self.ready_after = ready_after

    def execute_script(self, script, *args):
        self.calls += 1
        return self.calls >= self.ready_after


def _contend_for(lock, got):
    def contender():
        time.sleep(0.05)
        if lock.acquire(timeout=2):
            got.set()
            lock.release()

    thread = threading.Thread(target=contender)
    thread.start()
    return thread


def test_script_wait_releases_the_lock_between_polls():
    driver = _CountingDriver(ready_after=6)
    session = _register("audit-script-wait", driver=driver)
    got = threading.Event()
    thread = _contend_for(session.lock, got)
    result = browser_tools.wait_for_element(
        "", session_id="audit-script-wait", script="window.ready", timeout_seconds=10, poll_ms=60
    )
    thread.join()
    assert result["success"] is True and driver.calls == 6
    assert got.is_set(), "the script wait held the session lock throughout"
    assert "timeout_note" not in result


def test_script_wait_timeout_is_capped(monkeypatch):
    _register("audit-script-cap", driver=_CountingDriver(ready_after=10**9))
    clock = iter(float(n) for n in range(0, 100_000, 50))
    monkeypatch.setattr(waits.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(waits.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutException) as failure:
        browser_tools.wait_for_condition(
            "window.never", session_id="audit-script-cap", timeout_seconds=86_400
        )
    assert "after 300s" in failure.value.msg and "clamped" in failure.value.msg


class _Element:
    tag_name = "button"


def test_selector_wait_releases_the_lock_and_is_capped(monkeypatch):
    session = _register("audit-selector-wait")
    checks = {"n": 0}

    def one_check(driver, selector, state, timeout):
        assert timeout == 0.0
        checks["n"] += 1
        if checks["n"] < 5:
            raise TimeoutException(f"Selector '{selector}' was still not {state} after 0s")
        return _Element()

    monkeypatch.setattr(browser_tools, "_wait_for_locator", one_check)
    got = threading.Event()
    thread = _contend_for(session.lock, got)
    result = browser_tools.wait_for_element(
        "#go", session_id="audit-selector-wait", timeout_seconds=5000, poll_ms=60
    )
    thread.join()
    assert result["tag"] == "button" and checks["n"] == 5
    assert got.is_set(), "the selector wait held the session lock throughout"
    assert result["timeout_seconds"] == waits.MAX_WAIT_SECONDS
    assert "clamped" in result["timeout_note"]


def test_selector_wait_timeout_names_the_real_wait(monkeypatch):
    _register("audit-selector-timeout")

    def never(driver, selector, state, timeout):
        raise TimeoutException(f"Selector '{selector}' was still not {state} after 0s")

    monkeypatch.setattr(browser_tools, "_wait_for_locator", never)
    with pytest.raises(TimeoutException) as failure:
        browser_tools.wait_for_element(
            "#missing", session_id="audit-selector-timeout", timeout_seconds=0.3, poll_ms=50
        )
    assert failure.value.msg == "Selector '#missing' was still not visible after 0.3s"


# --- item 6: self-update poll does not spin -----------------------------------


def test_reload_companion_poll_sleeps(monkeypatch):
    class Bridge:
        calls = 0

        def request(self, *args):
            return {"version": "1.0"}

        def status(self, timeout):
            Bridge.calls += 1
            return {"connected": False}

    clock = {"t": 0.0}
    monkeypatch.setattr(chrome_bootstrap.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        chrome_bootstrap.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)
    )
    outcome = chrome_bootstrap._reload_companion(Bridge(), "2.0")
    assert outcome["self_update"] == "timeout"
    assert Bridge.calls <= int(15.0 / chrome_bootstrap._SELF_UPDATE_POLL_SECONDS) + 1


# --- item 7: cross-process ledger ---------------------------------------------


def _reserve_many(project: str, prefix: str, count: int) -> None:
    for index in range(count):
        path = macros._guarded_ledger_path(project)
        with file_lock.exclusive(path):
            ledger = macros._load_guarded_ledger(project)
            time.sleep(0.001)
            ledger["tokens"][f"{prefix}-{index}"] = {"state": "staged"}
            macros._write_guarded_ledger(ledger, project)


def test_ledger_updates_are_not_lost_across_processes(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(target=_reserve_many, args=(str(project), f"p{n}", 15)) for n in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    ledger = macros._load_guarded_ledger(str(project))
    assert len(ledger["tokens"]) == 45


def test_exclusive_is_reentrant_within_a_thread(tmp_path):
    path = tmp_path / "ledger.json"
    started = time.monotonic()
    with file_lock.exclusive(path, timeout=2.0):
        with file_lock.exclusive(path, timeout=2.0):
            with file_lock.exclusive(path, timeout=2.0):
                pass
        # Still held after the nested blocks: another thread cannot take it.
        taken: list[bool] = []

        def contend():
            try:
                with file_lock.exclusive(path, timeout=0.2):
                    taken.append(True)
            except TimeoutError:
                taken.append(False)

        worker = threading.Thread(target=contend)
        worker.start()
        worker.join()
        assert taken == [False]
    assert time.monotonic() - started < 1.5
    # Released by the outermost block.
    worker = threading.Thread(target=contend)
    worker.start()
    worker.join()
    assert taken == [False, True]


def test_atomic_write_uses_unique_temp_and_retries_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "ledger.json"
    real_replace = os.replace
    attempts = {"n": 0}
    sources = []

    def flaky(src, dst):
        attempts["n"] += 1
        sources.append(os.path.basename(src))
        if attempts["n"] < 3:
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(file_lock.os, "replace", flaky)
    file_lock.atomic_write_text(target, json.dumps({"a": 1}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert attempts["n"] == 3
    assert sources[0] != "ledger.tmp" and sources[0].endswith(".tmp")
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


# --- item 8: mock registry cleared on every teardown path -----------------------


def test_mock_registry_forgotten_when_the_browser_is_gone(monkeypatch):
    session = _register("audit-mock-gone")
    browser_tools.mock_add_request("https://api.test/*", session_id="audit-mock-gone")
    assert "audit-mock-gone" in request_mocks._REQUEST_MOCKS
    monkeypatch.setattr(browser_tools, "_browser_run_changed", lambda s: "new-run")
    browser_tools._sessions.pop("audit-mock-gone", None)
    outcome = browser_tools._shutdown_session(session, None, "audit-mock-gone")
    assert outcome["browser_gone"] is True
    assert "audit-mock-gone" not in request_mocks._REQUEST_MOCKS
    assert "audit-mock-gone" not in request_mocks._MOCK_STUB_SCRIPT_IDS


def test_mock_registry_forgotten_when_close_tabs_drops_the_session(monkeypatch):
    _register("audit-mock-tab", current_tab_id=77)
    request_mocks._REQUEST_MOCKS["audit-mock-tab"] = [{"pattern": "*"}]
    monkeypatch.setattr(
        browser_tools, "close_current_chrome_tabs", lambda *a, **k: {"closed": [{"tab_id": 77}]}
    )
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", lambda tab_id: None)
    result = browser_tools.close_tabs([77])
    assert result["sessions_dropped"] == ["audit-mock-tab"]
    assert "audit-mock-tab" not in request_mocks._REQUEST_MOCKS


def test_mock_registry_forgotten_when_a_closed_tab_is_shut_down(monkeypatch):
    session = _register("audit-mock-close")
    session.owns_browser = False
    request_mocks._REQUEST_MOCKS["audit-mock-close"] = [{"pattern": "*"}]
    monkeypatch.setattr(browser_tools, "_browser_run_changed", lambda s: None)
    browser_tools._sessions.pop("audit-mock-close", None)
    session.driver.service = type("S", (), {"stop": staticmethod(lambda: None)})()
    browser_tools._shutdown_session(session, True, "audit-mock-close")
    assert "audit-mock-close" not in request_mocks._REQUEST_MOCKS


# --- item 9: frame lookup errors -------------------------------------------------


def test_frame_count_reports_real_errors():
    broken = _Driver(script_error=WebDriverException("javascript error: boom"))
    with pytest.raises(ValueError) as caught:
        browser_tools._select_frame(broken, "iframe#x")
    assert "not a valid CSS selector" not in str(caught.value)
    assert "boom" in str(caught.value)
    bad = _Driver(script_error=InvalidSelectorException("bad"))
    with pytest.raises(ValueError, match="not a valid CSS selector"):
        browser_tools._select_frame(bad, "iframe[")


# --- item 10: stealth languages follow the locale --------------------------------


def test_stealth_languages_follow_the_session_locale(monkeypatch):
    assert stealth.stealth_languages(None) == ["en-US", "en"]
    assert stealth.stealth_languages("de_DE") == ["de-DE", "de"]
    assert stealth.stealth_languages("fr") == ["fr"]
    session = _register("audit-stealth")
    session.locale_override = "ru-RU"
    sources = []

    def fake_inject(op, session_id, source=None, identifier=None):
        sources.append(source)
        return {"identifier": "s1", "removed": True}

    monkeypatch.setattr(browser_tools, "inject_script", fake_inject)
    browser_tools.stealth("on", session_id="audit-stealth")
    assert '["ru-RU", "ru"]' in sources[0]
    assert "en-US" not in sources[0]


# --- item 2: the apply script against a real page ---------------------------------


def test_apply_script_invokes_data_callback_and_refuses_a_changed_page(local_site):
    try:
        browser_tools.open_page(
            f"{local_site.base_url}/page", session_id="audit-apply", headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    try:
        driver = browser_tools._get_session("audit-apply").driver
        driver.execute_script(
            "document.body.innerHTML = '<div class=\"h-captcha\" data-sitekey=\"k\" "
            "data-callback=\"onDone\"></div><textarea name=\"h-captcha-response\"></textarea>';"
            "window.hcaptchaOnLoad = () => { window.loadHookCalled = true; };"
            "window.onDone = (t) => { window.gotToken = t; };"
        )
        url = driver.current_url
        applied = driver.execute_script(captcha.APPLY_TOKEN_SCRIPT, "hcaptcha", "TOKEN", url)
        assert applied["applied"] is True and applied["callbacks"] == 1
        assert driver.execute_script("return window.gotToken") == "TOKEN"
        assert driver.execute_script("return !!window.loadHookCalled") is False
        refused = driver.execute_script(
            captcha.APPLY_TOKEN_SCRIPT, "hcaptcha", "OTHER", url + "#elsewhere-not-this"
        )
        assert refused["applied"] is False and "changed" in refused["reason"]
        assert driver.execute_script("return window.gotToken") == "TOKEN"
        # A data-callback naming a non-function (or an expression) is never run.
        driver.execute_script(
            "document.querySelector('.h-captcha').setAttribute('data-callback', 'alert(1)');"
        )
        odd = driver.execute_script(captcha.APPLY_TOKEN_SCRIPT, "hcaptcha", "T2", url)
        assert odd["callbacks"] == 0 and odd["applied"] is True
    finally:
        browser_tools.close_session("audit-apply")
