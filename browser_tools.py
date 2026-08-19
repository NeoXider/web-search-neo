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
from typing import Any, Callable
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchFrameException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as conditions
from selenium.webdriver.support.ui import Select, WebDriverWait

from chrome_bridge import (
    CHROME_EXTENSION_ID,
    DEFAULT_TAB_GROUP,
    ChromeBridgeDriver,
    ChromeBridgeError,
    ChromeBridgeUnavailable,
    get_chrome_bridge,
    list_current_chrome_tabs,
)
from chrome_bootstrap import (
    EXTENSION_DIR,
    expected_extension_version,
    setup_current_chrome,
)
import captcha
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
class ConsoleCursor:
    """One reader's place in the console sources.

    ``seq`` counts inside ``doc`` and nowhere else: every document numbers its own
    entries from one, so a sequence number kept across a navigation would hide the
    new page's first entries instead of skipping the old page's. ``log_index`` is
    a position in the session's browser-log buffer, which belongs to the session
    rather than to any document and therefore survives navigation untouched.
    """

    seq: int = 0
    doc: str = ""
    log_index: int = 0


@dataclass
class BrowserSession:
    driver: Any
    headless: bool
    profile_mode: str = "temporary"
    profile_id: str | None = None
    debugger_address: str | None = None
    current_tab_id: int | None = None
    tab_group: str | None = None
    # Which run of the user's Chrome the tab id above belongs to. Tab ids restart
    # with the browser, so without this a session that outlived a Chrome restart
    # would keep driving whatever tab inherited its number.
    browser_run: str | None = None
    owns_browser: bool = True
    held_keys: dict[str, str] = field(default_factory=dict)
    held_buttons: set[str] = field(default_factory=set)
    # Touch id -> the page-space point it is holding, so one finger can be lifted
    # without lifting the others and so a forgotten finger can still be found.
    held_touches: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Where this session last put the mouse, in page pixels. Chrome measures
    # movementX/movementY against exactly this point, and a session that has
    # dispatched nothing starts where Chrome's own pointer starts, at (0, 0).
    pointer_x: float = 0.0
    pointer_y: float = 0.0
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
    console: ConsoleCursor = field(default_factory=ConsoleCursor)
    browser_log: list[dict[str, Any]] = field(default_factory=list)
    # game_probe reads the same two sources as the console topic, but reports
    # what is new to *it*, so it carries its own place in both of them.
    probe_console: ConsoleCursor = field(default_factory=ConsoleCursor)
    probe_console_seen: list[dict[str, Any]] = field(default_factory=list)
    network_pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    network_rows: list[dict[str, Any]] = field(default_factory=list)
    # Capture runs from the moment the tab opens, so a long session outlives its
    # own buffer. Both backends bound what they keep; this counts what the
    # Selenium one threw away, as the extension counts evictions for the other.
    network_dropped: int = 0
    # Identifiers of scripts registered with Page.addScriptToEvaluateOnNewDocument
    # for this session, so inject_script can list and forget them by hand: CDP has
    # no matching remove, and the ids are the only handle a caller ever gets back.
    injected_scripts: list[str] = field(default_factory=list)
    # The extra HTTP headers Chrome adds to every request, echoed back so a caller
    # can see what is in force, and the injected-script id that enables stealth,
    # kept so it can be turned off without the caller tracking it.
    extra_headers: dict[str, str] = field(default_factory=dict)
    stealth_identifier: str | None = None
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
    """Keep owned browser sessions background-only unless visibility is explicit."""
    mode = profile_mode.strip().lower()
    if mode in {"extension", "current"}:
        return False
    if mode not in {"temporary", "persistent", "attach"}:
        raise ValueError("profile_mode must be 'temporary', 'persistent', 'attach', or 'current'")
    if headless is None:
        # An attached browser already has a window mode owned by its launcher;
        # recording it as visible preserves that state without trying to change
        # it. Browsers this server creates are headless by default so merely
        # opening a page cannot take the user's OS focus.
        return mode != "attach"
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
    tab_group: str = DEFAULT_TAB_GROUP,
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


def _claim_tab(tab_id: int) -> dict[str, Any]:
    """Reserve a Chrome tab across every agent driving this browser.

    The guard in ``_create_session`` only sees the sessions of this process. The
    daemon sees the other MCP clients too, which is the case that matters: two
    agents claiming the same tab each believe they own it, and the second
    debugger attach or the first close takes the other's page out from under it.

    An ``unavailable`` answer - no daemon, an older one, a link that just dropped
    - is not a refusal. A browser nobody is guarding is still perfectly usable,
    and failing closed here would turn a missing daemon into a permanently busy
    tab.
    """
    try:
        answer = dict(get_chrome_bridge().claim_tab(int(tab_id)) or {})
    except Exception as exc:
        logger.debug("Could not ask the daemon about tab %s: %s", tab_id, exc)
        return {"status": "unavailable"}
    if str(answer.get("status") or "") == "refused":
        raise RuntimeError(
            str(answer.get("reason") or "")
            or f"Chrome tab {tab_id} is already being driven by another agent."
        )
    return answer


def _release_claimed_tab(tab_id: int | None) -> None:
    """Give a tab back to whoever asks for it next."""
    if tab_id is None:
        return
    try:
        get_chrome_bridge().release_tab(int(tab_id))
    except Exception as exc:  # A link that is gone released it on our behalf.
        logger.debug("Could not release tab %s: %s", tab_id, exc)


def _abandon_driver(driver: Any, *, owns_browser: bool, owns_tab: bool) -> None:
    """Give back a browser that no session will ever own. Never raises.

    For the paths where a driver came up but the session around it did not. The
    tab is closed only if we opened it - a borrowed one goes back to the user
    exactly as it was found - but the debugger comes off either way, because a
    tab left attached wears the "is being debugged" banner until the user
    clears it themselves.
    """
    if driver is None:
        return
    if owns_tab and hasattr(driver, "close_tab"):
        try:
            driver.close_tab()
        except Exception as exc:
            logger.warning(
                "Could not close the tab of a session that never opened: %s: %s",
                type(exc).__name__,
                exc,
            )
    try:
        if owns_browser:
            driver.quit()
        else:
            driver.service.stop()
    except Exception as exc:
        logger.warning(
            "Could not let go of the browser of a session that never opened: %s: %s",
            type(exc).__name__,
            exc,
        )


def _tab_still_exists(session: BrowserSession) -> bool | None:
    """Whether this session's tab is still open; ``None`` when nobody could say.

    Only an answer counts. The companion refusing the question by name ("No tab
    with id 42") is evidence about the tab; the question never arriving is
    evidence about the link, and reading the second as the first condemns every
    session at once the moment Chrome's companion blinks - a service worker
    eviction, an extension reload, a link that dropped. That is what
    ``ChromeBridgeUnavailable`` exists to separate, because every one of those
    transport failures raises ``ChromeBridgeError`` too.
    """
    driver = session.driver
    if not getattr(driver, "is_extension_bridge", False):
        return True
    try:
        driver.bridge.request("tabs.get", {"tabId": driver.tab_id}, timeout=2.0)
    except ChromeBridgeUnavailable:
        return None
    except ChromeBridgeError:
        return False
    except Exception:
        # A timeout, a socket error: the companion never answered, so it said
        # nothing about the tab either.
        return None
    return True


def _tab_is_gone(session: BrowserSession) -> bool:
    """Whether the companion said, in so many words, that the tab is not there."""
    return _tab_still_exists(session) is False


def _drop_sessions_whose_tab_is_gone() -> None:
    """Free the session slots of tabs the user has since closed by hand.

    Only called at the session cap, because it costs one round trip per session:
    without it, four tabs closed from the tab strip leave a server that refuses
    to open a fifth and cannot say why.

    Every round trip here is made with no lock held. The probe is one trip per
    session and teardown several more, while every browser tool call goes through
    ``_get_session``, which wants ``_sessions_lock``: sweeping four wedged
    sessions inside it stalls the whole server for as long as the companion takes
    to answer. A session whose own lock is not free is skipped rather than waited
    for - a thread is mid-action on it, so it is neither idle nor safe to pull
    the debugger out from under.
    """
    with _sessions_lock:
        candidates = [
            (session_id, session)
            for session_id, session in _sessions.items()
            if session.profile_mode == "current"
        ]
    for session_id, session in candidates:
        if not session.lock.acquire(blocking=False):
            continue
        try:
            if _browser_run_changed(session) is not None:
                # Its browser is gone, so its slot is free - but this is a
                # teardown path too, and it may no more send `debugger.detach`
                # and a claim release at that tab id than the others: the id
                # names somebody else's tab now.
                _discard_stale_session(session_id, session)
                continue
            if not _tab_is_gone(session):
                continue
            with _sessions_lock:
                if _sessions.get(session_id) is not session:
                    continue
                del _sessions[session_id]
            logger.info("Dropped session '%s': its tab is no longer open", session_id)
            # No page is left to release held input on and no tab to close, so the
            # full teardown would only collect failures. Give up the debugger
            # attachment and nothing else - Chrome dropped it with the tab anyway.
            try:
                session.driver.quit()
            except Exception:
                pass
            _release_claimed_tab(session.current_tab_id)
        finally:
            session.lock.release()


def _create_session(
    session_id: str,
    width: int,
    height: int,
    headless: bool | None,
    profile_mode: str = "auto",
    profile_id: str | None = None,
    debugger_address: str | None = None,
    current_tab_id: int | None = None,
    tab_group: str = DEFAULT_TAB_GROUP,
) -> BrowserSession:
    profile_mode = _resolve_profile_mode(profile_mode, headless)
    mode, selected_profile, address, browser_key = _profile_configuration(
        session_id, profile_mode, profile_id, debugger_address
    )
    effective_headless = _resolve_headless(mode, headless)
    # The sweep at the cap asks Chrome about every current-mode session, and it
    # has to do that with the lock down: a wedged companion made an unrelated
    # `_get_session` on another thread wait the whole sweep out, and every
    # browser tool call is a `_get_session`. So the loop drops out of the lock to
    # sweep and comes back round, at most once.
    swept = False
    while True:
        with _sessions_condition:
            while session_id in _pending_sessions:
                _sessions_condition.wait(timeout=30)
            existing = _sessions.get(session_id)
            if existing is not None and _browser_run_changed(existing) is not None:
                # Its browser is gone, and reopening under the same name is exactly
                # the right response to that - so drop it here instead of refusing
                # the call and making the caller close a session that no longer is one.
                del _sessions[session_id]
                existing = None
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
            at_cap = len(_sessions) + len(_pending_sessions) >= MAX_SESSIONS
            if not (at_cap and not swept):
                if at_cap:
                    oldest = min(
                        _sessions.values(), key=lambda item: item.last_used, default=None
                    )
                    stalest = next(
                        (name for name, item in _sessions.items() if item is oldest), None
                    )
                    raise RuntimeError(
                        f"Maximum of {MAX_SESSIONS} browser sessions reached; close one first. "
                        f"Open: {sorted(_sessions)}."
                        + (f" Least recently used: '{stalest}'." if stalest else "")
                    )
                selected_current_tab_id = (
                    int(current_tab_id)
                    if mode == "current" and current_tab_id is not None
                    else None
                )
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
                break
        swept = True
        _drop_sessions_whose_tab_is_gone()
    session: BrowserSession | None = None
    driver: Any = None
    claim: dict[str, Any] = {}
    claimed_tab: int | None = None
    try:
        if selected_current_tab_id is not None:
            # Before the debugger attaches, not after: a refusal has to cost the
            # other agent's tab nothing.
            claim = _claim_tab(selected_current_tab_id)
            claimed_tab = selected_current_tab_id
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
        if mode == "current" and claimed_tab is None:
            opened = getattr(driver, "tab_id", None)
            if opened is not None:
                # Ours by construction, so this cannot be refused - it is filed
                # so that another agent's attach_tab is.
                claim = _claim_tab(int(opened))
                claimed_tab = int(opened)
        session = BrowserSession(
            driver=driver,
            headless=effective_headless,
            profile_mode=mode,
            profile_id=selected_profile,
            debugger_address=address,
            current_tab_id=getattr(driver, "tab_id", None),
            tab_group=(getattr(driver, "actual_tab_group", None) if mode == "current" else None),
            browser_run=(
                claim.get("browser_run") or _current_browser_run()
                if mode == "current"
                else None
            ),
            owns_browser=mode != "attach",
            # A tab we created is ours to clean up; a claimed one belongs to the user.
            owns_tab=mode == "current" and current_tab_id is None,
        )
    finally:
        if session is None:
            # The claim outlives this call only when a session came out of it.
            _release_claimed_tab(claimed_tab)
            # And so does the browser. A claim refused on a tab we had just
            # opened used to be raised straight past this point, leaving that tab
            # open in the user's Chrome with the debugger attached to it, its
            # banner up and no session that could ever close it.
            _abandon_driver(
                driver,
                owns_browser=mode != "attach",
                owns_tab=mode == "current" and current_tab_id is None,
            )
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


def _current_browser_run() -> str | None:
    """The companion's id for the browser run in front of us, when it is knowable.

    ``None`` covers three unrelated situations - no companion connected, one older
    than 1.3.2, and a bridge that never started - and not one of them is evidence
    that a session is stale, so all three read alike here: nothing to compare.
    """
    try:
        bridge = get_chrome_bridge()
        if not bridge.connected:
            return None
        return bridge.browser_run
    except Exception:
        return None


def _browser_run_changed(session: BrowserSession) -> str | None:
    """The current run id, but only when it proves the session's tab is gone.

    A session whose own run is unknown - opened against a companion older than
    1.3.2, or while the link happened to be down - adopts the first run that
    becomes knowable instead of staying unknowable for good. Both
    ``bridge_daemon._adopt_browser_run`` and ``ChromeBridge._apply_state``
    back-fill the same way and for the same reason: the run in front of us is the
    one this session has been driving, because a restart in between would have
    taken its debugger attachment with it. Left unadopted, the session goes on
    driving "tab 42" across every later Chrome restart, which is the one thing
    the run id exists to prevent.
    """
    if session.profile_mode != "current":
        return None
    current = _current_browser_run()
    if current is None:
        return None
    if not session.browser_run:
        session.browser_run = current
        return None
    if current == session.browser_run:
        return None
    return current


def _discard_stale_session(session_id: str, session: BrowserSession) -> None:
    """Forget a session whose browser is gone, without touching the new one.

    Nothing is sent to Chrome on the way out. The tab id this session holds now
    names a tab in a browser run that never heard of it - quite possibly one of
    the user's own - so the usual teardown, which releases held keys and closes
    the tab, would act on a stranger's tab instead of ours. The daemon's claim is
    not released either: it drops the whole registry when the run changes, and a
    release aimed at the old id could only hit a claim made since, in the new run.
    """
    with _sessions_lock:
        if _sessions.get(session_id) is session:
            del _sessions[session_id]


def _get_session(session_id: str) -> BrowserSession:
    _validate_session_id(session_id)
    with _sessions_lock:
        session = _sessions.get(session_id)
        open_sessions = sorted(_sessions)
    if session is not None and _browser_run_changed(session) is not None:
        _discard_stale_session(session_id, session)
        raise ValueError(
            f"Browser session '{session_id}' was opened in a Chrome that is no "
            "longer running - it was restarted, or the companion updated itself - "
            "so the tab it held no longer exists and its id now names a different "
            "tab. The session has been dropped rather than driving that tab. Open "
            f'the page again: web_action [{{"action":"open","url":...,'
            f'"session_id":"{session_id}"}}].'
        )
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
#
# The probe therefore gathers three things and leaves the verdict to
# _classify_challenge: which provider widgets are on the page, wherever they are -
# shadow roots and same-origin frames included, because half of them live one
# document down - which provider SDKs the markup loads, and whether any of it is
# lying over the middle of the viewport rather than sitting inside a form.
_CHALLENGE_WIDGET_SCRIPT = """
const WIDGETS = [
  'iframe[src*="recaptcha/api2"]', 'iframe[src*="recaptcha/enterprise"]',
  'iframe[src*="hcaptcha.com"]', 'iframe[src*="challenges.cloudflare.com"]',
  'iframe[src*="captcha-api.yandex"]', 'iframe[src*="captcha-delivery.com"]',
  'iframe[src*="captcha.awswaf.com"]', 'iframe[title*="captcha" i]',
  'div.g-recaptcha', 'div.h-captcha', 'div.cf-turnstile', 'div#cf-challenge-running',
  'form#challenge-form', '#px-captcha', '.smart-captcha', '.datadome-captcha',
  'awswaf-captcha', '[data-sitekey]'
];
// A script tag has no box of its own, so these are counted by presence. Both
// hosts only serve the SDK that asks a human to solve something; the tags that
// merely score a request quietly are deliberately not here.
const MARKERS = [
  'script[src*="captcha-sdk.awswaf.com"]', 'script[src*="captcha.awswaf.com"]'
];
const found = [];
const markers = [];
// The walk used to stop after 8000 nodes, which a marketplace listing page eats
// before the first shadow root is entered - and nothing said it had stopped, so
// "no captcha here" and "gave up looking" read the same. Walking 60000 elements
// costs 10ms, and the pages most likely to gate are the ones with that many.
const NODE_BUDGET = 50000;
const DEPTH_LIMIT = 8;
// A widget over the middle of the viewport is only in the way when the layer it
// sits in seals off enough of the page that there is nothing else to read.
const BLOCKING_COVER = 0.5;
const budget = {nodes: 0, truncated: false};
let blocking = false;

function isVisible(element) {
  const rect = element.getBoundingClientRect();
  if (rect.width < 20 || rect.height < 20) return false;
  const view = (element.ownerDocument && element.ownerDocument.defaultView) || window;
  const style = view.getComputedStyle(element);
  return style.visibility !== 'hidden' && style.opacity !== '0';
}

// data-sitekey alone says nothing: chat, payment and analytics widgets mint one
// too, and treating every one of them as a gate stopped the agent on pages that
// were never blocking it.
function isCaptchaSitekey(element) {
  const name = String(element.getAttribute('class') || '') + ' ' + String(element.id || '');
  if (/captcha|turnstile|challenge/i.test(name)) return true;
  return !!element.querySelector('iframe[src*="captcha"], iframe[src*="turnstile"]');
}

function matchedSelector(element, selectors) {
  for (const selector of selectors) {
    try { if (element.matches(selector)) return selector; } catch (error) { continue; }
  }
  return null;
}

function viewOf(node) {
  const doc = node.ownerDocument;
  return (doc && doc.defaultView) || null;
}

function coverOfRect(rect, view) {
  const area = view.innerWidth * view.innerHeight;
  if (area <= 0) return 0;
  const width = Math.min(rect.right, view.innerWidth) - Math.max(rect.left, 0);
  const height = Math.min(rect.bottom, view.innerHeight) - Math.max(rect.top, 0);
  return width <= 0 || height <= 0 ? 0 : (width * height) / area;
}

function overPoint(rect, x, y) {
  return rect.left <= x && rect.right >= x && rect.top <= y && rect.bottom >= y;
}

// Zero unless the widget is over the centre of its own viewport; otherwise the
// share of that viewport the widget - or the positioned layer it is painted in -
// covers. It is that layer, the scrim of a modal, that actually stops the page
// being used; the widget itself is far too small to.
//
// Boxes, not a hit test: elementFromPoint retargets shadow content to its host,
// so a widget inside a web component could never be the node at the centre, and
// the veil a provider paints over its own widget while it verifies wins that hit
// test while blocking the page just as thoroughly.
function coverOverCenter(element) {
  const view = viewOf(element);
  if (!view) return 0;
  const x = view.innerWidth / 2;
  const y = view.innerHeight / 2;
  const rect = element.getBoundingClientRect();
  if (!overPoint(rect, x, y)) return 0;
  let cover = coverOfRect(rect, view);
  let node = element;
  for (let step = 0; step < 40 && node; step += 1) {
    // parentNode.host is the step out of a shadow tree, which parentElement -
    // and every hit test - stops dead at.
    const parent = node.parentElement || (node.parentNode && node.parentNode.host) || null;
    if (!parent) break;
    node = parent;
    let position = '';
    let ancestorView = null;
    try {
      ancestorView = viewOf(node);
      position = ancestorView.getComputedStyle(node).position;
    } catch (error) { position = ''; }
    if (!ancestorView) break;
    if (position !== 'fixed' && position !== 'absolute' && position !== 'sticky') continue;
    const box = node.getBoundingClientRect();
    if (overPoint(box, x, y)) cover = Math.max(cover, coverOfRect(box, ancestorView));
  }
  return cover;
}

function scan(root, where, atCenter, depth, outerCover) {
  if (found.length >= 3) return;
  if (depth > DEPTH_LIMIT || budget.nodes > NODE_BUDGET) { budget.truncated = true; return; }
  const doc = root.ownerDocument || root;
  let matches = [];
  try { matches = Array.from(root.querySelectorAll(WIDGETS.join(','))); } catch (error) { matches = []; }
  for (const element of matches) {
    const selector = matchedSelector(element, WIDGETS);
    if (!selector) continue;
    if (selector === '[data-sitekey]' && !isCaptchaSitekey(element)) continue;
    if (!isVisible(element)) continue;
    found.push(selector + where);
    const cover = atCenter ? coverOverCenter(element) : 0;
    if (cover > 0 && Math.max(cover, outerCover) >= BLOCKING_COVER) blocking = true;
    if (found.length >= 3) break;
  }
  try {
    for (const element of root.querySelectorAll(MARKERS.join(','))) {
      const selector = matchedSelector(element, MARKERS);
      if (selector && markers.indexOf(selector + where) < 0) markers.push(selector + where);
    }
  } catch (error) { /* a root that cannot be queried has nothing to add */ }
  if (found.length >= 3) return;
  // Shadow roots and same-origin frames are where the rest of the challenges
  // live: a top-level querySelector never looks inside either one. A TreeWalker
  // stops the moment the budget runs out, where querySelectorAll('*') would have
  // built the whole element list of a huge page before anyone could check.
  let walker = null;
  try { walker = doc.createTreeWalker(root, NodeFilter.SHOW_ELEMENT); } catch (error) { walker = null; }
  while (walker) {
    const element = walker.nextNode();
    if (!element || found.length >= 3) return;
    if (++budget.nodes > NODE_BUDGET) { budget.truncated = true; return; }
    if (element.shadowRoot) {
      // A shadow tree is painted in its host's layer, so it stands over the page
      // exactly as far as the host does.
      scan(element.shadowRoot, ' (in shadow DOM)', atCenter, depth + 1, outerCover);
    } else if (element.tagName === 'IFRAME') {
      let inner = null;
      try { inner = element.contentDocument; } catch (error) { inner = null; }
      if (!inner) continue;
      const frameCover = atCenter ? coverOverCenter(element) : 0;
      scan(inner, ' (in a frame)', frameCover > 0, depth + 1, Math.max(outerCover, frameCover));
    }
  }
}

scan(document, '', true, 0, 0);
const heading = (document.title || '') + ' ' +
  Array.from(document.querySelectorAll('h1, h2')).slice(0, 3)
    .map(node => node.innerText || '').join(' ');
const body = (document.body && document.body.innerText) || '';
return {
  widgets: found,
  markers: markers,
  blocking: blocking,
  truncated: budget.truncated,
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
        "you have been blocked",
        "request blocked",
        "attention required",
        "enable js and disable any ad blocker",
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


def _interstitial_phrase(heading: str, sparse_body: str) -> tuple[str, str] | None:
    """Return the ``(challenge_type, evidence)`` an interstitial's own words give."""
    for challenge_type, phrases in _CHALLENGE_HEADINGS.items():
        for phrase in phrases:
            if phrase in heading:
                return challenge_type, "page heading"
            if sparse_body and 0 <= sparse_body.find(phrase) <= _CHALLENGE_LEAD_LIMIT:
                # An interstitial opens with the phrase; an article about
                # captchas mentions it somewhere in the middle of a paragraph.
                return challenge_type, "interstitial text"
    return None


def _classify_challenge(probe: dict[str, Any]) -> dict[str, Any]:
    """Turn raw page markers into a challenge verdict.

    A captcha widget is only a *challenge* when it stands between the caller and
    the page: on an interstitial that holds nothing else, or lying over the
    middle of the viewport in a layer that seals the rest of it off. A dismissible
    sign-in box is over the centre too and blocks nothing. The same widget at the
    foot of a readable article
    guards that one form and nothing more, so it is reported in
    ``captcha_widgets`` while ``challenge_detected`` stays false - otherwise the
    agent parks for three minutes waiting for a human it does not need.

    ``captcha_scan_incomplete`` says the page was too large or too deeply nested
    for the walk to finish, so an empty ``captcha_widgets`` means "nothing found
    where I looked" rather than "there is nothing here".
    """
    widgets = [str(item) for item in (probe.get("widgets") or [])]
    markers = [str(item) for item in (probe.get("markers") or [])]
    heading = str(probe.get("heading") or "").lower()
    body_length = int(probe.get("body_length") or 0)
    interstitial_page = body_length <= _CHALLENGE_BODY_LIMIT
    sparse_body = str(probe.get("body") or "").lower() if interstitial_page else ""
    phrase = _interstitial_phrase(heading, sparse_body)
    incomplete = bool(probe.get("truncated"))
    seen = widgets + markers
    if seen:
        if bool(probe.get("blocking")) or interstitial_page or phrase:
            return {
                "challenge_detected": True,
                "challenge_type": "captcha",
                "challenge_evidence": seen[:3],
                "manual_action_required": True,
                "captcha_widgets": seen,
                "captcha_scan_incomplete": incomplete,
            }
        return {
            "challenge_detected": False,
            "challenge_type": None,
            "challenge_evidence": [],
            "manual_action_required": False,
            "captcha_widgets": seen,
            "captcha_scan_incomplete": incomplete,
        }
    if phrase:
        return {
            "challenge_detected": True,
            "challenge_type": phrase[0],
            "challenge_evidence": [phrase[1]],
            "manual_action_required": True,
            "captcha_widgets": [],
            "captcha_scan_incomplete": incomplete,
        }
    return {
        "challenge_detected": False,
        "challenge_type": None,
        "challenge_evidence": [],
        "manual_action_required": False,
        "captcha_widgets": [],
        "captcha_scan_incomplete": incomplete,
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


def _leave_claimed_tab(
    session: BrowserSession, width: int, height: int, tab_group: str
) -> int | None:
    """Move a session off a borrowed tab before it navigates; name the tab freed.

    ``attach_tab`` borrows a tab the user opened, and an ``open`` on that session
    used to navigate it - taking away the page they were reading, in a tab they
    still thought was theirs. It gets its own tab in the agent's group instead,
    opened in the background, and the borrowed one is handed back exactly as it
    was found: the debugger detaches, nothing is closed, nothing is navigated.

    Returns ``None`` when there was nothing to leave, which is the common case.
    """
    if session.profile_mode != "current" or session.owns_tab:
        return None
    released = session.current_tab_id
    borrowed = session.driver
    driver = create_driver(
        width, height, session.headless, "current", None, None, None, tab_group
    )
    claim: dict[str, Any] = {}
    try:
        if getattr(driver, "tab_id", None) is not None:
            claim = _claim_tab(int(driver.tab_id))
    except BaseException:
        # The session is still on the borrowed tab and nothing below has run, so
        # the tab just opened belongs to nobody: close it here or it stays open
        # in the user's Chrome with the debugger attached, and the caller's own
        # cleanup - which closes the session, not this - can never reach it.
        _abandon_driver(driver, owns_browser=True, owns_tab=True)
        raise
    session.driver = driver
    session.current_tab_id = getattr(driver, "tab_id", None)
    session.tab_group = getattr(driver, "actual_tab_group", None)
    session.owns_tab = True
    # The run the claim was granted in, else the one the companion reports, else
    # the one already held: a companion that blinked at this instant makes the
    # run unknowable for a moment, and writing that None over a known run turns
    # the stale check off for this session for good.
    session.browser_run = (
        claim.get("browser_run") or _current_browser_run() or session.browser_run
    )
    # Every buffer below is an account of the borrowed tab. Carrying it into the
    # new one would answer "what did this page do" with another page's history.
    session.console = ConsoleCursor()
    session.probe_console = ConsoleCursor()
    session.probe_console_seen = []
    session.browser_log = []
    session.network_pending = {}
    session.network_rows = []
    session.network_dropped = 0
    session.render_bootstrap_registered = False
    try:
        borrowed.quit()
    except Exception as exc:
        logger.warning(
            "Detaching from claimed tab %s failed: %s: %s",
            released,
            type(exc).__name__,
            exc,
        )
    _release_claimed_tab(released)
    return released


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
    tab_group: str = DEFAULT_TAB_GROUP,
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
            # After the reset, so the borrowed tab is given back with no keys
            # held and no frame gate on it, and before anything is sent to the
            # new one.
            released_tab = _leave_claimed_tab(session, width, height, tab_group)
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
            **({"left_claimed_tab": released_tab} if released_tab is not None else {}),
        }
    # RuntimeError rather than ChromeBridgeError (which it covers, being its
    # base): a tab claim refused mid-open raises a plain one, and it used to
    # escape here, leaving the session registered and half moved.
    except (WebDriverException, RuntimeError, TimeoutError, ConnectionError, OSError):
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
    """Attach a named MCP session to an existing Chrome tab without navigating it.

    Console and network recording starts at the attach, so the console and
    network topics report what the tab does from here on. What it did before -
    the page load that is already finished, the request that already failed - was
    never recorded and cannot be recovered; reload the page to observe a full
    load.
    """
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
        DEFAULT_TAB_GROUP,
    )
    try:
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
    except Exception:
        # A claim that failed halfway used to leave the session registered: it
        # held the tab against every other session, answered nothing, and had to
        # be closed by name the caller never saw succeed.
        close_session(session_id)
        raise


# Shares the perception helpers so a selector, a visibility verdict and the
# aria-hidden rule mean the same thing here as in page_outline; the two topics
# disagreeing about what is on the page is the failure this avoids.
_INSPECT_SCRIPT = page_perception.JS_LIBRARY + r"""
const limit = arguments[0];
const offset = arguments[1];
const includeLinks = arguments[2];
const includeForms = arguments[3];
const includeButtons = arguments[4];

// A web component keeps its controls in a shadow root and an embedded form keeps
// them in another document, and `document.querySelectorAll` sees neither. That
// returned an empty page for an app that plainly has buttons on it, which is the
// first call the built-in recipes make.
// `depth` counts frames only, and stops where every other topic stops: a walk
// that reached deeper than the outline reported controls the outline denied, and
// one that stopped shallower hid controls the outline had already handed out.
let framesTooDeep = 0;
const WSN_ELEMENT_COLLECT_LIMIT = 20000;
const collectorTruncated = {links: false, forms: false, fields: false, buttons: false, iframes: false};

function collect(selectors) {
  const found = [];
  const seen = new Set();
  let truncated = false;
  const walk = (root, depth) => {
    if (truncated) return;
    let matched;
    try {
      matched = root.querySelectorAll(selectors);
    } catch (error) {
      matched = [];
    }
    for (const el of matched) {
      if (seen.has(el)) continue;
      if (found.length >= WSN_ELEMENT_COLLECT_LIMIT) {
        truncated = true;
        break;
      }
      seen.add(el);
      found.push(el);
    }
    if (truncated) return;
    let all;
    try {
      all = root.querySelectorAll('*');
    } catch (error) {
      return;
    }
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot, depth);
      if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
        if (depth >= WSN_MAX_FRAME_DEPTH) {
          framesTooDeep += 1;
          continue;
        }
        let doc = null;
        try {
          doc = el.contentDocument;
        } catch (error) {
          doc = null;
        }
        if (doc) walk(doc, depth + 1);
      }
    }
  };
  walk(document, 0);
  return {items: found, truncated: truncated};
}

function category(name, selectors) {
  const result = collect(selectors);
  collectorTruncated[name] = result.truncated;
  return order(result.items);
}

function visibility(el) {
  const reason = wsnHiddenReason(el);
  return {visible: !reason, hidden_reason: reason};
}

// Something nothing can reach must not push a usable control past the limit.
function order(elements) {
  const visible = [];
  const hidden = [];
  for (const el of elements) (wsnHiddenReason(el) ? hidden : visible).push(el);
  return visible.concat(hidden);
}

function labelFor(el) {
  if (el.labels && el.labels.length) return (el.labels[0].innerText || '').trim();
  let parent = null;
  try {
    parent = el.closest ? el.closest('label') : null;
  } catch (error) {
    parent = null;
  }
  return parent ? (parent.innerText || '').trim() : '';
}
function fieldInfo(el) {
  const result = Object.assign({
    selector: wsnSelector(el), tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    id: el.id || '', name: el.getAttribute('name') || '',
    label: labelFor(el), placeholder: el.getAttribute('placeholder') || '',
    required: !!el.required, disabled: !!el.disabled
  }, visibility(el));
  if (el.tagName.toLowerCase() === 'select') {
    result.options = Array.from(el.options).map(o => ({value: o.value, text: o.text, selected: o.selected}));
  }
  return result;
}
const output = {links: [], forms: [], fields: [], buttons: [], iframes: []};
const counts = {links: 0, forms: 0, fields: 0, buttons: 0, iframes: 0};
const FIELD_SELECTOR = 'input, textarea, select, [contenteditable="true"]';
if (includeLinks) {
  const links = category('links', 'a[href]');
  counts.links = links.length;
  output.links = links.slice(offset, offset + limit).map(a => Object.assign({
    selector: wsnSelector(a),
    text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
    href: a.href
  }, visibility(a)));
}
if (includeForms) {
  const forms = category('forms', 'form');
  counts.forms = forms.length;
  output.forms = forms.slice(offset, offset + limit).map((form, index) => Object.assign({
    index: offset + index, selector: wsnSelector(form), id: form.id || '',
    name: form.getAttribute('name') || '',
    action: form.action, method: (form.method || 'get').toLowerCase(), enctype: form.enctype,
    fields: Array.from(form.querySelectorAll(FIELD_SELECTOR)).slice(0, limit).map(fieldInfo)
  }, visibility(form)));
  const fields = category('fields', FIELD_SELECTOR);
  counts.fields = fields.length;
  output.fields = fields.slice(offset, offset + limit).map(fieldInfo);
}
if (includeButtons) {
  const buttons = category('buttons',
    'button, input[type="button"], input[type="submit"], input[type="reset"], ' +
    'input[type="image"], [role="button"]'
  );
  counts.buttons = buttons.length;
  output.buttons = buttons.slice(offset, offset + limit).map(button => Object.assign({
    selector: wsnSelector(button), tag: button.tagName.toLowerCase(),
    type: (button.getAttribute('type') || '').toLowerCase(), id: button.id || '',
    name: button.getAttribute('name') || '',
    text: (button.innerText || button.value || button.getAttribute('aria-label') || '').trim(),
    disabled: !!button.disabled
  }, visibility(button)));
}
const frames = category('iframes', 'iframe, frame');
counts.iframes = frames.length;
output.iframes = frames.slice(offset, offset + limit).map(frame => Object.assign({
  selector: wsnSelector(frame), id: frame.id || '', name: frame.name || '',
  src: frame.src || '', title: frame.title || '',
  same_origin: (() => {
    try {
      return !!frame.contentDocument;
    } catch (error) {
      return false;
    }
  })()
}, visibility(frame)));
output.found = counts;
output.returned = {};
output.range = {};
for (const key of Object.keys(counts)) {
  const returned = (output[key] || []).length;
  const start = Math.min(offset, counts[key]);
  const end = start + returned;
  output.returned[key] = returned;
  output.range[key] = {
    start: start,
    end: end,
    next_offset: end < counts[key] ? end : null,
    has_more: end < counts[key]
  };
}
output.offset = offset;
output.limit = limit;
output.collector_limit = WSN_ELEMENT_COLLECT_LIMIT;
output.collector_truncated = collectorTruncated;
output.truncated = Object.keys(counts).some(key => counts[key] > (output[key] || []).length);
output.frames_too_deep = framesTooDeep;
return output;
"""


def get_page_elements(
    session_id: str = "default",
    include_links: bool = True,
    include_forms: bool = True,
    include_buttons: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Return stable selectors and metadata for rendered page controls.

    Open shadow roots and same-origin frames are included; anything inside one is
    reported as a ``host >>> control`` path, which the action tools resolve. Every
    entry carries ``visible`` and, when it is not, ``hidden_reason``, so a control
    no click can reach is never handed over looking like an ordinary one.
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, min(int(offset), 20_000))
    session = _get_session(session_id)
    with session.lock:
        # This topic has no frame_selector: it always answers for the whole page.
        # It therefore starts at the top rather than trusting whatever frame the
        # previous call happened to leave selected - reporting one frame's
        # buttons, url and title as the page's is indistinguishable from the
        # truth on the way out.
        _leave_element_frame(session.driver)
        elements = session.driver.execute_script(
            _INSPECT_SCRIPT,
            limit,
            offset,
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
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Wait for a dynamic element to be present, visible, or clickable.

    ``selector`` accepts the same three locator forms as ``fill``: CSS, a ref
    handle, and a piercing path. ``frame_selector`` names the frame a CSS
    selector is looked up in, exactly as it does for ``find`` and ``page_text``.

    The wait is capped at 30 seconds - a two-minute blocking wait inside a tool
    call is worse than the surprise - and ``timeout_seconds`` in the result is the
    wait that was really made, not the one asked for. A timeout says the same
    number, so a wait that was cut short cannot read as one that ran its course.
    """
    if state not in _ELEMENT_STATES:
        raise ValueError("state must be 'present', 'visible', or 'clickable'")
    timeout = max(0.1, min(float(timeout_seconds), 30.0))
    session = _get_session(session_id)
    with session.lock:
        _enter_action_frame(session.driver, frame_selector, selector)
        try:
            element = _wait_for_locator(session.driver, selector, state, timeout)
            tag = element.tag_name
        finally:
            _release_action_frame(session.driver, frame_selector, selector)
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "selector": selector,
            "state": state,
            "tag": tag,
            "timeout_seconds": timeout,
            "frame_selector": frame_selector,
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


_CHECKBOX_TRUE = frozenset({"1", "true", "yes", "y", "on", "check", "checked"})
_CHECKBOX_FALSE = frozenset({"0", "false", "no", "n", "off", "uncheck", "unchecked", ""})


def _desired_checked(value: Any) -> bool:
    """Say whether a checkbox value means check or uncheck, or refuse to guess.

    Anything unrecognised used to mean "uncheck", so ``{"#terms": "check"}`` -
    and every typo - quietly cleared the box and still reported success.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _CHECKBOX_TRUE:
        return True
    if text in _CHECKBOX_FALSE:
        return False
    raise ValueError(
        f"'{value}' does not say whether to tick this box or clear it. Pass true or "
        "false, or one of 1/yes/on/check/checked, 0/no/off/uncheck/unchecked."
    )


# One round-trip that also does the scrolling, so reading what kind of control
# this is costs nothing extra. A control that cannot take the value says so here,
# instead of coming back as a driver stacktrace the caller has to decode.
_FIELD_PREPARE_SCRIPT = """
const element = arguments[0];
element.scrollIntoView({block: 'center'});
const type = String(element.type || '').toLowerCase();
// readonly is ignored by the controls that are not typed into.
const readonlyIgnored = ['checkbox', 'radio', 'range', 'color', 'file', 'hidden',
  'button', 'submit', 'reset', 'image'];
return {
  tag: element.tagName.toLowerCase(),
  type: type,
  multiple: !!element.multiple,
  disabled: !!element.disabled,
  editable: !!element.isContentEditable,
  readonly: !!element.readOnly && readonlyIgnored.indexOf(type) < 0
};
"""

# Reading the value back is the only honest report: maxlength truncates, a number
# input drops text it cannot parse, and an input handler may rewrite the lot.
_FIELD_READ_BODY = """
// The control written to is not always the control the page kept: a change
// handler is free to re-render it away, and reading on through the orphan
// reports a value the page has already thrown out.
if (!element.isConnected) return {kind: 'detached'};
const tag = element.tagName.toLowerCase();
if (tag === 'select') {
  const chosen = Array.from(element.selectedOptions);
  return {
    kind: 'select',
    multiple: !!element.multiple,
    values: chosen.map(option => String(option.value)),
    texts: chosen.map(option => String(option.text || '').trim()),
    value: element.value,
    text: chosen[0] ? String(chosen[0].text || '').trim() : '',
    disabled: chosen[0] ? !!chosen[0].disabled : false
  };
}
const type = String(element.type || '').toLowerCase();
if (type === 'checkbox' || type === 'radio') return {kind: 'checked', value: !!element.checked};
if (element.isContentEditable) return {kind: 'text', type: type, value: element.textContent};
return {kind: 'text', type: type, value: element.value === undefined ? '' : element.value};
"""

# Reading without touching anything: what a control that refused a write is left
# holding, so ``field_values`` answers for every selector it was asked about.
_FIELD_READ_SCRIPT = "const element = arguments[0];\n" + _FIELD_READ_BODY

# Blurring before the read settles those handlers and fires the `change` event
# that the last field of a fill otherwise never got, because nothing ever moved
# off it.
_FIELD_STATE_SCRIPT = (
    """
const element = arguments[0];
// blur() on a control that never had focus does nothing, so this needs no test
// for where the focus is - including inside a shadow root, where the document
// only ever names the host.
if (element.blur) element.blur();
"""
    + _FIELD_READ_BODY
)

# Typing into these is not something a keyboard can do: send_keys turned
# '2024-01-15' into '40115-02-20', and clear() on a slider moved it to its default
# midpoint before the write failed. The value is set on the control instead, and
# the two events a real edit raises are dispatched by hand - a value set from
# script never marks the control dirty, so the blur that follows will not fire
# `change` a second time.
_JS_VALUE_TYPES = frozenset(
    {"date", "time", "datetime-local", "month", "week", "range", "color"}
)

_VALUE_FORMATS = {
    "date": "YYYY-MM-DD, for example 2024-01-15",
    "time": "HH:MM or HH:MM:SS, for example 09:30",
    "datetime-local": "YYYY-MM-DDTHH:MM, for example 2024-01-15T09:30",
    "month": "YYYY-MM, for example 2024-01",
    "week": "YYYY-Www, for example 2024-W03",
    "range": "a number inside the control's own min and max",
    "color": "#rrggbb, for example #ff8800",
}

# The write is rehearsed on a throwaway control of the same type first, because
# these controls do not refuse a value they cannot parse - they replace it. A
# range takes its midpoint, a colour takes black and a date empties itself, so a
# failed fill used to leave the form holding a plausible wrong answer.
_SET_VALUE_SCRIPT = """
const element = arguments[0];
const wanted = String(arguments[1]);
const doc = element.ownerDocument || document;
const probe = doc.createElement('input');
probe.type = element.type;
if (element.min) probe.min = element.min;
if (element.max) probe.max = element.max;
if (element.step) probe.step = element.step;
probe.value = wanted;
const outcome = String(probe.value || '');
// A control may shorten what it is given - a datetime-local drops the seconds it
// does not carry - but not answer with something else entirely.
const usable = outcome !== '' &&
  wanted.toLowerCase().indexOf(outcome.toLowerCase()) === 0;
if (!usable) return {taken: false, value: String(element.value || ''), expected: outcome};
element.value = wanted;
element.dispatchEvent(new Event('input', {bubbles: true}));
element.dispatchEvent(new Event('change', {bubbles: true}));
return {taken: true, value: String(element.value || ''), expected: outcome};
"""

# A contenteditable editor (TipTap, ProseMirror, Slate, Quill) does not listen to
# value sets or DOM patches: its document model only moves on real editing
# events. Focus the host, select its whole contents, then insert the text
# through the same CDP input channel a keyboard uses, so the editor sees a
# genuine replacement edit.
_CONTENTEDITABLE_SELECT_SCRIPT = """
const element = arguments[0];
element.focus();
const range = document.createRange();
range.selectNodeContents(element);
const selection = window.getSelection();
selection.removeAllRanges();
selection.addRange(range);
return true;
"""


def _write_contenteditable(
    driver: webdriver.Chrome, element: Any, text: str
) -> str:
    """Replace a contenteditable's text the way an editor expects to be edited."""
    driver.execute_script(_CONTENTEDITABLE_SELECT_SCRIPT, element, text)
    cdp = getattr(driver, "execute_cdp_cmd", None)
    if cdp is not None:
        cdp("Input.insertText", {"text": text})
    else:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(text)
    return text


# select_by_value *adds* to the selection of a <select multiple>, so a second fill
# left the first option set too and the form submitted both. The whole selection
# is written at once instead - which is also the only way to ask for exactly one
# option - and it raises one change event rather than one per option clicked.
_SELECT_MANY_SCRIPT = """
const element = arguments[0];
const wanted = arguments[1];
for (const option of element.options) {
  option.selected = wanted.indexOf(String(option.value)) >= 0;
}
element.dispatchEvent(new Event('input', {bubbles: true}));
element.dispatchEvent(new Event('change', {bubbles: true}));
return Array.from(element.selectedOptions).map(option => String(option.value));
"""


_SELECT_OPTIONS_SCRIPT = """
return Array.from(arguments[0].options).slice(0, 40).map(option => ({
  value: String(option.value),
  text: String(option.text || '').trim(),
  disabled: !!option.disabled
}));
"""


def _pick_option(driver: Any, element: Any, value: str) -> dict[str, Any]:
    """Find the option a value names, by value first and then by visible text.

    Selenium reports only the last thing it tried, so 'this select has no option
    called unity' came back as 'could not locate element with visible text', and a
    disabled option went through silently on the companion bridge.
    """
    options = driver.execute_script(_SELECT_OPTIONS_SCRIPT, element) or []
    match = next((option for option in options if option.get("value") == value), None)
    if match is None:
        match = next((option for option in options if option.get("text") == value), None)
    if match is None:
        offered = ", ".join(f"'{option.get('value')}'" for option in options[:10])
        raise ValueError(
            f"No option of this select matches '{value}'. It offers "
            f"{offered or 'no options at all'}."
        )
    if match.get("disabled"):
        raise ValueError(f"The option '{value}' is disabled, so it cannot be selected")
    return match


def _requested_values(value: Any) -> list[str]:
    """The list of option values a request names; a scalar names exactly one."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return [str(value)]


def _value_taken(actual: str, requested: str) -> bool:
    """Say whether the control kept what it was given, sanitisation allowed for.

    ``type=email`` and ``type=url`` strip the whitespace around a value by the
    HTML value sanitisation algorithm, and a change handler is free to trim it or
    fold its case. The field took the value in every one of those, and calling
    them refusals left an agent retrying a write that had already worked.
    """
    if actual == requested:
        return True
    left = actual.replace("\r\n", "\n").replace("\r", "\n").strip()
    right = requested.replace("\r\n", "\n").replace("\r", "\n").strip()
    return left == right or left.casefold() == right.casefold()


def _quoted(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values) or "nothing"


def _selection_rejection(state: dict[str, Any], requested: Any) -> str | None:
    """Compare the whole selection, because a <select multiple> holds a list."""
    values = [str(item) for item in (state.get("values") or [])]
    texts = [str(item) for item in (state.get("texts") or [])]
    wanted = _requested_values(requested)
    unclaimed = list(zip(values, texts))
    for want in wanted:
        match = next(
            (
                pair
                for pair in unclaimed
                if _value_taken(pair[0], want) or _value_taken(pair[1], want)
            ),
            None,
        )
        if match is None:
            return f"the selection is {_quoted(values)} instead of {_quoted(wanted)}"
        unclaimed.remove(match)
    if unclaimed:
        return f"the selection is {_quoted(values)} instead of {_quoted(wanted)}"
    return None


def _field_rejection(state: dict[str, Any], requested: Any) -> str | None:
    """Explain why the control does not hold what was asked, or return None."""
    kind = state.get("kind")
    if kind == "checked":
        return None  # The checkbox paths verify their own state as they go.
    if kind in {"gone", "detached"}:
        return (
            "the page took the control away while the value was being committed, "
            "so there is nothing left holding it"
        )
    if kind == "select":
        if state.get("disabled"):
            return (
                f"the option '{state.get('text') or state.get('value')}' is disabled, so "
                "the selection stayed on "
                f"'{state.get('value')}'"
            )
        if state.get("multiple"):
            return _selection_rejection(state, requested)
        wanted = str(requested)
        if _value_taken(str(state.get("value")), wanted) or _value_taken(
            str(state.get("text")), wanted
        ):
            return None
        return f"the selection is '{state.get('value')}' instead of '{wanted}'"
    actual = "" if state.get("value") is None else str(state.get("value"))
    if _value_taken(actual, str(requested)):
        return None
    rejection = f"the field holds '{actual}' instead of '{requested}'"
    hint = _VALUE_FORMATS.get(str(state.get("type") or ""))
    if hint:
        rejection += f"; a {state.get('type')} input takes {hint}"
    if state.get("replaced"):
        rejection += " (the page replaced the control as the value was committed)"
    return rejection


def _brief_error(exc: Exception) -> str:
    """A driver stacktrace in a per-field error is noise the caller cannot use."""
    text = str(exc).split("Stacktrace:")[0].strip()
    first = text.splitlines()[0].strip() if text else ""
    first = first.removeprefix("Message:").strip()
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


_FILE_INPUT_STATE_SCRIPT = """
const element = arguments[0];
return {
  type: String(element.type || '').toLowerCase(),
  multiple: !!element.multiple,
  names: Array.from(element.files || []).map(file => file.name)
};
"""


def _attach_files(driver: Any, element: Any, paths: list[str]) -> list[str]:
    """Leave the input holding exactly ``paths``, and report what it holds.

    Chrome *appends* to an input that accepts multiple files, so a second upload
    left the first file attached while the result only ever described the call
    that had just been made. The names are read back off the input for the same
    reason a filled value is: only the input knows what it ended up with.
    """
    state = driver.execute_script(_FILE_INPUT_STATE_SCRIPT, element) or {}
    if state.get("type") != "file":
        raise ValueError("Selector does not point to an input[type=file]")
    if len(paths) > 1 and not state.get("multiple"):
        raise ValueError("Input does not accept multiple files")
    if state.get("names"):
        element.clear()
    element.send_keys("\n".join(paths))
    after = driver.execute_script(_FILE_INPUT_STATE_SCRIPT, element) or {}
    return [str(name) for name in (after.get("names") or [])]


def _held_value(state: dict[str, Any]) -> Any:
    """What ``field_values`` reports for one control.

    A ``<select multiple>`` holds a list, and a control the page took away holds
    nothing at all - which is not the same as holding an empty string.
    """
    kind = state.get("kind")
    if kind in {"gone", "detached"}:
        return None
    if kind == "select" and state.get("multiple"):
        return [str(item) for item in (state.get("values") or [])]
    return state.get("value")


def _value_left_behind(driver: Any, element: Any) -> Any:
    """What a control that refused the write is left holding, touching nothing."""
    if element is None:
        return None
    try:
        return _held_value(driver.execute_script(_FIELD_READ_SCRIPT, element) or {})
    except Exception:
        return None


def _reread_replaced_control(driver: Any, selector: str) -> dict[str, Any]:
    """Read the control the page put in the place of the one written to."""
    try:
        replacement = _resolve_element(driver, selector)
    except Exception:
        return {"kind": "gone"}
    state = driver.execute_script(_FIELD_READ_SCRIPT, replacement) or {}
    state["replaced"] = True
    return state


def _write_selection(
    driver: Any, element: Any, selector: str, control: dict[str, Any], value: Any
) -> Any:
    """Leave a select holding exactly the options asked for, and say which.

    A ``<select multiple>`` is written whole: ``select_by_value`` only ever added
    to what was already selected, so a second fill left the form submitting both
    options and one option on its own could not be asked for at all.
    """
    wanted = _requested_values(value)
    if not control.get("multiple") and len(wanted) > 1:
        raise ValueError(
            "This select holds one option at a time; only a <select multiple> can "
            "hold several"
        )
    options = [_pick_option(driver, element, item) for item in wanted]
    chosen = [str(option["value"]) for option in options]
    if control.get("multiple"):
        driver.execute_script(_SELECT_MANY_SCRIPT, element, chosen)
        return chosen
    option = options[0]
    if hasattr(driver, "select_option"):
        driver.select_option(selector, option["value"])
    else:
        select = Select(element)
        try:
            select.select_by_value(option["value"])
        except Exception:
            select.select_by_visible_text(option["text"])
    return value


def _write_by_script(driver: Any, element: Any, input_type: str, value: Any) -> Any:
    """Set a control no keyboard can type into, and refuse rather than damage it.

    Returns the value the control is expected to end up holding, which is not
    always the one asked for: a datetime-local keeps the minute and drops the
    seconds, a colour folds its case.
    """
    outcome = driver.execute_script(_SET_VALUE_SCRIPT, element, str(value)) or {}
    if not outcome.get("taken"):
        hint = _VALUE_FORMATS.get(input_type, "a value of its own kind")
        raise ValueError(
            f"A {input_type} control takes {hint}, so '{value}' was refused rather "
            f"than written; the control still holds '{outcome.get('value', '')}'"
        )
    return outcome.get("expected") or value


def fill_fields(
    fields: dict[str, Any],
    files: dict[str, str] | None = None,
    session_id: str = "default",
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Fill controls by CSS selector; file inputs are supplied separately.

    Every write is read back off the control the page kept - which is not always
    the control that was written to - because a control is free to refuse what it
    is given: a number input drops text it cannot parse, ``maxlength`` truncates,
    an input handler rewrites, a framework re-renders the field away.
    ``field_values`` answers for every selector asked about, ``errors`` names the
    ones that differ from the request, and ``success`` is false unless every
    control took its value. Whitespace and case a field sanitises away are not a
    refusal: the field took the value.

    A ``<select multiple>`` may be given a list, and ends up holding exactly what
    the list names and nothing else. Date, time, month, week, range and colour
    controls are set rather than typed into, and a value they cannot parse is
    refused instead of replaced with their idea of a default.

    ``frame_selector`` names the frame the CSS selectors are looked up in, exactly
    as it does for ``find`` and ``page_text``.
    """
    if not fields and not files:
        raise ValueError("At least one field or file must be provided")
    session = _get_session(session_id)
    filled: list[str] = []
    uploaded: dict[str, list[str]] = {}
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with session.lock:
        driver = session.driver
        _enter_action_frame(driver, frame_selector, *fields, *(files or {}))
        for selector, value in fields.items():
            element = None
            expected: Any = value
            try:
                element = _resolve_element(driver, selector)
                control = driver.execute_script(_FIELD_PREPARE_SCRIPT, element) or {}
                tag = str(control.get("tag") or "").lower()
                input_type = str(control.get("type") or "").lower()
                if input_type == "file":
                    raise ValueError("Use the files argument for file inputs")
                if control.get("disabled"):
                    raise ValueError(
                        "The control is disabled, so nothing can be written to it"
                    )
                if control.get("readonly"):
                    raise ValueError(
                        "The control is readonly, so its value cannot be changed"
                    )
                if tag != "select" and isinstance(value, (list, tuple, set, frozenset)):
                    raise ValueError(
                        "Only a <select multiple> takes several values; this control "
                        "takes one"
                    )
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
                    expected = _write_selection(driver, element, selector, control, value)
                elif input_type in _JS_VALUE_TYPES:
                    expected = _write_by_script(driver, element, input_type, value)
                elif control.get("editable"):
                    expected = _write_contenteditable(driver, element, str(value))
                else:
                    element.clear()
                    element.send_keys(str(value))
                state = driver.execute_script(_FIELD_STATE_SCRIPT, element) or {}
                if state.get("kind") == "detached":
                    # The blur fired `change`, and the page rebuilt the control
                    # while it ran. Whatever it kept is the answer.
                    state = _reread_replaced_control(driver, selector)
                values[selector] = _held_value(state)
                rejection = _field_rejection(state, expected)
                if rejection:
                    errors[selector] = f"The control did not take the value: {rejection}"
                else:
                    filled.append(selector)
            except Exception as exc:  # Return partial progress to the caller.
                errors[selector] = _brief_error(exc)
                if selector not in values:
                    # Every selector asked about is answered for, even the ones
                    # that refused the write: the caller needs to know what the
                    # form is left holding, not just that something went wrong.
                    values[selector] = _value_left_behind(driver, element)
            finally:
                # A field inside a frame leaves the driver there; the next field
                # is looked up from the top again. Under a frame_selector every
                # field shares the one frame, which is left at the end.
                if not frame_selector:
                    _release_locator_frame(driver, selector)

        for selector, file_path in (files or {}).items():
            try:
                path = Path(file_path).expanduser().resolve(strict=True)
                if not path.is_file():
                    raise ValueError("Upload path is not a file")
                element = _resolve_element(driver, selector)
                uploaded[selector] = _attach_files(driver, element, [str(path)])
            except Exception as exc:
                errors[selector] = _brief_error(exc)
            finally:
                if not frame_selector:
                    _release_locator_frame(driver, selector)

        if frame_selector:
            _leave_element_frame(driver)
        return {
            **_page_summary(driver, session_id),
            "success": not errors,
            "filled": filled,
            "field_values": values,
            "files_uploaded": uploaded,
            "errors": errors,
            "frame_selector": frame_selector,
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
    frame_selector: str | None = None,
    trusted: bool = False,
) -> dict[str, Any]:
    """Click a rendered element by CSS selector, ref handle, or piercing path.

    ``frame_selector`` names the frame a CSS selector is looked up in, exactly as
    it does for ``find`` and ``page_text``.

    ``trusted=True`` dispatches a real trusted mouse sequence through the
    browser's input pipeline instead of the element's synthetic click. It lands
    on the element's centre as a human pointer would, so pages that demand
    isTrusted events, or that read pointer position, behave as if a user
    clicked. The element is scrolled into the middle of the viewport first.
    """
    session = _get_session(session_id)
    with session.lock:
        _enter_action_frame(session.driver, frame_selector, selector)
        try:
            element = _wait_for_locator(session.driver, selector, "clickable", 10.0)
            session.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element
            )
            if trusted:
                _click_trusted(session, element, frame_selector)
            else:
                element.click()
        finally:
            # The click may have happened inside a frame; everything after it -
            # the settle, the page summary - is about the page as a whole.
            _release_action_frame(session.driver, frame_selector, selector)
        _wait_after_action(session.driver, wait_seconds)
        return {
            **_page_summary(session.driver, session_id),
            "success": True,
            "clicked": selector,
            "frame_selector": frame_selector,
            "trusted": trusted,
        }


_ELEMENT_CENTER_SCRIPT = """
const rect = arguments[0].getBoundingClientRect();
return {
  x: rect.x + rect.width / 2,
  y: rect.y + rect.height / 2,
  width: rect.width,
  height: rect.height
};
"""


def _click_trusted(
    session: BrowserSession, element: Any, frame_selector: str | None
) -> None:
    """Dispatch a trusted pointer click at the element's centre."""
    driver = session.driver
    center = driver.execute_script(_ELEMENT_CENTER_SCRIPT, element) or {}
    if not center.get("width") or not center.get("height"):
        raise ValueError(
            "The element has no visible box, so a trusted click has nothing to "
            "land on; use trusted=false to click it synthetically"
        )
    frame_map, viewport = _pointer_context(driver, frame_selector)
    _pointer_dispatch(
        session,
        "click",
        float(center["x"]),
        float(center["y"]),
        viewport,
        frame_map=frame_map,
    )


_MAX_SCRIPT_RESULT_CHARS = 200_000


def _clip_result(value: Any) -> Any:
    """Cut undisplayable size out of a script result, marking what was lost."""
    if isinstance(value, str) and len(value) > _MAX_SCRIPT_RESULT_CHARS:
        return {
            "clipped": True,
            "length": len(value),
            "head": value[:_MAX_SCRIPT_RESULT_CHARS],
        }
    return value


def execute_js(
    script: str,
    args: list[Any] | None = None,
    session_id: str = "default",
    await_promise: bool = False,
    user_gesture: bool = False,
) -> dict[str, Any]:
    """Run a JavaScript snippet in a session's page and report what it returns.

    ``script`` runs in the top document of the session's current tab; ``args``
    arrive as ``arguments[0..n]``. A JSON-serialisable value comes back as
    itself, and a returned promise is awaited. Strings longer than 200k
    characters are clipped in the report.

    This is the escape hatch for page state the DOM reads do not expose -
    localStorage, virtualised lists, framework state - and for mutations that
    have no input-shaped equivalent. With the Chrome bridge driver the result
    of ``await_promise`` is honoured because bridge evaluation awaits promises
    anyway; a plain Selenium driver cannot await one, so that mode is refused
    there rather than silently returning an unserialisable promise.

    ``user_gesture`` runs the script as though a person had just clicked, which
    is the only way to reach the APIs Chrome gates behind one - clipboard writes,
    fullscreen, autoplay with sound. It goes through CDP directly, so ``args``
    are inlined as ``arguments`` rather than passed by the WebDriver protocol.
    """
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        try:
            if await_promise and not hasattr(driver, "execute_cdp_cmd"):
                raise ValueError(
                    "await_promise needs the Chrome bridge driver; with a plain "
                    "Selenium driver, return the promise object and read it in a "
                    "later call instead"
                )
            if user_gesture:
                value = _evaluate_with_gesture(driver, script, args, await_promise)
            else:
                value = driver.execute_script(script, *(args or []))
        except Exception as exc:
            return {
                **_page_summary(driver, session_id),
                "success": False,
                "error": _brief_error(exc),
            }
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "value": _clip_result(value),
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
if (!element) { return {focused: false}; }
element.scrollIntoView({block: 'center', inline: 'center'});
if (!element.hasAttribute('tabindex') && element.tabIndex < 0) {
  element.setAttribute('tabindex', '-1');
}
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
        # CDP key dispatch does not need the browser window to be foreground.
        # Leaving both the DOM focus and OS focus untouched is important for a
        # user who is working in another tab or application.
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


@dataclass
class _StagedKeys:
    """A copy of a session's key state, so a batch commits only what it sent."""

    held_keys: dict[str, str]
    fresh_keys: set[str]

    @classmethod
    def of(cls, session: BrowserSession) -> "_StagedKeys":
        return cls(dict(session.held_keys), set(session.fresh_keys))

    def commit_to(self, session: BrowserSession) -> None:
        session.held_keys.clear()
        session.held_keys.update(self.held_keys)
        session.fresh_keys.clear()
        session.fresh_keys.update(self.fresh_keys)


def _held_slot(session: BrowserSession | _StagedKeys, normalized: str) -> str | None:
    """The id this session already holds the given physical key under, if any.

    Held keys are filed under the spelling the caller pressed them with, and one
    key answers to several spellings, so a lookup by id alone misses: hold('LEFT')
    followed by release('ARROW_LEFT') would lift nothing and leave the key down
    for the rest of the session, with its modifier bit stuck on.
    """
    wanted = key_table.physical_key(normalized)
    for held_id, held_key in session.held_keys.items():
        if key_table.physical_key(held_key) == wanted:
            return held_id
    return None


def _tap_of_held_key(spelling: str, held_id: str) -> ValueError:
    """Refuse a tap of a key this session already holds down.

    A tap presses and lifts, so tapping a key that is already held ends with the
    key up in the page while the session still counts it as down: its modifier
    bit then rides on every later mouse event, and the release meant to lift it
    is dropped by WebDriver, which knows the key is not pressed any more. In a
    batch it goes the other way - the tap is staged as a hold, so an already-held
    key gets no keydown at all and the batch's release tail lifts the hold the
    caller was relying on. Either way the caller asked for two things that cannot
    both be true, so they hear about it instead of one of them happening.
    """
    return ValueError(
        f"Key '{spelling}' is already held by this session (as '{held_id}'), so a "
        "tap of it would leave it up in the page while this session still counted "
        f"it down. Release '{held_id}' first, or drop the tap."
    )


def _key_event_pair(
    session: BrowserSession | _StagedKeys,
    normalized: list[str],
    selected_action: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the down/up streams for one key action against the held-key state."""
    down_events: list[dict[str, Any]] = []
    up_events: list[dict[str, Any]] = []
    if selected_action in {"tap", "hold"}:
        for key in normalized:
            if selected_action == "tap" or _held_slot(session, key) is None:
                down_events.append({"type": "down", "key": key})
    if selected_action in {"tap", "release"}:
        for key in reversed(normalized):
            if selected_action == "tap":
                up_events.append({"type": "up", "key": key})
                continue
            slot = _held_slot(session, key)
            if slot is not None:
                # Release exactly what was pressed: a hold("w") that is released
                # as "W" must still lift the same key.
                up_events.append({"type": "up", "key": session.held_keys[slot]})
    return down_events, up_events


def _commit_held_keys(
    session: BrowserSession | _StagedKeys,
    key_ids: list[str],
    normalized: list[str],
    selected_action: str,
) -> None:
    if selected_action == "hold":
        for key_id, key in zip(key_ids, normalized):
            if _held_slot(session, key) is not None:
                # Already down under some spelling of this key; a second entry
                # for it would survive the release of the first one.
                continue
            # A real keyboard waits before it repeats, so a key pressed for this
            # very frame must not also arrive as a repeat inside it. A key that
            # was already down got no keydown here, so it stays eligible to
            # repeat - otherwise re-holding it would silence the repeat forever.
            session.fresh_keys.add(key_id)
            session.held_keys[key_id] = key
    elif selected_action == "release":
        for key in normalized:
            key_id = _held_slot(session, key)
            if key_id is None:
                continue
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
    _refuse_non_css_frame(frame_selector)
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        if selected_action == "tap":
            # Before anything is switched, focused or sent: a refusal has to
            # leave the keys this session holds exactly as they were.
            for spelling, key in zip(keys, normalized):
                slot = _held_slot(session, key)
                if slot is not None:
                    raise _tap_of_held_key(str(spelling), slot)
        stepping = session.render_mode == "step" and _advance_frame
        driver.switch_to.default_content()
        frames_advanced = 0
        try:
            if frame_selector:
                # The same resolver the release below uses. Entering through a
                # looser one meant an ambiguous selector was accepted here and
                # refused half way through the call, with the keys already down.
                _select_frame(driver, frame_selector, css_only=True)
            if selected_focus != "none":
                _focus_target(driver, target_selector, selected_focus)
            runs = repetitions if selected_action == "tap" else 1
            dispatched = False
            try:
                for _ in range(runs):
                    down_events, up_events = _key_event_pair(
                        session, normalized, selected_action
                    )
                    if selected_action == "tap" and stepping:
                        _perform_key_events(driver, down_events)
                        try:
                            driver.switch_to.default_content()
                            _auto_advance_render_after_input(session, frames_held)
                            frames_advanced += frames_held
                        finally:
                            # The key is physically down in the browser from here on.
                            # A frame advance that fails - a document that reloaded
                            # and dropped the gate - must not end the call with the
                            # key still down: a tap records nothing in this session's
                            # state, so release_inputs could not reach it either.
                            try:
                                if frame_selector:
                                    _select_frame(driver, frame_selector, css_only=True)
                            finally:
                                _perform_key_events(driver, up_events)
                    else:
                        events = list(down_events)
                        if selected_action == "tap" and hold:
                            events.append({"type": "pause", "seconds": hold})
                        events.extend(up_events)
                        _perform_key_events(driver, events)
                dispatched = True
            finally:
                # A hold whose stream died part way may have pressed some of its
                # keys, so the session writes them all down: a key it believes it
                # holds can be lifted by release_inputs, a key it never heard of
                # cannot. A release is the mirror image - forgetting keys that may
                # still be down is the one outcome nothing recovers from - so it
                # is only written off once the events have actually gone.
                if selected_action == "hold" or dispatched:
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


class LocatorGone(ValueError):
    """The document that could answer this locator is not open any more.

    Waiting cannot bring a replaced document back, so the wait loops raise this
    on sight instead of spending their whole timeout on it.
    """


_POINTER_FALLBACK_HINT = (
    "A pointer action aimed at the node's page-level 'center' from "
    "web_info(topic='page_outline') reaches frame content that element handles "
    "cannot."
)


def _leave_element_frame(driver: Any) -> None:
    """Put the driver back at the top document after acting inside a frame."""
    try:
        driver.switch_to.default_content()
    except Exception:  # A dead session has nothing left to restore.
        pass


def _release_locator_frame(driver: Any, locator: str) -> None:
    """Give the driver back after an action, without paying for it needlessly.

    Only a ref handle or a piercing path can have entered a frame, so a plain CSS
    selector - the overwhelmingly common case - costs no extra round trip.
    """
    try:
        needs_release = page_perception.resolve_locator_expression(locator) is not None
    except ValueError:
        needs_release = False
    if needs_release:
        _leave_element_frame(driver)


def _enter_action_frame(driver: Any, frame_selector: str | None, *locators: str) -> None:
    """Enter the frame an action names, and refuse a locator that names another one.

    ``frame_selector`` is the same one ``find`` and ``page_text`` take: it says
    which document the plain CSS selectors are looked up in. A ref handle or a
    piercing path already carries the document it was read from, so pairing the
    two would let the caller name two different frames in one call and only one
    of them could win.
    """
    if not frame_selector:
        return
    for locator in locators:
        try:
            carries_own_frame = page_perception.resolve_locator_expression(locator) is not None
        except ValueError:
            carries_own_frame = False
        if carries_own_frame:
            raise ValueError(
                f"frame_selector '{frame_selector}' cannot be combined with '{locator}': an "
                "element handle and a ' >>> ' path already name the document they came "
                "from. Use one or the other."
            )
    _select_frame(driver, frame_selector)


def _release_action_frame(driver: Any, frame_selector: str | None, locator: str) -> None:
    """Give the driver back to the top document after a single-locator action."""
    if frame_selector:
        _leave_element_frame(driver)
        return
    _release_locator_frame(driver, locator)


def _enter_ref_document(driver: Any, epoch: str) -> str | None:
    """Switch the driver into the browsing context whose registry minted ``epoch``.

    An element reference belongs to one browsing context: handed over from
    another one it is refused as stale, which is why a ref read inside a frame
    could never be acted on. The frame is therefore entered before the ref is
    resolved, and the caller acts while the driver is still there.

    Returns the path of the frame it entered ('' for the top document), or
    ``None`` when no document in this tab minted the epoch. ``depth_limited`` in
    the returned dict means the search stopped at its own bound rather than
    exhausting the tab - a different thing to tell the caller than "it is gone".

    The bound is two frames deeper than any topic walks, so a ref that could be
    minted can always be followed back to the document that minted it.
    """
    driver.switch_to.default_content()
    path: list[str] = []
    depth_limited = False
    for _ in range(page_perception.MAX_FRAME_DEPTH + 2):
        try:
            step = driver.execute_script(page_perception.FRAME_FOR_EPOCH_SCRIPT, epoch)
        except WebDriverException:
            return {"found": False, "depth_limited": depth_limited}
        if not isinstance(step, dict):
            return {"found": False, "depth_limited": depth_limited}
        depth_limited = depth_limited or bool(step.get("depth_limited"))
        if step.get("here"):
            return {"found": True, "frame": " >>> ".join(path)}
        frame = step.get("frame")
        if frame is None:
            return {"found": False, "depth_limited": depth_limited}
        path.append(str(step.get("path") or "?"))
        try:
            driver.switch_to.frame(frame)
        except WebDriverException:
            # The frame went away between finding it and entering it.
            return {"found": False, "depth_limited": depth_limited}
    return {"found": False, "depth_limited": True}


def _resolve_ref(driver: Any, locator: str, expression: str) -> Any:
    epoch, _number = page_perception.parse_ref(locator)
    driver.switch_to.default_content()
    element = driver.execute_script(f"return {expression};")
    if element is not None:
        return element
    located = _enter_ref_document(driver, epoch)
    if not located["found"]:
        _leave_element_frame(driver)
        if located.get("depth_limited"):
            # Telling this caller the handle is stale would send them to re-read a
            # page that hands back the very same handle, forever.
            raise ValueError(
                f"Element handle '{locator}' was not found within the "
                f"{page_perception.MAX_FRAME_DEPTH} frames deep this session walks; "
                "its document may be nested deeper. Nothing that far in can be "
                f"acted on by handle. {_POINTER_FALLBACK_HINT}"
            )
        raise LocatorGone(
            f"Element handle '{locator}' was read from a document that is no longer "
            "open in this tab - the page, or the frame that held it, was replaced or "
            "removed - so it is stale. Read the page again with "
            "web_info(topic='page_outline') and use the handle it reports now."
        )
    frame = located.get("frame") or ""
    element = driver.execute_script(f"return {expression};")
    if element is not None:
        return element
    _leave_element_frame(driver)
    where = f"frame '{frame}'" if frame else "the page"
    raise ValueError(
        f"Element handle '{locator}' still names {where}, but the element it was "
        "read from has been removed from it, so it is stale. Read the page again "
        f"with web_info(topic='page_outline'). {_POINTER_FALLBACK_HINT}"
    )


def _resolve_piercing(driver: Any, locator: str) -> Any:
    """Walk a ``a >>> b`` path one document at a time, entering frames on the way."""
    remaining = [str(part) for part in (page_perception.split_piercing_path(locator) or [])]
    driver.switch_to.default_content()
    step: Any = None
    for _ in range(8):
        step = driver.execute_script(page_perception.PIERCING_STEP_SCRIPT, remaining)
        if not isinstance(step, dict):
            break
        element = step.get("element")
        if element is not None:
            return element
        frame = step.get("frame")
        if frame is None:
            break
        rest = [str(part) for part in (step.get("rest") or [])]
        try:
            driver.switch_to.frame(frame)
        except WebDriverException as exc:
            _leave_element_frame(driver)
            raise ValueError(
                f"Piercing path '{locator}' names a frame that cannot be entered: "
                f"{type(exc).__name__}. {_POINTER_FALLBACK_HINT}"
            ) from exc
        remaining = rest
    _leave_element_frame(driver)
    index = step.get("at") if isinstance(step, dict) else None
    segment = (
        remaining[index]
        if isinstance(index, int) and 0 <= index < len(remaining)
        else locator
    )
    if isinstance(step, dict) and step.get("invalid"):
        raise ValueError(
            f"Piercing path '{locator}' has a segment that is not valid CSS: '{segment}'."
        )
    raise ValueError(
        f"Piercing path '{locator}' matches nothing at segment '{segment}'. Each "
        "segment is looked up inside the previous one's shadow root or frame "
        "document; read the page again with web_info(topic='page_outline') to see "
        "what is there now."
    )


def _resolve_element(driver: Any, locator: str) -> Any:
    """Find one element from a CSS selector, a ``ref:<epoch>:N``, or a piercing path.

    Plain CSS keeps working exactly as before; ref handles and ``a >>> b`` are new
    forms that survive shadow roots and unstable DOM structure.

    A ref or a path may name something inside a frame. The driver is switched into
    that frame and **left there**, because an element handed over from another
    browsing context is refused as stale; whoever acts on the element calls
    ``_leave_element_frame`` afterwards.
    """
    expression = page_perception.resolve_locator_expression(locator)
    if expression is None:
        return driver.find_element(By.CSS_SELECTOR, locator)
    if getattr(driver, "is_extension_bridge", False):
        raise ValueError(
            f"Locator '{locator}' needs a live element handle, which the companion "
            f"bridge cannot return. Use a CSS selector in current-Chrome mode. "
            f"{_POINTER_FALLBACK_HINT}"
        )
    if page_perception.REF_PATTERN.match(str(locator).strip()):
        return _resolve_ref(driver, str(locator).strip(), expression)
    return _resolve_piercing(driver, str(locator).strip())


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

    The wait it actually did is named in the failure, because the timeout asked
    for is clamped: "never appeared in 120 seconds" about a wait that lasted 30
    sends the caller looking for the wrong problem.
    """
    waited = f"waited {timeout:g}s"
    if page_perception.resolve_locator_expression(locator) is None:
        return WebDriverWait(driver, timeout).until(
            _ELEMENT_STATES[state]((By.CSS_SELECTOR, locator)),
            f"Selector '{locator}' was still not {state} after {timeout:g}s",
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
        except LocatorGone:
            # A replaced document never comes back, so polling for it would only
            # spend the timeout before saying the same thing.
            raise
        except ValueError as exc:
            failure = str(exc)
        except WebDriverException as exc:
            failure = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            # The element may have resolved inside a frame without ever reaching
            # the state; nobody is going to act on it now, so give the driver back.
            _leave_element_frame(driver)
            raise TimeoutException(f"{failure} ({waited})")
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
        # Inside the try: a frame selection that fails part-way can leave the
        # driver in a frame, and then the next read answers for that frame.
        try:
            _select_frame(driver, frame_selector)
            result = page_perception.outline(
                driver,
                limit=limit,
                include_occlusion=include_occlusion,
                format=output,
            )
        finally:
            _leave_element_frame(driver)
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
        try:
            _select_frame(driver, frame_selector)
            result = page_perception.page_text(
                driver, max_chars=max_chars, mode=mode, include_links=include_links
            )
        finally:
            _leave_element_frame(driver)
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
        try:
            _select_frame(driver, frame_selector)
            result = page_perception.find(
                driver, query, role=role, limit=limit, visible_only=visible_only
            )
        finally:
            _leave_element_frame(driver)
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
        for cursor in (session.console, session.probe_console):
            cursor.log_index = max(0, cursor.log_index - overflow)
    return session.browser_log


def _console_since(
    session: BrowserSession, cursor: ConsoleCursor, clear: bool = False
) -> dict[str, Any]:
    """Collect the console output recorded after one reader's own cursor.

    A reader keeps a place in each source because they are counted in different
    units and neither may be consumed on another reader's behalf: the in-page
    hook numbers the entries it keeps, while Chrome's browser log is destroyed by
    reading it, so it is drained into a session buffer that readers index into.
    The companion backend buffers both inside the extension, where one sequence
    number covers everything.

    Either source can restart the numbering underneath a reader - a new document
    numbers from one again, and an evicted service worker restarts its counter -
    and both report it rather than leaving it to be guessed from a number that
    went backwards. A reader whose cursor was rebased is served from the start of
    what is buffered, so a page that logs while booting is read, not skipped.

    ``clear`` throws the buffered entries away once they have been collected,
    which leaves the cursor pointing at an empty buffer.
    """
    driver = session.driver
    if hasattr(driver, "get_events"):
        payload = driver.get_events(kinds=["console"], since_seq=cursor.seq, limit=500)
        entries = list(payload.get("entries") or [])
        # The extension replays from the beginning when its own counter restarted.
        rebased = bool(payload.get("reset"))
        cursor.seq = int(payload.get("next_seq") or cursor.seq)
        if clear:
            driver.clear_events(kinds=["console"])
            cursor.seq = 0
    else:
        payload = diagnostics.read_page_console(driver, cursor.seq, clear, cursor.doc)
        entries = list(payload.get("entries") or [])
        rebased = bool(payload.get("document_changed"))
        cursor.doc = str(payload.get("doc") or "")
        cursor.seq = int(payload.get("next_seq") or 0)
        buffered = _drain_browser_log(session)
        entries.extend(buffered[min(max(0, cursor.log_index), len(buffered)) :])
        cursor.log_index = len(buffered)
        if clear:
            session.browser_log.clear()
            cursor.log_index = 0
            cursor.seq = 0
            cursor.doc = ""
    entries.sort(key=lambda item: (item.get("ts") or 0, item.get("seq") or 0))
    return {
        "entries": diagnostics.dedupe_console(entries),
        "cursor_reset": rebased,
        "dropped": payload.get("dropped"),
    }


def _console_note(session: BrowserSession) -> str:
    """Say when this session's recording actually started, per backend.

    "Reload to capture load-time output" is false on both backends now and would
    cost a reload to learn nothing: the companion arms capture while its tab is
    still blank, and the Selenium hook is installed into every new document
    before the document's own first script. A tab claimed with ``attach_tab`` is
    the real exception - what it did before the claim was recorded by nobody.
    """
    if session.profile_mode == "current" and not session.owns_tab:
        return (
            "Recording started when this session claimed the tab: whatever the page "
            "logged before that was recorded by nobody. Reload the page to see a "
            "whole load."
        )
    if session.profile_mode == "current":
        return (
            "The companion armed recording while this tab was still blank, so "
            "load-time output is already included; no reload is needed."
        )
    return (
        "The console hook runs before each document's own first script, so "
        "load-time output is already included; no reload is needed."
    )


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

    A page that is replaced takes its numbering with it, so after a navigation the
    reading resumes at the new document's first entry and ``cursor_reset`` says
    so; ``next_seq`` from before that navigation means nothing afterwards.
    """
    session = _get_session(session_id)
    with session.lock:
        if since_seq:
            # An explicit cursor replaces the sequence number only: which document
            # minted it, and how much of the browser log was read, are still ours.
            session.console.seq = int(since_seq)
        payload = _console_since(session, session.console, clear)
        if clear:
            # The buffers everyone reads are gone, so no reader may keep a place
            # in them: a stale index would skip whatever arrives next.
            session.probe_console = ConsoleCursor()
            session.probe_console_seen.clear()
        selected = diagnostics.filter_console(
            payload["entries"], levels, contains, kinds, limit
        )
        return {
            "success": True,
            "session_id": session_id,
            "entries": selected,
            "returned": len(selected),
            "next_seq": session.console.seq,
            "cursor_reset": payload["cursor_reset"],
            "dropped": payload["dropped"],
            "levels": levels or list(diagnostics.LEVELS),
            "note": _console_note(session),
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
    """List finished HTTP requests made by the page.

    Recording starts when the session takes its tab, not when this is first
    called, so the requests of the very first navigation are here to be read. A
    tab claimed with ``attach_tab`` is recorded from the moment it was claimed:
    whatever it did before that was observed by nobody and cannot be recovered.

    Both backends keep a bounded history - the newest 500 records - so a session
    that outlives its own buffer loses the oldest requests rather than growing
    without limit. ``dropped`` says how many went that way instead of leaving the
    gap to be mistaken for a quiet page.
    """
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        if hasattr(driver, "get_events"):
            # The tab subscribed when it opened; this only repairs a subscription
            # that failed then. It can never recover traffic from before it runs,
            # which is why it cannot be the only place capture is turned on. A
            # stand-in driver that does not track the flag is already capturing.
            if not getattr(driver, "events_subscribed", True):
                driver.subscribe_events(["console", "network"])
            payload = driver.get_events(kinds=["network"], since_seq=0, limit=500)
            rows = list(payload.get("entries") or [])
            dropped = int((payload.get("dropped") or {}).get("network") or 0)
        else:
            session.network_rows.extend(
                diagnostics.selenium_network_rows(driver, session.network_pending)
            )
            overflow = len(session.network_rows) - 500
            if overflow > 0:
                del session.network_rows[:overflow]
                session.network_dropped += overflow
            rows = list(session.network_rows)
            dropped = session.network_dropped
        selected = diagnostics.filter_network(
            rows, url_pattern, types, status_min, status_max, only_errors, limit
        )
        response = {
            "success": True,
            "session_id": session_id,
            "returned": len(selected),
            "only_errors": bool(only_errors),
            "dropped": dropped,
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


def _evaluate_with_gesture(
    driver: Any,
    script: str,
    args: list[Any] | None,
    await_promise: bool,
) -> Any:
    """Evaluate a script under a synthetic user gesture, returning its value.

    ``Runtime.evaluate`` takes an expression, not a function body, so the script
    is wrapped in a function that is *applied* to the arguments rather than one
    that declares them: ``arguments`` is reserved inside a function body and
    cannot be assigned, and applying keeps the ``arguments[0..n]`` contract the
    WebDriver path already publishes. A page-side throw is raised here so the
    caller's existing error handling reports it the same way either route fails.
    """
    expression = (
        f"(function() {{\n{script}\n}}).apply(null, {json.dumps(args or [])})"
    )
    result = driver.execute_cdp_cmd(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": bool(await_promise),
            "userGesture": True,
        },
    )
    details = result.get("exceptionDetails")
    if details:
        raise RuntimeError(_exception_text(details))
    return (result.get("result") or {}).get("value")


def _exception_text(details: dict[str, Any]) -> str:
    """The most specific message a CDP exceptionDetails carries, line kept if present.

    The nested ``exception.description`` holds the real stack-bearing message when
    Chrome sends one; ``text`` is the flat fallback. Either way a line number, when
    given, is worth keeping - it is the only pointer back into the injected source.
    """
    exception = details.get("exception") or {}
    message = exception.get("description") or details.get("text") or "evaluation failed"
    line = details.get("lineNumber")
    if line is not None:
        return f"{message} (line {line})"
    return str(message)


def inject_script(
    op: str = "add",
    source: str | None = None,
    identifier: str | None = None,
    session_id: str = "default",
) -> dict[str, Any]:
    """Register, list, or forget page code that runs before every document's own scripts.

    ``add`` installs ``source`` with Page.addScriptToEvaluateOnNewDocument and keeps
    the returned identifier; it takes effect on the next navigation of this session.
    ``list`` returns the identifiers held. ``remove`` drops one from that list - CDP
    offers no matching removal, so the drop is best-effort and only stops us
    re-installing it, not what Chrome already queued for the next document.
    """
    session = _get_session(session_id)
    with session.lock:
        if op == "add":
            if not source:
                raise ValueError("inject_script op 'add' requires source")
            result = session.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": source}
            )
            script_id = str(result.get("identifier") or "")
            session.injected_scripts.append(script_id)
            return {"success": True, "session_id": session_id, "identifier": script_id}
        if op == "list":
            return {
                "success": True,
                "session_id": session_id,
                "identifiers": list(session.injected_scripts),
            }
        if op == "remove":
            removed = identifier in session.injected_scripts
            if removed:
                session.injected_scripts.remove(identifier)
            return {
                "success": True,
                "session_id": session_id,
                "identifier": identifier,
                "removed": removed,
            }
    raise ValueError(f"inject_script op must be add, list, or remove, not '{op}'")


def cookies(
    op: str = "get",
    session_id: str = "default",
    domain: str | None = None,
    name: str | None = None,
    set_cookies: list[dict[str, Any]] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read, write, or clear cookies as full objects - flags included, so defenses read too.

    ``get`` returns every field Chrome keeps (name, value, domain, path, secure,
    httpOnly, sameSite, expires), filtered client-side by ``domain`` substring and
    exact ``name``; a session or HttpOnly flag is as auditable as the value. ``set``
    installs the list in ``set_cookies``; ``clear`` wipes everything, or just the
    ``name``/``domain`` given.

    A real profile holds thousands of cookies, so ``get`` reports ``count`` for
    everything that matched and returns at most ``limit`` of them. Filter by
    ``domain`` rather than raising the limit: the answer to "what is this site
    setting" is never the other four thousand cookies.
    """
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        if op == "get":
            payload = driver.execute_cdp_cmd("Storage.getCookies", {})
            found = payload.get("cookies") or []
            if domain:
                found = [c for c in found if domain in (c.get("domain") or "")]
            if name:
                found = [c for c in found if c.get("name") == name]
            kept = max(1, min(int(limit), 1000))
            return {
                "success": True,
                "session_id": session_id,
                "count": len(found),
                "truncated": len(found) > kept,
                "cookies": found[:kept],
            }
        if op == "set":
            if not set_cookies:
                raise ValueError("cookies op 'set' requires set_cookies")
            driver.execute_cdp_cmd("Storage.setCookies", {"cookies": set_cookies})
            return {"success": True, "session_id": session_id, "count": len(set_cookies)}
        if op == "clear":
            params: dict[str, Any] = {}
            if name:
                params["name"] = name
            if domain:
                params["domain"] = domain
            driver.execute_cdp_cmd("Storage.clearCookies", params)
            return {"success": True, "session_id": session_id}
    raise ValueError(f"cookies op must be get, set, or clear, not '{op}'")


def local_storage(
    op: str = "read",
    session_id: str = "default",
    key: str | None = None,
    value: str | None = None,
    kind: str = "local",
) -> dict[str, Any]:
    """Read, write, or delete Web Storage for the open page (localStorage or sessionStorage).

    ``read`` with no ``key`` returns the whole store as a name->value map; with a
    ``key`` it returns that one value. ``write`` sets ``key`` to ``value``;
    ``delete`` removes ``key``. ``kind='session'`` targets sessionStorage, which a
    page clears on its own when the tab closes, rather than the persistent store.
    """
    # A typo must not quietly address the other store: "sesion" writing to
    # localStorage looks like it worked and leaves the value where nobody reads it.
    if kind not in {"local", "session"}:
        raise ValueError(f"local_storage kind must be local or session, not '{kind}'")
    store = "sessionStorage" if kind == "session" else "localStorage"
    if op == "read":
        if key is None:
            expression = (
                f"(() => {{ const out = {{}}; "
                f"for (let i = 0; i < {store}.length; i++) {{ "
                f"const k = {store}.key(i); out[k] = {store}.getItem(k); }} "
                f"return out; }})()"
            )
        else:
            expression = f"{store}.getItem({json.dumps(key)})"
    elif op == "write":
        if key is None or value is None:
            raise ValueError("local_storage op 'write' requires key and value")
        expression = f"{store}.setItem({json.dumps(key)}, {json.dumps(value)})"
    elif op == "delete":
        if key is None:
            raise ValueError("local_storage op 'delete' requires key")
        expression = f"{store}.removeItem({json.dumps(key)})"
    else:
        raise ValueError(f"local_storage op must be read, write, or delete, not '{op}'")

    result = execute_js(f"return {expression};", session_id=session_id)
    if not result.get("success"):
        return {"success": False, "session_id": session_id, "key": key, "error": result.get("error")}
    payload: dict[str, Any] = {"success": True, "session_id": session_id, "key": key}
    if op == "read":
        payload["value"] = result.get("value")
    elif op == "write":
        payload["value"] = value
    return payload


def solve_captcha(
    mode: str = "auto",
    session_id: str = "default",
    timeout_seconds: float = 180.0,
    poll_seconds: float = 3.0,
) -> dict[str, Any]:
    """Detect a captcha and get past it: wait for a human, or use a solving service.

    ``mode='detect'`` only looks. ``mode='wait'`` blocks until the challenge is
    gone from the page, which is what a human clicking the box actually produces.
    ``mode='solve'`` needs a configured service and refuses without one.
    ``mode='auto'`` - the default - solves when a service is configured and the
    widget is one with a sitekey, and otherwise waits, so the same call works on a
    machine with a key and on one without.

    Waiting returns as soon as the page stops showing the challenge, so a captcha
    the user clears in four seconds costs four seconds, not the whole timeout.
    """
    session = _get_session(session_id)
    with session.lock:
        status = _challenge_status(session.driver)
    if not status.get("challenge_detected") and not status.get("captcha_widgets"):
        return {"success": True, "session_id": session_id, "captcha_present": False, **status}
    if mode == "detect":
        return {"success": True, "session_id": session_id, "captcha_present": True, **status}

    identity = execute_js(captcha.IDENTIFY_SCRIPT, session_id=session_id)
    found = identity.get("value") or {}
    vendor, sitekey = found.get("vendor"), found.get("sitekey")
    configured = captcha.solver_config()["configured"]

    if mode == "solve" or (mode == "auto" and configured and vendor and sitekey):
        if not vendor or not sitekey:
            raise ValueError(
                "This captcha exposes no sitekey, so no service can be asked for a "
                "token; it has to be cleared in the page with mode='wait'."
            )
        solved = captcha.solve_remotely(
            found["task"], sitekey, found["url"], timeout_seconds, max(3.0, poll_seconds)
        )
        applied = execute_js(
            captcha.APPLY_TOKEN_SCRIPT,
            args=[vendor, solved["token"]],
            session_id=session_id,
        )
        return {
            "success": bool(applied.get("success")),
            "session_id": session_id,
            "captcha_present": True,
            "mode": "solve",
            "vendor": vendor,
            "applied": applied.get("value"),
            "cost": solved.get("cost"),
            "note": (
                "The token is in the page. Submitting the form is still the "
                "caller's move: some sites submit from the widget callback and "
                "some wait for the button."
            ),
        }

    if mode not in {"auto", "wait"}:
        raise ValueError(f"captcha mode must be detect, wait, solve, or auto, not '{mode}'")

    # Waiting is what wait_challenge already does, down to holding the session
    # open and reporting the page it ends on; repeating that loop here would only
    # give it a second set of edge cases to drift from.
    waited = wait_for_challenge_resolution(session_id, timeout_seconds, poll_seconds)
    outcome = {
        **waited,
        "captcha_present": not waited["resolved"],
        "mode": "wait",
        "vendor": vendor,
    }
    if not waited["resolved"]:
        outcome["error"] = (
            f"The captcha was still on the page after {waited['waited_seconds']:.0f}s. "
            "It needs a person to click it in the browser window, or a solving "
            "service configured through WEB_SEARCH_NEO_CAPTCHA_KEY."
        )
    return outcome


def set_extra_headers(
    headers: dict[str, str] | None = None,
    session_id: str = "default",
) -> dict[str, Any]:
    """Send extra HTTP headers with every request this session makes, until cleared.

    Chrome adds these to every outgoing request - navigations, fetches, images -
    so it is how a whole session is given an Authorization token, a custom
    User-Agent, or an A/B cookie without touching each call. Passing no headers
    (or an empty map) clears the override, which is the only way to stop it:
    Network.setExtraHTTPHeaders replaces the set every time, it does not add to it.
    """
    session = _get_session(session_id)
    payload = {str(key): str(value) for key, value in (headers or {}).items()}
    with session.lock:
        session.driver.execute_cdp_cmd("Network.enable", {})
        session.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": payload})
        session.extra_headers = payload
    return {
        "success": True,
        "session_id": session_id,
        "headers": payload,
        "cleared": not payload,
    }


# The token every automation-detection script looks at first. Setting it before
# the page's own scripts run is the difference between hiding the flag and being
# caught reading it late; inject_script is what makes that ordering possible.
STEALTH_SOURCE = """
Object.defineProperty(navigator, 'webdriver', {get: () => false, configurable: true});
if (!window.chrome) { window.chrome = {runtime: {}}; }
try {
  const original = navigator.permissions && navigator.permissions.query;
  if (original) {
    navigator.permissions.query = (parameters) =>
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : original(parameters);
  }
} catch (error) { /* a locked-down permissions API is not worth failing over */ }
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5], configurable: true});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en'], configurable: true});
"""


def stealth(
    op: str = "on",
    session_id: str = "default",
) -> dict[str, Any]:
    """Hide the usual automation tells from a page before its scripts can read them.

    ``on`` registers the overrides with inject_script, so they run before every
    document's own code and survive navigation - the only ordering that actually
    works, because a page reads ``navigator.webdriver`` on load. ``off`` forgets
    the registration for future documents.

    This lowers the chance a site *shows* a challenge; it does not solve one. A
    determined anti-bot service fingerprints far more than these flags, and the
    override cannot touch the ``--enable-automation`` switch Chrome was launched
    with. Pair it with a real profile and human-paced input, and use ``captcha``
    for what still gets through.
    """
    if op == "on":
        # Idempotent: a second on would otherwise register a duplicate script and
        # forget the first, leaving it running after off with no handle to remove it.
        session = _get_session(session_id)
        with session.lock:
            previous = session.stealth_identifier
        if previous:
            inject_script(op="remove", identifier=previous, session_id=session_id)
        result = inject_script(op="add", source=STEALTH_SOURCE, session_id=session_id)
        with session.lock:
            session.stealth_identifier = result["identifier"]
        return {
            "success": True,
            "session_id": session_id,
            "enabled": True,
            "identifier": result["identifier"],
            "note": (
                "Overrides take effect on the next navigation. They reduce captcha "
                "frequency, not certainty; a real profile and paced input matter more."
            ),
        }
    if op == "off":
        session = _get_session(session_id)
        with session.lock:
            identifier = session.stealth_identifier
            session.stealth_identifier = None
        removed = False
        if identifier:
            removed = inject_script(op="remove", identifier=identifier, session_id=session_id)["removed"]
        return {"success": True, "session_id": session_id, "enabled": False, "removed": removed}
    raise ValueError(f"stealth op must be on or off, not '{op}'")


# Re-issue a captured request from inside the page, so its cookies and origin are
# the page's own. Returning status, headers and a clipped body is what makes it a
# probe - resend the login POST, see whether the token still works - rather than
# a blind fire-and-forget.
_REPLAY_SCRIPT = """
const spec = arguments[0];
const started = performance.now();
try {
  const response = await fetch(spec.url, {
    method: spec.method || 'GET',
    headers: spec.headers || {},
    body: (spec.body != null && spec.method !== 'GET' && spec.method !== 'HEAD') ? spec.body : undefined,
    credentials: spec.credentials || 'include',
    redirect: 'follow',
  });
  const text = await response.text();
  const headers = {};
  response.headers.forEach((value, key) => { headers[key] = value; });
  return {
    ok: response.ok,
    status: response.status,
    url: response.url,
    redirected: response.redirected,
    headers: headers,
    body: text.length > 20000 ? text.slice(0, 20000) : text,
    truncated: text.length > 20000,
    ms: Math.round(performance.now() - started),
  };
} catch (error) {
  return {ok: false, status: 0, error: String(error && error.message || error)};
}
"""


def replay_request(
    request_id: str | None = None,
    session_id: str = "default",
    url: str | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    credentials: str = "include",
) -> dict[str, Any]:
    """Re-send a request from the page's own context and return the full response.

    Give it a ``request_id`` from the network topic to repeat that captured
    request - the url and method are taken from the row - or spell out ``url`` /
    ``method`` / ``headers`` / ``body`` directly. The fetch runs inside the page,
    so it carries the page's cookies and origin: this is how you check whether a
    session token is still valid, whether an endpoint is rate-limited, or what a
    form's POST actually returns, without driving the whole form again.

    Captured bodies are not retained by the network buffer, so a replay by
    ``request_id`` alone repeats a GET faithfully but cannot resend the original
    POST body - pass ``body`` explicitly for that.
    """
    session = _get_session(session_id)
    target_url, target_method = url, method
    if request_id is not None:
        with session.lock:
            rows = list(session.network_rows)
        match = next((row for row in rows if str(row.get("id")) == str(request_id)), None)
        if match is None:
            raise ValueError(
                f"No captured request has id '{request_id}'. Read the network topic "
                "with output='json' for current ids, or pass url and method directly."
            )
        target_url = url or match.get("url")
        target_method = method if method != "GET" else match.get("method", "GET")
    if not target_url:
        raise ValueError("replay_request needs a request_id or an explicit url")

    spec = {
        "url": target_url,
        "method": str(target_method or "GET").upper(),
        "headers": {str(key): str(value) for key, value in (headers or {}).items()},
        "body": body,
        "credentials": credentials,
    }
    result = execute_js("return await (async () => {" + _REPLAY_SCRIPT + "})();",
                        args=[spec], session_id=session_id, await_promise=True)
    if not result.get("success"):
        return {"success": False, "session_id": session_id, "error": result.get("error")}
    return {"success": True, "session_id": session_id, "request": spec, "response": result.get("value")}


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


def _refuse_non_css_frame(frame_selector: str | None) -> None:
    """Refuse a frame an input action cannot aim into, before it does anything.

    Every input path has to answer one string the same way. Keys enter a frame
    through ``_select_frame``, which walks a ref handle or a ``host >>> frame``
    path as well as CSS; coordinates are mapped through the frame's *box in the
    top document*, which only a CSS selector names from out here. An ``input``
    batch that took both would accept the path for its keys and then refuse it
    for its pointer entries, with half the call already sent. So every input path
    gives the narrower answer, and gives it before the first event.
    """
    if not frame_selector:
        return
    # A locator that means to be a ref or a path and cannot address anything
    # raises here, which is the earliest and most specific answer there is; no
    # string it raises on is a CSS selector that would have worked instead.
    if page_perception.resolve_locator_expression(frame_selector) is not None:
        raise ValueError(
            f"frame_selector '{frame_selector}' is an element handle or a "
            "' >>> ' path. Input is aimed by coordinate, which needs the frame's "
            "own box in the top-level page, and neither form gives one. Pass a "
            "CSS selector that matches this frame and nothing else."
        )


def _pointer_context(
    driver: webdriver.Chrome, frame_selector: str | None
) -> tuple[_FrameMap, dict[str, Any]]:
    """Resolve the frame's page mapping and viewport once, in that document's terms."""
    driver.switch_to.default_content()
    if not frame_selector:
        frame_map = _frame_map(driver, None)
        return frame_map, {
            "width": frame_map.page_width,
            "height": frame_map.page_height,
        }
    # The same strict resolution the keys of a batch get: one frame or none, and
    # a selector matching two of them is refused with the count rather than
    # answered with whichever came first.
    frame = _input_frame(driver, frame_selector)
    driver.switch_to.frame(frame)
    try:
        viewport = driver.execute_script(_VIEWPORT_SCRIPT)
    finally:
        driver.switch_to.default_content()
    return _frame_map(driver, frame_selector, frame), viewport


def _pointer_dispatch(
    session: BrowserSession,
    action: str,
    x: float,
    y: float,
    viewport: dict[str, Any],
    frame_map: _FrameMap | None = None,
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
    mapping = frame_map if frame_map is not None else _FrameMap(
        page_width=float(viewport["width"]), page_height=float(viewport["height"])
    )
    if selected_coordinate_mode in {"delta", "relative"}:
        # A run continues from wherever this session's last event landed, which
        # is also the position Chrome measures movementX/movementY against.
        # Teleporting somewhere else first - the viewport middle, say - turns the
        # first small delta of a pointer-locked run into a several-hundred-pixel
        # look-jump, because the warp itself is a movement the game reads.
        current_local_x, current_local_y = mapping.to_local(
            session.pointer_x, session.pointer_y
        )
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

    if unbounded:
        # A locked pointer has no position on the page at all, so its coordinates
        # are only a running total that movement is measured from.
        start_x, start_y = mapping.to_page(local_x, local_y)
        finish_x, finish_y = mapping.to_page(local_end_x, local_end_y)
    else:
        start_x, start_y = _page_point(mapping, local_x, local_y, "Pointer position")
        if (local_end_x, local_end_y) == (local_x, local_y):
            # A click has no second point; asking the page about the same one
            # twice would cost a round trip for an answer already given.
            finish_x, finish_y = start_x, start_y
        else:
            finish_x, finish_y = _page_point(
                mapping, local_end_x, local_end_y, "Pointer end position"
            )
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
    needs: the cursor never moves, only ``movementX``/``movementY`` matter. Each
    delta is measured from where this session last put the pointer - the lock
    click counts - so the movement a game reads is exactly the delta asked for.

    With a ``frame_selector`` the coordinates are the frame's own, carried onto
    the page through any CSS transform between the two; a point that maps outside
    the window is refused rather than dispatched into nowhere.
    """
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        frame_map, viewport = _pointer_context(driver, frame_selector)
        result = _pointer_dispatch(
            session,
            action,
            x,
            y,
            viewport,
            frame_map=frame_map,
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


_SCROLL_METRICS_SCRIPT = """
const root = document.scrollingElement || document.documentElement;
const width = window.innerWidth;
const height = window.innerHeight;
const pageWidth = Math.max(root ? root.scrollWidth : 0, document.documentElement.scrollWidth);
const pageHeight = Math.max(root ? root.scrollHeight : 0, document.documentElement.scrollHeight);
return {
  scroll_x: window.scrollX,
  scroll_y: window.scrollY,
  max_scroll_x: Math.max(0, pageWidth - width),
  max_scroll_y: Math.max(0, pageHeight - height),
  viewport_width: width,
  viewport_height: height,
  page_width: pageWidth,
  page_height: pageHeight,
  at_top: window.scrollY <= 0,
  at_bottom: window.scrollY >= Math.max(0, pageHeight - height) - 1
};
"""


def _scroll_metrics(driver: Any, frame_selector: str | None) -> dict[str, Any]:
    """Read the selected document's page scroll position and always leave the top selected."""
    try:
        _select_frame(driver, frame_selector, css_only=True)
        return dict(driver.execute_script(_SCROLL_METRICS_SCRIPT) or {})
    finally:
        driver.switch_to.default_content()


def scroll_page(
    delta_y: float,
    session_id: str = "default",
    delta_x: float = 0.0,
    x: float | None = None,
    y: float | None = None,
    frame_selector: str | None = None,
    wait_seconds: float = 0.1,
    include_summary: bool = True,
) -> dict[str, Any]:
    """Scroll at a viewport point, defaulting to its centre.

    Positive ``delta_y`` scrolls down and negative values scroll up. The point
    matters on pages with nested scroll containers: Chrome sends the wheel to
    whatever is painted under it. Page metrics still describe the selected
    document's window, so an inner container may move while they stay unchanged.
    """
    if (x is None) != (y is None):
        raise ValueError("x and y must be provided together, or both omitted for viewport centre")
    _refuse_non_css_frame(frame_selector)
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        frame_map, viewport = _pointer_context(driver, frame_selector)
        actual_x = float(x) if x is not None else float(viewport["width"]) / 2
        actual_y = float(y) if y is not None else float(viewport["height"]) / 2
        before = _scroll_metrics(driver, frame_selector)
        result = _pointer_dispatch(
            session,
            "wheel",
            actual_x,
            actual_y,
            viewport,
            frame_map=frame_map,
            delta_x=float(delta_x),
            delta_y=float(delta_y),
        )
        _wait_after_action(driver, wait_seconds)
        after = _scroll_metrics(driver, frame_selector)
        return {
            **_action_summary(driver, session_id, include_summary),
            **result,
            "frame_selector": frame_selector,
            "before": before,
            "after": after,
        }



# CDP input is addressed in top-level page pixels, so a point inside a frame has
# to be carried through everything that stands between the two: the origin of the
# frame's *content* box - the border box misses by exactly the border and padding
# - and any CSS transform, individual `rotate`/`scale`/`translate` property or
# `zoom` on the frame or on an ancestor. `wsnFrameMap` in the shared page-side
# library is that map, and the outline and find report their boxes through the
# very same function: two implementations of "where is this frame-local point on
# the page" drift apart, and then the centre a caller is told to click is not the
# pixel this module aims at.
_FRAME_MAP_SCRIPT = page_perception.JS_LIBRARY + """
const mapped = wsnFrameMap(arguments[0]);
return {
  x: mapped.x, y: mapped.y,
  ax: mapped.ax, ay: mapped.ay,
  bx: mapped.bx, by: mapped.by,
  flat: mapped.flat,
  page_width: window.innerWidth, page_height: window.innerHeight
};
"""


# Being inside the window is not the same as being reachable. A frame clipped by
# an `overflow: hidden` ancestor, or with a fixed header painted over it, answers
# every question about its own viewport as if it were whole, and the mapped point
# is a perfectly ordinary page coordinate - it just belongs to something else.
# Chrome hit-tests the top document before it delivers, so the same question is
# asked here, and the answer names what is in the way.
_FRAME_HIT_SCRIPT = """
const frame = arguments[0];
const hit = document.elementFromPoint(arguments[1], arguments[2]);
if (hit && (hit === frame || frame.contains(hit))) return null;
if (!hit) return 'nothing this document paints';
const classes = Array.from(hit.classList).map(name => '.' + name).join('');
return hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '') + classes;
"""


@dataclass(frozen=True)
class _FrameMap:
    """Where a point inside a frame lands in the top-level page CDP aims at.

    The default is the identity: with no frame selector, frame coordinates and
    page coordinates are the same thing.
    """

    x: float = 0.0
    y: float = 0.0
    ax: float = 1.0
    ay: float = 0.0
    bx: float = 0.0
    by: float = 1.0
    page_width: float = 0.0
    page_height: float = 0.0
    flat: bool = True
    # Asks the top document what it would hit at a page point: None when that is
    # the frame itself, otherwise a name for whatever is in the way. Only a frame
    # has one - without a frame selector the caller is aiming at the page, and
    # whatever is on top there is exactly what they meant to hit.
    hit_test: Callable[[float, float], str | None] | None = None

    def to_page(self, local_x: float, local_y: float) -> tuple[float, float]:
        return (
            self.x + self.ax * local_x + self.bx * local_y,
            self.y + self.ay * local_x + self.by * local_y,
        )

    def to_local(self, page_x: float, page_y: float) -> tuple[float, float]:
        determinant = self.ax * self.by - self.bx * self.ay
        if abs(determinant) < 1e-9:
            # A frame collapsed to nothing by its transform: no point in it has a
            # place on the page, so there is nothing to move relative to.
            raise ValueError(
                "The selected frame is scaled to zero on this page, so pointer "
                "coordinates inside it cannot be placed"
            )
        dx = page_x - self.x
        dy = page_y - self.y
        return (
            (dx * self.by - dy * self.bx) / determinant,
            (dy * self.ax - dx * self.ay) / determinant,
        )

    def on_page(self, page_x: float, page_y: float) -> bool:
        if not self.page_width or not self.page_height:
            return True
        return 0 <= page_x < self.page_width and 0 <= page_y < self.page_height


def _input_frame(driver: webdriver.Chrome, frame_selector: str) -> Any:
    """The one frame an input action names, resolved the way every path resolves it.

    ``_select_frame`` owns what "the one frame" means - it is what refuses two
    matches with the count - so it is asked here too rather than reimplemented,
    and the element it agreed on is handed back for the coordinate mapping.
    """
    _refuse_non_css_frame(frame_selector)
    _select_frame(driver, frame_selector, css_only=True)
    driver.switch_to.default_content()
    return WebDriverWait(driver, 10).until(
        conditions.visibility_of_element_located((By.CSS_SELECTOR, frame_selector))
    )


def _frame_map(
    driver: webdriver.Chrome, frame_selector: str | None, frame: Any = None
) -> _FrameMap:
    """Resolve how the named frame maps onto the page; CDP input is page-absolute.

    ``frame`` is the already-resolved element when the caller has one, so a
    pointer action pays for resolving its frame once rather than twice.
    """
    if not frame_selector:
        page = driver.execute_script(_VIEWPORT_SCRIPT)
        return _FrameMap(
            page_width=float(page["width"]), page_height=float(page["height"])
        )
    if frame is None:
        frame = _input_frame(driver, frame_selector)
    mapped = driver.execute_script(_FRAME_MAP_SCRIPT, frame)
    if not mapped["flat"]:
        # A perspective projection is not affine, so this mapping is the best
        # flat approximation of it rather than the truth. Saying so beats a
        # click that lands a little off with nothing to explain it.
        logger.warning(
            "Frame '%s' sits under a 3D transform; pointer coordinates inside it "
            "are mapped by their flat approximation and may be a few pixels off",
            frame_selector,
        )

    def hit_test(page_x: float, page_y: float) -> str | None:
        try:
            return driver.execute_script(_FRAME_HIT_SCRIPT, frame, page_x, page_y)
        except WebDriverException:
            # The document that could answer moved on. Refusing the action over a
            # question that could not be asked would be worse than sending it.
            return None

    return _FrameMap(
        x=float(mapped["x"]),
        y=float(mapped["y"]),
        ax=float(mapped["ax"]),
        ay=float(mapped["ay"]),
        bx=float(mapped["bx"]),
        by=float(mapped["by"]),
        page_width=float(mapped["page_width"]),
        page_height=float(mapped["page_height"]),
        flat=bool(mapped["flat"]),
        hit_test=hit_test,
    )


def _page_point(
    frame_map: _FrameMap,
    local_x: float,
    local_y: float,
    what: str,
    verify: bool = True,
) -> tuple[float, float]:
    """Map a frame-local point onto the page, refusing one the event cannot reach.

    A frame scrolled half out of the window still answers questions about its own
    viewport, so the local coordinate looks fine while the page coordinate it maps
    to is off-screen. CDP drops that event without a word, which reads exactly
    like a game that ignored the click. A frame clipped by an ancestor or covered
    by an overlay is worse: the point is a real one, and the event is delivered -
    to whatever is on top of it. ``verify`` skips the hit test for the
    intermediate points of a gesture, whose ends have already been checked.
    """
    page_x, page_y = frame_map.to_page(local_x, local_y)
    if not frame_map.on_page(page_x, page_y):
        raise ValueError(
            f"{what} ({local_x:g}, {local_y:g}) is inside the frame but lands at "
            f"({page_x:g}, {page_y:g}) in the page, outside the "
            f"{frame_map.page_width:g}x{frame_map.page_height:g} window, so no event "
            "would reach it. Scroll the frame into view first."
        )
    if verify and frame_map.hit_test is not None:
        blocking = frame_map.hit_test(page_x, page_y)
        if blocking is not None:
            raise ValueError(
                f"{what} ({local_x:g}, {local_y:g}) is inside the frame but lands at "
                f"({page_x:g}, {page_y:g}) in the page, where the browser would hit "
                f"{blocking} instead of the frame, so the event would go there and "
                "not to the frame. The frame is clipped or covered at that point: "
                "scroll it into view, or aim somewhere nothing is painted over."
            )
    return page_x, page_y


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
            session.held_touches.clear()
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
    ``swipe`` additionally reads ``end_x``/``end_y`` from each entry. ``release``
    lifts exactly the fingers it names - by ``id`` alone if the finger is already
    down - and every finger this session holds when it names none.
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
        frame_map, viewport = _pointer_context(driver, frame_selector)

        def resolve(index: int, entry: dict[str, Any], progress: float) -> dict[str, Any]:
            identifier = int(entry.get("id", index))
            tracked = session.held_touches.get(identifier)
            if "x" not in entry:
                # A finger that is already down can be named by id alone.
                if tracked is None:
                    raise ValueError(
                        f"Touch point id {identifier} is not down in this session, so "
                        "it has no position; give 'x' and 'y', or leave 'points' out "
                        "to lift every finger"
                    )
                return dict(tracked)
            start_x = float(entry["x"])
            start_y = float(entry["y"])
            local_x = start_x + (float(entry.get("end_x", start_x)) - start_x) * progress
            local_y = start_y + (float(entry.get("end_y", start_y)) - start_y) * progress
            if (
                not 0 <= local_x < float(viewport["width"])
                or not 0 <= local_y < float(viewport["height"])
            ):
                # Chrome takes an off-screen touch without a word and the page
                # never hears about it, which reads like a game that ignored it.
                raise ValueError(
                    f"Touch point ({local_x:g}, {local_y:g}) is outside the selected "
                    f"{float(viewport['width']):g}x{float(viewport['height']):g} viewport"
                )
            page_x, page_y = _page_point(
                frame_map,
                local_x,
                local_y,
                "Touch point",
                # The ends of a gesture are hit-tested; asking the page again for
                # every interpolated step in between would cost a round trip per
                # step to answer the same question.
                verify=progress <= 0.0 or progress >= 1.0,
            )
            return {
                "x": page_x,
                "y": page_y,
                "id": identifier,
                "radiusX": float(entry.get("radius_x", 6.0)),
                "radiusY": float(entry.get("radius_y", 6.0)),
                "force": float(entry.get("force", 1.0)),
            }

        def touch_points(progress: float) -> list[dict[str, Any]]:
            return [resolve(index, entry, progress) for index, entry in enumerate(entries)]

        def dispatch(event_type: str, resolved: list[dict[str, Any]]) -> None:
            driver.execute_cdp_cmd(
                "Input.dispatchTouchEvent",
                {"type": event_type, "touchPoints": resolved, "modifiers": _session_modifiers(session)},
            )

        def press(resolved: list[dict[str, Any]]) -> None:
            for point in resolved:
                held = session.held_touches.get(point["id"])
                if held is None:
                    continue
                # Chrome drops a touchStart for a finger that is already down
                # without a word. Recording the new position anyway would leave
                # every later id-only move or release aiming at a place the page
                # never had that finger.
                raise ValueError(
                    f"Touch point id {point['id']} is already down at "
                    f"({held['x']:g}, {held['y']:g}) and Chrome ignores a second "
                    "press of it; use action='move' to move that finger, or "
                    "'release' to lift it first"
                )
            dispatch("touchStart", resolved)
            for point in resolved:
                session.held_touches[point["id"]] = point

        def lift(resolved: list[dict[str, Any]]) -> None:
            # touchEnd carries the fingers that came up. An empty list ends every
            # one of them, so a two-finger gesture would lose the finger it meant
            # to keep - and Chrome then refuses to move that finger at all.
            dispatch("touchEnd", resolved)
            for point in resolved:
                session.held_touches.pop(point["id"], None)

        if selected_action == "cancel":
            # Chrome rejects a touchCancel that names points: it cancels the lot.
            dispatch("touchCancel", [])
            session.held_touches.clear()
        elif selected_action == "press":
            press(touch_points(0.0))
        elif selected_action == "move":
            moved = touch_points(1.0)
            dispatch("touchMove", moved)
            for point in moved:
                session.held_touches[point["id"]] = point
        elif selected_action == "release":
            lifted = touch_points(0.0) if entries else list(session.held_touches.values())
            if lifted:
                lift(lifted)
            else:
                # Nothing on the books - fingers from before this session started
                # tracking, or none at all - so fall back to ending everything.
                dispatch("touchEnd", [])
        elif selected_action == "tap":
            resolved = touch_points(0.0)
            press(resolved)
            lift(resolved)
        else:
            # The end of the swipe is resolved before the finger goes down: the
            # lift below is what un-plants it, and a lift that cannot resolve its
            # own point raises instead of lifting - leaving a finger on the page
            # under an error that reads as though nothing had happened at all.
            starting = touch_points(0.0)
            ending = touch_points(1.0)
            press(starting)
            try:
                for step in range(1, interpolation + 1):
                    dispatch("touchMove", touch_points(step / interpolation))
                    if duration:
                        time.sleep(duration / interpolation)
            finally:
                # A swipe that dies half way through must not leave the finger
                # planted on the page for every later gesture to fight with.
                lift(ending)
        if _advance_frame:
            _auto_advance_render_after_input(session)
        _wait_after_action(driver, wait_seconds)
        return {
            **_action_summary(driver, session_id, include_summary),
            "success": True,
            "action": selected_action,
            "points": len(entries),
            "touch_enabled": session.touch_enabled,
            "active_touches": sorted(session.held_touches),
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
    # Acquiring sends a real click, which is a coordinate: this call reads the
    # frame both ways and must not accept a locator only one of them can use.
    _refuse_non_css_frame(frame_selector)
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
            click_x, click_y = _frame_map(driver, frame_selector).to_page(
                float(rect["x"]), float(rect["y"])
            )
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
            # This click is the last thing Chrome saw the mouse do, so it is the
            # position the first relative move must be measured from.
            session.pointer_x = click_x
            session.pointer_y = click_y
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
    # One frame for the whole batch, decided before any of it is sent: the keys
    # and the pointer entries must not read the same string two ways.
    _refuse_non_css_frame(frame_selector)

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
            # batch: per-action round-trips are what make input feel laggy. The
            # stream is built against a staged copy of the key state and the real
            # state is written only once the browser has the events - a batch that
            # dies on its frame switch or its focus must not leave the session
            # believing it holds keys the page never saw, because every later
            # keystroke would then carry that phantom modifier.
            staged = _StagedKeys.of(session)
            events: list[dict[str, Any]] = []
            batch_taps: set[str] = set()
            for item in normalized_keys:
                key_ids = [item["key"].strip().upper()]
                normalized = [_normalize_game_key(item["key"])]
                action = "hold" if item["action"] == "tap" else item["action"]
                if item["action"] == "tap":
                    # A tap is staged as a hold, so a key the session already
                    # held would get no keydown at all and the release tail
                    # below would lift the caller's hold instead. Two taps of
                    # one key inside a single batch still collapse into the one
                    # press the frame can express - only a hold from an earlier
                    # call is a conflict.
                    physical = key_table.physical_key(normalized[0])
                    slot = _held_slot(staged, normalized[0])
                    if slot is not None and physical not in batch_taps:
                        raise _tap_of_held_key(item["key"], slot)
                    batch_taps.add(physical)
                down, up = _key_event_pair(staged, normalized, action)
                events.extend(down)
                events.extend(up)
                _commit_held_keys(staged, key_ids, normalized, action)
            _select_frame(driver, frame_selector)
            try:
                _focus_target(driver, target_selector, "focus")
                try:
                    if events:
                        _perform_key_events(driver, events)
                finally:
                    # Dispatch is not atomic - the bridge sends one event per
                    # round trip, and ActionChains.perform can die part way
                    # through as well - so by the time it fails any of these
                    # keys may be physically down. The books have to assume they
                    # all are: a key the page holds and the session has
                    # forgotten cannot be reached by release_inputs or by
                    # anything else, and every later keystroke carries it. The
                    # frame switch and the focus above are outside this window
                    # on purpose: a batch that never got that far sent nothing.
                    staged.commit_to(session)
            finally:
                if frame_selector:
                    driver.switch_to.default_content()
        if normalized_pointers:
            frame_map, viewport = _pointer_context(driver, frame_selector)
            for item in normalized_pointers:
                result = _pointer_dispatch(
                    session,
                    str(item["action"]),
                    float(item["x"]),
                    float(item["y"]),
                    viewport,
                    frame_map=frame_map,
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
        try:
            _auto_advance_render_after_input(session)
        finally:
            # Whatever the frame did, the taps this batch pressed have to come
            # back up; a failed advance must not leave them down for good.
            if tapped:
                key_ids = [key.strip().upper() for key in tapped]
                normalized = [_normalize_game_key(key) for key in tapped]
                _, up = _key_event_pair(session, normalized, "release")
                if up:
                    try:
                        _select_frame(driver, frame_selector)
                    finally:
                        try:
                            _perform_key_events(driver, up)
                            # Only a key the browser really lifted is forgotten;
                            # one that could not be sent stays on the books so
                            # release_inputs can still reach it.
                            _commit_held_keys(session, key_ids, normalized, "release")
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
    leaves the other's entries where they are. After a navigation the first probe
    starts at the new document's first entry, which is where a game that fails
    while booting says so.
    """
    duration = max(0.1, min(float(sample_seconds), 3.0))
    # The canvas rects this reports are aimed at with the same frame_selector, so
    # it answers that string exactly the way the input tools do: one frame, named
    # by CSS, or an error. Reading one of four matching frames and then clicking
    # in another is the failure this is here to prevent.
    _refuse_non_css_frame(frame_selector)
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        driver.switch_to.default_content()
        try:
            if frame_selector:
                _select_frame(driver, frame_selector)
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
                payload = _console_since(session, session.probe_console)
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
                "touches": sorted(session.held_touches),
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

// Yielding between frames must not be a timer. A tab nobody is looking at - and
// agent tabs open in the background so the user can keep working - clamps
// setTimeout to about a second, and to a minute once intensive throttling kicks
// in, so stepping sixty frames through nativeSetTimeout took 53 seconds where a
// visible tab took 241 ms. A MessageChannel message is still a macrotask, so page
// work, network callbacks and microtasks run between frames exactly as before,
// but nothing throttles it.
const yieldChannel = typeof MessageChannel === 'function' ? new MessageChannel() : null;
const yieldQueue = [];
if (yieldChannel) {
  yieldChannel.port1.onmessage = () => {
    const task = yieldQueue.shift();
    if (task) task();
  };
}
const yieldTask = task => {
  if (!yieldChannel) { nativeSetTimeout(task, 0); return; }
  yieldQueue.push(task);
  yieldChannel.port2.postMessage(0);
};

const state = {
    mode: 'normal',
    targetFps: null,
    interval: 1000 / 60,
    frameDelta: 1000 / 60,
    freezeTime: true,
    gateTimers: true,
    clockInstalled: false,
    clockPatched: false,
    timersInstalled: false,
    // How far ahead of the native clock the page-visible one has been carried by
    // stepping. It never shrinks, because a clock that goes backwards is worse
    // than one that is wrong: see restoreClock.
    skew: 0,
    // The last frame timestamp the page was given, which no later one may
    // undercut: see state.stamp.
    lastStamp: 0,
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
    // How deep inside a chain of timer callbacks the gate currently is, which is
    // what decides whether the next setTimeout gets the spec's nesting clamp.
    timerDepth: 0,
    nativeRequest: nativeRequest,
    nativeCancel: nativeCancel
};

// The page-visible clock. While the gate is engaged it only moves when a frame
// is released, so a game sees a constant delta no matter how long the agent
// spent thinking between calls. Off the gate it is the native clock plus the
// skew stepping has earned, so the two never disagree about which way time runs.
state.now = () => (
    state.gated() && state.freezeTime
      ? state.virtualNow
      : nativePerformanceNow() + state.skew
);
state.gated = () => state.mode !== 'normal';

// Sixty frames of 100ms cost the wall clock a fraction of that, so the frozen
// clock ends up seconds ahead of the native one. Handing the raw native clock
// back would drop the page's time by exactly that much: performance.now() falls,
// the next frame's delta is negative, physics integrates backwards, tweens snap
// and any timestamp the page stored now sits in the future. Carrying the
// difference forward as a fixed offset keeps the page's own clock monotonic
// across every mode change, at the price of it running ahead of wall time -
// which is what the page was told all along.
state.carryClock = previousNow => {
    state.skew = Math.max(state.skew, previousNow - nativePerformanceNow());
};

// Every frame timestamp the page is given passes through here, and none of them
// may undercut the last one. Measuring the skew is not enough on its own: a
// browser dates a frame from when it *began*, so the first native frame after a
// mode change can carry a stamp from before the moment the skew was read - up to
// a frame period earlier - and hand the page a small negative delta, which a
// game reads no differently from a large one. The shortfall is exactly what the
// skew was missing, so it is added there rather than papered over per frame:
// the whole page-visible clock moves up with the stamp instead of trailing it.
state.stamp = value => {
    if (value < state.lastStamp) {
      state.skew += state.lastStamp - value;
      // now() is the same clock as these stamps, so it has to be the moved one.
      state.patchClock();
      return state.lastStamp;
    }
    state.lastStamp = value;
    return value;
};

state.patchClock = () => {
    if (state.clockPatched) return;
    performance.now = () => state.now();
    Date.now = () => Math.round(epochOffset + state.now());
    state.clockPatched = true;
};
state.installClock = () => {
    state.clockInstalled = true;
    state.patchClock();
};
state.restoreClock = () => {
    state.clockInstalled = false;
    // Only a page that never gained a skew gets its untouched native clock back;
    // with one, the wrapper stays in place to keep applying it.
    if (state.skew) {
        state.patchClock();
        return;
    }
    if (!state.clockPatched) return;
    performance.now = nativePerformanceNow;
    Date.now = nativeDateNow;
    state.clockPatched = false;
};

// Timer wrappers are installed once, at bootstrap, and stay pass-through while
// the gate is off. Installing them only when step mode starts would leave every
// timer a real game registered during load running on the wall clock, which is
// exactly the case that matters.
state.wrapTimer = (callback, delay, args, interval) => {
    if (typeof callback !== 'function') return null;
    const id = state.nextTimerId--;
    const depth = state.timerDepth + 1;
    // HTML clamps a timeout scheduled from inside a timer to 4ms once the chain
    // is five deep, and that clamp is all that stands between a `setTimeout(loop,
    // 0)` game loop and an unbounded run of ticks inside a single frame, every
    // one of them reading the same instant off the frozen clock.
    const floor = interval === null ? (depth > 5 ? 4 : 0) : 1;
    const wait = Math.max(floor, Number(delay) || 0);
    if (state.gated() && state.gateTimers) {
      state.timers.set(id, {
        callback: callback, args: args, interval: interval,
        due: state.now() + wait, depth: depth
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
// A released frame covers `frameDelta` of virtual time, and every timer whose
// deadline falls inside that span really did come due inside it. So they run in
// deadline order with the page-visible clock parked at each one's own deadline,
// and an interval keeps its phase instead of being pushed to the end of the
// frame - rescheduling from `now` stretched every period out to a whole frame,
// which made a 5ms interval tick once per frame instead of three times, and made
// a 16ms one indistinguishable from a 100ms one under a 100ms frame delta.
state.runDueTimers = (now, spanStart) => {
    if (!state.gateTimers || !state.timers.size) return 0;
    const steer = state.gated() && state.freezeTime;
    let cursor = spanStart === undefined ? now : Math.min(spanStart, now);
    let count = 0;
    while (count < 512) {
      let dueId = null;
      let due = null;
      for (const [id, entry] of state.timers) {
        if (entry.due <= now && (due === null || entry.due < due.due)) {
          dueId = id;
          due = entry;
        }
      }
      if (due === null) break;
      cursor = Math.max(cursor, Math.min(now, due.due));
      if (steer) state.virtualNow = cursor;
      if (due.interval) due.due += due.interval;
      else state.timers.delete(dueId);
      const outerDepth = state.timerDepth;
      state.timerDepth = due.depth || 1;
      try { due.callback(...due.args); }
      catch (error) { nativeSetTimeout(() => { throw error; }, 0); }
      finally { state.timerDepth = outerDepth; }
      count += 1;
    }
    if (count >= 512) {
      // The page wants more timer work than one frame can hold. Carrying the
      // backlog forward would make every later frame slower still, so the
      // stragglers give up their missed ticks the way a real browser does.
      for (const entry of state.timers.values()) {
        if (entry.interval && entry.due <= now) entry.due = now + entry.interval;
      }
    }
    if (steer) state.virtualNow = now;
    return count;
};

state.flush = () => {
    const previous = state.lastFrame;
    if (state.gated() && state.freezeTime) state.virtualNow += state.frameDelta;
    else state.virtualNow = nativePerformanceNow() + state.skew;
    const timestamp = state.stamp(state.now());
    state.lastFrame = timestamp;
    state.frameCount += 1;
    state.runDueTimers(timestamp, previous);
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
    const pump = () => {
      state.timer = null;
      if (state.mode !== 'throttled') return;
      state.lastRealFlush = nativePerformanceNow();
      state.flush();
    };
    // A pump that is already late is not waiting for anything, so it yields
    // rather than arming a timer a hidden tab would clamp to a second. Zero is
    // not a timer id any browser hands out, so it marks the pending yield
    // without confusing the clearTimeout in setMode.
    if (delay > 0) {
      state.timer = nativeSetTimeout(pump, delay);
    } else {
      state.timer = 0;
      yieldTask(pump);
    }
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
        // A frame timestamp is the same clock performance.now() reads, and a
        // game measures its delta from the last one it was given - which may
        // well be a stepped one. Handing over the raw native stamp after a
        // skew has been earned is the backwards jump all over again.
        callback(state.stamp(timestamp + state.skew));
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
      // The stamp this callback was waiting for was going to come off the
      // stepped clock; see state.request for why it still has to.
      callback(state.stamp(timestamp + state.skew));
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
// The yield is a message rather than a timer because a hidden tab clamps timers:
// see yieldTask.
state.step = (count, done) => {
    let remaining = count;
    let callbacks = 0;
    const run = () => {
      callbacks += state.flush();
      remaining -= 1;
      if (remaining > 0) yieldTask(run);
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
      state.carryClock(previousNow);
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
      // Carry on from where the page's clock already is. Reading the native one
      // here threw away every skew an earlier round of stepping had earned, so
      // re-entering step mode dropped the clock just as leaving it did.
      state.virtualNow = previousNow;
      if (state.freezeTime) state.installClock();
      else { state.carryClock(previousNow); state.restoreClock(); }
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


_FRAME_COUNT_SCRIPT = "return document.querySelectorAll(arguments[0]).length;"


def _ambiguous_frame_message(frame_selector: str, count: int, *, css_only: bool) -> str:
    """Say a selector names more than one frame, and what to send instead.

    The advice has to fit the path that raised. The outline's ``frame`` path is
    verified unique, which makes it the right answer for a reader and a dead end
    for an input action, which cannot aim through one - a caller that followed it
    there would earn a second error for taking the first one's advice.
    """
    advice = (
        "Narrow it to a CSS selector that matches exactly one frame - an id, or a "
        "unique ancestor. The 'frame' path from web_info(topic='page_outline') is "
        "verified unique, but input cannot aim through it."
        if css_only
        else "Use a selector that matches exactly one frame, or the 'frame' path "
        "from web_info(topic='page_outline'), which is verified unique."
    )
    return (
        f"frame_selector '{frame_selector}' matches {count} elements in this "
        f"document, so it does not say which one to read. {advice}"
    )


def _select_frame(
    driver: webdriver.Chrome, frame_selector: str | None, *, css_only: bool = False
) -> None:
    """Enter the one frame ``frame_selector`` names, or say why it cannot.

    ``css_only`` says the caller is an input path, which changes nothing about
    what is accepted - ``_refuse_non_css_frame`` has already had its say - and
    only what an ambiguous selector is told to do instead. Sending a caller to
    the outline's verified-unique path is good advice for a reader and a dead end
    for an input action, which cannot aim through one.

    A selector that matches two frames used to switch into whichever one came
    first and report success, so the caller read one document believing it was
    another. Ambiguity is refused here instead. A ``host >>> frame`` path - what
    the outline reports for a frame inside a shadow root or a nested one - is
    walked frame by frame rather than handed to CSS, which cannot express it.
    """
    driver.switch_to.default_content()
    if not frame_selector:
        return
    if page_perception.resolve_locator_expression(frame_selector) is not None:
        # The resolver leaves the driver inside whatever frame it walked into, so
        # a failure here has to hand it back. Left inside, the *next* read - an
        # outline, a page_text, a page_elements - answers for that frame and
        # presents it as the page, with nothing anywhere saying so.
        element = _resolve_element(driver, frame_selector)
        try:
            driver.switch_to.frame(element)
        except WebDriverException as exc:
            _leave_element_frame(driver)
            raise ValueError(
                f"frame_selector '{frame_selector}' names an element that is not a "
                f"frame ({type(exc).__name__}). Pass the frame's own locator - the "
                "'frame' path a node reports in web_info(topic='page_outline') - "
                "not a locator for something inside it."
            ) from exc
        return
    deadline = time.monotonic() + 10.0
    failure = f"frame_selector '{frame_selector}' matched no frame"
    while True:
        try:
            count = int(driver.execute_script(_FRAME_COUNT_SCRIPT, frame_selector) or 0)
        except WebDriverException as exc:
            raise ValueError(
                f"frame_selector '{frame_selector}' is not a valid CSS selector: "
                f"{type(exc).__name__}"
            ) from exc
        if count > 1:
            raise ValueError(
                _ambiguous_frame_message(frame_selector, count, css_only=css_only)
            )
        if count == 1:
            try:
                driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, frame_selector))
                return
            except NoSuchFrameException:
                failure = (
                    f"frame_selector '{frame_selector}' matches an element that is not "
                    "a frame this session can enter"
                )
            except WebDriverException as exc:
                failure = f"frame_selector '{frame_selector}': {type(exc).__name__}"
        if time.monotonic() >= deadline:
            raise TimeoutException(f"{failure}. {_POINTER_FALLBACK_HINT}")
        time.sleep(0.1)


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
    """Release every key, mouse button and touch point held by the named session."""
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
        if session.held_touches:
            # A finger left down is as sticky as a key left down: the page keeps
            # a live touch in `event.touches` and every later gesture joins it.
            driver.execute_cdp_cmd(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchEnd",
                    "touchPoints": list(session.held_touches.values()),
                    "modifiers": 0,
                },
            )
            session.held_touches.clear()
        _auto_advance_render_after_input(session)
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "held_keys": [],
            "held_buttons": [],
            "held_touches": [],
        }


# The submit event is dispatched, and then the navigation it starts throws the
# whole window away - counter included. sessionStorage survives a same-origin
# load, and the token on the window says whether this is still the document the
# form was submitted from, which is the only way a POST back onto the same URL
# under the same title can be told apart from nothing happening at all.
#
# The listener goes on in the capture phase, because a framework that owns the
# form calls stopImmediatePropagation in its own handler: every listener added to
# the form after it was invisible, so a submit that had worked came back as one
# that never happened, and the caller sent it a second time.
#
# Nothing branded is left behind either. The key is this call's own token, read
# once and removed again, so a site that goes looking for an automation
# fingerprint finds a random string that is already gone; anything an earlier
# call could not clean up - its document was replaced before the read - is swept
# away here.
_SUBMIT_WATCH_SCRIPT = """
const form = arguments[0];
const token = arguments[1];
const state = {token: token, fired: 0, prevented: false};
window[token] = state;
try {
  for (const key of Object.keys(sessionStorage)) {
    if (key !== token && /^sf-[0-9]+$/.test(key)) sessionStorage.removeItem(key);
  }
} catch (error) { /* denied */ }
form.addEventListener('submit', (event) => {
  state.fired += 1;
  try { sessionStorage.setItem(token, '1'); } catch (error) { /* denied */ }
  // defaultPrevented is only final once every listener has run.
  queueMicrotask(() => { state.prevented = event.defaultPrevented; });
}, {capture: true, once: true});
// A form that delivers its result somewhere else leaves this document with
// nothing to say about what happened.
return String(form.target || '');
"""

_SUBMIT_RESULT_SCRIPT = """
const token = arguments[0];
const state = window[token];
let stored = false;
try {
  stored = sessionStorage.getItem(token) !== null;
  sessionStorage.removeItem(token);
} catch (error) { stored = false; }
try { delete window[token]; } catch (error) { window[token] = undefined; }
return {
  same_document: !!state && state.token === token,
  fired: !!state && state.fired > 0,
  prevented: !!state && !!state.prevented,
  stored: stored
};
"""


def _window_handle_count(driver: Any) -> int | None:
    """How many tabs this browser has open, or None when it will not say.

    The companion bridge drives one tab and keeps no list of the others, so a tab
    a submit opens there is named by the form's own target instead.
    """
    try:
        return len(driver.window_handles)
    except Exception:
        return None


def _same_origin(first: str, second: str) -> bool:
    left = urlsplit(first)
    right = urlsplit(second)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


def _submit_evidence(
    fired: bool, navigated: bool, cross_origin: bool, new_tab: bool
) -> str:
    """Say what the verdict rests on, so a missing signal does not read as doubt."""
    if fired and navigated:
        parts = ["the form's submit event fired and the document was replaced"]
    elif fired:
        parts = ["the form's submit event fired"]
    elif navigated:
        parts = ["the document was replaced by the submit"]
        if cross_origin:
            parts.append(
                "the submit event itself could not be read back, because the result "
                "is on another origin and nothing of this document survives a "
                "cross-origin load"
            )
    else:
        parts = ["no submit event was raised and no document was replaced"]
    if new_tab:
        parts.append(
            "the result opened in a new tab, so the url and title reported here are "
            "still this page's"
        )
    return "; ".join(parts)


def submit_form(
    form_selector: str,
    session_id: str = "default",
    submit_selector: str | None = None,
    wait_seconds: float = 0.5,
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Submit a form using requestSubmit so browser validation and events run.

    Whether the form actually went is decided by the document, not by its title: a
    search re-run lands on the very same URL under the very same heading, and a
    page that ticks a counter into its title changes it without submitting
    anything. The submit event is recorded where a navigation cannot erase it, and
    the document is tagged, so a load - even one back onto the same URL - is seen
    for what it is.

    ``submit_evidence`` says which of those two signals the verdict rests on, so a
    cross-origin result - where the event cannot be read back at all - is not left
    looking like a doubt. ``new_tab_opened`` says the result went somewhere else
    entirely, and that the url and title reported here are still this page's.

    ``frame_selector`` names the frame the form is looked up in, exactly as it
    does for ``find`` and ``page_text``; the submit button is looked for in that
    same document.
    """
    session = _get_session(session_id)
    token = f"sf-{time.monotonic_ns()}"
    form_target = ""
    tabs_before = None
    tabs_after = None
    with session.lock:
        # A form inside a frame is submitted from inside that frame, and so is the
        # submit button, which lives in the same document as the form.
        _enter_action_frame(
            session.driver, frame_selector, form_selector, submit_selector or ""
        )
        try:
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
            else:
                before_url = session.driver.current_url
                tabs_before = _window_handle_count(session.driver)
                form_target = str(
                    session.driver.execute_script(_SUBMIT_WATCH_SCRIPT, form, token) or ""
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
                try:
                    # Read from the form's own document, before the driver leaves it.
                    outcome = session.driver.execute_script(_SUBMIT_RESULT_SCRIPT, token) or {}
                except Exception:
                    # The document being watched is gone, which is itself the
                    # strongest evidence that the form navigated away.
                    outcome = {}
                tabs_after = _window_handle_count(session.driver)
        finally:
            _release_action_frame(session.driver, frame_selector, form_selector)
        if not validation["valid"]:
            return {
                **_page_summary(session.driver, session_id),
                "success": False,
                "validation_passed": False,
                "submit_triggered": False,
                "validation_errors": validation["invalid"],
                "submitted_form": form_selector,
                "frame_selector": frame_selector,
            }
        document_replaced = not bool(outcome.get("same_document"))
        submit_event_fired = bool(outcome.get("fired") or outcome.get("stored"))
        after_url = session.driver.current_url
        navigation_observed = document_replaced or before_url != after_url
        submit_triggered = submit_event_fired or navigation_observed
        prevented = bool(outcome.get("prevented"))
        # A result delivered to another tab leaves this document untouched, so the
        # url and title below describe the page the form was on and nothing that
        # happened to it. Selenium can count the tabs; the companion bridge cannot,
        # and there the form's own target is the evidence.
        opened_tab = (
            tabs_before is not None and tabs_after is not None and tabs_after > tabs_before
        ) or (
            submit_triggered and not prevented and form_target.lower() == "_blank"
        )
        return {
            **_page_summary(session.driver, session_id),
            "success": bool(validation["valid"] and submit_triggered),
            "validation_passed": bool(validation["valid"]),
            "submit_triggered": submit_triggered,
            "submit_event_fired": submit_event_fired,
            "submit_default_prevented": prevented,
            "submit_evidence": _submit_evidence(
                submit_event_fired,
                navigation_observed,
                not _same_origin(before_url, after_url),
                opened_tab,
            ),
            "navigation_observed": navigation_observed,
            "new_tab_opened": bool(opened_tab),
            "url_before": before_url,
            "validation_errors": [],
            "submitted_form": form_selector,
            "frame_selector": frame_selector,
        }


_MAX_SCREENSHOT_WIDTH = 3840
_MAX_SCREENSHOT_HEIGHT = 10_000


def _screenshot_size_pair(width: int | None, height: int | None) -> bool:
    if (width is None) != (height is None):
        raise ValueError("width and height must be provided together")
    return width is not None


def _capture_png(driver: Any, clip: dict[str, float]) -> bytes:
    capture = driver.execute_cdp_cmd(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {**clip, "scale": 1},
        },
    )
    return base64.b64decode(capture["data"])


def screenshot(
    session_id: str = "default",
    width: int | None = None,
    height: int | None = None,
    full_page: bool = False,
    mode: str | None = None,
    x: float | None = None,
    y: float | None = None,
) -> bytes:
    """Capture a viewport, full document, or exact page region as PNG bytes.

    ``full_page`` remains the compatibility alias for ``mode='full_page'``.
    Omitting width and height preserves the viewport exactly as it is. An
    explicit viewport size is supported by server-owned/attached Selenium Chrome,
    but never resizes or emulates the user's personal companion Chrome. Region
    coordinates are page CSS pixels and capture without changing page layout.
    """
    selected_mode = str(mode or ("full_page" if full_page else "viewport")).strip().lower()
    if selected_mode not in {"viewport", "full_page", "region"}:
        raise ValueError("mode must be 'viewport', 'full_page', or 'region'")
    if full_page and selected_mode != "full_page":
        raise ValueError("full_page=true conflicts with mode; omit it or use mode='full_page'")
    has_size = _screenshot_size_pair(width, height)
    if selected_mode != "region" and (x is not None or y is not None):
        raise ValueError("x and y are only accepted with mode='region'")
    if selected_mode == "region" and (x is None or y is None or not has_size):
        raise ValueError("mode='region' requires x, y, width, and height")

    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        is_current = bool(getattr(driver, "is_extension_bridge", False))

        if selected_mode == "viewport":
            if has_size:
                if is_current:
                    raise ValueError(
                        "An explicit viewport size cannot be applied in profile_mode='current': "
                        "Web Search Neo preserves the user's Chrome window. Omit width/height "
                        "for its actual viewport, or use mode='region' for an exact-size crop."
                    )
                bounded_width, bounded_height = _bounded_size(int(width), int(height))
                _set_viewport(driver, bounded_width, bounded_height)
            return driver.get_screenshot_as_png()

        if selected_mode == "region":
            region_width = int(width)
            region_height = int(height)
            if not 1 <= region_width <= _MAX_SCREENSHOT_WIDTH:
                raise ValueError(
                    f"region width must be 1-{_MAX_SCREENSHOT_WIDTH} CSS pixels"
                )
            if not 1 <= region_height <= _MAX_SCREENSHOT_HEIGHT:
                raise ValueError(
                    f"region height must be 1-{_MAX_SCREENSHOT_HEIGHT} CSS pixels"
                )
            region_x = float(x)
            region_y = float(y)
            if region_x < 0 or region_y < 0:
                raise ValueError("region x and y must be non-negative page coordinates")
            return _capture_png(
                driver,
                {
                    "x": region_x,
                    "y": region_y,
                    "width": float(region_width),
                    "height": float(region_height),
                },
            )

        # The old full_page call accepted width/height as the layout viewport used
        # before measuring the document, so keep that behaviour for Selenium. It
        # was silently ignored in current Chrome; saying so is safer than returning
        # an image whose requested dimensions mean nothing.
        if has_size:
            if is_current:
                raise ValueError(
                    "width/height cannot change full-page layout in profile_mode='current'; "
                    "omit them to capture the current layout"
                )
            bounded_width, bounded_height = _bounded_size(int(width), int(height))
            _set_viewport(driver, bounded_width, bounded_height)
        metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
        size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        page_width = float(size.get("width") or 0)
        page_height = float(size.get("height") or 0)
        if page_width <= 0 or page_height <= 0:
            raise RuntimeError("Chrome returned no document size for the full-page screenshot")
        if page_width > _MAX_SCREENSHOT_WIDTH or page_height > _MAX_SCREENSHOT_HEIGHT:
            raise ValueError(
                f"The full page is {page_width:g}x{page_height:g} CSS pixels, above the "
                f"safe {_MAX_SCREENSHOT_WIDTH}x{_MAX_SCREENSHOT_HEIGHT} screenshot limit. "
                "Use mode='region' to capture it in explicit pieces; no partial image was returned."
            )
        return _capture_png(
            driver,
            {"x": 0, "y": 0, "width": page_width, "height": page_height},
        )


def show_session(session_id: str = "default") -> dict[str, Any]:
    """Explicitly put a session in front without changing its window state.

    This is the sole browser-tools operation that may request foreground focus.
    All ordinary automation remains background-safe; callers must opt into the
    interruption by naming the ``show`` action.
    """
    session = _get_session(session_id)
    with session.lock:
        driver = session.driver
        if session.profile_mode == "current":
            # ChromeBridgeDriver.activate_tab is the audited bridge path that
            # activates the tab and focuses its containing window. It does not
            # request minimized/maximized/restored state.
            driver.activate_tab()
            focus_method = "tabs.activate"
        else:
            # CDP's page-level foreground request works for Selenium-owned and
            # debugger-attached browsers without a window-state mutation API.
            driver.execute_cdp_cmd("Page.bringToFront", {})
            focus_method = "Page.bringToFront"
        return {
            **_page_summary(driver, session_id),
            "success": True,
            "focus_requested": True,
            "focus_method": focus_method,
            "warning": (
                "Foreground focus was explicitly requested and may interrupt the user's "
                "current browser or OS focus. No minimize, maximize, restore, resize, or "
                "other window-state request was sent."
            ),
        }


def get_status(session_id: str = "default") -> dict[str, Any]:
    """Return browser availability and current session state."""
    session_id = _validate_session_id(session_id)
    with _sessions_lock:
        session = _sessions.get(session_id)
        active_ids = sorted(_sessions)
    if session is not None and _browser_run_changed(session) is not None:
        # The same question every action asks, and it has to be asked here too:
        # without it status summarised whatever tab now carries this session's
        # id - one of the user's, in a browser run that never heard of us - and
        # answered `session_open: true` over the top of it.
        _discard_stale_session(session_id, session)
        with _sessions_lock:
            active_ids = sorted(_sessions)
        bridge_status = _companion_status()
        return {
            "available": bool(bridge_status["connected"] or _browser_available),
            "availability_error": None,
            "session_open": False,
            "session_id": session_id,
            "active_sessions": active_ids,
            "engine": "Chrome companion extension",
            "browser_gone": True,
            "next": (
                f"The Chrome session '{session_id}' was opened in is no longer "
                "running - it was restarted, or the companion updated itself - so "
                "its tab no longer exists and its id now names a different tab. "
                "The session has been dropped rather than reported on. Open the "
                f'page again: web_action [{{"action":"open","url":...,'
                f'"session_id":"{session_id}"}}].'
            ),
            "current_chrome": bridge_status,
        }
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
    selector: str,
    file_paths: list[str],
    session_id: str = "default",
    frame_selector: str | None = None,
) -> dict[str, Any]:
    """Attach local files to an input[type=file], replacing whatever it held.

    Chrome appends to an input that accepts several files, so the input is
    cleared first: afterwards it holds exactly ``file_paths``. ``files_uploaded``
    maps the selector to the names the input actually reports holding - the same
    shape ``fill`` returns - rather than to what this call asked for.

    ``frame_selector`` names the frame the selector is looked up in, exactly as it
    does for ``find`` and ``page_text``.
    """
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
        _enter_action_frame(session.driver, frame_selector, selector)
        try:
            element = _resolve_element(session.driver, selector)
            attached = _attach_files(session.driver, element, resolved)
        finally:
            _release_action_frame(session.driver, frame_selector, selector)
        return {
            **_page_summary(session.driver, session_id),
            "success": len(attached) == len(resolved),
            "selector": selector,
            "files_uploaded": {selector: attached},
            "file_names": attached,
            "frame_selector": frame_selector,
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
        if session.held_touches:
            driver.execute_cdp_cmd(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchEnd",
                    "touchPoints": list(session.held_touches.values()),
                    "modifiers": 0,
                },
            )
            session.held_touches.clear()
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


def _shutdown_session(
    session: BrowserSession, close_tab: bool | None = None
) -> dict[str, Any]:
    """Release one session's tab and browser; report what could not be released.

    Returns ``{"tab_closed": bool, "browser_gone": bool, "problem": str | None}``.

    Cleanup must never raise - a failed teardown may not take the caller down with
    it - but it must not go quiet either. A debugger left attached keeps the "is
    being debugged" banner on a tab the user has been given back, and only they
    can clear it, so the caller is told rather than reassured.

    Each step is attempted on its own: one failure used to skip the two after it,
    which turned a hiccup while releasing held keys into a leaked tab as well.

    The first question is the one ``_get_session`` asks before every action, and
    for the same reason: teardown sends more to Chrome than any other path, so a
    session whose browser has been replaced must be forgotten in silence rather
    than have ``tabs.remove`` aimed at whichever of the user's tabs inherited
    its number.
    """
    if _browser_run_changed(session) is not None:
        # Exactly what _discard_stale_session does, and for the reason its
        # docstring gives: nothing is sent to Chrome and the claim is not
        # released either, because the daemon dropped the whole registry when the
        # run changed and a release aimed at the old id could only hit a claim
        # made since, in the new run.
        logger.info(
            "Session on tab %s was left alone: the Chrome it was opened in is gone",
            session.current_tab_id,
        )
        return {
            "tab_closed": False,
            "browser_gone": True,
            "problem": None,
        }

    problems: list[str] = []

    def attempt(step: str, action: Any) -> Any:
        try:
            return action()
        except Exception as exc:
            problems.append(f"{step} failed ({type(exc).__name__}: {exc})")
            logger.warning(
                "Browser session cleanup: %s failed: %s: %s", step, type(exc).__name__, exc
            )
            return None

    tab_closed = False
    should_close_tab = session.owns_tab if close_tab is None else bool(close_tab)
    attempt("releasing held input", lambda: _reset_session_runtime_state(session))
    if should_close_tab and hasattr(session.driver, "close_tab"):
        removed = attempt("closing the tab", session.driver.close_tab)
        tab_closed = bool((removed or {}).get("removed"))
        if not tab_closed:
            # `removed: false` is the extension's answer whenever
            # `chrome.tabs.remove` throws, and a tab the user closed by hand is
            # the commonest way to make it throw - so it does not mean the tab is
            # still open, and reporting a leak on it made the two most ordinary
            # teardowns name one that does not exist. Ask.
            still_there = _tab_still_exists(session)
            if still_there is True:
                problems.append("the tab is still open")
            elif still_there is None:
                problems.append(
                    "the tab could not be closed and the companion could not be "
                    "asked whether it is still open"
                )
    if session.owns_browser:
        # ChromeBridgeDriver.quit cannot raise - teardown may not - so `attempt`
        # can never see a failed detach and the banner would be left on the
        # user's tab unmentioned. The outcome comes back in the answer instead.
        detached = attempt("detaching from Chrome", session.driver.quit)
        detach_error = (detached or {}).get("error") if isinstance(detached, dict) else None
        if detach_error:
            problems.append(
                f"the debugger may still be attached to tab {session.current_tab_id} "
                f"({detach_error})"
            )
    else:
        attempt("stopping the driver service", session.driver.service.stop)
    if session.profile_mode == "current":
        # Let another agent have the tab even if the teardown above went badly:
        # a claim outliving the session that made it is a tab nobody can use.
        _release_claimed_tab(session.current_tab_id)
    return {
        "tab_closed": tab_closed,
        "browser_gone": False,
        "problem": "; ".join(problems) or None,
    }


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
    outcome: dict[str, Any] = {"tab_closed": False, "browser_gone": False, "problem": None}
    if session is not None:
        with session.lock:
            outcome = _shutdown_session(session, close_tab)
    problem = outcome["problem"]
    return {
        "session_id": session_id,
        "closed": closed,
        "tab_closed": bool(outcome["tab_closed"]),
        "active_sessions": remaining,
        # Closing something that is not there is a no-op rather than a failure,
        # but saying nothing lets a typo in a session id read as a clean close.
        **(
            {}
            if closed
            else {"note": f"No session named '{session_id}' was open, so nothing was closed."}
        ),
        # Nor may "the browser it was opened in is gone" read as a clean close:
        # no tab was closed because none of ours was there to close.
        **(
            {
                "browser_gone": True,
                "note": (
                    f"The Chrome session '{session_id}' was opened in is no longer "
                    "running, so nothing was sent to the browser: the session's tab "
                    "id now names a different tab. The session has been dropped."
                ),
            }
            if outcome["browser_gone"]
            else {}
        ),
        **({"released": False, "warning": problem} if problem else {}),
    }


def close_all_sessions() -> dict[str, Any]:
    """Close every session, including the Chrome tabs the server itself opened.

    Reports what would not release, so ``close_all`` can name a leaked tab
    instead of answering a flat "closed_all: true" over the top of it - and names
    the sessions whose browser was already gone, which release cleanly precisely
    because nothing of ours was left to release.
    """
    with _sessions_lock:
        sessions = sorted(_sessions.items())
        _sessions.clear()
    tabs_closed = 0
    problems: dict[str, str] = {}
    browsers_gone: list[str] = []
    for session_id, session in sessions:
        with session.lock:
            outcome = _shutdown_session(session)
        tabs_closed += int(bool(outcome["tab_closed"]))
        if outcome["browser_gone"]:
            browsers_gone.append(session_id)
        if outcome["problem"]:
            problems[session_id] = outcome["problem"]
    return {
        "closed_all": not problems,
        "closed_sessions": [session_id for session_id, _ in sessions],
        "tabs_closed": tabs_closed,
        "active_sessions": [],
        **({"warnings": problems} if problems else {}),
        **(
            {
                "browser_gone": browsers_gone,
                "note": (
                    "The Chrome these sessions were opened in is no longer running, "
                    "so nothing was sent to the browser: their tab ids now name "
                    f"different tabs. Left alone: {browsers_gone}."
                ),
            }
            if browsers_gone
            else {}
        ),
    }


def start_current_chrome_bridge() -> dict[str, Any]:
    """Link to the bridge daemon early, starting one if needed, so no action waits on it."""
    bridge = get_chrome_bridge()
    bridge.start()
    return bridge.status(0.0)


atexit.register(close_all_sessions)
