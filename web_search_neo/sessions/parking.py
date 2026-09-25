"""Registry of persistent (parked) sessions shared by every MCP client of one user.

A session normally lives exactly as long as the MCP server process that opened
it: the stdio client exits, the process's atexit hook closes every tab it opened,
and a later client asking for the same ``session_id`` is told it does not exist.
A session opened with ``persist=true`` is recorded here instead. At process exit
its tab is left open and only detached; the next process that is asked for that
``session_id`` finds the record and re-attaches to the same tab.

The record is advisory, never a lock: the tab claim the bridge daemon keeps is
still what stops two live clients from driving one tab. Every record carries the
Chrome ``browser_run`` it was made in, because tab ids restart with the browser
and a record from an earlier run would name somebody else's tab.

Stored as one small JSON file in the per-user state directory, written under a
cross-process lock (``file_lock``), so it survives the bridge daemon restarting
as well as the MCP client.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from web_search_neo.sessions.file_lock import atomic_write_text, exclusive

DEFAULT_PARKED_TTL_SECONDS = 24 * 3600.0
_FILE_NAME = "parked_sessions.json"


def registry_path() -> Path:
    """Where the registry lives; ``WEB_SEARCH_NEO_PARKED_SESSIONS_FILE`` overrides it."""
    override = (os.getenv("WEB_SEARCH_NEO_PARKED_SESSIONS_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "WebSearchNeo" / _FILE_NAME
    base = Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "web-search-neo" / _FILE_NAME


MIN_PARKED_TTL_SECONDS = 60.0
# A record written "in the future" by more than this is not trusted: a clock
# jump or a hand-edited file must not make a record immortal.
_FUTURE_SLACK_SECONDS = 300.0


def ttl_seconds() -> float:
    """How long an untouched record stays re-attachable (``WEB_SEARCH_NEO_PARKED_SESSION_TTL``).

    Always finite and at least a minute: zero, negative or garbage falls back to
    the default, because "never expires" would leave the user's tabs parked forever.
    """
    try:
        value = float(os.getenv("WEB_SEARCH_NEO_PARKED_SESSION_TTL", "") or DEFAULT_PARKED_TTL_SECONDS)
    except ValueError:
        return DEFAULT_PARKED_TTL_SECONDS
    if not value > 0 or value == float("inf"):
        return DEFAULT_PARKED_TTL_SECONDS
    return max(MIN_PARKED_TTL_SECONDS, value)


def _read(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _is_fresh(record: dict[str, Any], now: float) -> bool:
    stamp = record.get("updated_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    return now - ttl_seconds() <= float(stamp) <= now + _FUTURE_SLACK_SECONDS


def _fresh(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    now = time.time()
    return {key: value for key, value in records.items() if _is_fresh(value, now)}


def remember(session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Create or refresh the record for ``session_id``; returns what was stored.

    Expired records are kept until ``take_expired`` hands them to the caller:
    dropping them here would leave their tabs open with nobody told.
    """
    path = registry_path()
    stored = {**record, "session_id": session_id, "updated_at": time.time()}
    with exclusive(path):
        records = _read(path)
        records[session_id] = stored
        atomic_write_text(path, json.dumps(records, ensure_ascii=False, indent=1))
    return stored


def lookup(session_id: str) -> dict[str, Any] | None:
    """The live (not expired) record for ``session_id``, or ``None``."""
    path = registry_path()
    if not path.exists():
        return None
    with exclusive(path):
        found = _fresh(_read(path)).get(session_id)
    return dict(found, session_id=session_id) if found else None


def forget(session_id: str) -> bool:
    """Drop the record; true when one existed. Never raises for a missing file."""
    path = registry_path()
    if not path.exists():
        return False
    with exclusive(path):
        records = _read(path)
        existed = records.pop(session_id, None) is not None
        if existed:
            atomic_write_text(path, json.dumps(records, ensure_ascii=False, indent=1))
    return existed


def take_expired() -> list[dict[str, Any]]:
    """Remove and return every expired (or unreadable-stamp) record, for the caller to retire."""
    path = registry_path()
    if not path.exists():
        return []
    with exclusive(path):
        records = _read(path)
        now = time.time()
        expired = [dict(value, session_id=key) for key, value in records.items() if not _is_fresh(value, now)]
        if expired:
            atomic_write_text(path, json.dumps(
                {key: value for key, value in records.items() if _is_fresh(value, now)},
                ensure_ascii=False, indent=1,
            ))
    return expired


def is_fresh(record: dict[str, Any], now: float | None = None) -> bool:
    """Whether ``record`` is inside the TTL (an unreadable or future stamp is not)."""
    return _is_fresh(record, time.time() if now is None else now)


def transact(mutate: Callable[[dict[str, dict[str, Any]]], Any], path: Path | None = None) -> Any:
    """Read, change and write a registry under the cross-process lock in one step.

    ``mutate`` edits the records in place and returns what the caller wants back;
    the file is rewritten only when something changed. ``path`` names another
    registry file (parked owned browsers keep theirs next to this one).
    """
    path = path or registry_path()
    with exclusive(path):
        records = _read(path)
        before = json.dumps(records, sort_keys=True)
        result = mutate(records)
        if json.dumps(records, sort_keys=True) != before:
            atomic_write_text(path, json.dumps(records, ensure_ascii=False, indent=1))
    return result


def list_records() -> list[dict[str, Any]]:
    """Every live record, oldest first, for the status roster."""
    path = registry_path()
    if not path.exists():
        return []
    with exclusive(path):
        records = _fresh(_read(path))
    rows = [dict(value, session_id=key) for key, value in records.items()]
    return sorted(rows, key=lambda item: float(item.get("updated_at") or 0))
