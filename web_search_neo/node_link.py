"""The companion popup's view of LAN nodes, answered inside the bridge daemon.

The popup reaches nothing but its own service worker, and the worker reaches
nothing but this daemon, over the socket both already authenticated. So the
worker forwards ``{"type": "nodes", "id", "op", ...}`` frames here and gets
``{"type": "nodes_result", "id", "result" | "error"}`` back. Every operation
runs on its own thread: a LAN scan or a probe of an offline machine takes
seconds, and the extension's socket also carries every agent's commands.

Operations: ``overview`` (this machine, sharing state, every node; ``discover``
scans the LAN), ``add``/``remove`` a node, ``share`` (start or stop this
machine's node server), ``rotate_key``, ``send_tab`` (open a URL in another
computer's Chrome and leave the tab there), the button pairing of
:mod:`web_search_neo.nodes.pairing` - ``pair``/``pair_poll``/``pair_cancel`` on
the computer asking, ``pair_decide`` on the one being asked - and ``firewall``,
which admits the node through Windows Firewall behind its own UAC prompt.
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

from web_search_neo.nodes import client, config, firewall, overview, pairing
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
        include_key=False, timeout=2.5, firewall_wait=False,
    )
    sharing = result["sharing"]
    if sharing.get("enabled") or sharing.get("running"):  # the popup is how a person pairs
        sharing["pair_key"] = config.display_key(config.load_or_create_key())
    result["pair_requests"] = _waiting_requests() if sharing.get("running") else []
    result["pairings"] = _recent_pairings()
    return result


def _waiting_requests() -> list[dict[str, Any]]:
    try:
        waiting = client.local_call("GET", "/wsn/pair/waiting", None, timeout=2.0)
    except client.NodeError as exc:  # an older node, or one restarting
        LOGGER.debug("No pairing requests from the local node: %s", exc)
        return []
    return waiting if isinstance(waiting, list) else []


def _op_add(message: dict[str, Any], _version: str) -> dict[str, Any]:
    address = str(message.get("address") or "").strip()
    name = str(message.get("name") or "").strip() or config.normalize_address(address).split(":")[0]
    entry = config.add_node(name, address, message.get("key") or None)
    return {"saved": entry.public(), "status": overview.probe(entry)}


def _op_remove(message: dict[str, Any], _version: str) -> dict[str, Any]:
    return {"removed": config.remove_node(message.get("name"))}


def _settle_firewall() -> dict[str, Any]:
    """After the person switched sharing on: let the LAN in if Windows Firewall keeps it out.

    The switch is the person's consent, and Windows still asks its own question (UAC),
    so nothing changes unless they accept it there. A declined or failed prompt leaves
    sharing on and says why; the popup offers the same step again.
    """
    from web_search_neo.nodes import identity

    addresses = identity.lan_addresses()
    lan_ip = addresses[0] if addresses else ""
    verdict = firewall.state(lan_ip, refresh=True)
    if verdict.get("state") != "blocked":
        return verdict
    try:
        return firewall.open_ports(lan_ip)
    except Exception as exc:  # declined, timed out, or not allowed to change rules
        return {**verdict, "error": str(exc) or type(exc).__name__}


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
    state = _sharing(include_key=enabled)
    if enabled:
        state["firewall"] = _settle_firewall()
    return state


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


# Pairing attempts this daemon follows to the end, so closing the popup while the
# person walks to the other computer loses nothing: attempt id -> latest answer.
_PAIRINGS: dict[str, dict[str, Any]] = {}
_PAIRINGS_LOCK = threading.Lock()
_PAIRINGS_KEPT_SECONDS = 600.0
_PAIR_POLL_SECONDS = 1.5


def _note_pairing(attempt: str, answer: dict[str, Any]) -> None:
    with _PAIRINGS_LOCK:
        now = time.monotonic()
        for key in [key for key, item in _PAIRINGS.items() if now - item["at"] > _PAIRINGS_KEPT_SECONDS]:
            _PAIRINGS.pop(key, None)
        _PAIRINGS[attempt] = {**answer, "attempt": attempt, "at": now}


def _follow_pairing(attempt: str, started: dict[str, Any]) -> None:
    deadline = time.monotonic() + pairing.ATTEMPT_TTL_SECONDS
    answer: dict[str, Any] = {**started, "state": "expired"}
    while time.monotonic() < deadline:
        try:
            answer = pairing.check(attempt)
        except Exception as exc:  # unreachable, refused, or a seal that does not verify
            answer = {**started, "state": "failed", "error": str(exc) or type(exc).__name__}
            break
        if answer.get("state") != "waiting":
            break
        time.sleep(_PAIR_POLL_SECONDS)
    with _PAIRINGS_LOCK:
        if _PAIRINGS.get(attempt, {}).get("state") == "cancelled":
            return
    _note_pairing(attempt, answer)
    if answer.get("state") == "paired":
        try:
            answer["status"] = overview.probe(config.resolve(answer["saved"]["name"]))
        except Exception as exc:
            LOGGER.info("Paired, but the first probe failed: %s", exc)
        _note_pairing(attempt, answer)


def _recent_pairings() -> list[dict[str, Any]]:
    with _PAIRINGS_LOCK:
        return [{key: value for key, value in item.items() if key != "at"}
                for item in _PAIRINGS.values()]


def _op_pair(message: dict[str, Any], _version: str) -> dict[str, Any]:
    target = str(message.get("address") or message.get("node") or "").strip()
    if not target:
        raise ValueError("Pick the computer to pair with (its address, e.g. 192.168.1.115).")
    started = pairing.begin(config.resolve(target).address)
    _note_pairing(started["attempt"], started)
    threading.Thread(target=_follow_pairing, args=(started["attempt"], started),
                     name="wsn-bridge-pairing", daemon=True).start()
    return started


def _op_pair_poll(message: dict[str, Any], _version: str) -> dict[str, Any]:
    attempt = str(message.get("attempt") or "")
    with _PAIRINGS_LOCK:
        answer = _PAIRINGS.get(attempt)
    if answer is None:
        return {"attempt": attempt, "state": "expired"}
    return {key: value for key, value in answer.items() if key != "at"}


def _op_pair_cancel(message: dict[str, Any], _version: str) -> dict[str, Any]:
    attempt = str(message.get("attempt") or "")
    cancelled = pairing.cancel(attempt)
    if cancelled:
        _note_pairing(attempt, {"state": "cancelled"})
    return {"cancelled": cancelled}


def _op_pair_decide(message: dict[str, Any], _version: str) -> dict[str, Any]:
    return client.local_call("POST", "/wsn/pair/decide", {
        "request": str(message.get("request") or ""), "allow": message.get("allow") is True,
    })


def _op_firewall(_message: dict[str, Any], _version: str) -> dict[str, Any]:
    from web_search_neo.nodes import identity

    addresses = identity.lan_addresses()
    return firewall.open_ports(addresses[0] if addresses else "")


OPERATIONS: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
    "overview": _op_overview,
    "add": _op_add,
    "remove": _op_remove,
    "share": _op_share,
    "rotate_key": _op_rotate_key,
    "send_tab": _op_send_tab,
    "pair": _op_pair,
    "pair_poll": _op_pair_poll,
    "pair_cancel": _op_pair_cancel,
    "pair_decide": _op_pair_decide,
    "firewall": _op_firewall,
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
