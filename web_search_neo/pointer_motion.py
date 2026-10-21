"""How the pointer reaches an action's target: a short eased glide, shown live.

The CDP pointer used to teleport onto every target. A person never does that,
and a page that watches the mouse - a hover menu, a drag-and-drop library, a
bot score - can tell. For the gestures that are not frame-by-frame game input
the pointer now travels from where this session last left it, along the arc in
``actions/pointer_path.py``, in at most about a fifth of a second:

* a click aimed at an element (``click`` with ``trusted``, ``text`` or x/y,
  and ``click_text``), and ``pointer`` with ``pointer_action='hover'``;
* a drag that was given time (``duration_seconds`` > 0) approaches its start.

``pointer`` move/click/press/release/wheel, ``input`` batches and ``touch``
keep teleporting: they are the hot path a game loop drives, and every extra
event would be latency it never asked for. ``WEB_SEARCH_NEO_POINTER_GLIDE=0``
turns the glide off everywhere.

Just before the first event the same gesture is handed to the page's presence
overlay (``presence_live.lead_gesture``), so the ghost cursor a person watches
travels with the real events instead of catching up after the action.
"""

from __future__ import annotations

from typing import Any, Callable

from web_search_neo import presence_live
from web_search_neo.actions import pointer_path

# The verbs an approach can precede; anything else lands where it is aimed.
GLIDE_ACTIONS = frozenset({"click", "double_click", "move", "hover", "drag"})


def approach(
    session: Any,
    action: str,
    start: tuple[float, float],
    finish: tuple[float, float],
    frame_map: Any,
    duration: float,
    move: Callable[[float, float], None],
    *,
    glide: bool,
) -> None:
    """Glide the pointer onto ``start`` before ``action`` happens there.

    ``glide`` is the caller's decision that this gesture may take the time;
    ``move`` sends one ``mouseMoved`` carrying the buttons already held. A
    pointer lock (no position at all), a non-normal render mode (frames are not
    running between the events) or an unknown origin - a session that never
    moved the pointer starts at (0, 0), which is not where a hand would be -
    leaves the pointer to land directly, as before. Caller holds the lock and
    the driver is on the top document.
    """
    verb = str(action or "").strip().lower()
    if not glide or verb not in GLIDE_ACTIONS:
        return
    if session.pointer_locked or session.render_mode != "normal":
        return
    origin = (float(session.pointer_x), float(session.pointer_y))
    known = origin != (0.0, 0.0)
    bounds = (float(getattr(frame_map, "page_width", 0.0) or 0.0),
              float(getattr(frame_map, "page_height", 0.0) or 0.0))
    plan = (
        pointer_path.plan_approach(origin, start, bounds=bounds)
        if known and pointer_path.glide_enabled() else None
    )
    presence_live.lead_gesture(
        session, verb, origin if known else None, start, plan,
        finish=finish if verb == "drag" else None, drag_seconds=duration,
    )
    if plan is not None:
        pointer_path.run(plan, move)
