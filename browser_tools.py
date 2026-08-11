"""Stateful Selenium browser sessions used by MCP browser tools."""

from __future__ import annotations

import atexit
import base64
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as conditions
from selenium.webdriver.support.ui import Select, WebDriverWait

from web_client import validate_http_url


MAX_SESSIONS = 4
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass
class BrowserSession:
    driver: webdriver.Chrome
    headless: bool
    lock: threading.RLock = field(default_factory=threading.RLock)
    last_used: float = field(default_factory=time.monotonic)


_sessions: dict[str, BrowserSession] = {}
_sessions_lock = threading.RLock()
_sessions_condition = threading.Condition(_sessions_lock)
_pending_sessions: set[str] = set()
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


def create_driver(
    width: int = 1440, height: int = 900, headless: bool = True
) -> webdriver.Chrome:
    """Create Chrome through Selenium Manager, optionally visible for human handoff."""
    global _browser_available, _browser_error
    width, height = _bounded_size(width, height)
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
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
    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:
        _browser_available = False
        _browser_error = f"{type(exc).__name__}: {exc}"
        raise
    if headless and not browser_user_agent:
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
    session_id: str, width: int, height: int, headless: bool
) -> BrowserSession:
    with _sessions_condition:
        while session_id in _pending_sessions:
            _sessions_condition.wait(timeout=30)
        existing = _sessions.get(session_id)
        if existing is not None:
            if existing.headless != headless:
                raise ValueError(
                    "Session already exists with a different headless mode; close it first"
                )
            return existing
        if len(_sessions) + len(_pending_sessions) >= MAX_SESSIONS:
            raise RuntimeError(
                f"Maximum of {MAX_SESSIONS} browser sessions reached; close one first"
            )
        _pending_sessions.add(session_id)
    try:
        driver = create_driver(width, height, headless)
        session = BrowserSession(driver=driver, headless=headless)
    finally:
        with _sessions_condition:
            _pending_sessions.discard(session_id)
            _sessions_condition.notify_all()
    with _sessions_lock:
        _sessions[session_id] = session
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


def _set_viewport(driver: webdriver.Chrome, width: int, height: int) -> None:
    """Set exact CSS viewport dimensions instead of approximate outer-window size."""
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
        "human_verification": ("verify you are human", "checking your browser"),
        "access_challenge": ("unusual traffic", "access denied", "are you a robot"),
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
    headless: bool = True,
) -> dict[str, Any]:
    """Open a URL in a reusable rendered browser session."""
    normalized = validate_http_url(url)
    session_id = _validate_session_id(session_id)
    width, height = _bounded_size(width, height)
    session = _create_session(session_id, width, height, headless)
    try:
        with session.lock:
            _set_viewport(session.driver, width, height)
            session.driver.get(normalized)
            _wait_until_ready(session.driver, timeout_seconds)
            return _page_summary(session.driver, session_id)
    except WebDriverException:
        close_session(session_id)
        raise


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
        return {
            "available": _browser_available,
            "availability_error": _browser_error,
            "session_open": False,
            "session_id": session_id,
            "active_sessions": active_ids,
            "engine": "Chrome via Selenium Manager",
        }
    with session.lock:
        return {
            "available": True,
            "availability_error": None,
            "session_open": True,
            "active_sessions": active_ids,
            "engine": "Chrome via Selenium Manager",
            **_page_summary(session.driver, session_id),
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
                session.driver.quit()
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
                session.driver.quit()
            except Exception:
                pass


atexit.register(close_all_sessions)
