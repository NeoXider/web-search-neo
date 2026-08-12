"""Stateful Selenium browser sessions used by MCP browser tools."""

from __future__ import annotations

import atexit
import base64
from dataclasses import dataclass, field
from http.client import HTTPConnection
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as conditions
from selenium.webdriver.support.ui import Select, WebDriverWait

from chrome_bridge import (
    CHROME_EXTENSION_ID,
    ChromeBridgeDriver,
    ChromeBridgeError,
    get_chrome_bridge,
    list_current_chrome_tabs,
)
from chrome_bootstrap import (
    EXTENSION_DIR,
    expected_extension_version,
    setup_current_chrome,
)
import diagnostics
import key_table
import page_perception
from web_client import validate_http_url


logger = logging.getLogger(__name__)

MAX_SESSIONS = 4
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Chrome hands out its browser log exactly once, so the session keeps a bounded copy.
_BROWSER_LOG_LIMIT = 500


@dataclass
class BrowserSession:
    driver: Any
    headless: bool
    profile_mode: str = "temporary"
    profile_id: str | None = None
    debugger_address: str | None = None
    current_tab_id: int | None = None
    tab_group: str | None = None
    owns_browser: bool = True
    held_keys: dict[str, str] = field(default_factory=dict)
    held_buttons: set[str] = field(default_factory=set)
    pointer_x: float = 0.0
    pointer_y: float = 0.0
    # (0, 0) is a legitimate pointer position, so "never moved" needs its own flag.
    pointer_initialized: bool = False
    render_mode: str = "normal"
    key_repeat: bool = True
    render_target_fps: float | None = None
    render_frame_selector: str | None = None
    render_bootstrap_registered: bool = False
    render_options: dict[str, Any] = field(
        default_factory=lambda: {
            "frame_delta_ms": 1000 / 60,
            "freeze_time": True,
            "gate_timers": True,
        }
    )
    owns_tab: bool = False
    pointer_locked: bool = False
    touch_enabled: bool = False
    fresh_keys: set[str] = field(default_factory=set)
    console_cursor: int = 0
    browser_log: list[dict[str, Any]] = field(default_factory=list)
    browser_log_cursor: int = 0
    # game_probe reads the same two sources as the console topic, but reports
    # what is new to *it*, so it carries its own place in both of them.
    probe_console_cursor: int = 0
    probe_log_cursor: int = 0
    probe_console_seen: list[dict[str, Any]] = field(default_factory=list)
    network_pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    network_rows: list[dict[str, Any]] = field(default_factory=list)
    network_subscribed: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    last_used: float = field(default_factory=time.monotonic)


_sessions: dict[str, BrowserSession] = {}
_sessions_lock = threading.RLock()
_sessions_condition = threading.Condition(_sessions_lock)
_pending_sessions: set[str] = set()
_pending_browser_keys: set[str] = set()
_pending_current_tab_ids: set[int] = set()
_browser_available: bool | None = None
_browser_error: str | None = None


def _validate_session_id(session_id: str) -> str:
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            "session_id must be 1-64 characters using letters, digits, '.', '_' or '-'"
        )
    return session_id


def _bounded_size(width: int, height: int) -> tuple[int, int]:
    return max(320, min(int(width), 3840)), max(240, min(int(height), 2160))


def _validate_debugger_address(debugger_address: str | None) -> str:
    value = (debugger_address or os.getenv("WEB_SEARCH_NEO_DEBUGGER_ADDRESS") or "").strip()
    match = re.fullmatch(r"(localhost|127\.0\.0\.1):([0-9]{1,5})", value)
    if not match or not 1 <= int(match.group(2)) <= 65535:
        raise ValueError(
            "debugger_address must be a local address such as 127.0.0.1:9222"
        )
    return value


def _persistent_profile_dir(profile_id: str) -> Path:
    _validate_session_id(profile_id)
    configured_root = os.getenv("WEB_SEARCH_NEO_PROFILE_ROOT")
    if configured_root:
        root = Path(configured_root).expanduser()
    elif os.getenv("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "WebSearchNeo" / "profiles"
    else:
        root = Path.home() / ".web-search-neo" / "profiles"
    path = (root / profile_id).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _profile_configuration(
    session_id: str,
    profile_mode: str,
    profile_id: str | None,
    debugger_address: str | None,
) -> tuple[str, str | None, str | None, str | None]:
    mode = profile_mode.strip().lower()
    if mode == "extension":
        mode = "current"
    if mode not in {"temporary", "persistent", "attach", "current"}:
        raise ValueError(
            "profile_mode must be 'auto', 'current', 'temporary', 'persistent', or 'attach'"
        )
    if mode == "temporary":
        return mode, None, None, None
    if mode == "persistent":
        selected_profile = _validate_session_id(profile_id or session_id)
        return mode, selected_profile, None, f"persistent:{selected_profile}"
    if mode == "attach":
        address = _validate_debugger_address(debugger_address)
        return mode, None, address, f"attach:{address}"
    return mode, None, None, None


def _resolve_profile_mode(profile_mode: str, headless: bool | None) -> str:
    mode = profile_mode.strip().lower()
    if mode == "extension":
        mode = "current"
    if mode == "auto":
        if headless is True:
            return "temporary"
        return "current" if get_chrome_bridge().wait_connected(1.5) else "temporary"
    if mode not in {"current", "temporary", "persistent", "attach"}:
        raise ValueError(
            "profile_mode must be 'auto', 'current', 'temporary', 'persistent', or 'attach'"
        )
    if mode == "current" and headless is True:
        raise ValueError("profile_mode='current' controls a visible Chrome and cannot be headless")
    return mode


def resolve_profile_mode(profile_mode: str, headless: bool | None = None) -> str:
    """Resolve auto once so a multi-page request cannot split across browser modes."""
    return _resolve_profile_mode(profile_mode, headless)


def _resolve_headless(profile_mode: str, headless: bool | None) -> bool:
    """Default new browser sessions to a visible window unless explicitly hidden."""
    mode = profile_mode.strip().lower()
    if mode in {"extension", "current"}:
        return False
    if mode not in {"temporary", "persistent", "attach"}:
        raise ValueError("profile_mode must be 'temporary', 'persistent', 'attach', or 'current'")
    if headless is None:
        return False
    return bool(headless)


def _cached_chromedriver_for_debugger(debugger_address: str | None) -> Path | None:
    """Select a cached driver with the same Chrome major for reliable attach startup."""
    if not debugger_address:
        return None
    try:
        host, port = debugger_address.rsplit(":", 1)
        connection = HTTPConnection(host, int(port), timeout=2)
        try:
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            browser = str(json.loads(response.read()).get("Browser", ""))
        finally:
            connection.close()
        match = re.search(r"Chrome/([0-9]+)", browser)
        if not match:
            return None
        major = int(match.group(1))
    except Exception:
        return None
    cache_root = Path(
        os.getenv("SE_CACHE_PATH", str(Path.home() / ".cache" / "selenium"))
    ).expanduser()
    executable = "chromedriver.exe" if os.name == "nt" else "chromedriver"
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for path in cache_root.glob(f"chromedriver/**/{executable}"):
        try:
            version = tuple(int(part) for part in path.parent.name.split("."))
        except ValueError:
            continue
        if version and version[0] == major and path.is_file():
            candidates.append((version, path))
    return max(candidates, default=((), None), key=lambda item: item[0])[1]


def _latest_cached_chromedriver() -> Path | None:
    cache_root = Path(
        os.getenv("SE_CACHE_PATH", str(Path.home() / ".cache" / "selenium"))
    ).expanduser()
    executable = "chromedriver.exe" if os.name == "nt" else "chromedriver"
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for path in cache_root.glob(f"chromedriver/**/{executable}"):
        try:
            version = tuple(int(part) for part in path.parent.name.split("."))
        except ValueError:
            continue
        if version and path.is_file():
            candidates.append((version, path))
    return max(candidates, default=((), None), key=lambda item: item[0])[1]


def create_driver(
    width: int = 1440,
    height: int = 900,
    headless: bool = True,
    profile_mode: str = "temporary",
    profile_id: str | None = None,
    debugger_address: str | None = None,
    current_tab_id: int | None = None,
    tab_group: str = "AI",
) -> Any:
    """Create Chrome through Selenium Manager, optionally visible for human handoff."""
    global _browser_available, _browser_error
    width, height = _bounded_size(width, height)
    resolved_mode = _resolve_profile_mode(profile_mode, headless)
    if resolved_mode == "current":
        driver = ChromeBridgeDriver(
            get_chrome_bridge(), tab_id=current_tab_id, tab_group=tab_group
        )
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(15)
        _browser_available = True
        _browser_error = None
        return driver
    mode, selected_profile, address, _browser_key = _profile_configuration(
        "driver", resolved_mode, profile_id, debugger_address
    )
    options = Options()
    # Selenium cannot subscribe to CDP events, so console and network history are
    # recovered from Chrome's own logs instead.
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})
    browser_user_agent = None
    if mode == "attach":
        options.debugger_address = address
    else:
        if headless:
            options.add_argument("--headless=new")
        options.add_argument(f"--window-size={width},{height}")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-extensions")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        if mode == "persistent" and selected_profile:
            options.add_argument(
                f"--user-data-dir={_persistent_profile_dir(selected_profile)}"
            )
        browser_user_agent = os.getenv("WEB_SEARCH_NEO_BROWSER_USER_AGENT")
        if browser_user_agent:
            options.add_argument(f"--user-agent={browser_user_agent}")
        proxy = os.getenv("WEB_SEARCH_NEO_PROXY")
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")
        options.page_load_strategy = "eager"
        options.add_experimental_option(
            "prefs",
            {
                "download.prompt_for_download": False,
                "profile.default_content_setting_values.notifications": 2,
            },
        )
    cached_driver = (
        _cached_chromedriver_for_debugger(address)
        if mode == "attach"
        else _latest_cached_chromedriver()
    )
    service = Service(executable_path=str(cached_driver)) if cached_driver else None
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as exc:
        if service is not None:
            try:
                driver = webdriver.Chrome(options=options)
            except Exception as retry_exc:
                _browser_available = False
                # Report the retry, not the stale cached-driver attempt: the
                # caller was previously shown an error it had not just hit.
                _browser_error = _describe_browser_failure(retry_exc)
                raise WebDriverException(_browser_error) from retry_exc
        else:
            _browser_available = False
            _browser_error = _describe_browser_failure(exc)
            raise WebDriverException(_browser_error) from exc
    if mode != "attach" and headless and not browser_user_agent:
        try:
            native_user_agent = str(
                driver.execute_script("return navigator.userAgent") or ""
            )
            if "HeadlessChrome/" in native_user_agent:
                driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": native_user_agent.replace("HeadlessChrome/", "Chrome/")},
                )
        except WebDriverException:
            # A matching native UA is an optimization; browser startup must not depend on it.
            pass
    _browser_available = True
    _browser_error = None
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(15)
    return driver


def _create_session(
    session_id: str,
    width: int,
    height: int,
    headless: bool | None,
    profile_mode: str = "auto",
    profile_id: str | None = None,
    debugger_address: str | None = None,
    current_tab_id: int | None = None,
    tab_group: str = "AI",
) -> BrowserSession:
    profile_mode = _resolve_profile_mode(profile_mode, headless)
    mode, selected_profile, address, browser_key = _profile_configuration(
        session_id, profile_mode, profile_id, debugger_address
    )
    effective_headless = _resolve_headless(mode, headless)
    with _sessions_condition:
        while session_id in _pending_sessions:
            _sessions_condition.wait(timeout=30)
        existing = _sessions.get(session_id)
        if existing is not None:
            if (
                (headless is not None and existing.headless != effective_headless)
                or existing.profile_mode != mode
                or existing.profile_id != selected_profile
                or existing.debugger_address != address
                or (
                    current_tab_id is not None
                    and existing.current_tab_id != current_tab_id
                )
            ):
                raise ValueError(
                    "Session already exists with different browser/profile options; close it first"
                )
            return existing
        if len(_sessions) + len(_pending_sessions) >= MAX_SESSIONS:
            raise RuntimeError(
                f"Maximum of {MAX_SESSIONS} browser sessions reached; close one first"
            )
        selected_current_tab_id = int(current_tab_id) if mode == "current" and current_tab_id is not None else None
        if selected_current_tab_id is not None:
            active_current_tabs = {
                int(item.current_tab_id)
                for item in _sessions.values()
                if item.profile_mode == "current" and item.current_tab_id is not None
            }
            if (
                selected_current_tab_id in active_current_tabs
                or selected_current_tab_id in _pending_current_tab_ids
            ):
                raise RuntimeError(
                    f"Current Chrome tab {selected_current_tab_id} is already claimed by another session"
                )
            _pending_current_tab_ids.add(selected_current_tab_id)
        if browser_key:
            active_keys = {
                (
                    f"persistent:{item.profile_id}"
                    if item.profile_mode == "persistent"
                    else f"attach:{item.debugger_address}"
                )
                for item in _sessions.values()
                if item.profile_mode in {"persistent", "attach"}
            }
            if browser_key in active_keys or browser_key in _pending_browser_keys:
                raise RuntimeError(
                    "This persistent profile or debugger address is already in use"
                )
            _pending_browser_keys.add(browser_key)
        _pending_sessions.add(session_id)
    session: BrowserSession | None = None
    try:
        driver = create_driver(
            width,
            height,
            effective_headless,
            mode,
            selected_profile,
            address,
            current_tab_id,
            tab_group,
        )
        session = BrowserSession(
            driver=driver,
            headless=effective_headless,
            profile_mode=mode,
            profile_id=selected_profile,
            debugger_address=address,
            current_tab_id=getattr(driver, "tab_id", None),
            tab_group=(getattr(driver, "actual_tab_group", None) if mode == "current" else None),
            owns_browser=mode != "attach",
            # A tab we created is ours to clean up; a claimed one belongs to the user.
            owns_tab=mode == "current" and current_tab_id is None,
        )
    finally:
        with _sessions_condition:
            if session is not None:
                _sessions[session_id] = session
            _pending_sessions.discard(session_id)
            if browser_key:
                _pending_browser_keys.discard(browser_key)
            if mode == "current" and current_tab_id is not None:
                _pending_current_tab_ids.discard(int(current_tab_id))
            _sessions_condition.notify_all()
    if session is None:
        raise RuntimeError("Browser session creation failed")
    return session


def _describe_browser_failure(exc: Exception) -> str:
    """Turn a driver-creation failure into something a caller can act on.

    Selenium's own text is doubled ("Message: Message: ...") and says nothing
    about what still works, which matters here: search, fetch, and the status
    topics never needed a browser.
    """
    detail = " ".join(str(exc).replace("Message:", "").split()).strip(" .")
    if "cannot find chrome binary" in detail.lower() or "no chrome binary" in detail.lower():
        detail = "Google Chrome was not found on this machine"
    return (
        f"Chrome is unavailable: {detail}. Install Google Chrome 116 or newer to use "
        "browser actions. Search, fetch_text, fetch_links, fetch_many and the "
        "search_status and time topics work without a browser."
    )


def _get_session(session_id: str) -> BrowserSession:
    _validate_session_id(session_id)
    with _sessions_lock:
        session = _sessions.get(session_id)
        open_sessions = sorted(_sessions)
    if session is None:
        # Name the call the caller actually has. Pointing at an internal helper
        # leaves a small model stuck with no way to recover.
        raise ValueError(
            f"Browser session '{session_id}' does not exist. Open one first: "
            f'web_action [{{"action":"open","url":...,"session_id":"{session_id}"}}]. '
            f"Open sessions: {open_sessions}."
        )
    session.last_used = time.monotonic()
    return session


def _wait_until_ready(driver: webdriver.Chrome, timeout_seconds: float) -> None:
    timeout = max(1.0, min(float(timeout_seconds), 30.0))
    WebDriverWait(driver, timeout).until(
        lambda current: current.execute_script("return document.readyState") == "complete"
    )


def _set_viewport(driver: Any, width: int, height: int) -> None:
    """Set exact CSS viewport dimensions instead of approximate outer-window size."""
    if getattr(driver, "is_extension_bridge", False):
        # Never resize or emulate the user's already-open personal Chrome window.
        return
    driver.execute_cdp_cmd(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    )


# A challenge is a live widget, not the word "captcha" in prose. Matching text
# alone flags every article about CAPTCHAs and every search result for the word,
# and then the agent waits three minutes for a human who is not needed.
_CHALLENGE_WIDGET_SCRIPT = """
const selectors = [
  'iframe[src*="recaptcha/api2"]', 'iframe[src*="recaptcha/enterprise"]',
  'iframe[src*="hcaptcha.com"]', 'iframe[src*="challenges.cloudflare.com"]',
  'iframe[src*="captcha-api.yandex"]', 'iframe[title*="captcha" i]',
  'div.g-recaptcha', 'div.h-captcha', 'div.cf-turnstile', 'div#cf-challenge-running',
  'form#challenge-form', '[data-sitekey]', '#px-captcha', '.smart-captcha'
];
const found = [];
for (const selector of selectors) {
  const element = document.querySelector(selector);
  if (!element) continue;
  const rect = element.getBoundingClientRect();
  if (rect.width < 20 || rect.height < 20) continue;
  found.push(selector);
}
const heading = (document.title || '') + ' ' +
  Array.from(document.querySelectorAll('h1, h2')).slice(0, 3)
    .map(node => node.innerText || '').join(' ');
const body = (document.body && document.body.innerText) || '';
return {
  widgets: found,
  heading: heading.slice(0, 400),
  body: body.slice(0, 2000),
  body_length: body.length
};
"""

# A challenge interstitial carries almost no content. Requiring a short page
# before trusting body text keeps an article about CAPTCHAs from being mistaken
# for one.
_CHALLENGE_BODY_LIMIT = 1500

# How far into the body text an interstitial phrase may sit and still count.
_CHALLENGE_LEAD_LIMIT = 200

_CHALLENGE_HEADINGS = {
    "human_verification": (
        "verify you are human",
        "checking your browser",
        "just a moment",
        "подтвердите, что вы человек",
        "подтвердите, что вы не робот",
        "я не робот",
    ),
    "access_challenge": (
        "unusual traffic",
        "access denied",
        "are you a robot",
        "необычный трафик",
    ),
}


# Everything an action reports about the page, in one script. Reading url, title
# and dimensions separately costs three round-trips per action, and over the
# companion bridge each one is a full WebSocket exchange.
_PAGE_SUMMARY_SCRIPT = (
    """
let challenge = {};
try { challenge = (() => {"""
    + _CHALLENGE_WIDGET_SCRIPT
    + """})(); } catch (error) { challenge = {}; }
return {
  url: location.href,
  title: document.title || '',
  viewport_width: window.innerWidth,
  viewport_height: window.innerHeight,
  page_width: document.documentElement.scrollWidth,
  page_height: document.documentElement.scrollHeight,
  ready_state: document.readyState,
  challenge: challenge
};
"""
)


def _classify_challenge(probe: dict[str, Any]) -> dict[str, Any]:
    """Turn raw page markers into a challenge verdict."""
    widgets = probe.get("widgets") or []
    if widgets:
        return {
            "challenge_detected": True,
            "challenge_type": "captcha",
            "challenge_evidence": widgets[:3],
            "manual_action_required": True,
        }
    heading = str(probe.get("heading") or "").lower()
    sparse_body = (
        str(probe.get("body") or "").lower()
        if int(probe.get("body_length") or 0) <= _CHALLENGE_BODY_LIMIT
        else ""
    )
    for challenge_type, phrases in _CHALLENGE_HEADINGS.items():
        for phrase in phrases:
            if phrase in heading:
                evidence = "page heading"
            elif sparse_body and 0 <= sparse_body.find(phrase) <= _CHALLENGE_LEAD_LIMIT:
                # An interstitial opens with the phrase; an article about
                # captchas mentions it somewhere in the middle of a paragraph.
                evidence = "interstitial text"
            else:
                continue
            return {
                "challenge_detected": True,
                "challenge_type": challenge_type,
                "challenge_evidence": [evidence],
                "manual_action_required": True,
            }
    return {
        "challenge_detected": False,
        "challenge_type": None,
        "challenge_evidence": [],
        "manual_action_required": False,
    }


def _challenge_status(driver: webdriver.Chrome) -> dict[str, Any]:
    """Detect an interactive challenge by its widget, not by page prose."""
    try:
        probe = driver.execute_script(_CHALLENGE_WIDGET_SCRIPT) or {}
    except Exception:
        probe = {}
    return _classify_challenge(probe)


def _action_summary(
    driver: webdriver.Chrome, session_id: str, include_summary: bool
) -> dict[str, Any]:
    """Dropping the summary saves a whole round-trip on the hot input paths."""
    if not include_summary:
        return {"session_id": session_id}
    return _page_summary(driver, session_id)


def _page_summary(driver: webdriver.Chrome, session_id: str) -> dict[str, Any]:
    probe = driver.execute_script(_PAGE_SUMMARY_SCRIPT) or {}
    challenge = probe.pop("challenge", None) or {}
    return {
        "session_id": session_id,
        **probe,
        **_classify_challenge(challenge),
    }


def open_page(
    url: str,
    session_id: str = "default",
    width: int = 1440,
    height: int = 900,
    timeout_seconds: float = 20.0,
    headless: bool | None = None,
    profile_mode: str = "auto",
    profile_id: str | None = None,
    debugger_address: str | None = None,
    current_tab_id: int | None = None,
    tab_group: str = "AI",
) -> dict[str, Any]:
    """Open a URL in a reusable rendered browser session."""
    normalized = validate_http_url(url)
    session_id = _validate_session_id(session_id)
    width, height = _bounded_size(width, height)
    session = _create_session(
        session_id,
        width,
        height,
        headless,
        profile_mode,
        profile_id,
        debugger_address,
        current_tab_id,
        tab_group,
    )
    try:
        with session.lock:
            previous_mode = session.render_mode
            previous_frame = session.render_frame_selector
            _reset_session_runtime_state(session)
            _set_viewport(session.driver, width, height)
            _register_render_bootstrap(session)
            session.driver.get(normalized)
            _wait_until_ready(session.driver, timeout_seconds)
            # A new document drops the gate. Re-arm it, because a caller that
            # asked this session for step mode expects the next page to be
            # frozen too, not to run free while they wonder why nothing steps.
            restored = previous_mode != "normal"
            if restored:
                state = _apply_render_mode(
                    session.driver,
                    previous_frame,
                    previous_mode,
                    session.render_target_fps or 60.0,
                    session.render_options,
                )
                if not state.get("error"):
                    session.render_mode = previous_mode
                    session.render_frame_selector = previous_frame
                else:
                    restored = False
        return {
            **_page_summary(session.driver, session_id),
            "render_mode": session.render_mode,
            "render_mode_restored": restored,
            "headless": session.headless,
            "window_mode": "headless" if session.headless else "visible",
            "profile_mode": session.profile_mode,
            "profile_id": session.profile_id,
            "debugger_address": session.debugger_address,
            "current_tab_id": session.current_tab_id,
            "tab_group": session.tab_group,
        }
    except (WebDriverException, ChromeBridgeError, TimeoutError, ConnectionError, OSError):
        close_session(session_id)
        raise


def _companion_status() -> dict[str, Any]:
    """Report the companion in terms a caller can act on.

    ``connected: false`` on its own says nothing about what to do next, so the
    expected and running extension versions, its folder, and the exact next call
    are reported alongside it.
    """
    status = dict(get_chrome_bridge().status(0.0))
    expected = expected_extension_version()
    running = str((status.get("browser") or {}).get("extension_version") or "")
    status.update(
        extension_id=CHROME_EXTENSION_ID,
        extension_directory=str(EXTENSION_DIR),
        expected_version=expected,
        running_version=running or None,
        outdated=bool(running and expected and running != expected),
    )
    if not status["connected"]:
        status["next"] = (
            'Not connected. Send {"action": "setup_current_chrome"} for the exact '
            "steps the user has to perform; nothing can install the extension for "
            "them. Selenium modes (profile_mode temporary/persistent) need no "
            "extension."
        )
    elif status["outdated"]:
        status["next"] = (
            f"The connected companion is {running} but this server ships {expected}. "
            "Press Reload on its card at chrome://extensions; run "
            "setup_current_chrome for the exact steps."
        )
    else:
        status["next"] = None
    return status


def get_current_tabs(wait_seconds: float = 1.0) -> dict[str, Any]:
    """List normal web tabs exposed by the companion extension."""
    return list_current_chrome_tabs(max(0.0, min(float(wait_seconds), 5.0)))


def setup_current_chrome_companion(wait_seconds: float = 1.0) -> dict[str, Any]:
    """Publish the bridge secret and return the manual steps Chrome still requires."""
    return setup_current_chrome(wait_seconds)


def attach_current_tab(
    tab_id: int,
    session_id: str = "default",
) -> dict[str, Any]:
    """Attach a named MCP session to an existing Chrome tab without navigating it."""
    session_id = _validate_session_id(session_id)
    session = _create_session(
        session_id,
        1440,
        900,
        False,
        "current",
        None,
        None,
        int(tab_id),
        "AI",
    )
    with session.lock:
        _register_render_bootstrap(session)
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "headless": False,
            "window_mode": "visible",
            "profile_mode": "current",
            "current_tab_id": session.current_tab_id,
            "tab_group": session.tab_group,
        }


_INSPECT_SCRIPT = r"""
const limit = arguments[0];
const includeLinks = arguments[1];
const includeForms = arguments[2];
const includeButtons = arguments[3];

function esc(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value).replace(/[^a-zA-Z0-9_-]/g, c => '\\' + c);
}
function selector(el) {
  if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) {
    return '#' + esc(el.id);
  }
  const parts = [];
  let node = el;
  while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.documentElement) {
    let part = node.tagName.toLowerCase();
    const siblings = node.parentElement
      ? Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName)
      : [];
    if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
    parts.unshift(part);
    node = node.parentElement;
  }
  return parts.join(' > ');
}
function labelFor(el) {
  if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
  const parent = el.closest('label');
  return parent ? parent.innerText.trim() : '';
}
function fieldInfo(el) {
  const result = {
    selector: selector(el), tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    id: el.id || '', name: el.getAttribute('name') || '',
    label: labelFor(el), placeholder: el.getAttribute('placeholder') || '',
    required: !!el.required, disabled: !!el.disabled
  };
  if (el.tagName.toLowerCase() === 'select') {
    result.options = Array.from(el.options).map(o => ({value: o.value, text: o.text, selected: o.selected}));
  }
  return result;
}
const output = {links: [], forms: [], fields: [], buttons: [], iframes: []};
if (includeLinks) {
  output.links = Array.from(document.querySelectorAll('a[href]')).slice(0, limit).map(a => ({
    selector: selector(a), text: (a.innerText || a.getAttribute('aria-label') || '').trim(), href: a.href
  }));
}
if (includeForms) {
  output.forms = Array.from(document.forms).slice(0, limit).map((form, index) => ({
    index, selector: selector(form), id: form.id || '', name: form.getAttribute('name') || '',
    action: form.action, method: (form.method || 'get').toLowerCase(), enctype: form.enctype,
    fields: Array.from(form.querySelectorAll('input, textarea, select, [contenteditable="true"]'))
      .slice(0, limit).map(fieldInfo)
  }));
  output.fields = Array.from(document.querySelectorAll(
    'input, textarea, select, [contenteditable="true"]'
  )).slice(0, limit).map(fieldInfo);
}
if (includeButtons) {
  output.buttons = Array.from(document.querySelectorAll(
    'button, input[type="button"], input[type="submit"], input[type="reset"], input[type="image"], [role="button"]'
  )).slice(0, limit).map(button => ({
    selector: selector(button), tag: button.tagName.toLowerCase(),
    type: (button.getAttribute('type') || '').toLowerCase(), id: button.id || '',
    name: button.getAttribute('name') || '',
    text: (button.innerText || button.value || button.getAttribute('aria-label') || '').trim(),
    disabled: !!button.disabled
  }));
}
output.iframes = Array.from(document.querySelectorAll('iframe')).slice(0, limit).map(frame => ({
  selector: selector(frame), id: frame.id || '', name: frame.name || '', src: frame.src || '',
  title: frame.title || ''
}));
return output;
"""


def get_page_elements(
    session_id: str = "default",
    include_links: bool = True,
    include_forms: bool = True,
    include_buttons: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Return stable CSS selectors and metadata for rendered page controls."""
    limit = max(1, min(int(limit), 1000))
    session = _get_session(session_id)
    with session.lock:
        elements = session.driver.execute_script(
            _INSPECT_SCRIPT,
            limit,
            bool(include_links),
            bool(include_forms),
            bool(include_buttons),
        )
        return {**_page_summary(session.driver, session_id), **elements}


def wait_for_element(
    selector: str,
    session_id: str = "default",
    state: str = "visible",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Wait for a dynamic element to be present, visible, or clickable.

    ``selector`` accepts the same three locator forms as ``fill``: CSS, a ref
    handle, and a piercing path.
    """
    if state not in _ELEMENT_STATES:
        raise ValueError("state must be 'present', 'visible', or 'clickable'")
    timeout = max(0.1, min(float(timeout_seconds), 30.0))
    session = _get_session(session_id)
    with session.lock:
        element = _wait_for_locator(session.driver, selector, state, timeout)
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "selector": selector,
            "state": state,
            "tag": element.tag_name,
        }


def wait_for_challenge_resolution(
    session_id: str = "default",
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    """Wait for a human to clear a visible challenge while keeping the session open."""
    timeout = max(0.1, min(float(timeout_seconds), 300.0))
    poll_interval = max(0.05, min(float(poll_interval_seconds), 2.0))
    session = _get_session(session_id)
    started = time.monotonic()
    challenge_seen = False
    with session.lock:
        while True:
            challenge = _challenge_status(session.driver)
            challenge_seen = challenge_seen or bool(challenge["challenge_detected"])
            if not challenge["challenge_detected"]:
                return {
                    **_page_summary(session.driver, session_id),
                    "success": True,
                    "resolved": True,
                    "timed_out": False,
                    "challenge_seen": challenge_seen,
                    "waited_seconds": round(time.monotonic() - started, 2),
                    "session_open": True,
                }
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                return {
                    **_page_summary(session.driver, session_id),
                    "success": False,
                    "resolved": False,
                    "timed_out": True,
                    "challenge_seen": challenge_seen,
                    "waited_seconds": round(elapsed, 2),
                    "session_open": True,
                }
            time.sleep(min(poll_interval, timeout - elapsed))


def _desired_checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "checked"}


def fill_fields(
    fields: dict[str, Any],
    files: dict[str, str] | None = None,
    session_id: str = "default",
) -> dict[str, Any]:
    """Fill controls by CSS selector; file inputs are supplied separately."""
    if not fields and not files:
        raise ValueError("At least one field or file must be provided")
    session = _get_session(session_id)
    filled: list[str] = []
    uploaded: list[str] = []
    errors: dict[str, str] = {}
    with session.lock:
        for selector, value in fields.items():
            try:
                element = _resolve_element(session.driver, selector)
                tag = element.tag_name.lower()
                input_type = (element.get_attribute("type") or "").lower()
                session.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", element
                )
                if input_type == "file":
                    raise ValueError("Use the files argument for file inputs")
                if input_type == "checkbox":
                    desired = _desired_checked(value)
                    if element.is_selected() != desired:
                        element.click()
                    if element.is_selected() != desired:
                        raise ValueError("Checkbox state did not change")
                elif input_type == "radio":
                    desired = _desired_checked(value)
                    if not desired and element.is_selected():
                        raise ValueError(
                            "A selected radio cannot be unchecked directly; select another radio in its group"
                        )
                    if desired and not element.is_selected():
                        element.click()
                    if element.is_selected() != desired:
                        raise ValueError("Radio state did not change")
                elif tag == "select":
                    if hasattr(session.driver, "select_option"):
                        session.driver.select_option(selector, str(value))
                    else:
                        select = Select(element)
                        try:
                            select.select_by_value(str(value))
                        except Exception:
                            select.select_by_visible_text(str(value))
                else:
                    element.clear()
                    element.send_keys(str(value))
                filled.append(selector)
            except Exception as exc:  # Return partial progress to the caller.
                errors[selector] = f"{type(exc).__name__}: {exc}"

        for selector, file_path in (files or {}).items():
            try:
                path = Path(file_path).expanduser().resolve(strict=True)
                if not path.is_file():
                    raise ValueError("Upload path is not a file")
                element = _resolve_element(session.driver, selector)
                if (element.get_attribute("type") or "").lower() != "file":
                    raise ValueError("Selector does not point to an input[type=file]")
                element.send_keys(str(path))
                uploaded.append(selector)
            except Exception as exc:
                errors[selector] = f"{type(exc).__name__}: {exc}"

        return {
            **_page_summary(session.driver, session_id),
            "success": not errors,
            "filled": filled,
            "files_uploaded": uploaded,
            "errors": errors,
        }


def _wait_after_action(driver: webdriver.Chrome, wait_seconds: float) -> None:
    delay = max(0.0, min(float(wait_seconds), 5.0))
    if not delay:
        # Game input has nothing to settle, so even the readyState probe is a
        # round-trip spent for nothing.
        return
    time.sleep(delay)
    try:
        _wait_until_ready(driver, max(1.0, delay))
    except Exception:
        pass


def click(
    selector: str,
    session_id: str = "default",
    wait_seconds: float = 0.5,
) -> dict[str, Any]:
    """Click a rendered element by CSS selector, ref handle, or piercing path."""
    session = _get_session(session_id)
    with session.lock:
        element = _wait_for_locator(session.driver, selector, "clickable", 10.0)
        session.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", element
        )
        element.click()
        _wait_after_action(session.driver, wait_seconds)
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "clicked": selector,
        }


_KEY_ALIASES = {
    "SPACE": Keys.SPACE,
    "ENTER": Keys.ENTER,
    "RETURN": Keys.RETURN,
    "ESC": Keys.ESCAPE,
    "ESCAPE": Keys.ESCAPE,
    "TAB": Keys.TAB,
    "BACKSPACE": Keys.BACKSPACE,
    "DELETE": Keys.DELETE,
    "INSERT": Keys.INSERT,
    "HOME": Keys.HOME,
    "END": Keys.END,
    "PAGE_UP": Keys.PAGE_UP,
    "PAGE_DOWN": Keys.PAGE_DOWN,
    "ARROW_UP": Keys.ARROW_UP,
    "UP": Keys.ARROW_UP,
    "ARROW_DOWN": Keys.ARROW_DOWN,
    "DOWN": Keys.ARROW_DOWN,
    "ARROW_LEFT": Keys.ARROW_LEFT,
    "LEFT": Keys.ARROW_LEFT,
    "ARROW_RIGHT": Keys.ARROW_RIGHT,
    "RIGHT": Keys.ARROW_RIGHT,
    "SHIFT": Keys.SHIFT,
    "CONTROL": Keys.CONTROL,
    "CTRL": Keys.CONTROL,
    "ALT": Keys.ALT,
    "META": key_table.SELENIUM_KEYS["META"],
    "WIN": key_table.SELENIUM_KEYS["META"],
    "CMD": key_table.SELENIUM_KEYS["META"],
    "COMMAND": key_table.SELENIUM_KEYS["META"],
    "MULTIPLY": key_table.SELENIUM_KEYS["MULTIPLY"],
    "ADD": key_table.SELENIUM_KEYS["ADD"],
    "SUBTRACT": key_table.SELENIUM_KEYS["SUBTRACT"],
    "DECIMAL": key_table.SELENIUM_KEYS["DECIMAL"],
    "DIVIDE": key_table.SELENIUM_KEYS["DIVIDE"],
    **{f"F{index}": key_table.SELENIUM_KEYS[f"F{index}"] for index in range(1, 13)},
    **{
        f"NUMPAD{digit}": key_table.SELENIUM_KEYS[f"NUMPAD{digit}"]
        for digit in range(10)
    },
}


def _perform_key_events(driver: Any, events: list[dict[str, Any]]) -> None:
    """Dispatch an ordered key event stream through CDP or Selenium ActionChains."""
    if hasattr(driver, "perform_key_events"):
        driver.perform_key_events(events)
        return
    actions = ActionChains(driver)
    for event in events:
        event_type = event["type"]
        if event_type == "down":
            actions.key_down(event["key"])
        elif event_type == "up":
            actions.key_up(event["key"])
        elif event_type == "pause":
            actions.pause(float(event.get("seconds", 0.0)))
    actions.perform()


def _normalize_game_key(key: str) -> str:
    raw = str(key)
    if raw == " ":
        # A literal space is the spacebar, but strip() would turn it into "".
        return _KEY_ALIASES["SPACE"]
    value = raw.strip()
    if len(value) == 1 and value.isprintable():
        return value
    alias = value.upper().replace("-", "_").replace(" ", "_")
    if alias in _KEY_ALIASES:
        return _KEY_ALIASES[alias]
    raise ValueError(
        f"Unsupported key '{key}'; use a printable character or a named keyboard key"
    )


_FOCUS_SCRIPT = """
const element = arguments[0];
if (!element) { window.focus(); return {focused: false}; }
element.scrollIntoView({block: 'center', inline: 'center'});
if (!element.hasAttribute('tabindex') && element.tabIndex < 0) {
  element.setAttribute('tabindex', '-1');
}
window.focus();
element.focus({preventScroll: true});
return {focused: document.activeElement === element};
"""


def _focus_target(
    driver: webdriver.Chrome, target_selector: str | None, focus_mode: str
) -> None:
    """Give the keyboard target focus without synthesising a stray click.

    Clicking to focus a canvas fires a real click in the game, which reads as a
    shot or a jump. Focusing directly avoids that phantom input.
    """
    if not target_selector:
        driver.execute_script("window.focus();")
        return
    target = WebDriverWait(driver, 10).until(
        conditions.visibility_of_element_located((By.CSS_SELECTOR, target_selector))
    )
    if focus_mode == "click":
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});", target
        )
        target.click()
        return
    driver.execute_script(_FOCUS_SCRIPT, target)


def _key_event_pair(
    session: BrowserSession,
    key_ids: list[str],
    normalized: list[str],
    selected_action: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the down/up streams for one key action against the held-key state."""
    down_events: list[dict[str, Any]] = []
    up_events: list[dict[str, Any]] = []
    if selected_action in {"tap", "hold"}:
        for key_id, key in zip(key_ids, normalized):
            if selected_action == "tap" or key_id not in session.held_keys:
                down_events.append({"type": "down", "key": key})
    if selected_action in {"tap", "release"}:
        for key_id, key in reversed(list(zip(key_ids, normalized))):
            if selected_action == "tap":
                up_events.append({"type": "up", "key": key})
            elif key_id in session.held_keys:
                # Release exactly what was pressed: a hold("w") that is released
                # as "W" must still lift the same key.
                up_events.append({"type": "up", "key": session.held_keys[key_id]})
    return down_events, up_events


def _commit_held_keys(
    session: BrowserSession,
    key_ids: list[str],
    normalized: list[str],
    selected_action: str,
) -> None:
    if selected_action == "hold":
        # A real keyboard waits before it repeats, so a key pressed for this very
        # frame must not also arrive as a repeat inside it. A key that was
        # already down got no keydown here, so it stays eligible to repeat -
        # otherwise re-holding it would silence the repeat forever.
        session.fresh_keys.update(
            key_id for key_id in key_ids if key_id not in session.held_keys
        )
        session.held_keys.update(dict(zip(key_ids, normalized)))
    elif selected_action == "release":
        for key_id in key_ids:
            session.held_keys.pop(key_id, None)
            session.fresh_keys.discard(key_id)


def press_keys(
    keys: list[str],
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    hold_seconds: float = 0.05,
    repeat: int = 1,
    wait_seconds: float = 0.0,
    action: str = "tap",
    hold_frames: int = 1,
    focus_mode: str = "focus",
    include_summary: bool = True,
    _advance_frame: bool = True,
) -> dict[str, Any]:
    """Tap, hold, or release a key combination on a page, canvas, or iframe game.

    In step mode a tap keeps the key down across ``hold_frames`` released frames
    before lifting it, because an engine that polls key state in its loop cannot
    observe a press that was already released before the frame ran.
    """
    if not keys or len(keys) > 8:
        raise ValueError("Provide 1-8 keys")
    selected_action = action.strip().lower()
    if selected_action not in {"tap", "hold", "release"}:
        raise ValueError("action must be 'tap', 'hold', or 'release'")
    selected_focus = focus_mode.strip().lower()
    if selected_focus not in {"focus", "click", "none"}:
        raise ValueError("focus_mode must be 'focus', 'click', or 'none'")
    normalized = [_normalize_game_key(key) for key in keys]
    key_ids = [str(key).strip().upper() for key in keys]
    hold = max(0.0, min(float(hold_seconds), 5.0))
    repetitions = max(1, min(int(repeat), 50))
    frames_held = max(1, min(int(hold_frames), 30))
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        stepping = session.render_mode == "step" and _advance_frame
        driver.switch_to.default_content()
        frames_advanced = 0
        try:
            if frame_selector:
                WebDriverWait(driver, 10).until(
                    conditions.frame_to_be_available_and_switch_to_it(
                        (By.CSS_SELECTOR, frame_selector)
                    )
                )
            if selected_focus != "none":
                _focus_target(driver, target_selector, selected_focus)
            runs = repetitions if selected_action == "tap" else 1
            for _ in range(runs):
                down_events, up_events = _key_event_pair(
                    session, key_ids, normalized, selected_action
                )
                if selected_action == "tap" and stepping:
                    _perform_key_events(driver, down_events)
                    driver.switch_to.default_content()
                    _auto_advance_render_after_input(session, frames_held)
                    frames_advanced += frames_held
                    if frame_selector:
                        _select_frame(driver, frame_selector)
                    _perform_key_events(driver, up_events)
                else:
                    events = list(down_events)
                    if selected_action == "tap" and hold:
                        events.append({"type": "pause", "seconds": hold})
                    events.extend(up_events)
                    _perform_key_events(driver, events)
            _commit_held_keys(session, key_ids, normalized, selected_action)
        finally:
            driver.switch_to.default_content()
        if _advance_frame and not (selected_action == "tap" and stepping):
            _auto_advance_render_after_input(session)
            # Only step mode releases frames by hand. In normal and throttled mode
            # the page draws on its own schedule and this call held nothing back,
            # so counting a frame here would invent one the caller never got.
            if stepping:
                frames_advanced += 1
        _wait_after_action(driver, wait_seconds)
        return {
            **_action_summary(driver, session_id, include_summary),
            "success": True,
            "action": selected_action,
            "keys": [str(key) for key in keys],
            "repeat": runs,
            "hold_seconds": hold,
            "hold_frames": frames_held if selected_action == "tap" else None,
            "frames_advanced": frames_advanced,
            "render_mode": session.render_mode,
            "held_keys": sorted(session.held_keys),
            "target_selector": target_selector,
            "frame_selector": frame_selector,
        }


def _resolve_element(driver: Any, locator: str) -> Any:
    """Find one element from a CSS selector, a ``ref:<epoch>:N``, or a piercing path.

    Plain CSS keeps working exactly as before; ref handles and ``a >>> b`` are new
    forms that survive shadow roots and unstable DOM structure.
    """
    expression = page_perception.resolve_locator_expression(locator)
    if expression is None:
        return driver.find_element(By.CSS_SELECTOR, locator)
    if getattr(driver, "is_extension_bridge", False):
        raise ValueError(
            f"Locator '{locator}' needs a live element handle, which the companion "
            "bridge cannot return. Use a CSS selector in current-Chrome mode."
        )
    element = driver.execute_script(f"return {expression};")
    if element is None:
        raise ValueError(
            f"Locator '{locator}' resolves to nothing in this document. A ref is only "
            "valid for the page it was read from and only while its element is still "
            "attached, so this one is stale - read the page again with "
            "web_info(topic='page_outline') and use the ref it reports now."
        )
    return element


_ELEMENT_STATES = {
    "present": conditions.presence_of_element_located,
    "visible": conditions.visibility_of_element_located,
    "clickable": conditions.element_to_be_clickable,
}


def _element_reached_state(element: Any, state: str) -> bool:
    if state == "present":
        return True
    if not element.is_displayed():
        return False
    return state != "clickable" or bool(element.is_enabled())


def _wait_for_locator(driver: Any, locator: str, state: str, timeout: float) -> Any:
    """Wait for any supported locator form to reach ``present``/``visible``/``clickable``.

    ``expected_conditions`` only speak ``(By, selector)`` tuples, so ref handles and
    piercing paths are polled through ``_resolve_element`` instead.
    """
    if page_perception.resolve_locator_expression(locator) is None:
        return WebDriverWait(driver, timeout).until(
            _ELEMENT_STATES[state]((By.CSS_SELECTOR, locator))
        )
    if getattr(driver, "is_extension_bridge", False):
        _resolve_element(driver, locator)  # raises the bridge-specific explanation
    deadline = time.monotonic() + timeout
    failure = f"Locator '{locator}' never became {state}"
    while True:
        try:
            element = _resolve_element(driver, locator)
            if _element_reached_state(element, state):
                return element
        except ValueError as exc:
            failure = str(exc)
        except WebDriverException as exc:
            failure = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            raise TimeoutException(failure)
        time.sleep(0.1)


def get_page_outline(
    session_id: str = "default",
    limit: int = 200,
    include_occlusion: bool = True,
    output: str = "text",
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Return the accessibility outline: roles, names, states, refs, and boxes."""
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        _select_frame(driver, frame_selector)
        try:
            result = page_perception.outline(
                driver,
                limit=limit,
                include_occlusion=include_occlusion,
                format=output,
            )
        finally:
            driver.switch_to.default_content()
        return {**result, "session_id": session_id, "frame_selector": frame_selector}


def get_page_text(
    session_id: str = "default",
    max_chars: int = 20_000,
    mode: str = "main",
    include_links: bool = False,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Return the readable text of the rendered page, not of its HTML source."""
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        _select_frame(driver, frame_selector)
        try:
            result = page_perception.page_text(
                driver, max_chars=max_chars, mode=mode, include_links=include_links
            )
        finally:
            driver.switch_to.default_content()
        return {**result, "session_id": session_id, "frame_selector": frame_selector}


def find_elements(
    query: str,
    session_id: str = "default",
    role: str | None = None,
    limit: int = 5,
    visible_only: bool = True,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Find elements by meaning instead of dumping the whole page to the model."""
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        _select_frame(driver, frame_selector)
        try:
            result = page_perception.find(
                driver, query, role=role, limit=limit, visible_only=visible_only
            )
        finally:
            driver.switch_to.default_content()
        return {**result, "session_id": session_id, "frame_selector": frame_selector}


def _drain_browser_log(session: BrowserSession) -> list[dict[str, Any]]:
    """Move Chrome's browser log into the session buffer.

    ``get_log('browser')`` empties the log, so whoever reads it first destroys it
    for everyone else. Draining into one buffer keeps ``game_probe`` from eating
    the diagnostics the console topic is supposed to report.
    """
    session.browser_log.extend(diagnostics.selenium_browser_log(session.driver))
    overflow = len(session.browser_log) - _BROWSER_LOG_LIMIT
    if overflow > 0:
        del session.browser_log[:overflow]
        session.browser_log_cursor = max(0, session.browser_log_cursor - overflow)
        session.probe_log_cursor = max(0, session.probe_log_cursor - overflow)
    return session.browser_log


def _console_since(
    session: BrowserSession, console_seq: int, log_cursor: int, clear: bool = False
) -> dict[str, Any]:
    """Collect the console output recorded after one reader's own two cursors.

    A reader carries two of them because the sources are counted in different
    units and neither may be consumed on another reader's behalf: the in-page
    hook numbers the entries it keeps, while Chrome's browser log is destroyed by
    reading it, so it is drained into a session buffer that readers index into.
    The companion backend buffers both inside the extension, where one sequence
    number covers everything.

    ``clear`` throws the buffered entries away after they have been collected,
    which leaves every cursor pointing at an empty buffer.
    """
    driver = session.driver
    if hasattr(driver, "get_events"):
        payload = driver.get_events(kinds=["console"], since_seq=console_seq, limit=500)
        entries = list(payload.get("entries") or [])
        next_seq = int(payload.get("next_seq") or console_seq)
        next_log = log_cursor
        if clear:
            driver.clear_events(kinds=["console"])
    else:
        payload = diagnostics.read_page_console(driver, console_seq, clear)
        entries = list(payload.get("entries") or [])
        next_seq = int(payload.get("next_seq") or console_seq)
        buffered = _drain_browser_log(session)
        entries.extend(buffered[min(max(0, log_cursor), len(buffered)) :])
        next_log = len(buffered)
        if clear:
            session.browser_log.clear()
    entries.sort(key=lambda item: (item.get("ts") or 0, item.get("seq") or 0))
    return {
        "entries": diagnostics.dedupe_console(entries),
        "next_seq": 0 if clear else next_seq,
        "next_log_cursor": 0 if clear else next_log,
        "dropped": payload.get("dropped"),
    }


def get_console(
    session_id: str = "default",
    levels: list[str] | None = None,
    contains: str | None = None,
    kinds: list[str] | None = None,
    limit: int = 50,
    since_seq: int = 0,
    clear: bool = False,
) -> dict[str, Any]:
    """Read page console output, uncaught errors, and browser log entries.

    ``console.log`` never reaches Chrome's browser log, so this merges the
    in-page hook with the browser log rather than reporting one of them.
    """
    session = _get_session(session_id)
    with session.lock:
        payload = _console_since(
            session,
            since_seq or session.console_cursor,
            session.browser_log_cursor,
            clear,
        )
        session.console_cursor = payload["next_seq"]
        session.browser_log_cursor = payload["next_log_cursor"]
        if clear:
            # The buffers everyone reads are gone, so no reader may keep a place
            # in them: a stale index would skip whatever arrives next.
            session.probe_console_cursor = 0
            session.probe_log_cursor = 0
            session.probe_console_seen.clear()
        selected = diagnostics.filter_console(
            payload["entries"], levels, contains, kinds, limit
        )
        return {
            "success": True,
            "session_id": session_id,
            "entries": selected,
            "returned": len(selected),
            "next_seq": session.console_cursor,
            "dropped": payload["dropped"],
            "levels": levels or list(diagnostics.LEVELS),
            "note": (
                "The buffer starts when the session connects; reload the page to "
                "capture load-time output."
            ),
        }


def get_network(
    session_id: str = "default",
    url_pattern: str | None = None,
    types: list[str] | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    only_errors: bool = False,
    limit: int = 50,
    output: str = "text",
) -> dict[str, Any]:
    """List finished HTTP requests made by the page."""
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        if hasattr(driver, "get_events"):
            if not session.network_subscribed:
                driver.subscribe_events(["console", "network"])
                session.network_subscribed = True
            payload = driver.get_events(kinds=["network"], since_seq=0, limit=500)
            rows = list(payload.get("entries") or [])
        else:
            session.network_rows.extend(
                diagnostics.selenium_network_rows(driver, session.network_pending)
            )
            session.network_rows = session.network_rows[-500:]
            rows = list(session.network_rows)
        selected = diagnostics.filter_network(
            rows, url_pattern, types, status_min, status_max, only_errors, limit
        )
        response = {
            "success": True,
            "session_id": session_id,
            "returned": len(selected),
            "only_errors": bool(only_errors),
        }
        if output == "json":
            response["requests"] = selected
        else:
            response["requests"] = diagnostics.format_network(selected)
            response["format"] = "method status type ms size url"
        return response


def get_network_body(
    request_id: str,
    session_id: str = "default",
    max_chars: int = 20_000,
) -> dict[str, Any]:
    """Return one response body by the request_id reported by the network topic."""
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        if hasattr(driver, "get_network_body"):
            payload = driver.get_network_body(str(request_id))
        else:
            payload = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": str(request_id)}
            )
        body = str(payload.get("body") or "")
        limit = max(256, min(int(max_chars), 500_000))
        return {
            "success": True,
            "request_id": request_id,
            "session_id": session_id,
            "binary": bool(payload.get("binary") or payload.get("base64Encoded")),
            "truncated": len(body) > limit,
            "body": body[:limit],
        }


def _session_modifiers(session: BrowserSession) -> int:
    """Build the CDP modifier mask from the keys this session is holding.

    Without this a held Shift or Ctrl is invisible to mouse events, so
    Shift-click and Ctrl-click cannot be expressed at all.
    """
    mask = 0
    for held in session.held_keys.values():
        name = key_table.resolve_key(held)[0]
        mask |= key_table.MODIFIER_BITS.get(name, 0)
    return mask


_VIEWPORT_SCRIPT = "return {width: window.innerWidth, height: window.innerHeight};"


def _pointer_context(
    driver: webdriver.Chrome, frame_selector: str | None
) -> tuple[float, float, dict[str, Any]]:
    """Resolve the frame offset and viewport once, because CDP input is page-absolute."""
    driver.switch_to.default_content()
    if not frame_selector:
        return 0.0, 0.0, driver.execute_script(_VIEWPORT_SCRIPT)
    frame = WebDriverWait(driver, 10).until(
        conditions.visibility_of_element_located((By.CSS_SELECTOR, frame_selector))
    )
    rect = driver.execute_script(_FRAME_ORIGIN_SCRIPT, frame)
    driver.switch_to.frame(frame)
    try:
        viewport = driver.execute_script(_VIEWPORT_SCRIPT)
    finally:
        driver.switch_to.default_content()
    return float(rect["x"]), float(rect["y"]), viewport


def _pointer_dispatch(
    session: BrowserSession,
    action: str,
    x: float,
    y: float,
    viewport: dict[str, Any],
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    end_x: float | None = None,
    end_y: float | None = None,
    button: str = "left",
    duration_seconds: float = 0.3,
    coordinate_mode: str = "absolute",
    delta_x: float = 0.0,
    delta_y: float = 0.0,
) -> dict[str, Any]:
    """Validate and dispatch one pointer action against an already-resolved frame."""
    driver = session.driver
    requested_action = action.strip().lower()
    allowed_actions = {
        "click",
        "double_click",
        "move",
        "hover",
        "drag",
        "press",
        "release",
        "wheel",
    }
    if requested_action not in allowed_actions:
        raise ValueError(
            "action must be 'click', 'double_click', 'move', 'hover', 'drag', "
            "'press', 'release', or 'wheel'"
        )
    selected_action = "move" if requested_action == "hover" else requested_action
    selected_coordinate_mode = coordinate_mode.strip().lower()
    if selected_coordinate_mode not in {"absolute", "delta", "relative"}:
        raise ValueError("coordinate_mode must be 'absolute', 'delta', or 'relative'")
    selected_button = button.strip().lower()
    button_bits = {"left": 1, "right": 2, "middle": 4}
    if selected_button not in button_bits:
        raise ValueError("button must be 'left', 'right', or 'middle'")
    duration = max(0.0, min(float(duration_seconds), 5.0))
    unbounded = selected_coordinate_mode == "relative"
    current_local_x = session.pointer_x - offset_x
    current_local_y = session.pointer_y - offset_y
    if unbounded and not session.pointer_initialized:
        # Start a relative run from the middle so small deltas stay on screen.
        current_local_x = float(viewport["width"]) / 2
        current_local_y = float(viewport["height"]) / 2
    if selected_coordinate_mode in {"delta", "relative"}:
        local_x = current_local_x + float(x)
        local_y = current_local_y + float(y)
    else:
        local_x = float(x)
        local_y = float(y)
    if not unbounded and (
        not 0 <= local_x < float(viewport["width"])
        or not 0 <= local_y < float(viewport["height"])
    ):
        raise ValueError("Pointer coordinates must be inside the selected viewport")
    if selected_coordinate_mode in {"delta", "relative"}:
        local_end_x = local_x + (float(end_x) if end_x is not None else 0.0)
        local_end_y = local_y + (float(end_y) if end_y is not None else 0.0)
    else:
        local_end_x = float(end_x) if end_x is not None else local_x
        local_end_y = float(end_y) if end_y is not None else local_y
    if selected_action == "drag" and (end_x is None or end_y is None):
        raise ValueError("drag requires end_x and end_y")
    if not unbounded and (
        not 0 <= local_end_x < float(viewport["width"])
        or not 0 <= local_end_y < float(viewport["height"])
    ):
        raise ValueError("Pointer end coordinates must be inside the selected viewport")

    start_x = local_x + offset_x
    start_y = local_y + offset_y
    finish_x = local_end_x + offset_x
    finish_y = local_end_y + offset_y
    modifiers = _session_modifiers(session)

    def dispatch(event_type: str, px: float, py: float, **extra: Any) -> None:
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": px,
                "y": py,
                "button": extra.pop("button", "none"),
                "modifiers": modifiers,
                **extra,
            },
        )

    held_mask = sum(button_bits[item] for item in session.held_buttons)
    if selected_action == "wheel":
        dispatch(
            "mouseWheel",
            start_x,
            start_y,
            buttons=held_mask,
            deltaX=float(delta_x),
            deltaY=float(delta_y),
        )
    else:
        dispatch("mouseMoved", start_x, start_y, buttons=held_mask)
    if selected_action in {"move", "wheel"}:
        pass
    elif selected_action == "press":
        session.held_buttons.add(selected_button)
        dispatch(
            "mousePressed",
            start_x,
            start_y,
            button=selected_button,
            buttons=sum(button_bits[item] for item in session.held_buttons),
            clickCount=1,
        )
    elif selected_action == "release":
        session.held_buttons.discard(selected_button)
        dispatch(
            "mouseReleased",
            start_x,
            start_y,
            button=selected_button,
            buttons=sum(button_bits[item] for item in session.held_buttons),
            clickCount=1,
        )
    elif selected_action in {"click", "double_click"}:
        count = 2 if selected_action == "double_click" else 1
        for click_count in range(1, count + 1):
            dispatch(
                "mousePressed",
                start_x,
                start_y,
                button=selected_button,
                buttons=held_mask | button_bits[selected_button],
                clickCount=click_count,
            )
            dispatch(
                "mouseReleased",
                start_x,
                start_y,
                button=selected_button,
                buttons=held_mask,
                clickCount=click_count,
            )
    else:
        dispatch(
            "mousePressed",
            start_x,
            start_y,
            button=selected_button,
            buttons=held_mask | button_bits[selected_button],
            clickCount=1,
        )
        steps = max(2, min(30, int(duration * 30) or 2))
        for step in range(1, steps + 1):
            ratio = step / steps
            dispatch(
                "mouseMoved",
                start_x + (finish_x - start_x) * ratio,
                start_y + (finish_y - start_y) * ratio,
                button=selected_button,
                buttons=held_mask | button_bits[selected_button],
            )
            if duration:
                time.sleep(duration / steps)
        dispatch(
            "mouseReleased",
            finish_x,
            finish_y,
            button=selected_button,
            buttons=held_mask,
            clickCount=1,
        )
    session.pointer_x = finish_x if selected_action == "drag" else start_x
    session.pointer_y = finish_y if selected_action == "drag" else start_y
    session.pointer_initialized = True
    if unbounded and (
        abs(session.pointer_x) > 100_000 or abs(session.pointer_y) > 100_000
    ):
        # Relative runs never clamp, so recentre before the numbers get silly:
        # dropping the flag makes the next one start from the viewport middle again.
        session.pointer_x = 0.0
        session.pointer_y = 0.0
        session.pointer_initialized = False
    return {
        "success": True,
        "action": requested_action,
        "button": selected_button,
        "x": local_x,
        "y": local_y,
        "coordinate_mode": selected_coordinate_mode,
        "delta_x": (
            float(delta_x)
            if selected_action == "wheel"
            else (float(x) if selected_coordinate_mode != "absolute" else None)
        ),
        "delta_y": (
            float(delta_y)
            if selected_action == "wheel"
            else (float(y) if selected_coordinate_mode != "absolute" else None)
        ),
        "end_x": local_end_x if selected_action == "drag" else None,
        "end_y": local_end_y if selected_action == "drag" else None,
        "modifiers": modifiers,
        "held_buttons": sorted(session.held_buttons),
    }


def pointer_action(
    action: str,
    x: float,
    y: float,
    session_id: str = "default",
    end_x: float | None = None,
    end_y: float | None = None,
    button: str = "left",
    duration_seconds: float = 0.3,
    frame_selector: str | None = None,
    wait_seconds: float = 0.0,
    coordinate_mode: str = "absolute",
    delta_x: float = 0.0,
    delta_y: float = 0.0,
    include_summary: bool = True,
    _advance_frame: bool = True,
) -> dict[str, Any]:
    """Dispatch pointer movement, taps, drag, wheel, or held buttons.

    ``coordinate_mode='relative'`` skips the viewport bounds check and moves the
    pointer by a delta without clamping, which is what a pointer-locked game
    needs: the cursor never moves, only ``movementX``/``movementY`` matter.
    """
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        offset_x, offset_y, viewport = _pointer_context(driver, frame_selector)
        result = _pointer_dispatch(
            session,
            action,
            x,
            y,
            viewport,
            offset_x=offset_x,
            offset_y=offset_y,
            end_x=end_x,
            end_y=end_y,
            button=button,
            duration_seconds=duration_seconds,
            coordinate_mode=coordinate_mode,
            delta_x=delta_x,
            delta_y=delta_y,
        )
        if _advance_frame:
            _auto_advance_render_after_input(session)
        _wait_after_action(driver, wait_seconds)
        return {
            **_action_summary(driver, session_id, include_summary),
            **result,
            "frame_selector": frame_selector,
        }



# CDP input is addressed in top-level page pixels, so a frame's offset must be
# the origin of its *content* box. Using the border box misses by exactly the
# border and padding, which silently skews every click inside a framed game.
_FRAME_ORIGIN_SCRIPT = """
const frame = arguments[0];
const rect = frame.getBoundingClientRect();
const style = getComputedStyle(frame);
return {
  x: rect.x + parseFloat(style.borderLeftWidth || 0) + parseFloat(style.paddingLeft || 0),
  y: rect.y + parseFloat(style.borderTopWidth || 0) + parseFloat(style.paddingTop || 0)
};
"""


def _frame_offset(driver: webdriver.Chrome, frame_selector: str | None) -> tuple[float, float]:
    """Return the top-level offset of a frame; CDP input is always page-absolute."""
    if not frame_selector:
        return 0.0, 0.0
    frame = WebDriverWait(driver, 10).until(
        conditions.visibility_of_element_located((By.CSS_SELECTOR, frame_selector))
    )
    rect = driver.execute_script(_FRAME_ORIGIN_SCRIPT, frame)
    return float(rect["x"]), float(rect["y"])


def set_touch_emulation(
    session_id: str = "default",
    enabled: bool = True,
    max_touch_points: int = 5,
    reload_page: bool = True,
) -> dict[str, Any]:
    """Turn the page into a touch device so mobile branches actually run.

    Without this ``navigator.maxTouchPoints`` is 0 and ``'ontouchstart' in
    window`` is false, so a game that feature-detects touch stays on its desktop
    code path and ignores every touch event sent to it. ``'ontouchstart'`` is
    decided while the document loads, so the page is reloaded by default.
    """
    points = max(1, min(int(max_touch_points), 10))
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        driver.execute_cdp_cmd(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": bool(enabled), "maxTouchPoints": points},
        )
        for command, params in (
            ("Emulation.setEmitTouchEventsForMouse", {"enabled": False, "configuration": "mobile"}),
        ):
            try:
                driver.execute_cdp_cmd(command, params)
            except WebDriverException:
                pass
        if reload_page:
            driver.refresh()
            session.held_keys.clear()
            session.held_buttons.clear()
            session.render_mode = "normal"
            session.render_frame_selector = None
            session.pointer_locked = False
        session.touch_enabled = bool(enabled)
        detected = driver.execute_script(
            "return {ontouchstart: 'ontouchstart' in window,"
            " max_touch_points: navigator.maxTouchPoints};"
        )
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "touch_enabled": session.touch_enabled,
            "max_touch_points": points if enabled else 0,
            "reloaded": bool(reload_page),
            **detected,
            "note": (
                None
                if reload_page or not enabled
                else "'ontouchstart' in window only flips after a reload; "
                "pass reload_page=true or reload before feature detection."
            ),
        }


def touch_action(
    action: str,
    points: list[dict[str, Any]] | None = None,
    session_id: str = "default",
    frame_selector: str | None = None,
    steps: int = 8,
    duration_seconds: float = 0.2,
    wait_seconds: float = 0.0,
    include_summary: bool = True,
    _advance_frame: bool = True,
) -> dict[str, Any]:
    """Dispatch touch input: tap, multi-finger press/move/release, or a swipe.

    ``points`` carries one entry per finger as ``{"x":.., "y":.., "id":..}``;
    ``swipe`` additionally reads ``end_x``/``end_y`` from each entry.
    """
    selected_action = action.strip().lower()
    if selected_action not in {"tap", "press", "move", "release", "swipe", "cancel"}:
        raise ValueError(
            "action must be 'tap', 'press', 'move', 'release', 'swipe', or 'cancel'"
        )
    entries = list(points or [])
    if selected_action not in {"cancel", "release"} and not entries:
        raise ValueError("Provide 1-10 touch points")
    if len(entries) > 10:
        raise ValueError("Provide at most 10 touch points")
    interpolation = max(2, min(int(steps), 30))
    duration = max(0.0, min(float(duration_seconds), 5.0))
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        driver.switch_to.default_content()
        offset_x, offset_y = _frame_offset(driver, frame_selector)

        def touch_points(progress: float) -> list[dict[str, Any]]:
            resolved = []
            for index, entry in enumerate(entries):
                start_x = float(entry["x"])
                start_y = float(entry["y"])
                target_x = float(entry.get("end_x", start_x))
                target_y = float(entry.get("end_y", start_y))
                resolved.append(
                    {
                        "x": offset_x + start_x + (target_x - start_x) * progress,
                        "y": offset_y + start_y + (target_y - start_y) * progress,
                        "id": int(entry.get("id", index)),
                        "radiusX": float(entry.get("radius_x", 6.0)),
                        "radiusY": float(entry.get("radius_y", 6.0)),
                        "force": float(entry.get("force", 1.0)),
                    }
                )
            return resolved

        def dispatch(event_type: str, resolved: list[dict[str, Any]]) -> None:
            driver.execute_cdp_cmd(
                "Input.dispatchTouchEvent",
                {"type": event_type, "touchPoints": resolved, "modifiers": _session_modifiers(session)},
            )

        if selected_action == "cancel":
            dispatch("touchCancel", [])
        elif selected_action == "press":
            dispatch("touchStart", touch_points(0.0))
        elif selected_action == "move":
            dispatch("touchMove", touch_points(1.0))
        elif selected_action == "release":
            dispatch("touchEnd", [])
        elif selected_action == "tap":
            dispatch("touchStart", touch_points(0.0))
            dispatch("touchEnd", [])
        else:
            dispatch("touchStart", touch_points(0.0))
            for step in range(1, interpolation + 1):
                dispatch("touchMove", touch_points(step / interpolation))
                if duration:
                    time.sleep(duration / interpolation)
            dispatch("touchEnd", [])
        if _advance_frame:
            _auto_advance_render_after_input(session)
        _wait_after_action(driver, wait_seconds)
        return {
            **_action_summary(driver, session_id, include_summary),
            "success": True,
            "action": selected_action,
            "points": len(entries),
            "touch_enabled": session.touch_enabled,
            "frame_selector": frame_selector,
        }


_POINTER_LOCK_SCRIPT = """
const selector = arguments[0];
const wanted = arguments[1];
const target = selector ? document.querySelector(selector)
                        : (document.querySelector('canvas') || document.body);
if (!target) return {success: false, error: 'No pointer lock target on this page'};
if (wanted === 'release') {
  document.exitPointerLock();
  return {success: true, locked: false, element: null};
}
try { target.requestPointerLock(); } catch (error) {
  return {success: false, error: String(error)};
}
return {success: true, requested: true};
"""

_POINTER_LOCK_STATUS_SCRIPT = """
const locked = document.pointerLockElement;
return {
  locked: !!locked,
  element: locked ? (locked.id || locked.tagName.toLowerCase()) : null
};
"""


def pointer_lock(
    action: str = "status",
    session_id: str = "default",
    selector: str | None = None,
    frame_selector: str | None = None,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Acquire, release, or read pointer lock for first-person style games.

    ``requestPointerLock`` needs a user gesture, so acquiring first sends a real
    click on the target through CDP and then requests the lock from that gesture.
    """
    selected_action = action.strip().lower()
    if selected_action not in {"acquire", "release", "status"}:
        raise ValueError("action must be 'acquire', 'release', or 'status'")
    timeout = max(0.1, min(float(timeout_seconds), 10.0))
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        _select_frame(driver, frame_selector)
        try:
            if selected_action == "acquire":
                rect = driver.execute_script(
                    "const t = arguments[0] ? document.querySelector(arguments[0])"
                    " : (document.querySelector('canvas') || document.body);"
                    "if (!t) return null;"
                    "const r = t.getBoundingClientRect();"
                    "return {x: r.x + r.width / 2, y: r.y + r.height / 2};",
                    selector,
                )
                if rect is None:
                    raise ValueError("No pointer lock target on this page")
        finally:
            driver.switch_to.default_content()
        if selected_action == "acquire":
            offset_x, offset_y = _frame_offset(driver, frame_selector)
            click_x = offset_x + float(rect["x"])
            click_y = offset_y + float(rect["y"])
            for event_type in ("mousePressed", "mouseReleased"):
                driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": click_x,
                        "y": click_y,
                        "button": "left",
                        "buttons": 1 if event_type == "mousePressed" else 0,
                        "clickCount": 1,
                    },
                )
        _select_frame(driver, frame_selector)
        try:
            if selected_action != "status":
                result = driver.execute_script(
                    _POINTER_LOCK_SCRIPT, selector, selected_action
                )
                if not result.get("success"):
                    raise RuntimeError(result.get("error") or "Pointer lock failed")
            expected = selected_action == "acquire"
            deadline = time.monotonic() + timeout
            status = driver.execute_script(_POINTER_LOCK_STATUS_SCRIPT)
            while (
                selected_action != "status"
                and bool(status["locked"]) is not expected
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
                status = driver.execute_script(_POINTER_LOCK_STATUS_SCRIPT)
        finally:
            driver.switch_to.default_content()
        session.pointer_locked = bool(status["locked"])
        return {
            **_page_summary(driver, session_id),
            "success": selected_action == "status" or session.pointer_locked is expected,
            "action": selected_action,
            "locked": session.pointer_locked,
            "element": status.get("element"),
            "frame_selector": frame_selector,
            "note": (
                "Use pointer coordinate_mode='relative' while locked; only "
                "movementX/movementY reach the game."
                if session.pointer_locked
                else None
            ),
        }


def input_batch(
    key_actions: list[dict[str, str]] | None = None,
    pointer_actions: list[dict[str, Any]] | None = None,
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    wait_seconds: float = 0.0,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Apply mixed keyboard/pointer actions before advancing one step-mode frame."""
    selected_keys = list(key_actions or [])
    selected_pointers = list(pointer_actions or [])
    if not selected_keys and not selected_pointers:
        raise ValueError("Provide at least one key action or pointer action")
    if len(selected_keys) > 16 or len(selected_pointers) > 16:
        raise ValueError("A batch accepts at most 16 key and 16 pointer actions")

    normalized_keys: list[dict[str, str]] = []
    for item in selected_keys:
        if not isinstance(item, dict) or "key" not in item or "action" not in item:
            raise ValueError("Each key action requires 'key' and 'action'")
        key = str(item["key"])
        action = str(item["action"]).strip().lower()
        _normalize_game_key(key)
        if action not in {"tap", "hold", "release"}:
            raise ValueError("Key action must be 'tap', 'hold', or 'release'")
        normalized_keys.append({"key": key, "action": action})

    normalized_pointers: list[dict[str, Any]] = []
    for item in selected_pointers:
        if not isinstance(item, dict) or not {"action", "x", "y"} <= item.keys():
            raise ValueError("Each pointer action requires 'action', 'x', and 'y'")
        normalized_pointers.append(dict(item))

    session = _get_session(session_id)
    pointer_results: list[dict[str, Any]] = []
    with session.lock:
        driver = session.driver
        # A tap is pressed with the rest of the batch and lifted only after the
        # frame runs, so an engine that polls key state still sees it down.
        tapped = [item["key"] for item in normalized_keys if item["action"] == "tap"]
        if normalized_keys:
            # One frame switch, one focus and one event stream for the whole
            # batch: per-action round-trips are what make input feel laggy.
            events: list[dict[str, Any]] = []
            for item in normalized_keys:
                key_ids = [item["key"].strip().upper()]
                normalized = [_normalize_game_key(item["key"])]
                action = "hold" if item["action"] == "tap" else item["action"]
                down, up = _key_event_pair(session, key_ids, normalized, action)
                events.extend(down)
                events.extend(up)
                _commit_held_keys(session, key_ids, normalized, action)
            _select_frame(driver, frame_selector)
            try:
                _focus_target(driver, target_selector, "focus")
                if events:
                    _perform_key_events(driver, events)
            finally:
                if frame_selector:
                    driver.switch_to.default_content()
        if normalized_pointers:
            offset_x, offset_y, viewport = _pointer_context(driver, frame_selector)
            for item in normalized_pointers:
                result = _pointer_dispatch(
                    session,
                    str(item["action"]),
                    float(item["x"]),
                    float(item["y"]),
                    viewport,
                    offset_x=offset_x,
                    offset_y=offset_y,
                    end_x=float(item["end_x"]) if item.get("end_x") is not None else None,
                    end_y=float(item["end_y"]) if item.get("end_y") is not None else None,
                    button=str(item.get("button", "left")),
                    duration_seconds=float(item.get("duration_seconds", 0.0)),
                    coordinate_mode=str(item.get("coordinate_mode", "absolute")),
                    delta_x=float(item.get("delta_x", 0.0)),
                    delta_y=float(item.get("delta_y", 0.0)),
                )
                pointer_results.append(
                    {
                        key: result[key]
                        for key in (
                            "action",
                            "button",
                            "x",
                            "y",
                            "end_x",
                            "end_y",
                            "coordinate_mode",
                            "delta_x",
                            "delta_y",
                        )
                    }
                )
        frame_advanced = session.render_mode == "step"
        _auto_advance_render_after_input(session)
        if tapped:
            key_ids = [key.strip().upper() for key in tapped]
            normalized = [_normalize_game_key(key) for key in tapped]
            _, up = _key_event_pair(session, key_ids, normalized, "release")
            _commit_held_keys(session, key_ids, normalized, "release")
            if up:
                _select_frame(driver, frame_selector)
                try:
                    _perform_key_events(driver, up)
                finally:
                    if frame_selector:
                        driver.switch_to.default_content()
        _wait_after_action(driver, wait_seconds)
        return {
            **_action_summary(driver, session_id, include_summary),
            "success": True,
            "key_actions": normalized_keys,
            "pointer_actions": pointer_results,
            "render_mode": session.render_mode,
            "held_keys": sorted(session.held_keys),
            "held_buttons": sorted(session.held_buttons),
            "frame_advanced": frame_advanced,
            "frames_advanced": 1 if frame_advanced else 0,
            "target_selector": target_selector,
            "frame_selector": frame_selector,
        }


_GAME_PROBE_SCRIPT = r"""
function selector(el) {
  if (el.id) return '#' + CSS.escape(el.id);
  const nodes = Array.from(document.querySelectorAll(el.tagName.toLowerCase()));
  return el.tagName.toLowerCase() + ':nth-of-type(' + (nodes.indexOf(el) + 1) + ')';
}
const canvases = Array.from(document.querySelectorAll('canvas')).map(canvas => {
  const rect = canvas.getBoundingClientRect();
  let context = 'unknown';
  for (const kind of ['webgl2', 'webgl', '2d']) {
    try {
      if (canvas.getContext(kind)) { context = kind; break; }
    } catch (_) {}
  }
  return {
    selector: selector(canvas), context,
    width: canvas.width, height: canvas.height,
    client_width: canvas.clientWidth, client_height: canvas.clientHeight,
    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
    visible: !!(rect.width && rect.height)
  };
});
const navigation = performance.getEntriesByType('navigation')[0];
return {
  ready_state: document.readyState,
  visibility_state: document.visibilityState,
  document_has_focus: document.hasFocus(),
  canvas_count: canvases.length,
  canvases,
  iframe_count: document.querySelectorAll('iframe').length,
  iframes: Array.from(document.querySelectorAll('iframe')).map(frame => ({
    selector: selector(frame), src: frame.src || '', title: frame.title || ''
  })),
  navigation_ms: navigation ? Math.round(navigation.duration) : null
};
"""


def _unreported_console(
    session: BrowserSession, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop the copies of messages an earlier probe already reported.

    Chrome writes its own browser-log copy of a ``console.error`` a moment after
    the in-page hook has handed the message over, so the two copies can arrive in
    different probes, where the dedupe inside a single read cannot see them
    together. Remembering the last hooked messages is what keeps one error
    reported once rather than once per channel.
    """
    remembered = session.probe_console_seen
    fresh = diagnostics.dedupe_console(remembered + entries)[len(remembered) :]
    session.probe_console_seen = (
        remembered + [item for item in fresh if item.get("kind") != "browser"]
    )[-50:]
    return fresh


def game_probe(
    session_id: str = "default",
    frame_selector: str | None = None,
    sample_seconds: float = 1.0,
    include_console: bool = True,
) -> dict[str, Any]:
    """Inspect canvas/WebGL readiness, animation FPS, frames, focus, and console issues.

    ``console_messages`` carries only what appeared since this session's previous
    probe, each message once, so that polling in a loop reports a problem when it
    happens instead of re-reporting everything the session has ever logged. The
    console topic keeps its own place in the same buffers, so reading either one
    leaves the other's entries where they are.
    """
    duration = max(0.1, min(float(sample_seconds), 3.0))
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        driver.switch_to.default_content()
        try:
            if frame_selector:
                WebDriverWait(driver, 10).until(
                    conditions.frame_to_be_available_and_switch_to_it(
                        (By.CSS_SELECTOR, frame_selector)
                    )
                )
            probe = driver.execute_script(_GAME_PROBE_SCRIPT)
            if session.render_mode != "normal":
                # Frames are released by hand, so both requestAnimationFrame and the
                # safety setTimeout are gated: measuring would just burn the script
                # timeout and then report a fabricated zero. The gate may also live
                # in another document than the one probed, and reporting that
                # document's healthy fps would describe a frozen game as running.
                animation = {
                    "frames": 0,
                    "elapsed_ms": 0,
                    "fps": None,
                    "available": False,
                    "animation_suspended": True,
                    "reason": (
                        f"render mode '{session.render_mode}' releases frames manually"
                        + (
                            f" in {session.render_frame_selector}"
                            if session.render_frame_selector
                            else ""
                        )
                        + "; FPS is not measured while the gate is engaged."
                    ),
                    "gated_frame_selector": session.render_frame_selector,
                }
            else:
                try:
                    animation = driver.execute_async_script(
                        "const duration=arguments[0]*1000, done=arguments[arguments.length-1];"
                        "let frames=0, start=performance.now(), finished=false;"
                        "function finish(now,suspended){if(finished)return;finished=true;clearTimeout(fallback);"
                        "const elapsed=Math.max(1,now-start);done({frames,elapsed_ms:Math.round(elapsed),"
                        "fps:Math.round(frames*10000/elapsed)/10,available:frames>0,animation_suspended:suspended});}"
                        "function tick(now){frames++;if(now-start>=duration)finish(now,false);"
                        "else requestAnimationFrame(tick);}"
                        "const fallback=setTimeout(()=>finish(performance.now(),true),duration+2000);"
                        "requestAnimationFrame(tick);",
                        duration,
                    )
                except TimeoutException:
                    animation = {
                        "frames": 0,
                        "elapsed_ms": round(duration * 1000),
                        "fps": 0.0,
                        "available": False,
                        "animation_suspended": True,
                    }
        finally:
            driver.switch_to.default_content()
        console_messages: list[dict[str, Any]] = []
        console_error: str | None = None
        if include_console:
            try:
                payload = _console_since(
                    session, session.probe_console_cursor, session.probe_log_cursor
                )
                session.probe_console_cursor = payload["next_seq"]
                session.probe_log_cursor = payload["next_log_cursor"]
                console_messages = [
                    {
                        "level": item.get("level"),
                        "message": str(item.get("text", ""))[:2000],
                        "timestamp": item.get("ts"),
                    }
                    for item in _unreported_console(session, payload["entries"])[-100:]
                    if item.get("level") in {"warn", "error"}
                ]
            except Exception as exc:
                console_error = f"{type(exc).__name__}: {exc}"
        return {
            **_page_summary(driver, session_id),
            "success": probe["ready_state"] in {"interactive", "complete"},
            "frame_selector": frame_selector,
            **probe,
            "animation": animation,
            "console_messages": console_messages,
            "console_scope": "new since the previous game_probe call",
            "console_error": console_error,
            "render_control": {
                "mode": session.render_mode,
                "target_fps": session.render_target_fps,
                "frame_selector": session.render_frame_selector,
            },
            "held_inputs": {
                "keys": sorted(session.held_keys),
                "buttons": sorted(session.held_buttons),
            },
        }


_RENDER_BOOTSTRAP_SCRIPT = r"""
(() => {
const stateKey = '__webSearchNeoRenderControl';
if (window[stateKey]) return;

// Capture every timing primitive before anything is replaced, so the gate can
// restore real timing and can schedule its own work without gating itself.
const nativeRequest = window.requestAnimationFrame.bind(window);
const nativeCancel = window.cancelAnimationFrame.bind(window);
const nativeSetTimeout = window.setTimeout.bind(window);
const nativeClearTimeout = window.clearTimeout.bind(window);
const nativeSetInterval = window.setInterval.bind(window);
const nativeClearInterval = window.clearInterval.bind(window);
const nativeIdle = window.requestIdleCallback ? window.requestIdleCallback.bind(window) : null;
const nativeCancelIdle = window.cancelIdleCallback ? window.cancelIdleCallback.bind(window) : null;
const nativePerformanceNow = performance.now.bind(performance);
const nativeDateNow = Date.now.bind(Date);
const epochOffset = nativeDateNow() - nativePerformanceNow();

const state = {
    mode: 'normal',
    targetFps: null,
    interval: 1000 / 60,
    frameDelta: 1000 / 60,
    freezeTime: true,
    gateTimers: true,
    clockInstalled: false,
    timersInstalled: false,
    virtualNow: nativePerformanceNow(),
    lastFrame: nativePerformanceNow(),
    lastRealFlush: nativePerformanceNow(),
    frameCount: 0,
    nextId: -1,
    nextTimerId: -1,
    pending: new Map(),
    native: new Map(),
    nativeIds: new Map(),
    timers: new Map(),
    liveTimers: new Map(),
    timer: null,
    nativeRequest: nativeRequest,
    nativeCancel: nativeCancel
};

// The page-visible clock. While the gate is engaged it only moves when a frame
// is released, so a game sees a constant delta no matter how long the agent
// spent thinking between calls.
state.now = () => (state.gated() && state.freezeTime ? state.virtualNow : nativePerformanceNow());
state.gated = () => state.mode !== 'normal';

state.installClock = () => {
    if (state.clockInstalled) return;
    performance.now = () => state.now();
    Date.now = () => Math.round(epochOffset + state.now());
    state.clockInstalled = true;
};
state.restoreClock = () => {
    if (!state.clockInstalled) return;
    performance.now = nativePerformanceNow;
    Date.now = nativeDateNow;
    state.clockInstalled = false;
};

// Timer wrappers are installed once, at bootstrap, and stay pass-through while
// the gate is off. Installing them only when step mode starts would leave every
// timer a real game registered during load running on the wall clock, which is
// exactly the case that matters.
state.wrapTimer = (callback, delay, args, interval) => {
    if (typeof callback !== 'function') return null;
    const id = state.nextTimerId--;
    const wait = interval === null
      ? Math.max(0, Number(delay) || 0)
      : Math.max(1, Number(delay) || 0);
    if (state.gated() && state.gateTimers) {
      state.timers.set(id, {
        callback: callback, args: args, interval: interval, due: state.now() + wait
      });
      // A queued timer is work that only a released frame can run, so it has to
      // be able to start the pump on its own; in step mode this does nothing and
      // the agent stays the only source of frames.
      state.schedule();
      return id;
    }
    const fire = interval === null
      ? (...inner) => { state.liveTimers.delete(id); callback(...inner); }
      : callback;
    const nativeId = interval === null
      ? nativeSetTimeout(fire, wait, ...args)
      : nativeSetInterval(fire, wait, ...args);
    state.liveTimers.set(id, {
      nativeId: nativeId, callback: callback, args: args,
      interval: interval, realDue: nativePerformanceNow() + wait
    });
    return id;
};

state.dropTimer = id => {
    if (state.timers.delete(id)) return true;
    const live = state.liveTimers.get(id);
    if (!live) return false;
    if (live.interval === null) nativeClearTimeout(live.nativeId);
    else nativeClearInterval(live.nativeId);
    state.liveTimers.delete(id);
    return true;
};

state.installTimers = () => {
    if (state.timersInstalled) return;
    window.setTimeout = (callback, delay, ...args) =>
      state.wrapTimer(callback, delay, args, null) ?? nativeSetTimeout(callback, delay, ...args);
    window.clearTimeout = id => { if (!state.dropTimer(id)) nativeClearTimeout(id); };
    window.setInterval = (callback, delay, ...args) =>
      state.wrapTimer(callback, delay, args, Math.max(1, Number(delay) || 0))
        ?? nativeSetInterval(callback, delay, ...args);
    window.clearInterval = id => { if (!state.dropTimer(id)) nativeClearInterval(id); };
    if (nativeIdle) {
      window.requestIdleCallback = (callback, options) => {
        if (typeof callback !== 'function') return nativeIdle(callback, options);
        return state.wrapTimer(
          () => callback({didTimeout: false, timeRemaining: () => 8}), 0, [], null
        );
      };
      window.cancelIdleCallback = id => { if (!state.dropTimer(id)) nativeCancelIdle(id); };
    }
    state.timersInstalled = true;
};

// Pull timers the real scheduler is already holding into the virtual queue, so
// that gating catches everything the page set up before the gate existed.
state.captureTimers = () => {
    const now = state.now();
    const real = nativePerformanceNow();
    for (const [id, entry] of state.liveTimers) {
      if (entry.interval === null) nativeClearTimeout(entry.nativeId);
      else nativeClearInterval(entry.nativeId);
      const remaining = entry.interval === null
        ? Math.max(0, entry.realDue - real)
        : entry.interval;
      state.timers.set(id, {
        callback: entry.callback, args: entry.args,
        interval: entry.interval, due: now + remaining
      });
    }
    state.liveTimers.clear();
};

// Rebase virtual deadlines onto another clock. Turning freeze_time off swaps the
// clock underneath the queue, and a deadline read against the wrong one is
// already in the past, so the whole queue would detonate on the next frame.
state.rebaseTimers = (fromNow, toNow) => {
    for (const entry of state.timers.values()) {
      entry.due = toNow + Math.max(0, entry.due - fromNow);
    }
};

// Give the queue back to the real scheduler, keeping ids valid for clearTimeout.
// `referenceNow` is the clock the deadlines were written against; the caller has
// to pass it whenever the mode is about to change.
state.releaseTimers = referenceNow => {
    const queued = Array.from(state.timers.entries());
    state.timers.clear();
    const real = nativePerformanceNow();
    const now = referenceNow === undefined ? state.now() : referenceNow;
    for (const [id, entry] of queued) {
      const fire = entry.interval === null
        ? (...inner) => { state.liveTimers.delete(id); entry.callback(...inner); }
        : entry.callback;
      const wait = entry.interval === null ? Math.max(0, entry.due - now) : entry.interval;
      const nativeId = entry.interval === null
        ? nativeSetTimeout(fire, wait, ...entry.args)
        : nativeSetInterval(fire, wait, ...entry.args);
      state.liveTimers.set(id, {
        nativeId: nativeId, callback: entry.callback, args: entry.args,
        interval: entry.interval, realDue: real + wait
      });
    }
};
state.runDueTimers = now => {
    if (!state.gateTimers || !state.timers.size) return 0;
    let count = 0;
    for (let guard = 0; guard < 64; guard++) {
      const due = Array.from(state.timers.entries())
        .filter(entry => entry[1].due <= now)
        .sort((a, b) => a[1].due - b[1].due);
      if (!due.length) break;
      for (const [id, entry] of due) {
        if (entry.interval) entry.due = now + entry.interval;
        else state.timers.delete(id);
        try { entry.callback(...entry.args); }
        catch (error) { nativeSetTimeout(() => { throw error; }, 0); }
        count += 1;
      }
    }
    return count;
};

state.flush = () => {
    if (state.gated() && state.freezeTime) state.virtualNow += state.frameDelta;
    else state.virtualNow = nativePerformanceNow();
    const timestamp = state.now();
    state.lastFrame = timestamp;
    state.frameCount += 1;
    state.runDueTimers(timestamp);
    const batch = Array.from(state.pending.entries());
    state.pending.clear();
    for (const [, callback] of batch) {
      try { callback(timestamp); } catch (error) { nativeSetTimeout(() => { throw error; }, 0); }
    }
    state.schedule();
    return batch.length;
};

// Keep the throttled pump running for as long as anything is waiting on a
// frame - a queued frame callback or a gated timer. Waiting only on frame
// callbacks deadlocks a page whose loop boots from a timer: that timer runs on
// the frame it was itself going to ask for. With nothing queued no timer is
// armed at all, so an idle page costs nothing and the pump restarts from
// `request` and `wrapTimer` the moment work appears.
state.pumpWanted = () =>
    state.pending.size > 0 || (state.gateTimers && state.timers.size > 0);

state.schedule = () => {
    if (state.mode !== 'throttled' || state.timer !== null || !state.pumpWanted()) return;
    const elapsed = nativePerformanceNow() - state.lastRealFlush;
    const delay = Math.max(0, state.interval - elapsed);
    state.timer = nativeSetTimeout(() => {
      state.timer = null;
      state.lastRealFlush = nativePerformanceNow();
      state.flush();
    }, delay);
};

// While the gate is off the callback goes to the real scheduler, but it is also
// remembered: a callback already queued there when the gate engages would fire
// on the next compositor frame - one the agent never asked for, landing in the
// middle of an unrelated call - so setMode has to be able to reclaim it.
state.request = callback => {
    if (state.mode === 'normal') {
      const id = state.nativeRequest(timestamp => {
        state.native.delete(id);
        state.nativeIds.delete(id);
        callback(timestamp);
      });
      state.native.set(id, callback);
      return id;
    }
    const id = state.nextId--;
    state.pending.set(id, callback);
    state.schedule();
    return id;
};

// Hand a callback back to the real scheduler under the id the page already holds:
// re-registering it under a fresh id would make the page's cancelAnimationFrame
// silently miss, and the frame it thought it cancelled still runs.
state.adopt = (id, callback) => {
    const nativeId = state.nativeRequest(timestamp => {
      state.native.delete(id);
      state.nativeIds.delete(id);
      callback(timestamp);
    });
    state.native.set(id, callback);
    state.nativeIds.set(id, nativeId);
};

state.cancel = id => {
    if (state.pending.delete(id)) return;
    const adopted = state.nativeIds.get(id);
    state.nativeIds.delete(id);
    state.native.delete(id);
    state.nativeCancel(adopted === undefined ? id : adopted);
};

// Release `count` frames, yielding to the real task queue between them so that
// network callbacks and page microtasks can run like they would in a real frame.
state.step = (count, done) => {
    let remaining = count;
    let callbacks = 0;
    const run = () => {
      callbacks += state.flush();
      remaining -= 1;
      if (remaining > 0) nativeSetTimeout(run, 0);
      else done({
        success: true, frames: count, callbacks,
        pending_callbacks: state.pending.size,
        pending_timers: state.timers.size,
        frame_count: state.frameCount,
        virtual_now: Math.round(state.now())
      });
    };
    run();
};

state.setMode = (mode, targetFps, options) => {
    const settings = options || {};
    if (state.timer !== null) nativeClearTimeout(state.timer);
    state.timer = null;
    // Everything queued so far carries a deadline written against the clock that
    // is live right now. It has to be read before that clock is swapped.
    const previousNow = state.now();
    const nextFreeze = settings.freeze_time !== undefined
      ? !!settings.freeze_time : state.freezeTime;
    const nextGate = settings.gate_timers !== undefined
      ? !!settings.gate_timers : state.gateTimers;
    if (mode === 'normal' || !nextGate) state.releaseTimers(previousNow);
    state.mode = mode;
    state.targetFps = mode === 'throttled' ? targetFps : null;
    state.interval = 1000 / targetFps;
    if (settings.frame_delta_ms) state.frameDelta = settings.frame_delta_ms;
    state.freezeTime = nextFreeze;
    state.gateTimers = nextGate;
    if (mode === 'normal') {
      state.restoreClock();
      const callbacks = Array.from(state.pending.entries());
      state.pending.clear();
      for (const [id, callback] of callbacks) state.adopt(id, callback);
    } else {
      // Reclaim frames the real scheduler still owes, keeping their ids valid
      // so a later cancelAnimationFrame still finds them.
      for (const [id, callback] of state.native) {
        const adopted = state.nativeIds.get(id);
        state.nativeCancel(adopted === undefined ? id : adopted);
        state.pending.set(id, callback);
      }
      state.native.clear();
      state.nativeIds.clear();
      state.virtualNow = nativePerformanceNow();
      if (state.freezeTime) state.installClock(); else state.restoreClock();
      if (state.gateTimers) {
        state.rebaseTimers(previousNow, state.now());
        state.captureTimers();
      }
      state.schedule();
    }
};

window[stateKey] = state;
window.requestAnimationFrame = state.request;
window.cancelAnimationFrame = state.cancel;
// Wrap timers immediately so the ones a game registers while loading can be
// reclaimed later; while the gate is off they pass straight through.
state.installTimers();
})();
"""


_RENDER_CONTROL_SCRIPT = r"""
const mode = arguments[0];
const targetFps = arguments[1];
const options = arguments[2];
const state = window.__webSearchNeoRenderControl;
if (!state) {
  return {error: 'Render bootstrap is unavailable in this document'};
}
state.setMode(mode, targetFps, options);
return {
  mode: state.mode,
  target_fps: state.targetFps,
  pending_callbacks: state.pending.size,
  frame_delta_ms: Math.round(state.frameDelta * 1000) / 1000,
  time_frozen: state.clockInstalled,
  timers_gated: state.gated() && state.gateTimers
};
"""


def _register_render_bootstrap(session: BrowserSession) -> None:
    """Install the frame gate and console hook before any script in new documents."""
    if session.render_bootstrap_registered:
        return
    for source in (
        _RENDER_BOOTSTRAP_SCRIPT,
        diagnostics.CONSOLE_HOOK_SCRIPT,
        page_perception.REF_REGISTRY_SCRIPT,
    ):
        session.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": source}
        )
    session.render_bootstrap_registered = True


_RENDER_STEP_SCRIPT = r"""
const count = arguments[0];
const done = arguments[arguments.length - 1];
const state = window.__webSearchNeoRenderControl;
if (!state) {
  done({success: false, error: 'missing_bootstrap'});
  return;
}
if (state.mode !== 'step') {
  done({success: false, error: 'not_step_mode', mode: state.mode});
  return;
}
state.step(count, done);
"""


def _select_frame(driver: webdriver.Chrome, frame_selector: str | None) -> None:
    driver.switch_to.default_content()
    if frame_selector:
        WebDriverWait(driver, 10).until(
            conditions.frame_to_be_available_and_switch_to_it(
                (By.CSS_SELECTOR, frame_selector)
            )
        )


def _apply_render_mode(
    driver: webdriver.Chrome,
    frame_selector: str | None,
    mode: str,
    fps: float,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Install the gate in the selected document and switch it to one mode."""
    _select_frame(driver, frame_selector)
    try:
        driver.execute_script(_RENDER_BOOTSTRAP_SCRIPT)
        return driver.execute_script(_RENDER_CONTROL_SCRIPT, mode, fps, options)
    finally:
        driver.switch_to.default_content()


def set_render_control(
    mode: str,
    session_id: str = "default",
    target_fps: float = 10.0,
    frame_selector: str | None = None,
    frame_delta_ms: float = 1000 / 60,
    freeze_time: bool = True,
    gate_timers: bool = True,
    key_repeat: bool = True,
) -> dict[str, Any]:
    """Set normal, continuously throttled, or manual/input-driven frame stepping.

    While the gate is engaged, ``freeze_time`` makes ``performance.now()`` and
    ``Date.now()`` advance by exactly ``frame_delta_ms`` per released frame, and
    ``gate_timers`` queues ``setTimeout``/``setInterval`` against that same
    clock. Without them a game measures the agent's thinking time as its frame
    delta and behaves nothing like it does for a human.
    """
    selected_mode = mode.strip().lower()
    if selected_mode not in {"normal", "throttled", "step"}:
        raise ValueError("mode must be 'normal', 'throttled', or 'step'")
    fps = max(1.0, min(float(target_fps), 60.0))
    delta = max(0.1, min(float(frame_delta_ms), 1000.0))
    options = {
        "frame_delta_ms": delta,
        "freeze_time": bool(freeze_time),
        "gate_timers": bool(gate_timers),
    }
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        selected_frame = (
            frame_selector
            if frame_selector is not None
            else session.render_frame_selector
        )
        if (
            session.render_mode != "normal"
            and selected_frame != session.render_frame_selector
        ):
            _apply_render_mode(
                driver, session.render_frame_selector, "normal", 60.0, options
            )
        state = _apply_render_mode(driver, selected_frame, selected_mode, fps, options)
        if state.get("error"):
            raise RuntimeError(state["error"])
        session.render_options = options
        session.key_repeat = bool(key_repeat)
        session.render_mode = selected_mode
        session.render_target_fps = fps if selected_mode == "throttled" else None
        session.render_frame_selector = selected_frame if selected_mode != "normal" else None
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "mode": selected_mode,
            "target_fps": session.render_target_fps,
            "frame_selector": session.render_frame_selector,
            "key_repeat": session.key_repeat,
            "input_advances_frame": selected_mode == "step",
            "engine": "requestAnimationFrame gate",
            **state,
        }


def _step_frames_once(
    driver: webdriver.Chrome, frame_selector: str | None, frames: int
) -> dict[str, Any]:
    _select_frame(driver, frame_selector)
    try:
        return driver.execute_async_script(_RENDER_STEP_SCRIPT, frames)
    finally:
        driver.switch_to.default_content()


def _repeat_held_keys(session: BrowserSession) -> None:
    """Auto-repeat held keys the way a real keyboard does, before frames run.

    A single synthetic keydown never comes back, so a game that latches movement
    on keydown and clears that latch itself - the platformer fixture does it on
    respawn - stays dead forever even though the key is still held down.
    """
    if not session.key_repeat or not session.held_keys:
        session.fresh_keys.clear()
        return
    driver = session.driver
    events = [
        {"type": "down", "key": key, "repeat": True}
        for key_id, key in session.held_keys.items()
        if key_id not in session.fresh_keys
    ]
    session.fresh_keys.clear()
    if not events:
        return
    frame_selector = session.render_frame_selector
    if frame_selector:
        _select_frame(driver, frame_selector)
    try:
        _perform_key_events(driver, events)
    finally:
        if frame_selector:
            driver.switch_to.default_content()


def _advance_render_frames(
    session: BrowserSession, frames: int, session_id: str = ""
) -> dict[str, Any]:
    """Release frames, reinstalling the gate if the document was replaced.

    A game that reloads itself - a level restart, or a cross-origin game iframe
    swapping its document - drops the injected gate. Without recovery every
    later input call fails, so the gate is reinstalled once and retried.
    """
    driver = session.driver
    frame_selector = session.render_frame_selector
    _repeat_held_keys(session)
    result = _step_frames_once(driver, frame_selector, frames)
    if not result.get("success") and result.get("error") in {
        "missing_bootstrap",
        "not_step_mode",
    }:
        state = _apply_render_mode(
            driver, frame_selector, "step", 60.0, session.render_options
        )
        if not state.get("error"):
            result = _step_frames_once(driver, frame_selector, frames)
            result["gate_reinstalled"] = True
    if not result.get("success"):
        reason = {
            "missing_bootstrap": (
                "The frame gate is missing in this document, most likely because the "
                "page or game iframe reloaded. Call render(mode='step') again."
            ),
            "not_step_mode": "Render control is not in step mode",
        }.get(result.get("error"), result.get("error") or "Render step failed")
        raise RuntimeError(reason)
    return result


def render_step(
    frames: int = 1,
    session_id: str = "default",
    include_summary: bool = True,
) -> dict[str, Any]:
    """Advance an active step-mode canvas/WebGL loop by a bounded frame count."""
    frame_count = max(1, min(int(frames), 120))
    session = _get_session(session_id)
    with session.lock:
        if session.render_mode != "step":
            # Naming the exact call matters: "not in step mode" alone leaves the
            # caller stepping a page that is really running at full speed.
            raise ValueError(
                f"Frames can only be stepped in step mode; this session is in "
                f"'{session.render_mode}' mode, so the page is running on its own. "
                'Send {"action": "render", "mode": "step", "session_id": '
                f'"{session_id}"}} first.'
            )
        result = _advance_render_frames(session, frame_count, session_id)
        return {
            **_action_summary(session.driver, session_id, include_summary),
            "success": True,
            "mode": "step",
            "frame_selector": session.render_frame_selector,
            **result,
        }


def _auto_advance_render_after_input(session: BrowserSession, frames: int = 1) -> None:
    if session.render_mode == "step":
        _advance_render_frames(session, frames)


def release_inputs(session_id: str = "default") -> dict[str, Any]:
    """Release all keyboard keys and mouse buttons held by the named session."""
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        if session.held_keys:
            _perform_key_events(
                driver,
                [
                    {"type": "up", "key": key}
                    for key in reversed(list(session.held_keys.values()))
                ],
            )
            session.held_keys.clear()
        button_bits = {"left": 1, "right": 2, "middle": 4}
        for button in list(session.held_buttons):
            session.held_buttons.discard(button)
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": session.pointer_x,
                    "y": session.pointer_y,
                    "button": button,
                    "buttons": sum(button_bits[item] for item in session.held_buttons),
                    "clickCount": 1,
                },
            )
        _auto_advance_render_after_input(session)
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "held_keys": [],
            "held_buttons": [],
        }


def submit_form(
    form_selector: str,
    session_id: str = "default",
    submit_selector: str | None = None,
    wait_seconds: float = 0.5,
) -> dict[str, Any]:
    """Submit a form using requestSubmit so browser validation and events run."""
    session = _get_session(session_id)
    with session.lock:
        form = _resolve_element(session.driver, form_selector)
        validation = session.driver.execute_script(
            "const form = arguments[0]; const invalid = Array.from(form.elements)"
            ".filter(el => el.willValidate && !el.checkValidity())"
            ".map(el => ({name: el.name || '', id: el.id || '', message: el.validationMessage || ''}));"
            "return {valid: form.checkValidity(), invalid};",
            form,
        )
        if not validation["valid"]:
            session.driver.execute_script("arguments[0].reportValidity();", form)
            return {
                **_page_summary(session.driver, session_id),
                "success": False,
                "validation_passed": False,
                "submit_triggered": False,
                "validation_errors": validation["invalid"],
                "submitted_form": form_selector,
            }
        before_url = session.driver.current_url
        before_title = session.driver.title
        session.driver.execute_script(
            "window.__webSearchNeoSubmitCount = 0; arguments[0].addEventListener('submit', "
            "() => { window.__webSearchNeoSubmitCount += 1; }, {once: true});",
            form,
        )
        if submit_selector:
            button = WebDriverWait(session.driver, 10).until(
                conditions.element_to_be_clickable((By.CSS_SELECTOR, submit_selector))
            )
            button.click()
        else:
            session.driver.execute_script(
                "if (arguments[0].requestSubmit) arguments[0].requestSubmit(); else arguments[0].submit();",
                form,
            )
        _wait_after_action(session.driver, wait_seconds)
        after_url = session.driver.current_url
        after_title = session.driver.title
        try:
            submit_event_fired = bool(
                session.driver.execute_script(
                    "return window.__webSearchNeoSubmitCount && window.__webSearchNeoSubmitCount > 0;"
                )
            )
        except Exception:
            submit_event_fired = False
        navigation_observed = before_url != after_url or before_title != after_title
        submit_triggered = submit_event_fired or navigation_observed
        return {
            **_page_summary(session.driver, session_id),
            "success": bool(validation["valid"] and submit_triggered),
            "validation_passed": bool(validation["valid"]),
            "submit_triggered": submit_triggered,
            "submit_event_fired": submit_event_fired,
            "navigation_observed": navigation_observed,
            "url_before": before_url,
            "validation_errors": [],
            "submitted_form": form_selector,
        }


def screenshot(
    session_id: str = "default",
    width: int = 1440,
    height: int = 900,
    full_page: bool = False,
) -> bytes:
    """Capture the rendered page as PNG bytes."""
    session = _get_session(session_id)
    width, height = _bounded_size(width, height)
    with session.lock:
        _set_viewport(session.driver, width, height)
        if full_page:
            metrics = session.driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
            size = metrics["contentSize"]
            capture = session.driver.execute_cdp_cmd(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "captureBeyondViewport": True,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": min(float(size["width"]), 3840),
                        "height": min(float(size["height"]), 10000),
                        "scale": 1,
                    },
                },
            )
            return base64.b64decode(capture["data"])
        return session.driver.get_screenshot_as_png()


def get_status(session_id: str = "default") -> dict[str, Any]:
    """Return browser availability and current session state."""
    session_id = _validate_session_id(session_id)
    with _sessions_lock:
        session = _sessions.get(session_id)
        active_ids = sorted(_sessions)
    if session is None:
        bridge_status = _companion_status()
        available = bool(bridge_status["connected"] or _browser_available)
        return {
            "available": available,
            "availability_error": _browser_error,
            "session_open": False,
            "session_id": session_id,
            "active_sessions": active_ids,
            "engine": "Chrome companion extension or Selenium Manager",
            # Say which half of the server still works, so a caller that only
            # needs search or fetch does not conclude the server is dead.
            "next": (
                None
                if available
                else (
                    "Browser actions need Chrome; search, fetch_text, fetch_links, "
                    "fetch_many and the search_status and time topics do not. "
                    + (bridge_status.get("next") or "")
                ).strip()
            ),
            "current_chrome": bridge_status,
        }
    with session.lock:
        bridge_status = _companion_status()
        if session.profile_mode == "current" and not bridge_status["connected"]:
            return {
                "available": False,
                "availability_error": "Chrome companion extension disconnected",
                "session_open": True,
                "session_id": session_id,
                "active_sessions": active_ids,
                "engine": "Chrome companion extension",
                "headless": False,
                "window_mode": "visible",
                "profile_mode": "current",
                "profile_id": None,
                "debugger_address": None,
                "current_tab_id": session.current_tab_id,
                "tab_group": session.tab_group,
                "current_chrome": bridge_status,
            }
        try:
            summary = _page_summary(session.driver, session_id)
        except (ChromeBridgeError, TimeoutError, ConnectionError, OSError) as exc:
            return {
                "available": False,
                "availability_error": f"{type(exc).__name__}: {exc}",
                "session_open": True,
                "session_id": session_id,
                "active_sessions": active_ids,
                "engine": (
                    "Chrome companion extension"
                    if session.profile_mode == "current"
                    else "Chrome via Selenium Manager"
                ),
                "headless": session.headless,
                "window_mode": "headless" if session.headless else "visible",
                "profile_mode": session.profile_mode,
                "profile_id": session.profile_id,
                "debugger_address": session.debugger_address,
                "current_tab_id": session.current_tab_id,
                "tab_group": session.tab_group,
                "current_chrome": bridge_status,
            }
        return {
            "available": True,
            "availability_error": None,
            "session_open": True,
            "active_sessions": active_ids,
            "engine": (
                "Chrome companion extension"
                if session.profile_mode == "current"
                else "Chrome via Selenium Manager"
            ),
            "headless": session.headless,
            "window_mode": "headless" if session.headless else "visible",
            "profile_mode": session.profile_mode,
            "profile_id": session.profile_id,
            "debugger_address": session.debugger_address,
            "current_tab_id": session.current_tab_id,
            "tab_group": session.tab_group,
            "current_chrome": bridge_status,
            **summary,
        }


def upload_file(
    selector: str, file_paths: list[str], session_id: str = "default"
) -> dict[str, Any]:
    """Upload one or more local files to an input[type=file]."""
    if not file_paths:
        raise ValueError("file_paths must not be empty")
    session = _get_session(session_id)
    resolved: list[str] = []
    for file_path in file_paths:
        path = Path(file_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"Upload path is not a file: {path}")
        resolved.append(str(path))
    with session.lock:
        element = _resolve_element(session.driver, selector)
        if (element.get_attribute("type") or "").lower() != "file":
            raise ValueError("Selector does not point to an input[type=file]")
        if len(resolved) > 1 and element.get_attribute("multiple") is None:
            raise ValueError("Input does not accept multiple files")
        element.send_keys("\n".join(resolved))
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "selector": selector,
            "files_uploaded": len(resolved),
            "file_names": [Path(path).name for path in resolved],
        }


def _reset_session_runtime_state(session: BrowserSession) -> None:
    """Do not carry held input or a render gate across navigation or detach."""
    driver = session.driver
    try:
        if session.held_keys:
            _perform_key_events(
                driver,
                [
                    {"type": "up", "key": key}
                    for key in reversed(list(session.held_keys.values()))
                ],
            )
            session.held_keys.clear()
        button_bits = {"left": 1, "right": 2, "middle": 4}
        for button in list(session.held_buttons):
            session.held_buttons.discard(button)
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": session.pointer_x,
                    "y": session.pointer_y,
                    "button": button,
                    "buttons": sum(button_bits[item] for item in session.held_buttons),
                    "clickCount": 1,
                },
            )
        if session.render_mode != "normal":
            _select_frame(driver, session.render_frame_selector)
            try:
                driver.execute_script(_RENDER_CONTROL_SCRIPT, "normal", 60.0)
            finally:
                driver.switch_to.default_content()
            session.render_mode = "normal"
            session.render_target_fps = None
            session.render_frame_selector = None
    except Exception:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass


def _shutdown_session(session: BrowserSession, close_tab: bool | None = None) -> bool:
    """Release one session's tab and browser; report whether the tab was closed.

    Cleanup must never raise - a failed teardown may not take the caller down with
    it - but a silent failure leaks a tab or a whole Chrome process, so the reason
    is logged instead of being swallowed.
    """
    tab_closed = False
    should_close_tab = session.owns_tab if close_tab is None else bool(close_tab)
    try:
        _reset_session_runtime_state(session)
        if should_close_tab and hasattr(session.driver, "close_tab"):
            tab_closed = bool(session.driver.close_tab().get("removed"))
        if session.owns_browser:
            session.driver.quit()
        else:
            session.driver.service.stop()
    except Exception as exc:
        logger.warning(
            "Browser session cleanup failed: %s: %s", type(exc).__name__, exc
        )
    return tab_closed


def close_session(session_id: str = "default", close_tab: bool | None = None) -> dict[str, Any]:
    """Close one browser session and release Chrome resources.

    In ``current`` mode a tab the server opened is closed by default, otherwise
    every session would leave another tab behind in the user's Chrome. A tab that
    was claimed with ``attach_tab`` is always left open.
    """
    session_id = _validate_session_id(session_id)
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
        remaining = sorted(_sessions)
    closed = session is not None
    tab_closed = False
    if session is not None:
        with session.lock:
            tab_closed = _shutdown_session(session, close_tab)
    return {
        "session_id": session_id,
        "closed": closed,
        "tab_closed": tab_closed,
        "active_sessions": remaining,
    }


def close_all_sessions() -> None:
    """Close every session, including the Chrome tabs the server itself opened."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        with session.lock:
            _shutdown_session(session)


def start_current_chrome_bridge() -> dict[str, Any]:
    """Start the loopback listener early so the extension is ready before the first action."""
    bridge = get_chrome_bridge()
    bridge.start()
    return bridge.status(0.0)


atexit.register(close_all_sessions)
