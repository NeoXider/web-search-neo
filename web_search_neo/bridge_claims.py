"""Pure helpers for the bridge daemon's tab-claim register.

Kept out of ``bridge_daemon`` so the rules that decide whose tab a command
touches can be read, and tested, on their own.
"""

from __future__ import annotations

import math
import re
from typing import Any

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
