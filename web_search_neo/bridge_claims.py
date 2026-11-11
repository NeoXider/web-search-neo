"""Pure helpers for the bridge daemon's tab-claim register.

Kept out of ``bridge_daemon`` so the rules that decide whose tab a command
touches can be read, and tested, on their own.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

LOGGER = logging.getLogger("web_search_neo.bridge.claims")
_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_PREFIXED = re.compile(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+")


def js_number_tab_id(raw: Any) -> tuple[int | None, str | None]:
    """Read ``raw`` the way the companion's ``Number(params.tabId)`` will.

    Returns ``(tab_id, None)`` for a value JavaScript turns into an integer,
    ``(None, None)`` when no tab id was sent, and ``(None, reason)`` for any
    other value: a command whose target the daemon cannot read the same way the
    companion does must not slip past the claim check.
    """
    if raw is None:
        return None, None
    value: float | int | None = None
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if _PREFIXED.fullmatch(text):
            value = int(text, 0)
        elif _DECIMAL.fullmatch(text):
            value = float(text)
    if isinstance(value, float):
        value = int(value) if math.isfinite(value) and value.is_integer() else None
    if value is None:
        return None, (
            f"tabId must be an integer tab id, not {raw!r}; the command was not sent"
        )
    return value, None


def same_agent(holder: Any, requester: Any) -> bool:
    """True when two client records are the same MCP server process.

    A server that reconnects gets a new connection while its old, half-open one
    may still be registered; the process id, program name and - when both sent
    one - the bridge instance id are what survive. One process may run several
    bridge objects, so differing instance ids keep them apart.
    """
    if holder is requester:
        return True
    pid = getattr(holder, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid != getattr(requester, "pid", None):
        return False
    if getattr(holder, "program", "") != getattr(requester, "program", ""):
        return False
    mine, theirs = getattr(holder, "instance", ""), getattr(requester, "instance", "")
    return not (mine and theirs) or mine == theirs


def claim_is_own(holder: Any, requester: Any, live_clients: Any) -> bool:
    """A claim belongs to ``requester`` if it holds it, or its holder is a gone
    or superseded connection of the same process."""
    if holder is requester:
        return True
    return holder not in live_clients or same_agent(holder, requester)


def held_tabs(claims: dict[int, Any]) -> str:
    """``tab 5: main.py#412, held 226 min`` for every claim, for a message a person reads."""
    parts = []
    for tab_id in sorted(claims):
        claim = claims.get(tab_id)
        label = getattr(getattr(claim, "client", None), "label", None) or "an agent"
        held = getattr(claim, "held_seconds", None)
        parts.append(f"tab {tab_id}: {label}" + (f", held {held() / 60:.0f} min" if callable(held) else ""))
    return "; ".join(parts)


def reload_refusal(claims: dict[int, Any], params: Any) -> str | None:
    """Why a companion reload is not sent to Chrome, or None when it may go.

    A reload detaches the debugger from every tab, so it waits while any tab is
    held - a tab is held from an agent's ``open`` to its ``close``, hours of idle
    time included, and the refusal names who holds what. ``force`` in the request
    is the owner's word that they may be cut off. Only ``--update --force`` sends
    it: before 1.30.1 that command restarted the daemon and was then refused here
    all the same, because an agent's server had reconnected and claimed its tab
    again before the companion was back.
    """
    if not claims:
        return None
    if isinstance(params, dict) and params.get("force") is True:
        LOGGER.warning("A forced companion reload detaches %s", held_tabs(claims))
        return None
    return (
        f"The companion was not reloaded: it would detach the tabs agents hold ({held_tabs(claims)}). "
        "It is reloaded once no tab is held; the owner can cut the agents off with "
        "`python main.py --update --force`."
    )
