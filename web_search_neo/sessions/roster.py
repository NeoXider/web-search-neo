"""One roster row per session, for the driverless ``sessions_overview``.

Moved out of browser_tools.py (a size-ratcheted facade) unchanged in behaviour:
nothing here touches a driver, so reading the roster never waits on a session
another agent is driving.
"""
from __future__ import annotations

import time
from typing import Any

ACTIVITY_NOTE = (
    "An agent is driving this tab now or was within the last 5 minutes (its favicon "
    "carries the activity dot). Observe it read-only via page_text/page_outline/"
    "screenshot, or open your own session instead of acting on this one."
)


def iso_local(timestamp: float | None) -> str | None:
    """Format a wall-clock timestamp the way a reader can compare against a clock."""
    if not timestamp:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp)))
    except (OverflowError, OSError, ValueError):
        return None


def session_row(name: str, session: Any, active_window_seconds: float) -> dict[str, Any]:
    """What the roster says about one session, from memory only."""
    idle_seconds = round(max(0.0, time.monotonic() - session.last_used), 1)
    busy = bool(session.lock.busy)
    # Active means driven right now or within the last five minutes - the same
    # window the tab's activity badge glows. A human reading the roster sees
    # which tabs are agent-held; an agent sees which to observe read-only.
    agent_active = busy or idle_seconds < active_window_seconds
    return {
        "session_id": name,
        "agent_label": session.agent_label,
        "current_tab_id": session.current_tab_id,
        "tab_group": session.tab_group,
        "profile_mode": session.profile_mode,
        "headless": session.headless,
        # "Last seen", not "current": a session never summarised reports None.
        "last_url": session.last_url,
        "last_title": session.last_title,
        "created_at": iso_local(session.created_at),
        "last_used_at": iso_local(session.last_used_at),
        "idle_seconds": idle_seconds,
        "busy": busy,
        "concurrent_callers": session.lock.concurrent_callers,
        "agent_active": agent_active,
        **({"activity_note": ACTIVITY_NOTE} if agent_active else {}),
    }


def close_all_note(wanted: str, owner: str | None, foreign: int, recent: int,
                    idle_for_seconds: float | None) -> str:
    """Say why each kept session was kept: another agent's, or used too recently."""
    whose = f"'{owner}'" if owner else "no agent_label (anonymous)"
    parts = ["scope='all' closed every session in this MCP server, including other agents'"
             if wanted == "all" else f"Closed only the sessions owned by {whose}"]
    if recent:
        parts.append(f"{recent} session(s) were used within the last {float(idle_for_seconds or 0):g} s "
                     "(or are busy right now) and were left running - lower idle_for_seconds to include them")
    if foreign and wanted != "all":
        parts.append(f"{foreign} session(s) belonging to other agents were left running; "
                     "scope='all' closes those too")
    return "; ".join(parts) + "."
