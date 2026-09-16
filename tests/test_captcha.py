"""Captcha: identification, waiting a human out, and the solving-service path."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from web_search_neo import browser_tools
from web_search_neo import captcha


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
        # Both scripts embed the vendor table, so the apply script is recognised
        # first by what only it does.
        if "spec.field" in script:
            return self.applied
        if "vendors" in script and "sitekey" in script:
            return self.identity
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


def test_auto_never_spends_money_just_because_a_key_exists(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    monkeypatch.delenv("WEB_SEARCH_NEO_CAPTCHA_AUTO_SOLVE", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("auto must not reach the paid service without opt-in")

    monkeypatch.setattr(captcha, "solve_remotely", explode)
    _register(_CannedDriver([BLOCKED, CLEAR], IDENTIFIED))
    result = browser_tools.solve_captcha(mode="auto", timeout_seconds=10, poll_seconds=0.5)
    assert result["mode"] == "wait"
    assert result["success"] is True


def test_auto_solves_only_with_the_explicit_env_opt_in(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_AUTO_SOLVE", "1")
    monkeypatch.setattr(
        captcha,
        "solve_remotely",
        lambda *a, **k: {"token": "tok", "task_id": 1, "cost": "0.001"},
    )
    driver = _CannedDriver([BLOCKED], IDENTIFIED)
    _register(driver)
    assert browser_tools.solve_captcha(mode="auto")["mode"] == "solve"


def test_auto_opt_in_without_a_key_still_waits(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_AUTO_SOLVE", "1")
    _register(_CannedDriver([BLOCKED, CLEAR], IDENTIFIED))
    result = browser_tools.solve_captcha(mode="auto", timeout_seconds=10, poll_seconds=0.5)
    assert result["mode"] == "wait"


def test_solve_mode_without_a_key_is_refused(monkeypatch):
    _register(_CannedDriver([BLOCKED], IDENTIFIED))
    with pytest.raises(ValueError, match="WEB_SEARCH_NEO_CAPTCHA_KEY"):
        browser_tools.solve_captcha(mode="solve")


def test_unknown_mode_is_refused_even_on_a_clear_page():
    _register(_CannedDriver([CLEAR]))
    with pytest.raises(ValueError, match="detect, wait, solve, or auto"):
        browser_tools.solve_captcha(mode="bogus")


def test_a_token_the_page_did_not_take_is_not_success(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    monkeypatch.setattr(
        captcha, "solve_remotely", lambda *a, **k: {"token": "tok", "task_id": 1, "cost": "0.002"}
    )
    driver = _CannedDriver(
        [BLOCKED], IDENTIFIED, applied={"applied": False, "fields": 0, "callbacks": 0}
    )
    _register(driver)
    result = browser_tools.solve_captcha(mode="solve")
    assert result["success"] is False
    assert "did not take it" in result["error"]


def test_the_token_is_only_applied_to_the_url_it_was_minted_for(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    monkeypatch.setattr(
        captcha, "solve_remotely", lambda *a, **k: {"token": "tok", "task_id": 1, "cost": None}
    )
    changed = {"applied": False, "reason": "the page changed since the captcha was found"}
    driver = _CannedDriver([BLOCKED], IDENTIFIED, applied=changed)
    _register(driver)
    result = browser_tools.solve_captcha(mode="solve")
    assert result["success"] is False
    assert "page changed" in result["error"]
    apply_args = [args for script, args in driver.scripts if "spec.field" in script]
    assert apply_args == [["recaptcha", "tok", "https://hh.ru/apply"]]
    assert "location.href !== expectedUrl" in captcha.APPLY_TOKEN_SCRIPT


def test_enterprise_uses_the_enterprise_task(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    asked = {}
    monkeypatch.setattr(
        captcha, "solve_remotely",
        lambda task, *a, **k: asked.setdefault("task", task) and {"token": "t", "cost": None},
    )
    identity = {**IDENTIFIED, "enterprise": True, "task": "RecaptchaV2EnterpriseTaskProxyless"}
    _register(_CannedDriver([BLOCKED], identity))
    result = browser_tools.solve_captcha(mode="solve")
    assert asked["task"] == "RecaptchaV2EnterpriseTaskProxyless"
    assert result["enterprise"] is True
    assert "grecaptcha.enterprise" in captcha.IDENTIFY_SCRIPT


def test_a_variant_with_no_task_is_refused_before_paying(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    monkeypatch.setattr(captcha, "solve_remotely", lambda *a, **k: pytest.fail("paid"))
    _register(_CannedDriver([BLOCKED], {**IDENTIFIED, "task": None}))
    with pytest.raises(ValueError, match="No solving task"):
        browser_tools.solve_captcha(mode="solve")


def test_apply_script_calls_the_widget_data_callback_not_the_load_hook():
    script = captcha.APPLY_TOKEN_SCRIPT
    assert "hcaptchaOnLoad" not in script
    assert "getAttribute('data-callback')" in script
    assert "typeof target === 'function'" in script
    for name in ("recaptcha", "hcaptcha", "turnstile"):
        assert "[data-callback]" in captcha._VENDORS[name]["widget"]


def test_missing_task_id_fails_fast(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    calls = []
    monkeypatch.setattr(captcha, "_post", lambda url, payload, timeout: calls.append(url) or {"errorId": 0})
    monkeypatch.setattr(captcha.time, "sleep", lambda s: pytest.fail("must not poll"))
    with pytest.raises(RuntimeError, match="no taskId"):
        captcha.solve_remotely("TurnstileTaskProxyless", "k", "https://example.com")
    assert len(calls) == 1


def test_timeout_message_reports_the_real_budget(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    answers = iter([{"taskId": 9}] + [{"status": "processing"}] * 100)
    monkeypatch.setattr(captcha, "_post", lambda *a, **k: next(answers))
    clock = iter(float(n) for n in range(0, 1000, 10))
    monkeypatch.setattr(captcha.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(captcha.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="within 30s"):
        captcha.solve_remotely("TurnstileTaskProxyless", "k", "https://e.com", timeout_seconds=5)


def test_a_captcha_without_a_sitekey_cannot_be_sent_to_a_service(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    _register(_CannedDriver([BLOCKED], NAMELESS))
    with pytest.raises(ValueError, match="no sitekey"):
        browser_tools.solve_captcha(mode="solve")


def test_every_known_vendor_has_a_field_and_a_task_type():
    for name, spec in captcha._VENDORS.items():
        assert spec["detect"] and spec["field"] and spec["task"], name


def test_a_solve_wait_is_capped_and_says_so(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    budgets = []

    def fake_solve(task_type, sitekey, page_url, timeout_seconds=180.0, poll_seconds=5.0):
        budgets.append(timeout_seconds)
        return {"token": "tok", "task_id": 1, "cost": None}

    monkeypatch.setattr(captcha, "solve_remotely", fake_solve)
    _register(_CannedDriver([BLOCKED], IDENTIFIED))
    result = browser_tools.solve_captcha(mode="solve", timeout_seconds=3600)
    assert budgets == [300.0]
    assert "clamped to 300s" in result["timeout_note"]


def test_solve_remotely_never_polls_past_the_cap(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_CAPTCHA_KEY", "secret")
    answers = iter([{"taskId": 9}] + [{"status": "processing"}] * 100)
    monkeypatch.setattr(captcha, "_post", lambda *a, **k: next(answers))
    clock = iter(float(n) for n in range(0, 10000, 10))
    monkeypatch.setattr(captcha.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(captcha.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="within 300s"):
        captcha.solve_remotely("TurnstileTaskProxyless", "k", "https://e.com", timeout_seconds=1e9)


_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_dotted_data_callback_is_called_on_its_owner():
    harness = """
const widget = {getAttribute: () => 'app.captcha.onSolved'};
const field = {value: '', dispatchEvent() {}};
globalThis.location = {href: 'https://e.test/form'};
globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.document = {querySelectorAll: (sel) => sel.includes('data-callback') ? [widget] : [field]};
globalThis.window = globalThis;
globalThis.app = {captcha: {name: 'owner', onSolved(token) { this.got = token; globalThis.seenThis = this.name; }}};
const run = new Function(%s);
const out = run('hcaptcha', 'TOKEN', 'https://e.test/form');
console.log(JSON.stringify({out, seenThis: globalThis.seenThis, got: app.captcha.got}));
""" % json.dumps(captcha.APPLY_TOKEN_SCRIPT.replace("arguments[", "__args["))
    harness = harness.replace("new Function(", "new Function('...__args', ")
    completed = subprocess.run(
        [_NODE, "-e", harness], capture_output=True, text=True, timeout=30, check=False
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["seenThis"] == "owner" and report["got"] == "TOKEN"
    assert report["out"]["callbacks"] == 1 and report["out"]["applied"] is True
