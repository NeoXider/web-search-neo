"""Bring a connected Chrome companion up to the server's version, safely.

Chrome keeps running the service worker it loaded; after the checkout is
updated the live companion keeps its old commands until something reloads it.
This module decides when the server may do that on its own:

- only when the companion's manifest VERSION differs from the folder's. A
  difference in the code hash alone (same version, edited files - a developer's
  checkout) is reported, never acted on: it is not a release, and a folder that
  never matches would otherwise be reloaded forever;
- only while no agent drives a tab - no claim in the daemon and no current-Chrome
  session in this server - because a reload detaches every debugger session;
- at most once per interval across every MCP server process of this user (the
  timestamp lives in a small per-user state file under a cross-process lock);
- never again for a (running version, live hash, folder version, folder hash)
  combination whose reload already failed to take: that is recorded as
  ineffective and answered with the manual steps instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from web_search_neo.chrome_bootstrap import (
    _reload_companion, expected_code_hash, expected_extension_version, reload_steps,
)
from web_search_neo.chrome_bridge import get_chrome_bridge
from web_search_neo.sessions.file_lock import atomic_write_text, exclusive

AUTO_RELOAD_INTERVAL_SECONDS = 300.0
_INEFFECTIVE_LIMIT = 20


def state_path() -> Path:
    """Per-user state file; ``WEB_SEARCH_NEO_COMPANION_STATE_FILE`` overrides it."""
    override = (os.getenv("WEB_SEARCH_NEO_COMPANION_STATE_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "WebSearchNeo"
    else:
        base = Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "web-search-neo"
    return base / "companion_refresh.json"


def _read(path: Path) -> dict[str, Any]:
    """The state file, with anything damaged treated as absent (B1)."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    stamp = state.get("last_auto_reload")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp > time.time() + 60:
        state["last_auto_reload"] = 0.0  # garbage or a clock that jumped: no pause
    ineffective = state.get("ineffective")
    state["ineffective"] = (
        [item for item in ineffective if isinstance(item, str)]
        if isinstance(ineffective, list) else []
    )
    return state


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """``1.18.5`` -> ``(1, 18, 5)``; None for anything that is not plain dotted numbers."""
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return None


def _manual(reason: str, pair: str | None = None) -> dict[str, Any]:
    return {"self_update": "ineffective", "reason": reason, "manual_steps": reload_steps(),
            **({"combination": pair} if pair else {})}


def refresh_stale_companion(bridge: Any = None, *, local_sessions: int = 0) -> dict[str, Any] | None:
    """Reload an outdated companion when it is safe; ``None`` when nothing was needed.

    ``local_sessions`` is how many current-Chrome sessions this server holds -
    sessions a daemon that was unreachable at claim time never heard of.
    """
    bridge = bridge or get_chrome_bridge()
    status = bridge.status(0.0)
    if not status.get("connected"):
        return None
    browser = status.get("browser") or {}
    running = str(browser.get("extension_version") or "")
    expected = expected_extension_version()
    live_hash, disk_hash = browser.get("code_hash"), expected_code_hash()
    version_differs = bool(running and expected and running != expected)
    running_version, expected_version = _version_tuple(running), _version_tuple(expected)
    older = (running_version is not None and expected_version is not None
             and running_version < expected_version)
    hash_differs = bool(isinstance(live_hash, str) and disk_hash and live_hash != disk_hash)
    if not version_differs:
        if hash_differs:
            return {"self_update": "not_attempted", "stale_code": True, "reason": (
                "The companion reports this server's version but runs older code than its "
                "folder (an edited checkout, not a release); it is not reloaded automatically. "
                "Reload it with setup_current_chrome or the steps below."),
                "manual_steps": reload_steps()}
        return None
    if not older:  # a newer companion, or versions that do not compare: never downgrade
        return {"self_update": "not_attempted", "reason": (
            f"The companion runs {running} and this server ships {expected}; only an older "
            "companion is reloaded automatically."), "manual_steps": reload_steps()}
    deferred = {"self_update": "deferred",
                "reason": "an agent is driving a tab; the companion is reloaded once none is"}
    if (status.get("daemon") or {}).get("claims") or local_sessions:
        return deferred
    pair = f"{running}|{live_hash}|{expected}|{disk_hash}"
    path = state_path()
    with exclusive(path):
        state = _read(path)
        if pair in (state.get("ineffective") or []):
            return _manual("An earlier automatic reload of this exact companion build did not "
                           "take, so it is not retried; reload it by hand.", pair)
        if time.time() - float(state.get("last_auto_reload") or 0) < AUTO_RELOAD_INTERVAL_SECONDS:
            return None
        state["last_auto_reload"] = time.time()
        atomic_write_text(path, json.dumps(state))
    # Claims can appear while the lock above was held: look again right before the
    # reload (B3). The daemon also refuses runtime.reload while any tab is claimed.
    if (bridge.status(1.0).get("daemon") or {}).get("claims"):
        return deferred
    result = _reload_companion(bridge, expected)
    if result.get("self_update") == "done":
        return result
    with exclusive(path):
        state = _read(path)
        state["ineffective"] = ((state.get("ineffective") or []) + [pair])[-_INEFFECTIVE_LIMIT:]
        atomic_write_text(path, json.dumps(state))
    return {**_manual("The automatic reload did not bring the companion up to this server's "
                      "build.", pair), "attempt": result}


def explain_tabs(answer: dict[str, Any], local_sessions: int, relist: Any) -> dict[str, Any]:
    """Refresh an outdated companion behind ``browser_tabs``, or say why it is absent."""
    if answer.get("connected"):
        try:
            refresh = refresh_stale_companion(local_sessions=local_sessions)
        except Exception as exc:
            refresh = {"self_update": "failed", "error": f"{type(exc).__name__}: {exc}"}
        if refresh:
            fresh = relist() if refresh.get("self_update") == "done" else answer
            return {**fresh, "companion_refresh": refresh}
        return answer
    if (answer.get("daemon") or {}).get("linked"):
        answer["companion_note"] = (
            "The bridge is up but the Chrome companion has not reconnected yet. After the "
            "bridge starts it retries on its own within about a minute: call again with "
            "wait_seconds=75. If it is still false, open the companion popup in Chrome and "
            "press Reconnect (it reads Restart companion when its service worker has "
            "stopped); only if that is missing, press Reload on its card at chrome://extensions."
        )
    return answer
