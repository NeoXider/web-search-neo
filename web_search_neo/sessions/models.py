"""Leaf implementation independent of MCP registration."""

from __future__ import annotations
from dataclasses import dataclass, field
import threading
import time
from typing import Any

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


class SessionLock:
    """An ``RLock`` that can also say whether a second caller is inside.

    Sessions are the unit of isolation between agents, and across processes the
    bridge daemon's tab register keeps two of them off one tab. Inside one MCP
    server there is no such register: several agents - subagents of one run, most
    of the time - share this process, and two that both leave ``session_id`` at
    its default land on the same :class:`BrowserSession`. The lock below then did
    its job perfectly and made that invisible: the second caller waited, took the
    lock, and acted on a page the first one had navigated out from under it. No
    error, no warning, a click on whatever happened to be there.

    Nothing here can tell the two agents apart - an MCP call carries no caller
    identity - so this does not try to arbitrate. It counts, so that the overlap
    can be reported instead of swallowed: :attr:`busy` is true while another
    thread holds the session, and :attr:`waiting` counts the callers queued
    behind it.

    The API is the subset of ``threading.RLock`` this module actually uses -
    the context manager plus ``acquire``/``release`` - so every ``with
    session.lock:`` in this file keeps working untouched.
    """

    __slots__ = ("_lock", "_state", "_owner", "_depth", "_waiting")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self._waiting = 0

    @property
    def waiting(self) -> int:
        """How many callers are queued for a session somebody else is using."""
        with self._state:
            return self._waiting

    @property
    def busy(self) -> bool:
        """Whether a *different* thread is inside this session right now.

        Reentrancy is why the owner is compared rather than the depth: a tool
        that takes its own session's lock twice is one caller, not two.
        """
        with self._state:
            return self._owner is not None and self._owner != threading.get_ident()

    @property
    def concurrent_callers(self) -> int:
        """Callers inside or queued, counting from the point of view of others.

        Zero when this thread is the only one that has anything to do with the
        session, which is the ordinary case and the one that must stay silent.
        """
        with self._state:
            mine = threading.get_ident()
            inside = 1 if self._owner is not None and self._owner != mine else 0
            return inside + self._waiting

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        mine = threading.get_ident()
        counted = False
        with self._state:
            # Only a wait that is actually about to happen counts: a free lock,
            # or one this very thread already holds, is not contention.
            if blocking and self._owner is not None and self._owner != mine:
                self._waiting += 1
                counted = True
        try:
            acquired = self._lock.acquire(blocking, timeout)
        finally:
            if counted:
                with self._state:
                    self._waiting -= 1
        if acquired:
            with self._state:
                self._owner = mine
                self._depth += 1
        return acquired

    def release(self) -> None:
        with self._state:
            # Released inside the bookkeeping section, and before it, on purpose.
            # Before, because a release by a thread that does not hold the lock
            # raises and must leave the counters alone rather than corrupt them.
            # Inside, because otherwise the thread this hands off to could record
            # itself as owner and then be overwritten by our own tidying.
            self._lock.release()
            self._depth -= 1
            if self._depth <= 0:
                self._depth = 0
                self._owner = None

    def __enter__(self) -> "SessionLock":
        self.acquire()
        return self

    def __exit__(self, *_exception: Any) -> None:
        self.release()


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
    render_deterministic: bool = False
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
    # for this session, so inject_script can list them and remove them by id -
    # the id is the only handle CDP's removal takes, and the only one a caller gets.
    injected_scripts: list[str] = field(default_factory=list)
    # The extra HTTP headers Chrome adds to every request, echoed back so a caller
    # can see what is in force, and the injected-script id that enables stealth,
    # kept so it can be turned off without the caller tracking it.
    extra_headers: dict[str, str] = field(default_factory=dict)
    stealth_identifier: str | None = None
    # Per-session fingerprint overrides (isolated/temporary/persistent only).
    # One real Chrome profile means one fingerprint, so multi-account work gets
    # one isolated session per account instead of two sessions on one profile.
    user_agent_override: str | None = None
    timezone_override: str | None = None
    locale_override: str | None = None
    geolocation_override: dict[str, Any] | None = None
    lock: SessionLock = field(default_factory=SessionLock)
    last_used: float = field(default_factory=time.monotonic)
    # Who opened this session, in the opener's own words. Nothing in an MCP call
    # carries caller identity, so this is the only way one agent's tab can be
    # told from another's - and it is why `close_all` can now leave other
    # agents' work alone. Optional on purpose: a caller that says nothing is
    # anonymous, not refused.
    agent_label: str | None = None
    # The tab-strip label this session applied ("[ag-mail] "), or None when the
    # tab is unlabelled. Read topics strip exactly this prefix back off, so what
    # a human sees in the tab strip never leaks into titles macros compare.
    tab_label_prefix: str | None = None
    # The Page.addScriptToEvaluateOnNewDocument id that keeps the label alive
    # across navigations, kept apart from injected_scripts: that list is the
    # caller's own inject_script bookkeeping, this one is the session's.
    tab_label_script_id: str | None = None
    # The activity badge (a green dot on the tab's favicon while an agent is
    # driving it), same bookkeeping shape as the label: script id plus the last
    # server-side ping, so pings stay throttled to one a minute per session.
    tab_activity_script_id: str | None = None
    activity_pinged_at: float = 0.0
    # The same, for the agent-presence script that badges the favicon and
    # flashes the last action. Separate id because the two signals are
    # independently switchable and one may be off while the other is on.
    presence_script_id: str | None = None
    # When this session last marked its page. Only the steps that draw nothing
    # consult it: they are throttled so a loop of waits cannot spend a bridge
    # round trip per iteration on a badge that is already lit.
    last_presence_ping: float = 0.0
    # Visual click location survives a target removing itself or navigating.
    # Separate from the actual CDP pointer used by relative input.
    presence_click_point: tuple[float, float] | None = None
    # Wall-clock, unlike `last_used`, because these two are reported to a reader
    # and a monotonic number means nothing to one.
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    # The last page this session was seen on, recorded whenever a call summarises
    # it. The status topic reports every session at once, and asking another
    # agent's tab for its URL would mean waiting on that agent's lock - so what
    # is reported is the last thing seen, labelled as such.
    last_url: str | None = None
    last_title: str | None = None
    # Opened with persist=true: at process exit the tab is detached and parked in
    # sessions/parking.py instead of closed, so a later client re-attaches it.
    persist: bool = False
    # Whether the tab-strip label is wanted; followed/re-attached tabs honour it too.
    label_tab: bool = True
    # When the persist record was last written, so use can refresh it (throttled).
    parked_at: float = 0.0
    # One-shot fields for the next page summary (a re-attach, a followed tab), so
    # the caller learns about a recovery on the very call that benefited from it.
    pending_notice: dict[str, Any] | None = None
