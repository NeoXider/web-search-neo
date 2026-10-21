"""The companion popup's view of LAN nodes, answered inside the bridge daemon.

The popup reaches nothing but its own service worker, and the worker reaches
nothing but this daemon, over the socket both already authenticated. So the
worker forwards ``{"type": "nodes", "id", "op", ...}`` frames here and gets
``{"type": "nodes_result", "id", "result" | "error"}`` back. Every operation
runs on its own thread: a LAN scan or a probe of an offline machine takes
seconds, and the extension's socket also carries every agent's commands.

Operations: ``overview`` (this machine, sharing state, every node; ``discover``
scans the LAN), ``add``/``remove`` a node, ``share`` (start or stop this
machine's node server), ``rotate_key``, and ``send_tab`` (open a URL in another
computer's Chrome and leave the tab there).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from web_search_neo.nodes import client, config, overview
from web_search_neo.process_probe import popen_detached

LOGGER = logging.getLogger("web_search_neo.bridge.nodes")
_CREATE_NO_WINDOW = 0x08000000
_START_WAIT_SECONDS = 8.0
_URL = re.compile(r"^https?://[^\s]{1,2040}$", re.I)


def _sharing(include_key: bool = True) -> dict[str, Any]:
    from web_search_neo.nodes import identity

    return overview.sharing_state(identity.lan_addresses(), include_key=include_key)


def start_node_process() -> None:
    """Start ``wsn --node`` detached, with the interpreter this daemon runs on."""
    command = [sys.executable, "-m", "web_search_neo.main", "--node"]
    popen_detached(command, windows=sys.platform == "win32", windows_flags=_CREATE_NO_WINDOW,
                   cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def _wait_running(expected: bool) -> bool:
    deadline = time.monotonic() + _START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if bool(_sharing(include_key=False).get("running")) == expected:
            return True
        time.sleep(0.25)
    return False


def _op_overview(message: dict[str, Any], version: str) -> dict[str, Any]:
    ensure_shared_node()
    result = overview.overview(
        version=version, local_extra={"browser": {"companion_connected": True}},
        discover=bool(message.get("discover")), refresh_public=bool(message.get("refresh")),
        include_key=False, timeout=2.5,
    )
    sharing = result["sharing"]
    if sharing.get("enabled") or sharing.get("running"):  # the popup is how a person pairs
        sharing["pair_key"] = config.display_key(config.load_or_create_key())
    return result


def _op_add(message: dict[str, Any], _version: str) -> dict[str, Any]:
    address = str(message.get("address") or "").strip()
    name = str(message.get("name") or "").strip() or config.normalize_address(address).split(":")[0]
    entry = config.add_node(name, address, message.get("key") or None)
    return {"saved": entry.public(), "status": overview.probe(entry)}


def _op_remove(message: dict[str, Any], _version: str) -> dict[str, Any]:
    return {"removed": config.remove_node(message.get("name"))}


def _op_share(message: dict[str, Any], _version: str) -> dict[str, Any]:
    enabled = bool(message.get("enabled"))
    config.set_share(enabled)
    running = bool(_sharing(include_key=False).get("running"))
    if enabled and not running:
        config.load_or_create_key()
        start_node_process()
        if not _wait_running(True):
            raise RuntimeError(
                "The node did not start within 8 s. Run `wsn --node` in a terminal to see why "
                f"(port {config.listen_port()} in use, or the firewall)."
            )
    elif not enabled and running:
        client.stop_local()
        _wait_running(False)
    return _sharing(include_key=enabled)


def _op_rotate_key(_message: dict[str, Any], _version: str) -> dict[str, Any]:
    config.rotate_key()
    if _sharing(include_key=False).get("running"):  # the running node still holds the old key
        client.stop_local()
        _wait_running(False)
        start_node_process()
        _wait_running(True)
    return _sharing(include_key=True)


def _op_send_tab(message: dict[str, Any], _version: str) -> dict[str, Any]:
    url = str(message.get("url") or "").strip()
    if not _URL.match(url):
        raise ValueError("Only http(s) pages can be sent to another computer.")
    entry = config.resolve(message.get("node"))
    session = f"companion-sent-{int(time.time() * 1000) % 10_000_000}"
    result = client.call(entry, "web_action", {"summary": "min", "actions": [
        {"action": "open", "url": url, "session_id": session, "profile_mode": "current",
         "agent_label": "companion"},
        {"action": "close", "session_id": session, "close_tab": False},
    ]}, timeout=60.0)
    ok = isinstance(result, dict) and result.get("success") is not False
    return {"sent": ok, "node": entry.name, "url": url,
            **({} if ok else {"error": str((result or {}).get("error") or result)[:400]})}


OPERATIONS: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
    "overview": _op_overview,
    "add": _op_add,
    "remove": _op_remove,
    "share": _op_share,
    "rotate_key": _op_rotate_key,
    "send_tab": _op_send_tab,
}


def _answer(send: Callable[[str], None], message: dict[str, Any], version: str) -> None:
    request_id = message.get("id")
    operation = OPERATIONS.get(str(message.get("op") or "overview"))
    try:
        if operation is None:
            raise ValueError(f"Unknown nodes operation {message.get('op')!r}")
        reply: dict[str, Any] = {"result": operation(message, version)}
    except Exception as exc:
        LOGGER.info("Popup nodes %s failed: %s: %s", message.get("op"), type(exc).__name__, exc)
        reply = {"error": str(exc) or type(exc).__name__}
    try:
        send(json.dumps({"type": "nodes_result", "id": request_id, **reply}, ensure_ascii=False,
                        default=str))
    except Exception as exc:  # the companion went away while we worked
        LOGGER.info("Could not deliver a nodes answer: %s: %s", type(exc).__name__, exc)


def handle(send: Callable[[str], None], message: dict[str, Any], version: str = "") -> None:
    """Answer one popup ``nodes`` frame on a worker thread."""
    threading.Thread(target=_answer, args=(send, message, version), name="wsn-bridge-nodes",
                     daemon=True).start()


AUTOSTART_ENV = "WEB_SEARCH_NEO_NODE_AUTOSTART"
# A node that cannot start (its port taken by something else) must not be
# respawned on every companion reconnect.
AUTOSTART_COOLDOWN_SECONDS = 60.0
_last_autostart = 0.0


def ensure_shared_node() -> None:
    """Start this computer's node when sharing is on and none answers.

    Sharing is on by default, so this is what makes a computer reachable from
    the others without anyone running ``wsn --node``: the daemon calls it each
    time the Chrome companion connects, and the popup each time it opens.
    ``WEB_SEARCH_NEO_NODE_AUTOSTART=0`` leaves starting it to a person.
    """
    global _last_autostart
    if os.getenv(AUTOSTART_ENV, "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        if not config.share_enabled() or _sharing(include_key=False).get("running"):
            return
        if time.monotonic() - _last_autostart < AUTOSTART_COOLDOWN_SECONDS:
            return
        _last_autostart = time.monotonic()
        config.load_or_create_key()
        start_node_process()
        LOGGER.info("Started this computer's LAN node (sharing is on)")
    except Exception as exc:
        LOGGER.info("Could not start the shared node: %s: %s", type(exc).__name__, exc)


def ensure_shared_node_soon() -> None:
    """:func:`ensure_shared_node` off the caller's thread (it probes and may spawn)."""
    threading.Thread(target=ensure_shared_node, name="wsn-node-autostart", daemon=True).start()
