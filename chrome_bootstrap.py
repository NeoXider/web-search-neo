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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import bridge_auth
from chrome_bridge import CHROME_EXTENSION_ID, get_chrome_bridge


EXTENSION_NAME = "Web Search Neo Companion"
EXTENSION_DIR = (Path(__file__).resolve().parent / "chrome-extension").resolve()


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


def _companion_state(status: dict[str, Any], expected_version: str) -> dict[str, Any]:
    connected = bool(status.get("connected"))
    running = str((status.get("browser") or {}).get("extension_version") or "")
    outdated = bool(connected and expected_version and running != expected_version)
    return {
        "already_connected": connected,
        "ready": connected and not outdated,
        "update_required": outdated,
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
        return {
            "steps": reload_steps(),
            "next": (
                f"The connected companion is {state['connected_version']} but this "
                f"server ships {state['extension_version']}. Chrome keeps running the "
                "service worker it already loaded, and builds 1.2.0 and older do not "
                "authenticate against the bridge, so the server rejects them. Press "
                "Reload on the card to pick up the current build and secret."
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

    This changes nothing in any browser. It writes the shared secret, compares
    the bundled build with the connected one, and returns the manual steps.
    Raise ``wait_seconds`` when calling right after the user pressed Load
    unpacked, so the companion has time to reach the bridge.
    """
    wait = max(0.0, min(float(wait_seconds), 30.0))

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
    guidance = _guidance(state, manifest_error)

    result: dict[str, Any] = {
        "success": bool(state["ready"] or not manifest_error),
        **state,
        "extension_id": CHROME_EXTENSION_ID,
        "extension_directory": str(EXTENSION_DIR),
        "manual_steps": guidance["steps"],
        "next": guidance["next"],
        "current_chrome": status,
        **token_info,
    }
    if "message" in guidance:
        result["message"] = guidance["message"]
    if manifest_error:
        result["error"] = manifest_error
    return result
