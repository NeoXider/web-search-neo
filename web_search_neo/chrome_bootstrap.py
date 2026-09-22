"""Companion setup for Web Search Neo: prepare everything, then say what is left.

Chrome does not let a program add an unpacked extension to a browser the user
already has open. The installed set lives in Secure Preferences behind a MAC,
the policy path needs a packed CRX with an update URL, and a Chrome started
without a DevTools port cannot be given one afterwards. Driving Chrome's own UI
to fake the click was tried and removed: it depended on the interface language,
on which window had focus, and on a folder picker that the automation backend
does not enumerate at all.

So this module clicks nothing. It prepares what a machine can prepare - the
shared secret, the extension folder, the version comparison - and returns the
exact steps a person still has to perform.

Updating is a different matter. A companion that is already installed can be
told to re-read its own folder, so from 1.3.1 onwards this module updates a
stale build itself and only falls back to the manual Reload when the running
worker is too old to understand the request.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from web_search_neo import bridge_auth
from web_search_neo.extension_path import EXTENSION_DIR
from web_search_neo.chrome_bridge import CHROME_EXTENSION_ID, get_chrome_bridge


EXTENSION_NAME = "Web Search Neo Companion"
_SELF_UPDATE_POLL_SECONDS = 0.25


def prepare_bridge_token() -> dict[str, Any]:
    """Make sure the shared secret exists on disk and inside the extension folder."""
    try:
        token = bridge_auth.load_or_create_token()
        return {"token_ready": True, "token_file": str(bridge_auth.write_extension_token(token))}
    except Exception as exc:
        return {"token_ready": False, "token_error": f"{type(exc).__name__}: {exc}"}


def expected_extension_version() -> str:
    try:
        return str(json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        return ""


def expected_code_hash() -> str | None:
    """SHA-256 of the service worker this clone ships, or None when unreadable.

    The manifest version is a promise about the folder; the hash is a promise
    about what Chrome actually executed. An unpacked extension keeps running
    the worker it loaded until someone presses Reload, so a folder that gained
    new commands can sit behind an old worker whose version string still
    matches - and only this comparison sees it.
    """
    try:
        return hashlib.sha256((EXTENSION_DIR / "service-worker.js").read_bytes()).hexdigest()
    except Exception:
        return None


def manual_steps(extension_dir: Path | None = None) -> list[str]:
    """The steps Chrome reserves for a human, written to be relayed verbatim."""
    folder = str(extension_dir or EXTENSION_DIR)
    return [
        "1. Open a new tab and go to chrome://extensions",
        "2. Switch on 'Developer mode' (top-right corner of that page)",
        f"3. Click 'Load unpacked' and pick exactly this folder: {folder}",
        "4. Leave 'Web Search Neo Companion' enabled. Its toolbar badge turns ON "
        "when it reaches this server.",
    ]


def reload_steps() -> list[str]:
    """Steps for a companion that is connected but older than the bundled build."""
    return [
        "1. Open chrome://extensions",
        "2. Find the 'Web Search Neo Companion' card",
        "3. Press Reload on that card",
    ]


def _reload_companion(bridge: Any, expected_version: str) -> dict[str, Any]:
    """Ask a connected companion to reload itself, and report whether it came back.

    Chrome reserves the Reload button on an unpacked card for a human, but the
    worker can re-read its own folder from disk. Builds older than 1.3.1 have no
    such command, and that is exactly the case where a person still has to click.
    """
    try:
        answer = bridge.request("runtime.reload", {}, 10.0)
    except Exception as exc:
        return {"self_update": "unsupported", "self_update_error": f"{type(exc).__name__}: {exc}"}

    replaced = (answer or {}).get("version") if isinstance(answer, dict) else None
    # The worker answers before it dies, so the old version is still on the wire
    # here; only a later status read can tell whether the new one came back. A
    # same-version companion with stale code comes back the same way: its hello
    # carries the hash of what it now runs, and that must match the folder.
    disk_hash = expected_code_hash()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        status = bridge.status(1.0)
        browser = status.get("browser") or {}
        running = str(browser.get("extension_version") or "")
        live_hash = browser.get("code_hash")
        version_ok = not expected_version or running == expected_version
        hash_ok = disk_hash is None or not isinstance(live_hash, str) or live_hash == disk_hash
        if status.get("connected") and version_ok and hash_ok:
            return {"self_update": "done", "replaced_version": replaced}
        # status() answers at once while the daemon knows the worker is gone,
        # so without a pause this loop spins a core for the whole deadline.
        time.sleep(min(_SELF_UPDATE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    return {"self_update": "timeout", "replaced_version": replaced}


def _companion_state(status: dict[str, Any], expected_version: str) -> dict[str, Any]:
    connected = bool(status.get("connected"))
    browser = status.get("browser") or {}
    running = str(browser.get("extension_version") or "")
    outdated = bool(connected and expected_version and running != expected_version)
    live_hash = browser.get("code_hash")
    disk_hash = expected_code_hash()
    stale_code = bool(
        connected
        and isinstance(live_hash, str)
        and disk_hash is not None
        and live_hash != disk_hash
    )
    return {
        "already_connected": connected,
        "ready": connected and not outdated and not stale_code,
        "update_required": outdated or stale_code,
        "stale_code": stale_code,
        "extension_version": expected_version or None,
        "connected_version": running or None,
    }


def _guidance(state: dict[str, Any], manifest_error: str) -> dict[str, Any]:
    """Turn the measured state into steps a person can follow word for word."""
    if manifest_error:
        return {
            "steps": [],
            "next": (
                f"{manifest_error} The companion cannot be installed from this "
                "clone; re-clone or restore the chrome-extension folder."
            ),
        }
    if state["ready"]:
        return {
            "steps": [],
            "next": None,
            "message": (
                f"{EXTENSION_NAME} {state['extension_version']} is connected. Nothing to do."
            ),
        }
    if state["update_required"]:
        reason = (
            f"The connected companion is {state['connected_version']} but this "
            f"server ships {state['extension_version']}"
            if not state.get("stale_code")
            else "The connected companion reports the same version as this server, "
                 "but it is running older code than its folder contains"
        )
        return {
            "steps": reload_steps(),
            "next": (
                f"{reason}, and it could not be reloaded from here. Chrome keeps "
                "running the service worker it already loaded; builds before 1.3.1 "
                "cannot reload themselves and builds 1.2.0 and older do not "
                "authenticate against the bridge at all. Press Reload on the card "
                "to pick up the current build. Every update after this one applies "
                "without a click."
            ),
        }
    return {
        "steps": manual_steps(),
        "next": (
            "The companion is not connected. No program can install an unpacked "
            "extension into the Chrome the user already has open, so show these "
            "numbered steps verbatim and let them do it. Selenium modes "
            "(profile_mode temporary/persistent) need no extension at all."
        ),
    }


def setup_current_chrome(wait_seconds: float = 1.0) -> dict[str, Any]:
    """Prepare the companion and report exactly what is still missing.

    It writes the shared secret, compares the bundled build with the connected
    one, and returns the manual steps. It opens no page and reads no browsing
    data; its one effect on a browser is reloading a companion that is out of
    date, which is the only part of installing that a machine is allowed to do.
    Raise ``wait_seconds`` when calling right after the user pressed Load
    unpacked, so the companion has time to reach the bridge.
    """
    wait = max(0.0, float(wait_seconds))

    # The companion refuses to talk to the bridge without the shared secret, so
    # the token has to be on disk before Chrome ever reads the folder.
    token_info = prepare_bridge_token()
    manifest = EXTENSION_DIR / "manifest.json"
    expected_version = expected_extension_version()
    manifest_error = ""
    if not manifest.is_file():
        manifest_error = f"Companion manifest is missing: {manifest}."
    elif not expected_version:
        manifest_error = f"Companion manifest has no readable version: {manifest}."

    bridge = get_chrome_bridge()
    bridge.start()
    status = bridge.status(wait)
    state = _companion_state(status, expected_version)

    # A stale worker is the one case a machine can still fix on its own: since
    # 1.3.1 the companion can re-read its own folder, so try that before asking
    # a person to press a button Chrome only exposes to them.
    self_update: dict[str, Any] = {}
    if state["update_required"]:
        self_update = _reload_companion(bridge, expected_version)
        if self_update.get("self_update") == "done":
            status = bridge.status(1.0)
            state = _companion_state(status, expected_version)

    guidance = _guidance(state, manifest_error)

    result: dict[str, Any] = {
        "success": bool(state["ready"] or not manifest_error),
        **state,
        "extension_id": CHROME_EXTENSION_ID,
        "extension_directory": str(EXTENSION_DIR),
        "manual_steps": guidance["steps"],
        "next": guidance["next"],
        "current_chrome": status,
        # Driving a tab always raises Chrome's own debugging banner; say so
        # where the human will read it, with the one supported silence.
        "debug_banner": {
            "what": (
                "Chrome shows 'Web Search Neo Companion started debugging this "
                "browser' on every tab an agent drives. It is Chrome's mandatory "
                "banner and cannot be dismissed while the tab is driven; its "
                "Cancel detaches the debugger and the next agent action brings "
                "the banner back."
            ),
            "silence": (
                "Quit Chrome completely and relaunch it once with "
                "--silent-debugger-extension-api (Windows: chrome.exe "
                "--silent-debugger-extension-api; macOS: open -a 'Google Chrome' "
                "--args --silent-debugger-extension-api; Linux: google-chrome "
                "--silent-debugger-extension-api). Selenium modes "
                "(profile_mode temporary/persistent/isolated) show no banner."
            ),
        },
        **self_update,
        **token_info,
    }
    if "message" in guidance:
        result["message"] = guidance["message"]
    if manifest_error:
        result["error"] = manifest_error
    return result
