"""The session side of agent presence: arm it, mark each action, lead a gesture.

Moved out of ``browser_tools`` (a size-ratcheted facade) unchanged in
behaviour, plus two calls that show an action *while* it runs rather than after
it returned: ``lead_gesture`` for a pointer gesture sent over time, and
``lead_action`` for a slow step such as a wait. ``browser_tools`` keeps its old
names as aliases and thin wrappers, so callers and tests are unaffected.

Every function here is decoration and swallows its own failures - a page that
is gone, a backend without CDP, a companion whose presence switch is off (it
answers every script carrying the ``__wsnPresence`` marker with a stand-in) -
because a signal must never cost a caller an action.
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import Any, Callable

from web_search_neo import agent_presence, presence_icon

logger = logging.getLogger(__name__)

# How long a drawing-nothing action stays silent after the last mark. Well under
# the five minutes the badge lives, so no throttled step can let it lapse.
QUIET_SECONDS = 1.0

# The slow steps that say what they are doing before they start: a wait is
# mostly waiting, and a caption that appears once it is over describes nothing.
# Hot-path input (pointer, input, press_keys, type_text, touch, step) is never
# led this way, because a lead costs one page round trip.
LEAD_ACTIONS = frozenset({"wait", "wait_challenge", "wait_frames", "captcha", "navigate", "reload"})
# How long a led step is assumed to run when it names no limit of its own; the
# page caps it (presence_badge.IN_FLIGHT_MAX_MS) and the step's own note ends it.
DEFAULT_IN_FLIGHT_SECONDS = 30.0

# Set while a macro script drives the tabs (macro_flow.run_record): its actions mark the
# tab with the macro badge - a robot - instead of the agent's slime, so a person can tell
# a replayed script from a model thinking live.
MACRO_RUN: contextvars.ContextVar[str | None] = contextvars.ContextVar("wsn_macro_run", default=None)

# The gestures whose lead ends in a press.
_TAP_GESTURES = frozenset({"click", "double_click"})
# Sessions (by id) whose last mark was an action lead. The note that closes
# such a step is never throttled: it is what tells the badge the step is over.
_LED: dict[int, float] = {}


def applies(session: Any) -> bool:
    """Visible normal-mode sessions qualify; frozen/headless pages skip overlays."""
    return (
        bool(agent_presence.enabled())
        and not session.headless
        and session.render_mode == "normal"
    )


def remove(session: Any) -> None:
    """Give the favicon back and drop the installed script. Best-effort.

    Called from teardown paths where the driver may already be half gone; a
    badge left on a tab that is closing is cosmetic and never worth an error.
    Caller holds the session lock.
    """
    _LED.pop(id(session), None)
    if session.presence_script_id:
        try:
            session.driver.execute_cdp_cmd(
                "Page.removeScriptToEvaluateOnNewDocument",
                {"identifier": session.presence_script_id},
            )
        except Exception:
            pass
        session.presence_script_id = None
    if not session.last_presence_ping:
        return  # never painted: a handed-back tab must come back untouched
    session.last_presence_ping = 0.0
    try:
        session.driver.execute_script(agent_presence.RESTORE_SCRIPT)
    except Exception:
        pass


def apply(session: Any, session_id: str) -> None:
    """Arm the presence script for every document this session will open.

    Registration alone marks nothing: the script installs the machinery and
    waits, and it is ``note`` after each action that lights the badge. Caller
    holds the session lock.
    """
    if not applies(session):
        remove(session)
        return
    if session.presence_script_id:
        return
    try:
        result = session.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": agent_presence.install_source()},
        )
    except Exception as exc:
        # No CDP on this backend, or a bridge that refused: the per-action ping
        # installs the script itself when it finds none, so the signals still
        # work, they just do not survive a navigation for free.
        logger.debug(
            "Agent presence for session '%s' was not pre-registered: %s: %s",
            session_id,
            type(exc).__name__,
            exc,
        )
        return
    session.presence_script_id = str((result or {}).get("identifier") or "")


def hide_overlays(driver: Any, hide: bool = True) -> None:
    """Take the action flashes off the page before it is photographed.

    A flash lives 250 ms, and a screenshot taken inside that window would hand
    an agent a picture of a box its own click drew - which it would then read as
    something the page did. Cheap enough to run before every capture, and a
    no-op in a page that has no overlay up. The ghost cursor, its trail, the
    caption and the press rings go with them; pass ``hide=False`` after the
    capture to bring the cursor and a still-live caption back.
    """
    script = agent_presence.HIDE_FLASHES_SCRIPT if hide else agent_presence.SHOW_EPHEMERAL_SCRIPT
    try:
        driver.execute_script(script)
    except Exception:
        pass


def _ping(session: Any, payload: dict[str, Any], *, install: bool) -> bool:
    driver = session.driver
    marked = driver.execute_script(agent_presence.ping_script(), payload)
    if not marked and install:
        # Nothing installed in this document - a navigation the registration did
        # not cover, or a backend without CDP. Install it here and mark the page in
        # the same call rather than skipping the signal.
        driver.execute_script(agent_presence.install_source())
        marked = driver.execute_script(agent_presence.ping_script(), payload)
    if isinstance(marked, dict) and marked.get("needBase"):
        presence_icon.deliver(session, str(marked["needBase"]))
    return bool(marked)


def note(
    session: Any,
    session_id: str,
    action: str,
    arguments: dict[str, Any] | None = None,
    ok: bool = True,
) -> bool:
    """Decorate a completed action without failing the action if the page is gone."""
    if not applies(session):
        return False
    payload = agent_presence.payload_for(
        action,
        arguments,
        ok=ok,
        label=session.agent_label or session_id,
        pointer=(session.pointer_x, session.pointer_y),
        held=bool(session.held_buttons),
    )
    if action == "click" and "cursor" not in payload and ok and session.presence_click_point:
        x, y = session.presence_click_point
        payload["cursor"] = {"x": x, "y": y, "tap": True}
    if action == "click":
        session.presence_click_point = None
    if MACRO_RUN.get():
        payload["macro"] = MACRO_RUN.get()
    now = time.monotonic()
    led = _LED.pop(id(session), None) is not None
    if not payload["flash"] and not led and now - session.last_presence_ping < QUIET_SECONDS:
        # A step that draws nothing only refreshes the badge's five minutes, and
        # the badge cannot tell one second from the next. A polling loop would
        # otherwise pay a round trip per iteration for no visible difference.
        return False
    session.last_presence_ping = now
    try:
        with session.lock:
            return _ping(session, payload, install=True)
    except Exception:
        return False


def _lead(session: Any, payload: dict[str, Any]) -> bool:
    """Ping an installed script only: a lead is not worth shipping the installer.

    The action's own note follows and installs it when the document has none.
    """
    payload["flash"] = False
    try:
        marked = _ping(session, payload, install=False)
    except Exception:
        return False
    if marked:
        session.last_presence_ping = time.monotonic()
    return marked


def lead_gesture(
    session: Any,
    action: str,
    origin: tuple[float, float] | None,
    target: tuple[float, float] | None,
    glide: Any = None,
    *,
    finish: tuple[float, float] | None = None,
    drag_seconds: float = 0.0,
) -> bool:
    """Show a pointer gesture while it is being sent. Caller holds the session lock.

    ``glide`` is the ``pointer_path.Glide`` the events follow (or None when the
    pointer jumps), so the arrow plays the same arc over the same milliseconds.
    ``finish`` and ``drag_seconds`` describe a drag that follows the approach.
    The driver must be on the top document: these are page coordinates.
    """
    if target is None or not applies(session):
        return False
    payload = agent_presence.payload_for(
        "pointer", {"pointer_action": action}, label=session.agent_label, pointer=target,
    )
    if "cursor" not in payload:
        return False  # not a point on the page
    lead: dict[str, Any] = {"tap": action in _TAP_GESTURES}
    if glide is not None:
        lead.update(ms=glide.ms, bend=glide.bend)
    if origin is not None:
        lead.update(fromX=float(origin[0]), fromY=float(origin[1]))
    if finish is not None:
        lead.update(dragX=float(finish[0]), dragY=float(finish[1]), dragMs=round(max(0.0, drag_seconds) * 1000))
    payload["lead"] = lead
    return _lead(session, payload)


def lead_action(
    get_session: Callable[[str], Any],
    session_id: str,
    action: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    """Caption a slow step (``LEAD_ACTIONS``) before it runs. Never raises."""
    if action not in LEAD_ACTIONS or not agent_presence.enabled():
        return False
    try:
        session = get_session(session_id)
    except Exception:
        return False
    if not applies(session):
        return False
    payload = agent_presence.payload_for(action, arguments, label=session.agent_label or session_id)
    source = arguments or {}
    limit = next((source[key] for key in ("timeout_seconds", "seconds")
                  if isinstance(source.get(key), (int, float)) and source[key] > 0), DEFAULT_IN_FLIGHT_SECONDS)
    # The badge keeps pulsing for as long as the step may take.
    payload["inFlightMs"] = int(float(limit) * 1000)
    try:
        with session.lock:
            marked = _lead(session, payload)
    except Exception:
        return False
    if marked:
        _LED[id(session)] = time.monotonic()
    return marked
