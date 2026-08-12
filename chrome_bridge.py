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


# The tab group the agent's pages land in. It carries the project's mascot so a
# user glancing at a crowded window can tell at once which tabs are not theirs.
DEFAULT_TAB_GROUP = "🟢 AI"


class ChromeBridgeError(RuntimeError):
    """Raised when the companion extension isn't connected or rejects a command."""


class ChromeBridgeUnavailable(ChromeBridgeError):
    """Raised when the question never reached the companion at all.

    The distinction is the whole point of the class: an answer is evidence about
    the browser, and a transport failure is evidence about the link. A command
    that was refused by name ("No tab with id 42") says the tab is gone; a
    command that could not be sent - no companion connected, the link dropped
    between the check and the send, the daemon stopped mid-flight - says only
    that nobody was listening, and a caller that reads the second as the first
    condemns every live tab the moment Chrome's service worker blinks.

    It subclasses :class:`ChromeBridgeError` so that every existing
    ``except ChromeBridgeError`` keeps catching it; only a caller that has to
    tell the two apart needs to name it.
    """


LOGGER = logging.getLogger("web_search_neo.bridge")
PROJECT_DIR = Path(__file__).resolve().parent
DAEMON_ENTRY = PROJECT_DIR / "main.py"

# Two daemons of the wrong version in a row means another checkout is fighting us
# over the port, and replacing each other forever would be worse than saying so.
MAX_DAEMON_REPLACEMENTS = 2

# Long enough for a capture of a window nothing is looking at, which measured
# between 70 ms and half a minute depending on whether Chrome was painting it,
# and short enough that a capture which will never arrive still ends in an
# answer rather than in whatever timeout the caller above us happens to have.
SCREENSHOT_TIMEOUT = 45.0

__all__ = [
    "CHROME_EXTENSION_ID",
    "TOKEN_MISMATCH_REASON",
    "ChromeBridge",
    "ChromeBridgeDriver",
    "ChromeBridgeElement",
    "ChromeBridgeError",
    "ChromeBridgeUnavailable",
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
    # Set when the error was written by this side because the link ended, rather
    # than read off an answer the companion sent. See ChromeBridgeUnavailable.
    transport_failure: bool = False


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
        # tab id -> the browser run it was granted under. Kept so that a link
        # rebuilt after a drop can ask for the same tabs again: the daemon ties a
        # claim to the connection that made it, and connections do not last.
        self._claimed: dict[int, str | None] = {}
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
        try:
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
            acknowledgement = json.loads(connection.recv(timeout=5.0))
        except (TypeError, ValueError) as exc:
            close_quietly(connection, 1008, "Bridge daemon hello_ack must be JSON")
            raise ChromeBridgeError(f"The bridge daemon answered with junk: {exc}") from exc
        except Exception as exc:
            # Every other failure here closes the socket, and this one has to as
            # well: a peer that accepts the connection and then says nothing - a
            # wedged daemon, or something else listening on the port - would
            # otherwise leave one abandoned socket and its reader thread behind
            # on every retry, for as long as the server runs.
            close_quietly(connection, 1002, "The bridge daemon did not finish the hello")
            raise ChromeBridgeError(
                f"The peer on the bridge port did not answer the hello: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
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

    def _control_inline(
        self,
        connection: Any,
        method: str,
        fields: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        """Ask one control question on a link whose reader thread has not started.

        Between the handshake and ``_read_until_closed`` nobody is draining the
        socket, so a question asked here has to read its own answer - and hand
        every other frame to the ordinary dispatcher on the way, because the
        daemon pushes state whenever it likes and those frames arrive first.
        """
        request_id = uuid.uuid4().hex
        pending = _PendingRequest(event=threading.Event(), connection=connection)
        with self._state_lock:
            self._pending[request_id] = pending
        try:
            connection.send(
                json.dumps(
                    {"type": "control", "id": request_id, "method": method, **(fields or {})}
                )
            )
            deadline = time.monotonic() + timeout
            while not pending.event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"The bridge daemon did not answer '{method}'")
                frame = json.loads(connection.recv(timeout=remaining))
                if isinstance(frame, dict):
                    self._dispatch(frame)
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        if pending.error:
            raise ChromeBridgeError(str(pending.error))
        return pending.result

    def _refresh_state(self, connection: Any) -> None:
        """Ask the fresh link what the browser is doing before anyone calls status."""
        with self._state_lock:
            self._daemon = connection
        self._apply_state(self._control_inline(connection, "status") or {})
        self._reassert_claims(connection)

    def _reassert_claims(self, connection: Any) -> None:
        """Ask again for the tabs we already hold, on a link that is new.

        A claim belongs to a connection: a daemon we have just reconnected to -
        or one that restarted under us - knows nothing about ours. Saying nothing
        would let the guard evaporate silently at the moment it is most likely to
        matter, since a daemon restart is exactly when another agent is starting
        up too. Anything a newcomer took while we were away is dropped from our
        own book and said out loud, so that a caller reading
        :attr:`claimed_tabs` is never told it owns a tab that it does not.
        """
        with self._state_lock:
            remembered = list(self._claimed.items())
        for tab_id, granted_run in remembered:
            # The run travels with the claim. A daemon that has just started has
            # not met the companion yet, and without this it records the claim as
            # belonging to no browser in particular - then voids it seconds later,
            # when the companion says hello and the daemon reads its own former
            # ignorance as a browser change.
            fields: dict[str, Any] = {"tab_id": tab_id}
            if isinstance(granted_run, str) and granted_run:
                fields[bridge_daemon.BROWSER_RUN_KEY] = granted_run
            try:
                answer = self._control_inline(connection, "claim_tab", fields)
            except Exception as exc:
                # A daemon that cannot answer at all is either too old to know
                # about claims or already gone; the next link will try again, and
                # forgetting our claims here would only make the next agent's
                # question easier to answer wrongly.
                LOGGER.warning(
                    "Could not re-claim tab %s on the new bridge link: %s: %s",
                    tab_id,
                    type(exc).__name__,
                    exc,
                )
                return
            if isinstance(answer, dict) and answer.get("granted"):
                with self._state_lock:
                    if tab_id in self._claimed:
                        self._claimed[tab_id] = answer.get("browser_run") or granted_run
                continue
            with self._state_lock:
                self._claimed.pop(tab_id, None)
            LOGGER.warning(
                "Tab %s was taken over while this server was not linked to the bridge: %s",
                tab_id,
                (answer or {}).get("reason") if isinstance(answer, dict) else answer,
            )

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
        lost: list[int] = []
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
            # Every companion connect pushes this state, so it is also where we
            # learn that the browser changed under us. The daemon has already
            # dropped the claims of the browser that is gone; keeping them in our
            # own book would leave `claimed_tabs` promising tabs that now belong
            # to whoever asks next. A claim granted before anyone knew the run is
            # adopted into it, exactly as the daemon adopts its copy.
            run = self._browser_info.get(bridge_daemon.BROWSER_RUN_KEY)
            if isinstance(run, str) and run:
                for tab_id, granted_run in list(self._claimed.items()):
                    if granted_run is None:
                        self._claimed[tab_id] = run
                    elif granted_run != run:
                        self._claimed.pop(tab_id, None)
                        lost.append(tab_id)
        for tab_id in lost:
            LOGGER.warning(
                "Tab %s belonged to a browser that is no longer the one on the bridge; "
                "this server no longer holds it",
                tab_id,
            )

    def _fail_pending(self, message: str, connection: Any | None = None) -> None:
        with self._state_lock:
            pending_items = [
                item
                for item in self._pending.values()
                if connection is None or item.connection is connection
            ]
        for pending in pending_items:
            if not pending.error:
                pending.error = message
                # Nobody answered this; the link under it ended. Marked so the
                # waiter raises ChromeBridgeUnavailable rather than passing a
                # dead link off as the companion's verdict.
                pending.transport_failure = True
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
        """What the connected companion says about its browser.

        The daemon keeps this dictionary and hands it to us twice over: in the
        ``status`` control answer this client asks for the moment a link is
        established - which is what makes it correct when we attach to a daemon
        that has been linked to a companion for hours - and in the ``extension``
        state pushed to every client whenever the companion connects or drops.
        Both paths land in ``_apply_state``, so there is one shape to read:

            {"name": "Chrome",              # what the companion calls itself
             "extension_version": "1.3.2",  # its manifest version
             "browser_run": "9f3c...b1"}    # str | None, see below

        ``browser_run`` is a 32-character hex string minted by the companion once
        per browser run: identical across service worker restarts within one run,
        different after Chrome is closed and reopened. It is the only thing that
        can tell a caller that a tab id it stored earlier now belongs to a
        different browser, because ids restart with the browser and the daemon
        does not. Compare the value recorded when a session was created with the
        value here; if they differ, the tab id in that session names some other
        tab, quite possibly one of the user's.

        Two values need care:

        * ``None`` means the companion is older than 1.3.2 and mints no identity.
          Nothing can be concluded from it, in either direction.
        * The whole dictionary is empty (so ``.get("browser_run")`` is ``None``)
          while no companion is connected. Check ``connected`` first; a session
          cannot be checked against a browser that is not there.
        """
        with self._state_lock:
            return dict(self._browser_info)

    @property
    def browser_run(self) -> str | None:
        """Shorthand for ``browser_info.get("browser_run")``; see there."""
        with self._state_lock:
            return self._browser_info.get(bridge_daemon.BROWSER_RUN_KEY)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Any:
        """Ask the companion something and return its answer.

        Raises :class:`ChromeBridgeUnavailable` when the question never reached
        the companion and plain :class:`ChromeBridgeError` when it did and came
        back refused; ``TimeoutError`` when it was sent and nothing came back.
        """
        if not self.wait_connected(min(max(timeout, 0.0), 3.0)):
            detail = f" ({self._startup_error})" if self._startup_error else ""
            # Telling a caller to click through chrome://extensions is useless to
            # an agent: name the two calls it can actually make instead.
            raise ChromeBridgeUnavailable(
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
            raise ChromeBridgeUnavailable("Chrome companion extension disconnected")
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
                raise ChromeBridgeUnavailable(
                    f"The bridge daemon link dropped while sending '{method}': {exc}"
                ) from exc
            if not pending.event.wait(max(0.1, timeout)):
                raise TimeoutError(f"Chrome bridge command '{method}' timed out")
            if pending.error:
                if pending.transport_failure:
                    raise ChromeBridgeUnavailable(pending.error)
                raise ChromeBridgeError(pending.error)
            return pending.result
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def status(self, wait_seconds: float = 0.0) -> dict[str, Any]:
        """The bridge as a caller sees it; ``browser`` is :attr:`browser_info`."""
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

    def _control(
        self, method: str, fields: dict[str, Any] | None = None, timeout: float = 5.0
    ) -> Any:
        """Ask the daemon something about itself, rather than asking the browser."""
        self.start()
        with self._state_lock:
            connection = self._daemon
            if connection is None:
                raise ChromeBridgeError(
                    f"No bridge daemon is linked, so '{method}' could not be asked"
                )
            request_id = uuid.uuid4().hex
            pending = _PendingRequest(event=threading.Event(), connection=connection)
            self._pending[request_id] = pending
        try:
            payload = json.dumps(
                {"type": "control", "id": request_id, "method": method, **(fields or {})}
            )
            try:
                with self._send_lock:
                    connection.send(payload)
            except Exception as exc:
                raise ChromeBridgeError(
                    f"The bridge daemon link dropped while asking '{method}': {exc}"
                ) from exc
            if not pending.event.wait(max(0.1, timeout)):
                raise TimeoutError(f"The bridge daemon did not answer '{method}'")
            if pending.error:
                raise ChromeBridgeError(str(pending.error))
            return pending.result
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def claim_tab(self, tab_id: int, timeout: float = 5.0) -> dict[str, Any]:
        """Ask the daemon for exclusive use of one Chrome tab.

        Several MCP servers can now drive one browser, and the guard that keeps
        two of them off one tab cannot live in either process. The daemon holds
        it, and this is how to ask. The answer is always a dictionary with a
        ``status`` of exactly one of three words, because the three mean
        different things and only one of them is a "no":

        ``granted``
            The tab is yours until the first of three things happens: you release
            it, this client's link to the daemon ends, or the browser behind the
            bridge turns out to be a different one. ``browser_run`` is the run the
            claim is tied to - record it with the session, because a claim is
            about a tab id, and tab ids only mean anything within one browser run.
            A link this client rebuilds by itself does not end the claim: the
            remembered run travels with the re-assert, so the daemon puts it back
            where it belongs.
        ``refused``
            Somebody else is driving that tab. ``reason`` is a sentence written
            to be shown to a person, and ``holder`` describes the other client.
            Do not start the session.
        ``unavailable``
            There was nobody to ask - no daemon, an older daemon that does not
            know about claims, or a link that dropped mid-question. ``reason``
            says which. This is not a refusal and must not be treated as one:
            proceed as before, with whatever in-process guard you already have.
            ``granted`` is ``True`` for exactly this reason, so a caller that
            only looks at ``granted`` fails open rather than locking itself out
            of a browser it could have used.

        Only ``profile_mode: "current"`` has a browser to share; the Selenium
        modes drive a Chrome of their own, where a claim is meaningless and this
        call is simply not needed.
        """
        try:
            answer = self._control("claim_tab", {"tab_id": int(tab_id)}, timeout=timeout)
        except Exception as exc:
            # A refusal is a well-formed answer. Anything else - no daemon, an
            # unknown-method error from a daemon that predates claims, a dropped
            # link - means the question was never put, which is a different fact.
            return {
                "status": "unavailable",
                "granted": True,
                "tab_id": int(tab_id),
                "reason": f"The bridge daemon could not be asked about tab {tab_id}: {exc}",
            }
        if not isinstance(answer, dict):
            return {
                "status": "unavailable",
                "granted": True,
                "tab_id": int(tab_id),
                "reason": f"The bridge daemon answered a tab claim with {answer!r}",
            }
        granted = bool(answer.get("granted"))
        with self._state_lock:
            if granted:
                self._claimed[int(tab_id)] = answer.get("browser_run")
            else:
                self._claimed.pop(int(tab_id), None)
        return {
            "status": "granted" if granted else "refused",
            "granted": granted,
            "tab_id": int(tab_id),
            "browser_run": answer.get("browser_run"),
            "reason": answer.get("reason"),
            "holder": answer.get("holder"),
        }

    @property
    def claimed_tabs(self) -> tuple[int, ...]:
        """The tabs this client holds, as far as it knows.

        A tab leaves this list when it is released, and also when a reconnect
        finds that somebody else took it in the meantime, so it is safe to read
        as "may I still drive this?" - as long as the answer is understood to be
        this process's view of a fact the daemon owns.
        """
        with self._state_lock:
            return tuple(sorted(self._claimed))

    def release_tab(self, tab_id: int, timeout: float = 5.0) -> dict[str, Any]:
        """Give a claimed tab back. Never raises: letting go must always work.

        Returns ``{"released": bool, "tab_id": int, "reason": str | None}``.
        ``released`` is ``False`` when the daemon could not be reached or the tab
        was not ours, neither of which a caller can do anything about - and both
        of which are handled anyway, because a client's claims are dropped when
        its link to the daemon ends.
        """
        with self._state_lock:
            self._claimed.pop(int(tab_id), None)
        try:
            answer = self._control("release_tab", {"tab_id": int(tab_id)}, timeout=timeout)
        except Exception as exc:
            return {"released": False, "tab_id": int(tab_id), "reason": str(exc)}
        if not isinstance(answer, dict):
            return {"released": False, "tab_id": int(tab_id), "reason": "unreadable answer"}
        return {
            "released": bool(answer.get("released")),
            "tab_id": int(tab_id),
            "reason": answer.get("reason"),
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
    """WebDriver-shaped adapter that controls an already-open Chrome via extension.

    ``foreground`` decides whether this driver is allowed to take the screen. It
    is false by default, which is the point of the mode: the browser belongs to
    the user and they are usually still working in it, so the tab opens in the
    background, navigations leave their view alone, and claiming a tab does not
    raise it. Passing ``foreground=True`` restores the old behaviour exactly - a
    tab that opens in front, stays in front through every navigation and is
    raised when it is claimed - for a caller who is watching the agent work.

    Nothing else changes: input, screenshots and scripts all go through the
    debugger, which addresses the tab's renderer and does not care which tab is
    on screen. :meth:`activate_tab` is the explicit "show me this" in either
    mode, for a caller who wants to hand the tab back to the user.
    """

    is_extension_bridge = True

    def __init__(
        self,
        bridge: ChromeBridge | None = None,
        tab_id: int | None = None,
        tab_group: str = DEFAULT_TAB_GROUP,
        *,
        foreground: bool = False,
    ) -> None:
        self.bridge = bridge or get_chrome_bridge()
        self.tab_group = tab_group.strip() or DEFAULT_TAB_GROUP
        self.foreground = bool(foreground)
        self._script_timeout = 15.0
        self._page_load_timeout = 30.0
        self._frame_session_id: str | None = None
        self._same_origin_frame_selector: str | None = None
        self.service = _BridgeService()
        self._modifier_mask = 0
        # Whether this tab is recording network traffic right now. It belongs to
        # the tab rather than to whoever reads the events, because the extension
        # keeps one capture per tab and a reader cannot tell from a payload
        # whether an empty result means "quiet page" or "nothing was recorded".
        self.events_subscribed = False
        if tab_id is None:
            tab = self.bridge.request(
                "tabs.create",
                {"url": "about:blank", "group": self.tab_group, "active": self.foreground},
                timeout=10.0,
            )
        else:
            # Claiming a tab the user pointed at can be a "show me" moment, but
            # only when someone is watching: a background session that raised the
            # claimed tab would interrupt exactly the work it was told not to.
            tab = self.bridge.request("tabs.get", {"tabId": int(tab_id)}, timeout=10.0)
            if self.foreground:
                tab = self.bridge.request(
                    "tabs.activate", {"tabId": int(tab_id)}, timeout=10.0
                )
        self.tab_id = int(tab["id"])
        self.actual_tab_group = tab.get("group")
        self._start_capture()

    def _start_capture(self) -> None:
        """Record console and network from this tab's first navigation onwards.

        Capture has to be armed before the page it is meant to explain. The
        extension only records a request while ``Network.enable`` is on, so a
        subscription made when someone first asks for the network topic answers
        the question "why did this page misbehave" with "no requests were made" -
        a false premise, and the worst possible answer. The tab exists and is
        still blank here, which is the last moment before it can request
        anything; a tab claimed with ``tab_id`` starts recording from the claim,
        since traffic it made before that was never recorded by anyone.

        A failure to subscribe is not fatal: the tab is still usable without
        capture, and the network topic subscribes again when it is first read, so
        a bridge hiccup here costs history rather than the whole session.
        """
        try:
            self.subscribe_events(["console", "network"])
        except Exception as exc:
            LOGGER.warning(
                "Capturing events on tab %s failed: %s: %s",
                self.tab_id,
                type(exc).__name__,
                exc,
            )

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

    def activate_tab(self) -> dict[str, Any]:
        """Put this tab in front and raise its window - the only call that may.

        Nothing in the driver calls this on its own any more. It is here so that
        a caller with a reason to interrupt the user ("here is the page you asked
        to see") can say so out loud, in one place that is easy to audit.
        """
        return self.bridge.request("tabs.activate", {"tabId": self.tab_id}, timeout=10.0)

    def get(self, url: str) -> None:
        self.switch_to.default_content()
        self.bridge.request(
            "tabs.navigate",
            {"tabId": self.tab_id, "url": url, "active": self.foreground},
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
        # A screenshot of a tab that is not on screen is the one command whose
        # cost is set by the window manager rather than by the page. Measured on
        # this Chrome: about 70 ms while the browser window is being composited,
        # but tens of seconds - 25 s, 33 s, and once no answer at all - whenever
        # another window covers it, which is the normal state of affairs while
        # the user works elsewhere. The script timeout is far too short for that
        # and turns a slow picture into a failed call.
        timeout = SCREENSHOT_TIMEOUT if command == "Page.captureScreenshot" else (
            self._script_timeout
        )
        try:
            return self.bridge.request(
                "cdp.send",
                {
                    "tabId": self.tab_id,
                    "sessionId": self._frame_session_id,
                    "method": command,
                    "params": params,
                },
                timeout=timeout,
            )
        except TimeoutError as exc:
            if command != "Page.captureScreenshot":
                raise
            # Same exception type, because callers above catch it by type; what
            # changes is that the message names the cause and a way out.
            raise TimeoutError(
                f"Chrome did not return a screenshot within {timeout:.0f}s. This is what "
                "an obscured browser window looks like: Chrome stops painting a window "
                "nothing can see, and a capture then waits for a frame that is not being "
                "produced. The page itself is fine - reading the DOM, clicking and typing "
                "do not need the window. Ask for the screenshot again, or bring the tab to "
                "the front first if a picture is what matters."
            ) from exc

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
        result = dict(self.bridge.request("events.subscribe", params, timeout=timeout) or {})
        # The extension answers with every domain the tab is capturing, not just
        # the ones this call asked for, so the flag follows the tab's real state.
        self.events_subscribed = "network" in (result.get("domains") or params["domains"])
        return result

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
        result = dict(self.bridge.request("events.unsubscribe", params, timeout=timeout) or {})
        self.events_subscribed = "network" in (result.get("domains") or [])
        return result

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
        """Detach and close the tab this driver owns.

        ``removed: false`` is not the same fact as "the tab is still open": the
        extension answers that way whenever ``chrome.tabs.remove`` throws, and
        the commonest reason for that is a tab the user closed by hand. A caller
        that wants to report a leak has to ask whether the tab is there, which is
        what :func:`browser_tools._tab_still_exists` is for.
        """
        try:
            return dict(
                self.bridge.request("tabs.remove", {"tabId": self.tab_id}, timeout=timeout) or {}
            )
        except Exception as exc:
            LOGGER.warning("Closing tab %s failed: %s: %s", self.tab_id, type(exc).__name__, exc)
            return {"removed": False, "id": self.tab_id}

    def quit(self) -> dict[str, Any]:
        """Detach the debugger; say whether it came off.

        Never raises - teardown may not take its caller down with it - so the
        outcome has to travel in the return value instead. A debugger left
        attached keeps the "is being debugged" banner on a tab the user has been
        given back, and only they can clear it, so ``error`` is how a caller
        learns to say so rather than reporting a clean detach over the top of it.

        ``detached`` is what the extension reported: ``False`` with no ``error``
        means there was nothing attached, which is a clean outcome, not a
        failure.
        """
        try:
            answer = self.bridge.request(
                "debugger.detach", {"tabId": self.tab_id}, timeout=5.0
            )
        except Exception as exc:
            LOGGER.warning(
                "Detaching the debugger from tab %s failed: %s: %s",
                self.tab_id,
                type(exc).__name__,
                exc,
            )
            return {
                "detached": False,
                "id": self.tab_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        result = dict(answer or {})
        result.setdefault("id", self.tab_id)
        result["detached"] = bool(result.get("detached"))
        return result


def list_current_chrome_tabs(wait_seconds: float = 1.0) -> dict[str, Any]:
    bridge = get_chrome_bridge()
    status = bridge.status(wait_seconds)
    if not status["connected"]:
        return {**status, "tabs": []}
    tabs = bridge.request("tabs.list", timeout=10.0)
    return {**status, "tabs": tabs}


atexit.register(_bridge.shutdown)
