"""Stateful Selenium browser sessions used by MCP browser tools."""

from __future__ import annotations

import atexit
import base64
from dataclasses import dataclass, field
from http.client import HTTPConnection
import json
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
    ChromeBridgeDriver,
    ChromeBridgeError,
    get_chrome_bridge,
    list_current_chrome_tabs,
)
from chrome_bootstrap import setup_current_chrome
from web_client import validate_http_url


MAX_SESSIONS = 4
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


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
    render_mode: str = "normal"
    render_target_fps: float | None = None
    render_frame_selector: str | None = None
    render_bootstrap_registered: bool = False
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
            except Exception:
                _browser_available = False
                _browser_error = f"{type(exc).__name__}: {exc}"
                raise
        else:
            _browser_available = False
            _browser_error = f"{type(exc).__name__}: {exc}"
            raise
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


def _get_session(session_id: str) -> BrowserSession:
    _validate_session_id(session_id)
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise ValueError(
            f"Browser session '{session_id}' does not exist; call browser_open_page first"
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


def _challenge_status(driver: webdriver.Chrome) -> dict[str, Any]:
    url = driver.current_url.lower()
    title = driver.title.lower()
    try:
        body = str(
            driver.execute_script(
                "return (document.body && document.body.innerText || '').slice(0, 8000);"
            )
            or ""
        ).lower()
    except Exception:
        body = ""
    markers = {
        "captcha": ("captcha", "smartcaptcha", "recaptcha"),
        "human_verification": (
            "verify you are human",
            "checking your browser",
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
    haystacks = (url, title, body)
    for challenge_type, phrases in markers.items():
        if any(phrase in haystack for phrase in phrases for haystack in haystacks):
            return {
                "challenge_detected": True,
                "challenge_type": challenge_type,
                "manual_action_required": True,
            }
    return {
        "challenge_detected": False,
        "challenge_type": None,
        "manual_action_required": False,
    }


def _page_summary(driver: webdriver.Chrome, session_id: str) -> dict[str, Any]:
    dimensions = driver.execute_script(
        "return {viewport_width: window.innerWidth, viewport_height: window.innerHeight, "
        "page_width: document.documentElement.scrollWidth, "
        "page_height: document.documentElement.scrollHeight, "
        "ready_state: document.readyState};"
    )
    return {
        "session_id": session_id,
        "url": driver.current_url,
        "title": driver.title,
        **_challenge_status(driver),
        **dimensions,
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
            _reset_session_runtime_state(session)
            _set_viewport(session.driver, width, height)
            _register_render_bootstrap(session)
            session.driver.get(normalized)
            _wait_until_ready(session.driver, timeout_seconds)
        return {
            **_page_summary(session.driver, session_id),
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


def get_current_tabs(wait_seconds: float = 1.0) -> dict[str, Any]:
    """List normal web tabs exposed by the companion extension."""
    return list_current_chrome_tabs(max(0.0, min(float(wait_seconds), 5.0)))


def setup_current_chrome_companion(
    confirm_install: bool = False,
    timeout_seconds: float = 30.0,
    window_title: str | None = None,
) -> dict[str, Any]:
    """Install or enable the current-Chrome companion after explicit consent."""
    return setup_current_chrome(confirm_install, timeout_seconds, window_title)


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
    """Wait for a dynamic element to be present, visible, or clickable."""
    conditions_by_state = {
        "present": conditions.presence_of_element_located,
        "visible": conditions.visibility_of_element_located,
        "clickable": conditions.element_to_be_clickable,
    }
    if state not in conditions_by_state:
        raise ValueError("state must be 'present', 'visible', or 'clickable'")
    timeout = max(0.1, min(float(timeout_seconds), 30.0))
    session = _get_session(session_id)
    with session.lock:
        element = WebDriverWait(session.driver, timeout).until(
            conditions_by_state[state]((By.CSS_SELECTOR, selector))
        )
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
                element = session.driver.find_element(By.CSS_SELECTOR, selector)
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
                element = session.driver.find_element(By.CSS_SELECTOR, selector)
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
    if delay:
        time.sleep(delay)
    try:
        _wait_until_ready(driver, max(1.0, delay or 1.0))
    except Exception:
        pass


def click(
    selector: str,
    session_id: str = "default",
    wait_seconds: float = 0.5,
) -> dict[str, Any]:
    """Click a rendered element by CSS selector."""
    session = _get_session(session_id)
    with session.lock:
        element = WebDriverWait(session.driver, 10).until(
            conditions.element_to_be_clickable((By.CSS_SELECTOR, selector))
        )
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
    value = str(key).strip()
    if len(value) == 1 and value.isprintable():
        return value
    alias = value.upper().replace("-", "_").replace(" ", "_")
    if alias in _KEY_ALIASES:
        return _KEY_ALIASES[alias]
    raise ValueError(
        f"Unsupported key '{key}'; use a printable character or a named keyboard key"
    )


def press_keys(
    keys: list[str],
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    hold_seconds: float = 0.05,
    repeat: int = 1,
    wait_seconds: float = 0.2,
    action: str = "tap",
    _advance_frame: bool = True,
) -> dict[str, Any]:
    """Tap, hold, or release a key combination on a page, canvas, or iframe game."""
    if not keys or len(keys) > 8:
        raise ValueError("Provide 1-8 keys")
    selected_action = action.strip().lower()
    if selected_action not in {"tap", "hold", "release"}:
        raise ValueError("action must be 'tap', 'hold', or 'release'")
    normalized = [_normalize_game_key(key) for key in keys]
    key_ids = [str(key).strip().upper() for key in keys]
    hold = max(0.0, min(float(hold_seconds), 5.0))
    repetitions = max(1, min(int(repeat), 50))
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        driver.switch_to.default_content()
        try:
            if frame_selector:
                frame = WebDriverWait(driver, 10).until(
                    conditions.frame_to_be_available_and_switch_to_it(
                        (By.CSS_SELECTOR, frame_selector)
                    )
                )
                del frame
            if target_selector:
                target = WebDriverWait(driver, 10).until(
                    conditions.visibility_of_element_located(
                        (By.CSS_SELECTOR, target_selector)
                    )
                )
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                    target,
                )
                target.click()
            else:
                driver.execute_script("window.focus();")
            runs = repetitions if selected_action == "tap" else 1
            for _ in range(runs):
                events: list[dict[str, Any]] = []
                if selected_action in {"tap", "hold"}:
                    for key_id, key in zip(key_ids, normalized):
                        if selected_action == "tap" or key_id not in session.held_keys:
                            events.append({"type": "down", "key": key})
                    if selected_action == "tap" and hold:
                        events.append({"type": "pause", "seconds": hold})
                if selected_action in {"tap", "release"}:
                    for key_id, key in reversed(list(zip(key_ids, normalized))):
                        if selected_action == "tap" or key_id in session.held_keys:
                            events.append({"type": "up", "key": key})
                _perform_key_events(driver, events)
            if selected_action == "hold":
                session.held_keys.update(dict(zip(key_ids, normalized)))
            elif selected_action == "release":
                for key_id in key_ids:
                    session.held_keys.pop(key_id, None)
        finally:
            driver.switch_to.default_content()
        if _advance_frame:
            _auto_advance_render_after_input(session)
        _wait_after_action(driver, wait_seconds)
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "action": selected_action,
            "keys": [str(key) for key in keys],
            "repeat": runs,
            "hold_seconds": hold,
            "held_keys": sorted(session.held_keys),
            "target_selector": target_selector,
            "frame_selector": frame_selector,
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
    wait_seconds: float = 0.2,
    coordinate_mode: str = "absolute",
    _advance_frame: bool = True,
) -> dict[str, Any]:
    """Dispatch absolute/relative pointer movement, taps, drag, or held buttons."""
    requested_action = action.strip().lower()
    allowed_actions = {
        "click",
        "double_click",
        "move",
        "hover",
        "drag",
        "press",
        "release",
    }
    if requested_action not in allowed_actions:
        raise ValueError(
            "action must be 'click', 'double_click', 'move', 'hover', 'drag', "
            "'press', or 'release'"
        )
    selected_action = "move" if requested_action == "hover" else requested_action
    selected_coordinate_mode = coordinate_mode.strip().lower()
    if selected_coordinate_mode not in {"absolute", "delta"}:
        raise ValueError("coordinate_mode must be 'absolute' or 'delta'")
    selected_button = button.strip().lower()
    button_bits = {"left": 1, "right": 2, "middle": 4}
    if selected_button not in button_bits:
        raise ValueError("button must be 'left', 'right', or 'middle'")
    duration = max(0.0, min(float(duration_seconds), 5.0))
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        driver.switch_to.default_content()
        offset_x = 0.0
        offset_y = 0.0
        try:
            if frame_selector:
                frame = WebDriverWait(driver, 10).until(
                    conditions.visibility_of_element_located(
                        (By.CSS_SELECTOR, frame_selector)
                    )
                )
                rect = driver.execute_script(
                    "const r=arguments[0].getBoundingClientRect();"
                    "return {x:r.x,y:r.y,width:r.width,height:r.height};",
                    frame,
                )
                offset_x = float(rect["x"])
                offset_y = float(rect["y"])
                driver.switch_to.frame(frame)
            viewport = driver.execute_script(
                "return {width: window.innerWidth, height: window.innerHeight};"
            )
            current_local_x = session.pointer_x - offset_x
            current_local_y = session.pointer_y - offset_y
            if selected_coordinate_mode == "delta":
                local_x = current_local_x + float(x)
                local_y = current_local_y + float(y)
            else:
                local_x = float(x)
                local_y = float(y)
            if not 0 <= local_x < float(viewport["width"]) or not 0 <= local_y < float(
                viewport["height"]
            ):
                raise ValueError("Pointer coordinates must be inside the selected viewport")
            if selected_coordinate_mode == "delta":
                local_end_x = local_x + (float(end_x) if end_x is not None else 0.0)
                local_end_y = local_y + (float(end_y) if end_y is not None else 0.0)
            else:
                local_end_x = float(end_x) if end_x is not None else local_x
                local_end_y = float(end_y) if end_y is not None else local_y
            if selected_action == "drag" and (end_x is None or end_y is None):
                raise ValueError("drag requires end_x and end_y")
            if not 0 <= local_end_x < float(viewport["width"]) or not 0 <= local_end_y < float(
                viewport["height"]
            ):
                raise ValueError("Pointer end coordinates must be inside the selected viewport")
        finally:
            driver.switch_to.default_content()

        start_x = local_x + offset_x
        start_y = local_y + offset_y
        finish_x = local_end_x + offset_x
        finish_y = local_end_y + offset_y

        def dispatch(event_type: str, px: float, py: float, **extra: Any) -> None:
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": px,
                    "y": py,
                    "button": extra.pop("button", "none"),
                    **extra,
                },
            )

        held_mask = sum(button_bits[item] for item in session.held_buttons)
        dispatch("mouseMoved", start_x, start_y, buttons=held_mask)
        if selected_action == "move":
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
        if _advance_frame:
            _auto_advance_render_after_input(session)
        _wait_after_action(driver, wait_seconds)
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "action": requested_action,
            "button": selected_button,
            "x": local_x,
            "y": local_y,
            "coordinate_mode": selected_coordinate_mode,
            "delta_x": float(x) if selected_coordinate_mode == "delta" else None,
            "delta_y": float(y) if selected_coordinate_mode == "delta" else None,
            "end_x": local_end_x if selected_action == "drag" else None,
            "end_y": local_end_y if selected_action == "drag" else None,
            "frame_selector": frame_selector,
            "held_buttons": sorted(session.held_buttons),
        }


def input_batch(
    key_actions: list[dict[str, str]] | None = None,
    pointer_actions: list[dict[str, Any]] | None = None,
    session_id: str = "default",
    target_selector: str | None = None,
    frame_selector: str | None = None,
    wait_seconds: float = 0.2,
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
        for index, item in enumerate(normalized_keys):
            press_keys(
                [item["key"]],
                session_id,
                target_selector if index == 0 else None,
                frame_selector,
                0.0,
                1,
                0.0,
                item["action"],
                False,
            )
        for item in normalized_pointers:
            result = pointer_action(
                str(item["action"]),
                float(item["x"]),
                float(item["y"]),
                session_id,
                float(item["end_x"]) if item.get("end_x") is not None else None,
                float(item["end_y"]) if item.get("end_y") is not None else None,
                str(item.get("button", "left")),
                float(item.get("duration_seconds", 0.0)),
                frame_selector,
                0.0,
                str(item.get("coordinate_mode", "absolute")),
                False,
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
        _wait_after_action(session.driver, wait_seconds)
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "key_actions": normalized_keys,
            "pointer_actions": pointer_results,
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


def game_probe(
    session_id: str = "default",
    frame_selector: str | None = None,
    sample_seconds: float = 1.0,
    include_console: bool = True,
) -> dict[str, Any]:
    """Inspect canvas/WebGL readiness, animation FPS, frames, focus, and console issues."""
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
                console_messages = [
                    {
                        "level": item.get("level"),
                        "message": str(item.get("message", ""))[:2000],
                        "timestamp": item.get("timestamp"),
                    }
                    for item in driver.get_log("browser")[-100:]
                    if item.get("level") in {"WARNING", "SEVERE"}
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
const state = {
    mode: 'normal',
    targetFps: null,
    interval: 1000 / 60,
    lastFrame: performance.now(),
    nextId: -1,
    pending: new Map(),
    timer: null,
    nativeRequest: window.requestAnimationFrame.bind(window),
    nativeCancel: window.cancelAnimationFrame.bind(window)
};
state.flush = timestamp => {
    state.lastFrame = timestamp;
    const batch = Array.from(state.pending.entries());
    state.pending.clear();
    for (const [, callback] of batch) {
      try { callback(timestamp); } catch (error) { setTimeout(() => { throw error; }, 0); }
    }
    state.schedule();
    return batch.length;
};
state.schedule = () => {
    if (state.mode !== 'throttled' || state.timer !== null || !state.pending.size) return;
    const delay = Math.max(0, state.interval - (performance.now() - state.lastFrame));
    state.timer = setTimeout(() => {
      state.timer = null;
      state.flush(performance.now());
    }, delay);
};
state.request = callback => {
    if (state.mode === 'normal') return state.nativeRequest(callback);
    const id = state.nextId--;
    state.pending.set(id, callback);
    state.schedule();
    return id;
};
state.cancel = id => {
    if (!state.pending.delete(id)) state.nativeCancel(id);
};
state.setMode = (mode, targetFps) => {
    if (state.timer !== null) clearTimeout(state.timer);
    state.timer = null;
    state.mode = mode;
    state.targetFps = mode === 'throttled' ? targetFps : null;
    state.interval = 1000 / targetFps;
    if (mode === 'normal') {
      const callbacks = Array.from(state.pending.values());
      state.pending.clear();
      for (const callback of callbacks) state.nativeRequest(callback);
    } else {
      state.schedule();
    }
};
window[stateKey] = state;
window.requestAnimationFrame = state.request;
window.cancelAnimationFrame = state.cancel;
})();
"""


_RENDER_CONTROL_SCRIPT = r"""
const mode = arguments[0];
const targetFps = arguments[1];
const state = window.__webSearchNeoRenderControl;
if (!state) {
  return {error: 'Render bootstrap is unavailable in this document'};
}
state.setMode(mode, targetFps);
return {mode: state.mode, target_fps: state.targetFps, pending_callbacks: state.pending.size};
"""


def _register_render_bootstrap(session: BrowserSession) -> None:
    """Install the frame gate before any script in future documents and frames."""
    if session.render_bootstrap_registered:
        return
    session.driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": _RENDER_BOOTSTRAP_SCRIPT},
    )
    session.render_bootstrap_registered = True


_RENDER_STEP_SCRIPT = r"""
const count = arguments[0];
const done = arguments[arguments.length - 1];
const state = window.__webSearchNeoRenderControl;
if (!state || state.mode !== 'step') {
  done({success: false, error: 'Render control is not in step mode'});
  return;
}
let remaining = count;
let callbacks = 0;
const run = () => {
  callbacks += state.flush(performance.now());
  remaining -= 1;
  if (remaining > 0) queueMicrotask(run);
  else done({success: true, frames: count, callbacks, pending_callbacks: state.pending.size});
};
run();
"""


def _select_frame(driver: webdriver.Chrome, frame_selector: str | None) -> None:
    driver.switch_to.default_content()
    if frame_selector:
        WebDriverWait(driver, 10).until(
            conditions.frame_to_be_available_and_switch_to_it(
                (By.CSS_SELECTOR, frame_selector)
            )
        )


def set_render_control(
    mode: str,
    session_id: str = "default",
    target_fps: float = 10.0,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Set normal, continuously throttled, or manual/input-driven frame stepping."""
    selected_mode = mode.strip().lower()
    if selected_mode not in {"normal", "throttled", "step"}:
        raise ValueError("mode must be 'normal', 'throttled', or 'step'")
    fps = max(1.0, min(float(target_fps), 60.0))
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
            _select_frame(driver, session.render_frame_selector)
            try:
                driver.execute_script(_RENDER_CONTROL_SCRIPT, "normal", 60.0)
            finally:
                driver.switch_to.default_content()
        _select_frame(driver, selected_frame)
        try:
            driver.execute_script(_RENDER_BOOTSTRAP_SCRIPT)
            state = driver.execute_script(_RENDER_CONTROL_SCRIPT, selected_mode, fps)
        finally:
            driver.switch_to.default_content()
        if state.get("error"):
            raise RuntimeError(state["error"])
        session.render_mode = selected_mode
        session.render_target_fps = fps if selected_mode == "throttled" else None
        session.render_frame_selector = selected_frame if selected_mode != "normal" else None
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "mode": selected_mode,
            "target_fps": session.render_target_fps,
            "frame_selector": session.render_frame_selector,
            "input_advances_frame": selected_mode == "step",
            "engine": "requestAnimationFrame gate",
            **state,
        }


def _advance_render_frames(
    driver: webdriver.Chrome, frame_selector: str | None, frames: int
) -> dict[str, Any]:
    _select_frame(driver, frame_selector)
    try:
        result = driver.execute_async_script(_RENDER_STEP_SCRIPT, frames)
    finally:
        driver.switch_to.default_content()
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "Render step failed")
    return result


def render_step(
    frames: int = 1,
    session_id: str = "default",
) -> dict[str, Any]:
    """Advance an active step-mode canvas/WebGL loop by a bounded frame count."""
    frame_count = max(1, min(int(frames), 120))
    session = _get_session(session_id)
    with session.lock:
        if session.render_mode != "step":
            raise ValueError("Render control must be in step mode")
        result = _advance_render_frames(
            session.driver, session.render_frame_selector, frame_count
        )
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "mode": "step",
            "frame_selector": session.render_frame_selector,
            **result,
        }


def _auto_advance_render_after_input(session: BrowserSession) -> None:
    if session.render_mode == "step":
        _advance_render_frames(session.driver, session.render_frame_selector, 1)


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
        form = session.driver.find_element(By.CSS_SELECTOR, form_selector)
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
        bridge_status = get_chrome_bridge().status(0.0)
        return {
            "available": bool(bridge_status["connected"] or _browser_available),
            "availability_error": _browser_error,
            "session_open": False,
            "session_id": session_id,
            "active_sessions": active_ids,
            "engine": "Chrome companion extension or Selenium Manager",
            "current_chrome": bridge_status,
        }
    with session.lock:
        bridge_status = get_chrome_bridge().status(0.0)
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
        element = session.driver.find_element(By.CSS_SELECTOR, selector)
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


def close_session(session_id: str = "default") -> dict[str, Any]:
    """Close one browser session and release Chrome resources."""
    session_id = _validate_session_id(session_id)
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
        remaining = sorted(_sessions)
    closed = session is not None
    if session is not None:
        with session.lock:
            try:
                _reset_session_runtime_state(session)
                if session.owns_browser:
                    session.driver.quit()
                else:
                    session.driver.service.stop()
            except Exception:
                pass
    return {"session_id": session_id, "closed": closed, "active_sessions": remaining}


def close_all_sessions() -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        with session.lock:
            try:
                _reset_session_runtime_state(session)
                if session.owns_browser:
                    session.driver.quit()
                else:
                    session.driver.service.stop()
            except Exception:
                pass


def start_current_chrome_bridge() -> dict[str, Any]:
    """Start the loopback listener early so the extension is ready before the first action."""
    bridge = get_chrome_bridge()
    bridge.start()
    return bridge.status(0.0)


atexit.register(close_all_sessions)
