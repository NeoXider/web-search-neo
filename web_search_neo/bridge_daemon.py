"""The bridge daemon: the one process on this machine that owns the companion port.

Until this release the loopback listener lived inside the MCP server process, so
the companion's connection lasted exactly as long as whichever agent happened to
be running. The badge went dark between calls and Chrome logged a refused
connection on every retry; a second MCP server could not bind the port at all and
lost ``profile_mode: "current"`` entirely; and after a long outage a freshly
started server had to wait out the extension's reconnect backoff.

Inverting the direction would fix all three, but MV3 gives an extension no way to
listen for connections, so the listener moved out of the MCP process instead. It
runs here, alone, and the MCP servers connect to it as clients. It does three
things: it holds the single connection to the Chrome companion; it relays
commands from any number of local clients to that companion, returning each
answer to the client that asked for it; and it keeps the register of which
client owns which tab, because letting several servers share one browser also
means they can reach for the same tab, and this process is the only one that can
see them all.

The frames exchanged with the extension are byte-for-byte what they were, because
the extension is not being changed. A local client speaks the same authenticated
hello and is told apart by the ``role`` field it adds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
import uuid

from web_search_neo import bridge_auth


CHROME_EXTENSION_ID = "ndbmcjhbdjpefojkoljacjhammmcigao"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROTOCOL = 1
TOKEN_MISMATCH_REASON = "Companion token mismatch; reload the extension on chrome://extensions"
MAX_FRAME_BYTES = 64 * 1024 * 1024
# The companion mints one of these per browser run and sends it in its hello.
# Because this process now outlives Chrome, it is the only thing that tells a
# client the browser behind the bridge is no longer the one it was talking to -
# tab ids alone cannot, since a new run reissues the same numbers.
BROWSER_RUN_KEY = "browser_run"
# Long enough for any identity worth minting, short enough that a peer cannot
# park a novel in the state every client is pushed on every connect.
MAX_BROWSER_RUN_CHARS = 256

# Nobody is served by a process that has had neither a browser nor an agent for a
# quarter of an hour: Chrome is closed or the companion is gone, and the price of
# being wrong is one respawn of about a second on the next call that needs it.
# Zero disables the timer for a machine that would rather keep the port held.
IDLE_SHUTDOWN_SECONDS = 900.0

LOGGER = logging.getLogger("web_search_neo.bridge.daemon")

_ADDRESS_IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}


def bridge_port() -> int:
    """Return the port both roles use, so a test can move the whole bridge."""
    try:
        return int(os.getenv("WEB_SEARCH_NEO_BRIDGE_PORT", "") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def idle_shutdown_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS", "")))
    except ValueError:
        return IDLE_SHUTDOWN_SECONDS


def parse_version(text: str) -> tuple[int, ...]:
    """The numeric release components of a version string, for ordering.

    Ordering versions is not string comparison: ``"1.3.10" < "1.3.9"`` is true
    for text and false for releases, and the bridge decides whether to replace a
    daemon on exactly that question. Leading numeric components are read and a
    trailing label is ignored, so ``"1.3.10-rc1"`` orders as ``(1, 3, 10)``; a
    string with no leading number at all yields ``()``, which compares equal only
    to another such string and is therefore never mistaken for "older".
    """
    components: list[int] = []
    for chunk in str(text or "").strip().split("."):
        match = re.match(r"\d+", chunk)
        if not match:
            break
        components.append(int(match.group()))
        if match.end() != len(chunk):
            # A label starts here ("10-rc1"): the number counted, the rest cannot.
            break
    return tuple(components)


def compare_versions(left: str, right: str) -> int | None:
    """``-1``/``0``/``1`` for older/same/newer, or ``None`` when unorderable.

    ``None`` is a real answer and not a failure: two builds whose versions carry
    no numbers - a development checkout, a fork's naming scheme - can be told
    apart but not ranked, and a caller that must not guess needs to hear so
    rather than be handed an arbitrary side of zero.
    """
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if left_text == right_text:
        return 0
    first = parse_version(left_text)
    second = parse_version(right_text)
    if not first or not second:
        return None
    # Trailing zeros must not make 1.3 look older than 1.3.0.
    width = max(len(first), len(second))
    first = first + (0,) * (width - len(first))
    second = second + (0,) * (width - len(second))
    if first == second:
        # Same release, different labels ("1.3.10" vs "1.3.10-rc1"): same code as
        # far as anything here can tell, so neither side may evict the other.
        return 0
    return -1 if first < second else 1


def browser_state(hello: dict[str, Any]) -> dict[str, Any]:
    """Describe the browser a companion hello came from, run identity included.

    ``browser_run`` is always present so that a client never has to tell "the
    companion did not say" apart from "the daemon forgot to relay it". It is
    ``None`` for a companion older than 1.3.2, which sends no identity at all;
    requiring one would take ``profile_mode: "current"`` away from anyone whose
    extension has not updated yet, and the update needs a working connection.
    """
    browser = dict(hello.get("browser") or {})
    # Nested is where the companion puts it, next to the browser name it
    # describes; the top level is accepted too so that the daemon does not have
    # to be released in lockstep with a companion that moves it.
    for candidate in (browser.get(BROWSER_RUN_KEY), hello.get(BROWSER_RUN_KEY)):
        if isinstance(candidate, str) and 1 <= len(candidate) <= MAX_BROWSER_RUN_CHARS:
            browser[BROWSER_RUN_KEY] = candidate
            return browser
    # Anything else - a number, an object, an empty string - is not an identity,
    # and passing junk on would let it be compared against a later, real one.
    browser[BROWSER_RUN_KEY] = None
    return browser


def close_quietly(connection: Any, code: int, reason: str) -> None:
    try:
        connection.close(code=code, reason=reason)
    except Exception as exc:
        LOGGER.warning("Closing a bridge socket failed: %s: %s", type(exc).__name__, exc)


@dataclass(eq=False)
class _Client:
    """One local process — an MCP server — that relays commands through us.

    Identity is the connection, not the field values: two MCP servers of the same
    version are different clients and must land in the registry separately.
    """

    connection: Any
    version: str
    label: str
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, payload: dict[str, Any]) -> None:
        # Results, state pushes and control answers reach a client from different
        # threads, and a websocket frame cannot be interleaved with another.
        text = json.dumps(payload, ensure_ascii=False)
        with self.lock:
            self.connection.send(text)

    def send_quietly(self, payload: dict[str, Any]) -> None:
        try:
            self.send(payload)
        except Exception as exc:
            LOGGER.warning(
                "Could not answer bridge client %s: %s: %s", self.label, type(exc).__name__, exc
            )


@dataclass
class _Route:
    """Where one in-flight command came from, and which companion it went to."""

    client: _Client
    client_id: Any
    extension: Any


@dataclass
class _Claim:
    """One tab, and the client that is currently driving it.

    The guard against two agents steering one tab used to live in the MCP
    server's own module globals, which worked for exactly as long as there was
    one MCP server. This daemon is the only thing that sees every client, so the
    guard has to live here or it does not exist at all.
    """

    client: _Client
    tab_id: int
    browser_run: str | None
    claimed_at: float
    # The same instant on the wall clock. ``claimed_at`` is monotonic, which is
    # right for measuring here and useless anywhere else: it counts from an
    # origin only this process knows. A duration derived from it can therefore
    # only be computed here - and a duration computed here and then pushed to the
    # other clients is stale the moment it lands. They are given the instant and
    # do their own subtraction, which cannot go out of date between pushes.
    since: float = field(default_factory=time.time)

    def held_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.claimed_at)


class BridgeDaemon:
    """Owns the companion port and relays between the extension and local clients."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int | None = None,
        token: str | None = None,
        version: str = "",
        idle_seconds: float | None = None,
    ) -> None:
        self.host = host
        self.port = int(port or bridge_port())
        self.version = str(version or "")
        # A client that asked a daemon to step aside has to recognise it if it
        # answers again; the process id cannot say that when the daemon is
        # embedded, and two daemons of one machine are told apart by this.
        self.instance = uuid.uuid4().hex
        self.idle_seconds = idle_shutdown_seconds() if idle_seconds is None else float(idle_seconds)
        # An explicit token belongs to the caller (tests); only a daemon that was
        # given none owns the on-disk secret and the extension's copy of it.
        self._token: str | None = token
        self._owns_token = token is None
        self._lock = threading.RLock()
        self._extension: Any = None
        self._extension_send_lock = threading.Lock()
        self._browser_info: dict[str, Any] = {}
        # The run the claims below belong to. It is kept apart from
        # ``_browser_info`` because that is emptied whenever the companion's
        # socket drops, and a companion that merely reconnects is still the same
        # browser holding the same tabs; only a genuinely different run may
        # invalidate anyone's claim.
        self._browser_run: str | None = None
        self._claims: dict[int, _Claim] = {}
        self._clients: set[_Client] = set()
        self._routes: dict[str, _Route] = {}
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._startup_error: str | None = None
        self.port_taken = False
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._idle_since: float | None = time.monotonic()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Serve in a background thread; used by tests and by an embedded daemon."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._serve, name="web-search-neo-bridge-daemon", daemon=True
            )
            self._thread.start()
        self._started.wait(timeout=5.0)

    def serve_forever(self) -> str | None:
        """Serve on this thread until shutdown; returns the startup error, if any."""
        self._serve()
        return self._startup_error

    def shutdown(self) -> None:
        self._stopped.set()
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception as exc:
                LOGGER.warning(
                    "Bridge daemon shutdown failed: %s: %s", type(exc).__name__, exc
                )
        # ``Server.shutdown`` stops accepting new sockets, but websockets keeps
        # existing handlers alive until their peers leave.  A client linked to
        # that retired handler therefore has no signal to reconnect to the
        # replacement daemon: it can sit on the dead generation until the next
        # ping timeout, silently losing every tab claim in the meantime.  Take a
        # snapshot under the lock and close outside it, because each handler's
        # ``finally`` block needs the same lock to remove itself and its routes.
        with self._lock:
            connections = [client.connection for client in self._clients]
            if self._extension is not None:
                connections.append(self._extension)
        for connection in connections:
            close_quietly(connection, 1001, "Bridge daemon is shutting down")

    @property
    def startup_error(self) -> str | None:
        return self._startup_error

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def _serve(self) -> None:
        try:
            from websockets.sync.server import serve

            self._ensure_token()
            extension_origin = re.compile(
                rf"^chrome-extension://{re.escape(CHROME_EXTENSION_ID)}/?$"
            )
            with serve(
                self._handle_connection,
                self.host,
                self.port,
                # A local client sends no Origin at all; None is how websockets
                # spells that, and it is not a header a browser can omit.
                origins=[extension_origin, None],
                ping_interval=20,
                ping_timeout=20,
                max_size=MAX_FRAME_BYTES,
            ) as server:
                self._server = server
                self._started.set()
                if self.idle_seconds > 0:
                    threading.Thread(
                        target=self._watch_idle, name="web-search-neo-bridge-idle", daemon=True
                    ).start()
                LOGGER.info(
                    "Bridge daemon %s listening on %s:%s (pid %s)",
                    self.version or "unversioned",
                    self.host,
                    self.port,
                    os.getpid(),
                )
                server.serve_forever()
        except OSError as exc:
            self.port_taken = exc.errno in _ADDRESS_IN_USE
            self._startup_error = f"{type(exc).__name__}: {exc}"
            self._started.set()
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            self._started.set()
        finally:
            self._stopped.set()
            self._started.set()
            self._fail_routes("The bridge daemon stopped")

    def _watch_idle(self) -> None:
        # Checking four times per idle window keeps a long default cheap and a
        # short one (a test, or a machine that wants the port back) honest.
        interval = max(0.05, min(15.0, self.idle_seconds / 4))
        while not self._stopped.wait(interval):
            with self._lock:
                idle_since = self._idle_since
            if idle_since is None or time.monotonic() - idle_since < self.idle_seconds:
                continue
            LOGGER.info(
                "No companion and no client for %.0f s; the bridge daemon is exiting",
                self.idle_seconds,
            )
            self.shutdown()
            return

    def _refresh_idle(self) -> None:
        with self._lock:
            busy = self._extension is not None or bool(self._clients)
            self._idle_since = None if busy else time.monotonic()

    def _ensure_token(self) -> str:
        """Return the shared secret, publishing it to the extension when we own it."""
        with self._lock:
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

    def _reload_token(self) -> str:
        """Re-read the secret from disk after a peer presented a different one.

        Rotating the secret means deleting the file and letting the next start
        mint a new one. A daemon that outlives that restart would otherwise hold
        the retired secret for as long as it runs and reject every peer, and the
        documented remedy would be to hunt the process down by hand. Whoever can
        write this file could already read it, so trusting the newer value costs
        nothing that was not already spent.
        """
        with self._lock:
            if not self._owns_token:
                return self._token or ""
            self._token = None
        return self._ensure_token()

    # -- state -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What a client needs to answer for the browser without asking Chrome.

        ``browser`` is whatever the companion said about itself, with
        ``browser_run`` (see :func:`browser_state`) added: a string that is the
        same for every hello of one browser run and different after Chrome
        restarts, or ``None`` from a companion too old to mint one. While no
        companion is connected ``browser`` is empty, so the key is absent
        entirely - "no browser" rather than "an unnamed browser".

        ``clients`` and ``claims`` describe the other tenants. They are here
        because this process is the only one that can see them: an MCP server
        asked how many agents share the browser, or which tabs are already
        spoken for, has nowhere else to look, and until it could ask, the first
        news of a co-tenant was a refusal after the attach had been attempted.
        ``claims`` names the holder rather than "somebody", for the same reason
        the refusal does - "the tab is busy" invites retrying forever.

        This is the answer to the ``status`` control call and, with a ``type`` of
        ``extension``, the state pushed to every client whenever the companion
        connects or drops.
        """
        with self._lock:
            return {
                "connected": self._extension is not None,
                "browser": dict(self._browser_info),
                "version": self.version,
                "pid": os.getpid(),
                "instance": self.instance,
                "clients": len(self._clients),
                # A list rather than a dict: JSON turns integer keys into
                # strings, and a tab id that arrives back as "42" is one more
                # thing every reader has to remember to convert.
                "claims": [
                    {
                        "tab_id": tab_id,
                        "holder": claim.client.label,
                        "since": claim.since,
                    }
                    for tab_id, claim in sorted(self._claims.items())
                ],
                "host": self.host,
                "port": self.port,
            }

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._extension is not None

    @property
    def browser_info(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._browser_info)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def claimed_tabs(self) -> dict[int, str]:
        """Which tabs are spoken for, and by whom - for diagnostics and tests."""
        with self._lock:
            return {tab_id: claim.client.label for tab_id, claim in self._claims.items()}

    # -- tab ownership -----------------------------------------------------

    def _adopt_browser_run(self, run: str | None) -> None:
        """Note the run a companion reports, dropping claims of a *different* one.

        Caller holds the lock. Tab ids are only unique inside one browser run, so
        a claim on tab 100 made against the browser that has just been replaced
        says nothing about tab 100 of the browser that replaced it - keeping it
        would lock a live tab out of the pool for as long as the daemon runs.

        A companion that reloads itself (which is how it updates) also comes back
        with a new run id, and that clears the claims too. That is the right
        outcome for a different reason: the reload drops every ``chrome.debugger``
        attachment, so the sessions behind those claims are already broken.

        The delicate case is a run this daemon did not know yet, which is every
        daemon's first seconds: a client reconnects in well under a second, while
        the companion is still working through its own backoff, so its claims
        land here before anything can say which browser they belong to. They
        belong to the browser that is about to say hello - there is only one - so
        learning its name adopts them. Reading "newly known" as "changed" voided
        those claims while their holder went on believing it held them, and the
        next agent to ask was handed the same tab.

        ``None`` means a companion too old to say: nothing can be concluded from
        it, so nothing is taken from anybody.
        """
        if run is None:
            return
        previous = self._browser_run
        self._browser_run = run
        if run == previous:
            return
        adopted: list[int] = []
        dropped: list[int] = []
        for tab_id, claim in list(self._claims.items()):
            if claim.browser_run is None:
                claim.browser_run = run
                adopted.append(tab_id)
            elif claim.browser_run != run:
                self._claims.pop(tab_id, None)
                dropped.append(tab_id)
        if dropped:
            LOGGER.info(
                "A different browser is on the bridge (run %s); dropping tab claim(s) %s",
                run,
                ", ".join(str(tab_id) for tab_id in dropped),
            )
        if adopted:
            LOGGER.info(
                "The bridge now knows it serves browser run %s; the tab claim(s) %s made "
                "before that belong to it",
                run,
                ", ".join(str(tab_id) for tab_id in adopted),
            )

    def _claim_tab(
        self, client: _Client, raw_tab_id: Any, raw_run: Any = None
    ) -> dict[str, Any]:
        """Hand one tab to one client, or say who already has it.

        ``raw_run`` is the browser run a client believes its claim was granted
        under. A first claim sends none; a client re-asserting a claim on a link
        it had to rebuild sends the run it was told, which is what lets a daemon
        that has not met the companion yet record the claim against the right
        browser instead of against nothing.
        """
        try:
            tab_id = int(raw_tab_id)
        except (TypeError, ValueError):
            return {
                "granted": False,
                "tab_id": raw_tab_id,
                "reason": "A tab claim needs a numeric tab id",
            }
        asserted = raw_run if isinstance(raw_run, str) and raw_run else None
        with self._lock:
            run = self._browser_run
            if asserted is not None and run is not None and asserted != run:
                # The claim is about a browser that is over. Its tab 41 and this
                # browser's tab 41 are different tabs, so honouring it would hand
                # out exactly the stranger's tab this register exists to protect.
                LOGGER.info(
                    "Refused %s a claim on tab %s made against browser run %s, not %s",
                    client.label,
                    tab_id,
                    asserted,
                    run,
                )
                return {
                    "granted": False,
                    "tab_id": tab_id,
                    "browser_run": run,
                    "reason": (
                        f"Chrome tab {tab_id} was claimed against a different browser "
                        f"(run {asserted}); this bridge is now serving run {run}, where "
                        "that tab id means another tab. Open a tab in the browser that "
                        "is running now."
                    ),
                }
            held = self._claims.get(tab_id)
            # Re-claiming a tab you already hold is how a caller renews its grip
            # after a reconnect, and must never be mistaken for a conflict.
            if held is not None and held.client is not client:
                held_for = held.held_seconds()
                LOGGER.info(
                    "Refused %s the tab %s that %s has held for %.0f s",
                    client.label,
                    tab_id,
                    held.client.label,
                    held_for,
                )
                return {
                    "granted": False,
                    "tab_id": tab_id,
                    "browser_run": run,
                    # Written to be shown to a person: it has to say what has the
                    # tab, since "the tab is busy" invites them to try again
                    # forever against an agent that is not going to let go.
                    "reason": (
                        f"Chrome tab {tab_id} is already being driven by another agent on "
                        f"this machine ({held.client.label}, for {held_for:.0f}s). Two "
                        "sessions on one tab fight over it, so this one was not started. "
                        "Use a different tab, or stop that agent first."
                    ),
                    "holder": {
                        "label": held.client.label,
                        "version": held.client.version,
                        "held_seconds": round(held_for, 1),
                    },
                }
            # A run the daemon knows wins over one a client remembers; when it
            # knows none, remembering the client's is what survives the moment
            # the companion finally says hello.
            recorded = run if run is not None else asserted
            self._claims[tab_id] = _Claim(
                client=client, tab_id=tab_id, browser_run=recorded, claimed_at=time.monotonic()
            )
        LOGGER.info("Bridge client %s claimed tab %s", client.label, tab_id)
        # Every other client's picture of who holds what has just gone stale, and
        # a picture that is only refreshed when the companion happens to
        # reconnect is worse than none: it reads as current. Sent outside the
        # lock, because a slow peer must not hold the register shut.
        self._broadcast_state()
        return {"granted": True, "tab_id": tab_id, "browser_run": recorded}

    def _release_tab(self, client: _Client, raw_tab_id: Any) -> dict[str, Any]:
        try:
            tab_id = int(raw_tab_id)
        except (TypeError, ValueError):
            return {
                "released": False,
                "tab_id": raw_tab_id,
                "reason": "A tab release needs a numeric tab id",
            }
        with self._lock:
            held = self._claims.get(tab_id)
            if held is None:
                return {"released": False, "tab_id": tab_id, "reason": "Nobody holds that tab"}
            if held.client is not client:
                return {
                    "released": False,
                    "tab_id": tab_id,
                    "reason": f"Tab {tab_id} is held by {held.client.label}, not by you",
                }
            self._claims.pop(tab_id, None)
        LOGGER.info("Bridge client %s released tab %s", client.label, tab_id)
        self._broadcast_state()
        return {"released": True, "tab_id": tab_id}

    def _drop_claims_of(self, client: _Client) -> list[int]:
        """Free everything one client held. Caller holds the lock."""
        dropped = [tab_id for tab_id, claim in self._claims.items() if claim.client is client]
        for tab_id in dropped:
            self._claims.pop(tab_id, None)
        return dropped

    def _broadcast_state(self) -> None:
        state = self.status()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.send_quietly({"type": "extension", **state})

    # -- connections -------------------------------------------------------

    def _handle_connection(self, websocket: Any) -> None:
        handshake = self._authenticate(websocket)
        if handshake is None:
            return
        hello, token = handshake
        if (hello.get("role") or "extension") == "client":
            self._serve_client(websocket, hello, token)
        else:
            self._serve_extension(websocket, hello, token)

    def _authenticate(self, websocket: Any) -> tuple[dict[str, Any], str] | None:
        """Run the shared hello, or close the socket and say why in the close frame."""
        try:
            first = json.loads(websocket.recv(timeout=5.0))
        except (TypeError, ValueError):
            first = None
        except Exception as exc:
            LOGGER.warning("A bridge peer never sent a hello: %s: %s", type(exc).__name__, exc)
            return None
        if not isinstance(first, dict):
            # Without this the AttributeError below lands in the catch-all and
            # the peer only ever sees a bare 1000, with no hint of what broke.
            LOGGER.warning("Rejected a bridge client whose first frame was not a JSON object")
            close_quietly(websocket, 1008, "Companion hello must be a JSON object")
            return None
        if first.get("type") != "hello" or first.get("protocol") != PROTOCOL:
            # The number is in the reason on purpose: this close is the only thing
            # a peer of another revision ever receives, so a bare "wrong hello"
            # left it unable to report what it could not agree with.
            close_quietly(
                websocket, 1008, f"Expected Web Search Neo bridge protocol {PROTOCOL}"
            )
            return None
        token = self._ensure_token()
        nonce = first.get("nonce")
        presented = first.get("token")
        if not bridge_auth.token_matches(token, presented):
            # The secret on disk may have been rotated under a daemon that is
            # still holding the retired one; re-read before calling this a
            # rejection, so rotation does not need the process killed by hand.
            token = self._reload_token()
        if not bridge_auth.token_matches(token, presented):
            LOGGER.warning(
                "Rejected a bridge client that did not present the companion token "
                "(claimed browser: %r)",
                dict(first.get("browser") or {}),
            )
            close_quietly(websocket, 1008, TOKEN_MISMATCH_REASON)
            return None
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 256:
            LOGGER.warning("Rejected a bridge client whose hello carried no usable nonce")
            close_quietly(websocket, 1008, "Companion hello must carry a nonce")
            return None
        role = first.get("role")
        if role not in (None, "extension", "client"):
            close_quietly(websocket, 1008, "Unknown bridge role")
            return None
        return first, token

    def _serve_extension(self, websocket: Any, hello: dict[str, Any], token: str) -> None:
        try:
            with self._lock:
                previous = self._extension
                self._extension = websocket
                self._browser_info = browser_state(hello)
                self._adopt_browser_run(self._browser_info.get(BROWSER_RUN_KEY))
                self._idle_since = None
            if previous is not None and previous is not websocket:
                # The newest authenticated companion wins: a stale socket that
                # answered first must not be able to hold the bridge hostage.
                LOGGER.info("A newer Chrome companion replaced the previous connection")
                self._fail_routes("Chrome companion reconnected", extension=previous)
                # Closing waits for the peer's close frame, so it must not delay
                # the ack the fresh companion is blocked on.
                threading.Thread(
                    target=close_quietly,
                    args=(previous, 1000, "Replaced by a newer Chrome companion"),
                    name="web-search-neo-bridge-evict",
                    daemon=True,
                ).start()
            websocket.send(
                json.dumps(
                    {
                        "type": "hello_ack",
                        "protocol": PROTOCOL,
                        "proof": bridge_auth.sign(token, str(hello.get("nonce"))),
                    }
                )
            )
            self._broadcast_state()
            for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except (TypeError, ValueError):
                    # One malformed frame must not tear down the whole session.
                    LOGGER.warning("Chrome companion sent a frame that is not JSON")
                    continue
                if not isinstance(message, dict):
                    continue
                message_type = message.get("type")
                if message_type == "result":
                    self._deliver_result(websocket, message)
                elif message_type == "ping":
                    websocket.send(json.dumps({"type": "pong", "at": time.time()}))
                else:
                    LOGGER.warning("Ignored unknown bridge frame type %r", message_type)
        except Exception as exc:
            LOGGER.warning(
                "Chrome companion connection ended: %s: %s", type(exc).__name__, exc
            )
        finally:
            with self._lock:
                if self._extension is websocket:
                    self._extension = None
                    self._browser_info = {}
            self._refresh_idle()
            self._fail_routes("Chrome companion extension disconnected", extension=websocket)
            self._broadcast_state()

    def _deliver_result(self, websocket: Any, message: dict[str, Any]) -> None:
        """Send one companion answer back to the client that asked for it."""
        relay_id = str(message.get("id", ""))
        with self._lock:
            route = self._routes.pop(relay_id, None)
        if route is None:
            LOGGER.warning("Dropped a late bridge result for id %s", relay_id)
            return
        if route.extension is not websocket:
            # The command was sent to a companion that has since been replaced;
            # its answer is about a browser session the caller no longer has.
            LOGGER.warning("Dropped a result that arrived on a replaced companion socket")
            return
        answer: dict[str, Any] = {"type": "result", "id": route.client_id}
        if message.get("error"):
            answer["error"] = message.get("error")
        else:
            answer["result"] = message.get("result")
        route.client.send_quietly(answer)

    def _serve_client(self, websocket: Any, hello: dict[str, Any], token: str) -> None:
        details = hello.get("client") or {}
        label = f"{details.get('program') or 'client'}#{details.get('pid') or '?'}"
        client = _Client(
            connection=websocket, version=str(hello.get("version") or ""), label=label
        )
        try:
            # The acknowledgement goes out before this client joins the register,
            # and the order matters: a client is not listening for pushed state
            # until it has read the ack, so a broadcast that reached it first -
            # from any thread, for any reason - would be read as the ack and the
            # handshake would fail with "not a bridge daemon". Registering only
            # after the ack is on the wire makes that impossible rather than
            # unlikely.
            client.send(
                {
                    "type": "hello_ack",
                    "protocol": PROTOCOL,
                    "role": "client",
                    "proof": bridge_auth.sign(token, str(hello.get("nonce"))),
                    "version": self.version,
                    "pid": os.getpid(),
                    "instance": self.instance,
                }
            )
            with self._lock:
                self._clients.add(client)
                self._idle_since = None
            LOGGER.info(
                "Bridge client %s attached (version %s)", label, client.version or "unknown"
            )
            # A second agent arriving is news for the first one, and this is the
            # only moment it can be told: nothing else on this bridge changes
            # when a client merely connects.
            self._broadcast_state()
            for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except (TypeError, ValueError):
                    LOGGER.warning("Bridge client %s sent a frame that is not JSON", label)
                    continue
                if not isinstance(message, dict):
                    continue
                message_type = message.get("type")
                if message_type == "command":
                    self._relay_command(client, message)
                elif message_type == "control":
                    self._answer_control(client, message)
                elif message_type == "ping":
                    client.send({"type": "pong", "at": time.time()})
                else:
                    LOGGER.warning("Ignored unknown client frame type %r", message_type)
        except Exception as exc:
            LOGGER.info(
                "Bridge client %s detached: %s: %s", label, type(exc).__name__, exc
            )
        finally:
            # Every exit from the loop above lands here - a clean goodbye, a
            # dropped socket, an exception inside a handler - and a tab whose
            # owner is gone has to come back to the pool on all of them. A claim
            # that outlives its client cannot be undone by anyone: the process
            # that would release it no longer exists, and the tab would stay
            # unusable until the daemon itself is restarted.
            with self._lock:
                self._clients.discard(client)
                stale = [key for key, route in self._routes.items() if route.client is client]
                for key in stale:
                    self._routes.pop(key, None)
                released = self._drop_claims_of(client)
            self._refresh_idle()
            if released:
                LOGGER.info(
                    "Released tab(s) %s held by the departing client %s",
                    ", ".join(str(tab_id) for tab_id in released),
                    label,
                )
            LOGGER.info("Bridge client %s is gone", label)
            # The client count changed, and so did the claims if it held any.
            # Whoever is left has to hear it, or a tab that came back to the pool
            # goes on reading as taken until something else makes the daemon speak.
            self._broadcast_state()

    def _relay_command(self, client: _Client, message: dict[str, Any]) -> None:
        client_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            client.send_quietly(
                {"type": "result", "id": client_id, "error": "Bridge command needs a method"}
            )
            return
        with self._lock:
            extension = self._extension
        if extension is None:
            client.send_quietly(
                {
                    "type": "result",
                    "id": client_id,
                    "error": "Chrome companion extension is not connected",
                }
            )
            return
        # The companion sees one id space, so ids from different clients — which
        # may well collide — are replaced by one the daemon mints and maps back.
        relay_id = uuid.uuid4().hex
        with self._lock:
            self._routes[relay_id] = _Route(
                client=client, client_id=client_id, extension=extension
            )
        payload = json.dumps(
            {
                "type": "command",
                "id": relay_id,
                "method": method,
                "params": message.get("params") or {},
            },
            ensure_ascii=False,
        )
        try:
            with self._extension_send_lock:
                extension.send(payload)
        except Exception as exc:
            with self._lock:
                self._routes.pop(relay_id, None)
            client.send_quietly(
                {
                    "type": "result",
                    "id": client_id,
                    "error": f"Chrome companion extension disconnected: {exc}",
                }
            )

    def _answer_control(self, client: _Client, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "status":
            client.send_quietly(
                {"type": "control_result", "id": message.get("id"), "result": self.status()}
            )
            return
        if method == "claim_tab":
            client.send_quietly(
                {
                    "type": "control_result",
                    "id": message.get("id"),
                    "result": self._claim_tab(
                        client, message.get("tab_id"), message.get(BROWSER_RUN_KEY)
                    ),
                }
            )
            return
        if method == "release_tab":
            client.send_quietly(
                {
                    "type": "control_result",
                    "id": message.get("id"),
                    "result": self._release_tab(client, message.get("tab_id")),
                }
            )
            return
        if method == "shutdown":
            reason = str(message.get("reason") or "no reason given")
            LOGGER.info("Bridge client %s asked the daemon to exit: %s", client.label, reason)
            client.send_quietly(
                {
                    "type": "control_result",
                    "id": message.get("id"),
                    "result": {"stopping": True, "version": self.version, "pid": os.getpid()},
                }
            )
            # Stopping the server closes this very socket, so it cannot run on
            # the thread that still has to return from this handler.
            threading.Thread(
                target=self.shutdown, name="web-search-neo-bridge-stop", daemon=True
            ).start()
            return
        client.send_quietly(
            {
                "type": "control_result",
                "id": message.get("id"),
                "error": f"Unknown bridge control method {method!r}",
            }
        )

    def _fail_routes(
        self, message: str, extension: Any | None = None, client: _Client | None = None
    ) -> None:
        with self._lock:
            doomed = [
                (key, route)
                for key, route in self._routes.items()
                if (extension is None or route.extension is extension)
                and (client is None or route.client is client)
            ]
            for key, _ in doomed:
                self._routes.pop(key, None)
        for _, route in doomed:
            route.client.send_quietly(
                {"type": "result", "id": route.client_id, "error": message}
            )


def log_path() -> Path:
    """Keep the daemon's log beside the token, not in the checkout."""
    return bridge_auth.token_path().parent / "bridge-daemon.log"


def _use_own_log() -> None:
    """Log to our own file: two processes rotating one file fight on Windows."""
    logger = logging.getLogger("web_search_neo")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    handler = RotatingFileHandler(
        log_path(), maxBytes=500_000, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def run_forever(version: str = "", port: int | None = None) -> int:
    """Entry point of ``python main.py --bridge``; returns a process exit code."""
    _use_own_log()
    daemon = BridgeDaemon(port=port, version=version)
    error = daemon.serve_forever()
    if error is None:
        return 0
    if daemon.port_taken:
        # Two MCP servers starting together both try this; the one that loses the
        # bind has nothing to do, because the winner serves it just as well.
        LOGGER.info("Another bridge daemon already owns %s: %s", daemon.port, error)
        return 0
    LOGGER.error("Bridge daemon could not start: %s", error)
    return 1
