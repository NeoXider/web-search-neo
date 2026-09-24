"""Find the tab Chrome put in place of a session's vanished tab - and nothing else.

Chrome can swap the tab under a page without the page ever closing: a
prerendered or instant navigation replaces the tab and hands the page a new id
(``chrome.tabs.onReplaced``). The session still holds the old id, so its next
call fails with ``No tab with given id`` although its page is right there.

Only that record counts. The companion (1.18.4+) keeps it and answers
``tabs.resolve``; the successor must also still exist. There are deliberately no
heuristics - "a tab the lost one opened", "a tab on the same URL" - because this
is the user's own Chrome: a tab the agent's page opened (a payment window, an
OAuth popup) or one the user opened on the same address is not the agent's to
take over. No record, an older companion, or a companion that says "no
successor" all mean the same thing: there is nothing to follow.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_successor(bridge: Any, tab_id: int | None) -> int | None:
    """The live tab Chrome recorded as replacing ``tab_id``, or ``None``."""
    if tab_id is None or bridge is None:
        return None
    try:
        answer = bridge.request("tabs.resolve", {"tabId": int(tab_id)}, timeout=3.0) or {}
    except Exception as exc:  # an older companion: "Unknown bridge method"
        logger.debug("tabs.resolve unavailable for tab %s: %s", tab_id, exc)
        return None
    if not isinstance(answer, dict):
        return None
    successor = _as_int(answer.get("replaced_by"))
    if successor is None or successor == int(tab_id) or answer.get("successor_alive") is not True:
        return None
    return successor
