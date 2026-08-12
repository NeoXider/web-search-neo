"""Client of the bridge daemon, and the WebDriver-compatible adapter above it.

The listener that the Chrome companion dials lives in its own process now (see
``bridge_daemon``), so what this module holds is a client of it: ``start()``
means "make sure a daemon is reachable, spawning one if it is not", and
``request()`` is relayed through that daemon to the extension. The public surface
is unchanged, because ``browser_tools`` calls it from everywhere.
"""

from __future__ import annotations

import atexit
import base64
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException

import bridge_auth
import bridge_daemon
from bridge_daemon import (
    CHROME_EXTENSION_ID,
    DEFAULT_HOST,
    MAX_FRAME_BYTES,
    PROTOCOL,
    TOKEN_MISMATCH_REASON,
    close_quietly,
)
from key_table import MODIFIER_BITS, resolve_key


class ChromeBridgeError(RuntimeError):
    """Raised when the companion extension isn't connected or rejects a command."""


LOGGER = logging.getLogger("web_search_neo.bridge")
PROJECT_DIR = Path(__file__).resolve().parent
DAEMON_ENTRY = PROJECT_DIR / "main.py"

# Two daemons of the wrong version in a row means another checkout is fighting us
# over the port, and replacing each other forever would be worse than saying so.
MAX_DAEMON_REPLACEMENTS = 2

__all__ = [
    "CHROME_EXTENSION_ID",
    "TOKEN_MISMATCH_REASON",
    "ChromeBridge",
    "ChromeBridgeDriver",
    "ChromeBridgeElement",
    "ChromeBridgeError",
    "get_chrome_bridge",
    "list_current_chrome_tabs",
    "spawn_bridge_daemon",
]


class _VersionConflict(RuntimeError):
    """The daemon on the port runs other code and would not step aside."""


@dataclass
class _PendingRequest:
    event: threading.Event
    connection: Any = None
    result: Any = None
    error: str | None = None


def spawn_bridge_daemon(port: int) -> None:
    """Start the bridge daemon detached, so it outlives the process that needs it.

    Detaching matters twice over: the daemon must survive this MCP server, and it
    must not inherit its stdio, which is the MCP transport itself.
    """
    if not sys.executable:
        LOGGER.warning("No interpreter to start the bridge daemon with")
        return
    command = [sys.executable, str(DAEMON_ENTRY), "--bridge"]
    environment = dict(os.environ, WEB_SEARCH_NEO_BRIDGE_PORT=str(port))
    detach: dict[str, Any] = {}
    if os.name == "nt":
        detach["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    else:
        detach["start_new_session"] = True
    try:
        subprocess.Popen(  # noqa: S603 - fixed command, no shell, no caller input
            command,
            cwd=str(PROJECT_DIR),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **detach,
        )
    except OSError as exc:
        LOGGER.warning("Could not start the bridge daemon: %s: %s", type(exc).__name__, exc)


def _autospawn_enabled() -> bool:
    return os.getenv("WEB_SEARCH_NEO_BRIDGE_AUTOSPAWN", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _env_seconds(name: str, fallback: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, "")))
    except ValueError:
        return fallback


def _package_version() -> str:
    """The server version, read late: main defines it after it imports us."""
    try:
        import main

        return str(main.__version__)
    except Exception:
        return ""


class ChromeBridge:
    """Client of the bridge daemon that owns the port the Chrome companion dials.

    ``start()`` connects to that daemon and starts one if nobody answers;
    everything above this class keeps calling ``request``, ``status``,
    ``wait_connected`` and ``connected`` exactly as before. What the class no
    longer does is listen: the daemon outlives this process, which is what keeps
    the companion's badge on between agent calls and lets two MCP servers share
    one browser.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int | None = None,
        token: str | None = None,
        *,
        version: str | None = None,
        spawn: bool | Callable[[int], None] | None = None,
        connect_timeout: float | None = None,
        start_timeout: float | None = None,
    ) -> None:
        self.host = host
        self.port = int(port or bridge_daemon.bridge_port())
        # An explicit token belongs to the caller (tests); only the default
        # instance owns the on-disk secret and the extension's copy of it.
        self._token: str | None = token
        self._owns_token = token is None
        self._version = version
        self._spawn = self._resolve_spawn(spawn)
        self._connect_timeout = float(
            connect_timeout if connect_timeout is not None else _env_seconds(
                "WEB_SEARCH_NEO_BRIDGE_CONNECT_TIMEOUT", 12.0
            )
        )
        self._start_timeout = float(
            start_timeout if start_timeout is not None else _env_seconds(
                "WEB_SEARCH_NEO_BRIDGE_START_TIMEOUT", 2.0
            )
        )
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        self._daemon: Any = None
        self._daemon_version = ""
        self._daemon_pid: int | None = None
        self._browser_info: dict[str, Any] = {}
        self._connected = threading.Event()
        self._attempted = threading.Event()
        self._wake = threading.Event()
        self._closing = False
        self._thread: threading.Thread | None = None
        self._startup_error: str | None = None
        self._fatal_error: str | None = None

    @staticmethod
    def _resolve_spawn(
        spawn: bool | Callable[[int], None] | None,
    ) -> Callable[[int], None] | None:
        if callable(spawn):
            return spawn
        if spawn is False:
            return None
        if spawn is None and not _autospawn_enabled():
            return None
        return spawn_bridge_daemon

    def _expected_version(self) -> str:
        if self._version is None:
            self._version = _package_version()
        return self._version

    def _ensure_token(self) -> str:
        """Return the shared secret, publishing it to the extension when we own it."""
        with self._state_lock:
            if self._token is None:
                self._token = bridge_auth.load_or_create_token()
            token = self._token
            owns = self._owns_token
        if owns:
            try:
                bridge_auth.write_extension_token(token)
            except OSError as exc:
                # A read-only checkout still works if the companion copy is current.
                LOGGER.warning(
                    "Could not refresh the companion token file: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        return token

    # -- link to the daemon ------------------------------------------------

    def start(self) -> None:
        """Make sure a daemon is reachable, without blocking longer than it takes."""
        with self._state_lock:
            if self._fatal_error:
                return
            if self._thread is None or not self._thread.is_alive():
                self._closing = False
                self._wake.clear()
                self._attempted.clear()
                self._thread = threading.Thread(
                    target=self._maintain_link,
                    name="web-search-neo-bridge-client",
                    daemon=True,
                )
                self._thread.start()
        # The link keeps forming in the background: a caller that needs the
        # browser waits on wait_connected, not on this.
        self._attempted.wait(timeout=max(0.0, self._start_timeout))

    def _maintain_link(self) -> None:
        delay = 0.0
        while not self._closing:
            if delay and self._wake.wait(delay):
                break
            try:
                connection = self._establish()
            except _VersionConflict as exc:
                # Latched on purpose: retrying would restart the very tug-of-war
                # over the port that this reports.
                with self._state_lock:
                    self._fatal_error = str(exc)
                    self._startup_error = str(exc)
                LOGGER.error("%s", exc)
                self._attempted.set()
                return
            except Exception as exc:
                self._startup_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("No bridge daemon on %s:%s: %s", self.host, self.port, exc)
                self._attempted.set()
                delay = min(max(delay * 2, 0.5), 5.0)
                continue
            self._startup_error = None
            delay = 0.5
            self._attempted.set()
            self._read_until_closed(connection)
        self._attempted.set()

    def _establish(self) -> Any:
        """Connect to the daemon, starting or replacing one when that is needed."""
        # The generous budget is there to cover a daemon we start ourselves; with
        # nobody allowed to start one, waiting that long only delays the answer.
        budget = self._connect_timeout if self._spawn is not None else min(
            self._connect_timeout, 1.0
        )
        deadline = time.monotonic() + budget
        spawned = False
        replacements = 0
        retired: set[Any] = set()
        while True:
            if self._closing:
                raise ChromeBridgeError("The bridge client is shutting down")
            try:
                connection = self._dial()
            except OSError:
                if not spawned and self._spawn is not None:
                    LOGGER.info(
                        "Nothing is listening on %s:%s; starting the bridge daemon",
                        self.host,
                        self.port,
                    )
                    self._spawn(self.port)
                    spawned = True
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.15)
                continue
            acknowledgement = self._handshake(connection)
            daemon_version = str(acknowledgement.get("version") or "")
            expected = self._expected_version()
            if not expected or not daemon_version or daemon_version == expected:
                self._remember_daemon(acknowledgement)
                try:
                    self._refresh_state(connection)
                except Exception:
                    with self._state_lock:
                        if self._daemon is connection:
                            self._daemon = None
                    close_quietly(connection, 1000, "The daemon handshake did not finish")
                    raise
                return connection
            pid = acknowledgement.get("pid")
            identity = acknowledgement.get("instance") or pid
            if identity in retired:
                # The daemon we told to leave has not finished dying yet.
                close_quietly(connection, 1000, "Waiting for the replaced daemon to exit")
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Bridge daemon {pid} did not exit after it was asked to"
                    )
                time.sleep(0.2)
                continue
            replacements += 1
            if replacements > MAX_DAEMON_REPLACEMENTS:
                close_quietly(connection, 1000, "Version mismatch")
                raise _VersionConflict(
                    f"The bridge daemon on {self.host}:{self.port} reports version "
                    f"{daemon_version}, but this server is {expected}, and replacing it "
                    "did not help. Another checkout of Web Search Neo is running against "
                    "the same port; stop it, then restart this server."
                )
            LOGGER.info(
                "Bridge daemon %s (pid %s) predates this server (%s); replacing it",
                daemon_version,
                pid,
                expected,
            )
            self._retire(connection, expected)
            retired.add(identity)
            # The port is about to be free, so the next refusal is ours to answer.
            spawned = False

    def _dial(self) -> Any:
        from websockets.sync.client import connect

        return connect(
            f"ws://{self.host}:{self.port}",
            open_timeout=5.0,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_FRAME_BYTES,
        )

    def _handshake(self, connection: Any) -> dict[str, Any]:
        """Prove we hold the machine secret, and make the daemon prove it too."""
        token = self._ensure_token()
        nonce = secrets.token_hex(16)
        connection.send(
            json.dumps(
                {
                    "type": "hello",
                    "protocol": PROTOCOL,
                    "role": "client",
                    "token": token,
                    "nonce": nonce,
                    "version": self._expected_version(),
                    "client": {
                        "pid": os.getpid(),
                        "program": Path(sys.argv[0] or "python").name,
                    },
                }
            )
        )
        try:
            acknowledgement = json.loads(connection.recv(timeout=5.0))
        except (TypeError, ValueError) as exc:
            close_quietly(connection, 1008, "Bridge daemon hello_ack must be JSON")
            raise ChromeBridgeError(f"The bridge daemon answered with junk: {exc}") from exc
        if not isinstance(acknowledgement, dict) or acknowledgement.get("type") != "hello_ack":
            close_quietly(connection, 1008, "Expected a bridge daemon hello_ack")
            raise ChromeBridgeError("The peer on the bridge port is not a bridge daemon")
        if not bridge_auth.verify(token, nonce, acknowledgement.get("proof")):
            close_quietly(connection, 1008, TOKEN_MISMATCH_REASON)
            raise ChromeBridgeError(
                "The peer on the bridge port did not prove it knows the companion token"
            )
        return acknowledgement

    def _remember_daemon(self, acknowledgement: dict[str, Any]) -> None:
        with self._state_lock:
            self._daemon_version = str(acknowledgement.get("version") or "")
            self._daemon_pid = acknowledgement.get("pid")

    def _retire(self, connection: Any, expected: str) -> None:
        try:
            connection.send(
                json.dumps(
                    {
                        "type": "control",
                        "id": uuid.uuid4().hex,
                        "method": "shutdown",
                        "reason": f"replaced by version {expected}",
                    }
                )
            )
            # The daemon acks before it stops; reading it also lets the close
            # frame arrive before we dial the port again.
            connection.recv(timeout=5.0)
        except Exception as exc:
            LOGGER.warning(
                "The outdated bridge daemon did not answer the stop request: %s: %s",
                type(exc).__name__,
                exc,
            )
        close_quietly(connection, 1000, "Replaced by a newer server")

    def _refresh_state(self, connection: Any) -> None:
        """Ask the fresh link what the browser is doing before anyone calls status."""
        request_id = uuid.uuid4().hex
        pending = _PendingRequest(event=threading.Event(), connection=connection)
        with self._state_lock:
            self._pending[request_id] = pending
            self._daemon = connection
        try:
            connection.send(
                json.dumps({"type": "control", "id": request_id, "method": "status"})
            )
            deadline = time.monotonic() + 5.0
            while not pending.event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("The bridge daemon did not report the companion state")
                frame = json.loads(connection.recv(timeout=remaining))
                if isinstance(frame, dict):
                    self._dispatch(frame)
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        if pending.error:
            raise ChromeBridgeError(str(pending.error))
        self._apply_state(pending.result or {})

    def _read_until_closed(self, connection: Any) -> None:
        try:
            for raw_message in connection:
                try:
                    frame = json.loads(raw_message)
                except (TypeError, ValueError):
                    LOGGER.warning("The bridge daemon sent a frame that is not JSON")
                    continue
                if isinstance(frame, dict):
                    self._dispatch(frame)
        except Exception as exc:
            LOGGER.info("Bridge daemon link ended: %s: %s", type(exc).__name__, exc)
        finally:
            with self._state_lock:
                if self._daemon is connection:
                    self._daemon = None
                    self._browser_info = {}
                    self._connected.clear()
            # A caller blocked on an answer that can no longer come must be told
            # so, rather than sit out its whole timeout.
            self._fail_pending(
                "The bridge daemon stopped while the command was in flight", connection
            )

    def _dispatch(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind in {"result", "control_result"}:
            request_id = str(frame.get("id", ""))
            with self._state_lock:
                pending = self._pending.get(request_id)
            if pending is None:
                LOGGER.warning("Dropped a late bridge result for id %s", request_id)
                return
            pending.result = frame.get("result")
            pending.error = frame.get("error")
            pending.event.set()
        elif kind == "extension":
            self._apply_state(frame)
        elif kind == "pong":
            return
        else:
            LOGGER.warning("Ignored unknown bridge daemon frame type %r", kind)

    def _apply_state(self, state: dict[str, Any]) -> None:
        with self._state_lock:
            self._browser_info = dict(state.get("browser") or {})
            if state.get("version") is not None:
                self._daemon_version = str(state.get("version") or "")
            if state.get("pid") is not None:
                self._daemon_pid = state.get("pid")
            if state.get("connected"):
                self._connected.set()
            else:
                self._connected.clear()

    def _fail_pending(self, message: str, connection: Any | None = None) -> None:
        with self._state_lock:
            pending_items = [
                item
                for item in self._pending.values()
                if connection is None or item.connection is connection
            ]
        for pending in pending_items:
            pending.error = pending.error or message
            pending.event.set()

    # -- public surface ----------------------------------------------------

    def wait_connected(self, timeout: float = 0.0) -> bool:
        self.start()
        if self._fatal_error:
            return False
        return self._connected.wait(max(0.0, timeout))

    @property
    def connected(self) -> bool:
        return self.wait_connected(0.0)

    @property
    def startup_error(self) -> str | None:
        self.start()
        return self._startup_error

    @property
    def browser_info(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._browser_info)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Any:
        if not self.wait_connected(min(max(timeout, 0.0), 3.0)):
            detail = f" ({self._startup_error})" if self._startup_error else ""
            # Telling a caller to click through chrome://extensions is useless to
            # an agent: name the two calls it can actually make instead.
            raise ChromeBridgeError(
                "Chrome companion extension is not connected, so profile_mode "
                f"'current' cannot be used{detail}. Either retry with "
                '{"action": "open", "url": "...", "profile_mode": "temporary"}, '
                'which needs no extension, or send {"action": '
                '"setup_current_chrome"}, which returns the steps to install it.'
            )
        request_id = uuid.uuid4().hex
        with self._state_lock:
            connection = self._daemon
            pending = _PendingRequest(event=threading.Event(), connection=connection)
            self._pending[request_id] = pending
        if connection is None:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise ChromeBridgeError("Chrome companion extension disconnected")
        try:
            payload = json.dumps(
                {
                    "type": "command",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                },
                ensure_ascii=False,
            )
            try:
                with self._send_lock:
                    connection.send(payload)
            except Exception as exc:
                # The link can drop between the check above and this send, and a
                # raw websocket error here would reach the agent as a stack trace.
                raise ChromeBridgeError(
                    f"The bridge daemon link dropped while sending '{method}': {exc}"
                ) from exc
            if not pending.event.wait(max(0.1, timeout)):
                raise TimeoutError(f"Chrome bridge command '{method}' timed out")
            if pending.error:
                raise ChromeBridgeError(pending.error)
            return pending.result
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def status(self, wait_seconds: float = 0.0) -> dict[str, Any]:
        connected = self.wait_connected(wait_seconds)
        with self._state_lock:
            daemon = {
                "linked": self._daemon is not None,
                "version": self._daemon_version or None,
                "pid": self._daemon_pid,
            }
        return {
            "connected": connected,
            "host": self.host,
            "port": self.port,
            "startup_error": self._startup_error,
            "browser": self.browser_info,
            "daemon": daemon,
        }

    def stop_daemon(self, reason: str = "requested") -> bool:
        """Ask the daemon to exit. Only a caller replacing it should want this."""
        self.start()
        with self._state_lock:
            connection = self._daemon
        if connection is None:
            return False
        self._closing = True
        self._wake.set()
        request_id = uuid.uuid4().hex
        pending = _PendingRequest(event=threading.Event(), connection=connection)
        with self._state_lock:
            self._pending[request_id] = pending
        try:
            with self._send_lock:
                connection.send(
                    json.dumps(
                        {
                            "type": "control",
                            "id": request_id,
                            "method": "shutdown",
                            "reason": reason,
                        }
                    )
                )
            return pending.event.wait(5.0) and not pending.error
        except Exception as exc:
            LOGGER.warning(
                "Could not ask the bridge daemon to stop: %s: %s", type(exc).__name__, exc
            )
            return False
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def shutdown(self) -> None:
        """Let go of the daemon. The daemon itself stays up, which is the point."""
        self._closing = True
        self._wake.set()
        with self._state_lock:
            connection = self._daemon
            thread = self._thread
        if connection is not None:
            close_quietly(connection, 1000, "The MCP server is going away")
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)


_bridge = ChromeBridge()


def get_chrome_bridge() -> ChromeBridge:
    return _bridge


class _BridgeService:
    def stop(self) -> None:
        return None


class ChromeBridgeElement:
    """Small Selenium WebElement subset backed by a stable CSS selector."""

    def __init__(self, driver: "ChromeBridgeDriver", selector: str) -> None:
        self.parent = driver
        self.selector = selector

    @property
    def tag_name(self) -> str:
        return str(
            self.parent.execute_script(
                "return arguments[0].tagName.toLowerCase();", self
            )
        )

    def get_attribute(self, name: str) -> Any:
        return self.parent.execute_script(
            "const el=arguments[0], name=arguments[1];"
            "if (name === 'value') return el.value;"
            "if (name === 'checked') return el.checked;"
            "return el.getAttribute(name);",
            self,
            name,
        )

    def is_selected(self) -> bool:
        return bool(self.parent.execute_script("return !!arguments[0].checked;", self))

    def is_displayed(self) -> bool:
        return bool(
            self.parent.execute_script(
                "const el=arguments[0], r=el.getBoundingClientRect(), s=getComputedStyle(el);"
                "return !!(r.width && r.height && s.display !== 'none' && "
                "s.visibility !== 'hidden' && s.opacity !== '0');",
                self,
            )
        )

    def is_enabled(self) -> bool:
        return not bool(
            self.parent.execute_script(
                "return !!(arguments[0].disabled || arguments[0].getAttribute('aria-disabled') === 'true');",
                self,
            )
        )

    def click(self) -> None:
        point = self.parent.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'});"
            "const r=arguments[0].getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};",
            self,
        )
        for event_type, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)):
            self.parent.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": point["x"],
                    "y": point["y"],
                    "button": "left" if event_type != "mouseMoved" else "none",
                    "buttons": buttons,
                    "clickCount": 1,
                },
            )

    def clear(self) -> None:
        self.parent.execute_script(
            "const el=arguments[0]; el.focus();"
            "if (el.isContentEditable) el.textContent=''; else el.value='';"
            "el.dispatchEvent(new Event('input',{bubbles:true}));",
            self,
        )

    def send_keys(self, value: str) -> None:
        text = str(value)
        input_type = str(self.get_attribute("type") or "").lower()
        if input_type == "file":
            self.parent.set_file_input_files(self.selector, text.splitlines())
            return
        self.parent.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'}); arguments[0].focus();",
            self,
        )
        self.parent.execute_cdp_cmd("Input.insertText", {"text": text})


class _BridgeSwitchTo:
    def __init__(self, driver: "ChromeBridgeDriver") -> None:
        self.driver = driver

    def default_content(self) -> None:
        self.driver._frame_session_id = None
        self.driver._same_origin_frame_selector = None

    def frame(self, frame: ChromeBridgeElement) -> None:
        resolved = self.driver.bridge.request(
            "frames.resolve",
            {"tabId": self.driver.tab_id, "selector": frame.selector},
            timeout=10.0,
        )
        self.driver._frame_session_id = resolved.get("sessionId")
        self.driver._same_origin_frame_selector = (
            frame.selector if resolved.get("sameOrigin") else None
        )


class ChromeBridgeDriver:
    """WebDriver-shaped adapter that controls an already-open Chrome via extension."""

    is_extension_bridge = True

    def __init__(
        self,
        bridge: ChromeBridge | None = None,
        tab_id: int | None = None,
        tab_group: str = "AI",
    ) -> None:
        self.bridge = bridge or get_chrome_bridge()
        self.tab_group = tab_group.strip() or "AI"
        self._script_timeout = 15.0
        self._page_load_timeout = 30.0
        self._frame_session_id: str | None = None
        self._same_origin_frame_selector: str | None = None
        self.service = _BridgeService()
        self._modifier_mask = 0
        if tab_id is None:
            tab = self.bridge.request(
                "tabs.create", {"url": "about:blank", "group": self.tab_group}, timeout=10.0
            )
        else:
            tab = self.bridge.request("tabs.get", {"tabId": int(tab_id)}, timeout=10.0)
            tab = self.bridge.request("tabs.activate", {"tabId": int(tab_id)}, timeout=10.0)
        self.tab_id = int(tab["id"])
        self.actual_tab_group = tab.get("group")

    @property
    def switch_to(self) -> _BridgeSwitchTo:
        return _BridgeSwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(self.bridge.request("tabs.get", {"tabId": self.tab_id})["url"])

    @property
    def title(self) -> str:
        return str(self.bridge.request("tabs.get", {"tabId": self.tab_id}).get("title") or "")

    def set_page_load_timeout(self, seconds: float) -> None:
        self._page_load_timeout = max(1.0, float(seconds))

    def set_script_timeout(self, seconds: float) -> None:
        self._script_timeout = max(1.0, float(seconds))

    def get(self, url: str) -> None:
        self.switch_to.default_content()
        self.bridge.request(
            "tabs.navigate",
            {"tabId": self.tab_id, "url": url},
            timeout=self._page_load_timeout,
        )

    def _argument_expression(self, value: Any) -> str:
        if isinstance(value, ChromeBridgeElement):
            return f"document.querySelector({json.dumps(value.selector)})"
        return json.dumps(value, ensure_ascii=False)

    def _wrap_script(self, script: str, args: tuple[Any, ...], asynchronous: bool) -> str:
        arguments = ",".join(self._argument_expression(arg) for arg in args)
        frame_prefix = ""
        frame_suffix = ""
        if self._same_origin_frame_selector:
            selector = json.dumps(self._same_origin_frame_selector)
            frame_prefix = (
                f"const __frame=document.querySelector({selector});"
                "if(!__frame||!__frame.contentWindow) throw new Error('Frame is unavailable');"
                "const window=__frame.contentWindow, document=window.document, "
                "performance=window.performance, requestAnimationFrame=window.requestAnimationFrame.bind(window), "
                "cancelAnimationFrame=window.cancelAnimationFrame.bind(window);"
            )
        if asynchronous:
            return (
                "new Promise((__done,__reject)=>{try{"
                + frame_prefix
                + f"const arguments=[{arguments},__done];"
                + f"(function(){{{script}}}).apply(window,arguments);"
                + frame_suffix
                + "}catch(__error){__reject(__error);}})"
            )
        return (
            "(()=>{"
            + frame_prefix
            + f"const arguments=[{arguments}];"
            + f"return (function(){{{script}}}).apply(window,arguments);"
            + frame_suffix
            + "})()"
        )

    def _evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = True,
        return_by_value: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        response = self.bridge.request(
            "cdp.send",
            {
                "tabId": self.tab_id,
                "sessionId": self._frame_session_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "awaitPromise": await_promise,
                    "returnByValue": return_by_value,
                    "userGesture": True,
                },
            },
            timeout=timeout or self._script_timeout,
        )
        if response.get("exceptionDetails"):
            detail = response["exceptionDetails"].get("text") or "JavaScript evaluation failed"
            raise WebDriverException(detail)
        return response.get("result") or {}

    def execute_script(self, script: str, *args: Any) -> Any:
        result = self._evaluate(self._wrap_script(script, args, False))
        return result.get("value")

    def execute_async_script(self, script: str, *args: Any) -> Any:
        try:
            result = self._evaluate(
                self._wrap_script(script, args, True),
                timeout=self._script_timeout + 1.0,
            )
        except TimeoutError as exc:
            raise TimeoutException(str(exc)) from exc
        return result.get("value")

    def execute_cdp_cmd(self, command: str, params: dict[str, Any]) -> Any:
        return self.bridge.request(
            "cdp.send",
            {
                "tabId": self.tab_id,
                "sessionId": self._frame_session_id,
                "method": command,
                "params": params,
            },
            timeout=self._script_timeout,
        )

    def find_element(self, by: str, value: str) -> ChromeBridgeElement:
        if by not in {"css selector", "css"}:
            raise ValueError("Current Chrome bridge currently supports CSS selectors")
        exists = self.execute_script("return !!document.querySelector(arguments[0]);", value)
        if not exists:
            raise NoSuchElementException(f"No element matches selector: {value}")
        return ChromeBridgeElement(self, value)

    def select_option(self, selector: str, value: str) -> None:
        selected = self.execute_script(
            "const el=document.querySelector(arguments[0]), wanted=String(arguments[1]);"
            "if(!el) return false; const option=Array.from(el.options).find(o=>o.value===wanted)"
            "||Array.from(el.options).find(o=>o.text===wanted); if(!option)return false;"
            "el.value=option.value; el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true})); return true;",
            selector,
            value,
        )
        if not selected:
            raise ValueError(f"No select option matches '{value}'")

    def set_file_input_files(self, selector: str, paths: list[str]) -> None:
        expression = f"document.querySelector({json.dumps(selector)})"
        remote_object = self._evaluate(expression, return_by_value=False)
        object_id = remote_object.get("objectId")
        if not object_id:
            raise NoSuchElementException(f"No file input matches selector: {selector}")
        self.execute_cdp_cmd(
            "DOM.setFileInputFiles", {"objectId": object_id, "files": paths}
        )

    def perform_key_events(self, events: list[dict[str, Any]]) -> None:
        modifiers = self._modifier_mask
        for event in events:
            if event["type"] == "pause":
                time.sleep(max(0.0, float(event.get("seconds", 0.0))))
                continue
            shifted = bool(modifiers & MODIFIER_BITS["Shift"])
            key, code, key_code, location = resolve_key(str(event["key"]), shifted=shifted)
            event_type = "keyDown" if event["type"] == "down" else "keyUp"
            bit = MODIFIER_BITS.get(key, 0)
            if event_type == "keyDown":
                modifiers |= bit
            params = {
                "type": event_type,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": key_code,
                "nativeVirtualKeyCode": key_code,
                "modifiers": modifiers,
                "location": location,
                "autoRepeat": bool(event.get("repeat", False)),
            }
            if event_type == "keyDown" and len(key) == 1 and not (modifiers & 3):
                # Only a printable keypress carries text; Ctrl/Alt chords never do.
                params["text"] = key
            self.execute_cdp_cmd("Input.dispatchKeyEvent", params)
            if event_type == "keyUp":
                modifiers &= ~bit
        self._modifier_mask = modifiers

    def get_screenshot_as_png(self) -> bytes:
        capture = self.execute_cdp_cmd("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(capture["data"])

    def get_log(self, log_type: str) -> list[dict[str, Any]]:
        if log_type != "browser":
            return []
        return list(
            self.bridge.request("console.get", {"tabId": self.tab_id}, timeout=5.0) or []
        )

    def subscribe_events(
        self,
        domains: list[str] | tuple[str, ...] = ("console",),
        *,
        include_headers: bool = False,
        limits: dict[str, int] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Turn on console and/or network capture for this tab."""
        params: dict[str, Any] = {
            "tabId": self.tab_id,
            "domains": [str(domain).lower() for domain in domains],
            "include_headers": bool(include_headers),
        }
        if limits:
            params["limits"] = {str(key): int(value) for key, value in limits.items()}
        return dict(self.bridge.request("events.subscribe", params, timeout=timeout) or {})

    def get_events(
        self,
        *,
        kinds: list[str] | tuple[str, ...] | None = None,
        since_seq: int = 0,
        limit: int = 200,
        level: str | list[str] | None = None,
        contains: str | None = None,
        url_pattern: str | None = None,
        types: list[str] | tuple[str, ...] | None = None,
        only_errors: bool = False,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Drain captured events; filtering happens inside the extension."""
        params: dict[str, Any] = {
            "tabId": self.tab_id,
            "since_seq": int(since_seq),
            "limit": int(limit),
            "only_errors": bool(only_errors),
        }
        if kinds:
            params["kinds"] = [str(kind).lower() for kind in kinds]
        if level:
            params["level"] = level if isinstance(level, str) else [str(item) for item in level]
        if contains:
            params["contains"] = str(contains)
        if url_pattern:
            params["url_pattern"] = str(url_pattern)
        if types:
            params["types"] = [str(item) for item in types]
        return dict(self.bridge.request("events.get", params, timeout=timeout) or {})

    def clear_events(
        self,
        kinds: list[str] | tuple[str, ...] | None = None,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"tabId": self.tab_id}
        if kinds:
            params["kinds"] = [str(kind).lower() for kind in kinds]
        return dict(self.bridge.request("events.clear", params, timeout=timeout) or {})

    def unsubscribe_events(
        self,
        domains: list[str] | tuple[str, ...] | None = None,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"tabId": self.tab_id}
        if domains:
            params["domains"] = [str(domain).lower() for domain in domains]
        return dict(self.bridge.request("events.unsubscribe", params, timeout=timeout) or {})

    def get_network_body(self, request_id: str, *, timeout: float = 15.0) -> dict[str, Any]:
        """Fetch one response body; binary payloads come back as metadata only."""
        return dict(
            self.bridge.request(
                "network.body",
                {"tabId": self.tab_id, "requestId": str(request_id)},
                timeout=timeout,
            )
            or {}
        )

    def close_tab(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Detach and close the tab this driver owns."""
        try:
            return dict(
                self.bridge.request("tabs.remove", {"tabId": self.tab_id}, timeout=timeout) or {}
            )
        except Exception as exc:
            LOGGER.warning("Closing tab %s failed: %s: %s", self.tab_id, type(exc).__name__, exc)
            return {"removed": False, "id": self.tab_id}

    def quit(self) -> None:
        try:
            self.bridge.request("debugger.detach", {"tabId": self.tab_id}, timeout=5.0)
        except Exception as exc:
            LOGGER.warning(
                "Detaching the debugger from tab %s failed: %s: %s",
                self.tab_id,
                type(exc).__name__,
                exc,
            )


def list_current_chrome_tabs(wait_seconds: float = 1.0) -> dict[str, Any]:
    bridge = get_chrome_bridge()
    status = bridge.status(wait_seconds)
    if not status["connected"]:
        return {**status, "tabs": []}
    tabs = bridge.request("tabs.list", timeout=10.0)
    return {**status, "tabs": tabs}


atexit.register(_bridge.shutdown)
