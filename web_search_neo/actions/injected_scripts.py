"""The ``inject_script`` action: page code registered to run before every document's scripts.

Kept apart from the browser facade; the caller holds the session lock and hands
over the session, whose ``injected_scripts`` list is the record of what this
session registered.
"""
from __future__ import annotations

from typing import Any


def run(session: Any, session_id: str, op: str, source: str | None, identifier: str | None) -> dict[str, Any]:
    """``add`` / ``list`` / ``remove`` one new-document script (lock held by the caller)."""
    if op == "add":
        if not source:
            raise ValueError("inject_script op 'add' requires source")
        result = session.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        script_id = str(result.get("identifier") or "")
        session.injected_scripts.append(script_id)
        return {"success": True, "session_id": session_id, "identifier": script_id}
    if op == "list":
        return {"success": True, "session_id": session_id, "identifiers": list(session.injected_scripts)}
    if op == "remove":
        removed = identifier in session.injected_scripts
        if removed:
            session.injected_scripts.remove(identifier)
            # Actually stop it in Chrome, not just forget the id: the CDP removal
            # exists, so a "removed" that left the script running on every future
            # document would be a lie the caller acts on.
            try:
                session.driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument",
                                               {"identifier": identifier})
            except Exception:
                # A backend without the removal still forgets the id; the script
                # lapses on the next navigation rather than at once.
                pass
        return {"success": True, "session_id": session_id, "identifier": identifier, "removed": removed}
    raise ValueError(f"inject_script op must be add, list, or remove, not '{op}'")
