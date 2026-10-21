"""MCP glue for LAN nodes: the ``nodes`` topic and action, ``node=`` forwarding, ``wsn --node``.

``main.py`` is a compatibility facade held to a size ratchet, so everything that
connects :mod:`web_search_neo.nodes` to the facade lives here: the wrappers
``extra_actions`` registers, the forwarding ``web_action``/``web_info`` call
when given ``node=``, and the long-running node server that answers those
calls on the other machine by running the very same facade functions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
import threading
from typing import Any, Literal

from mcp.server.fastmcp import Image

from web_search_neo import browser_tools, plugins
from web_search_neo.nodes import client, config, identity, overview
from web_search_neo.nodes.server import NodeServer

LOGGER = logging.getLogger("web_search_neo.nodes")
DENY_ENV = "WEB_SEARCH_NEO_NODE_DENY"
# Pulling a checkout and reloading the companion is the node owner's decision.
DEFAULT_DENY = frozenset({"update_companion"})
IMAGE_KEY = "__wsn_image__"
NODE_PARAM_HELP = (
    "Run this whole call on another computer: a node name from web_info topic 'nodes', "
    "or its IP. Omit for this computer."
)
_ACTION_KEYS = ("actions", "continue_on_error", "summary")
_INFO_KEYS = ("topic", "params")
_facade: Any = None


def bind(facade: Any) -> None:
    """Remember main's module: forwarding and the node server call through it."""
    global _facade
    _facade = facade


def _version() -> str:
    return str(getattr(_facade, "__version__", "") or "")


# -- what a node says about itself ------------------------------------------------


def browser_summary() -> dict[str, Any]:
    """The companion link and the session budget, without asking Chrome anything."""
    summary: dict[str, Any] = {}
    try:
        status = browser_tools.get_chrome_bridge().status(0.0)
        browser = status.get("browser") or {}
        daemon = status.get("daemon") or {}
        summary.update(companion_connected=bool(status.get("connected")),
                       browser=browser.get("name"),
                       extension_version=browser.get("extension_version"),
                       agents=daemon.get("clients"), tabs_claimed=len(daemon.get("claims") or []))
    except Exception as exc:  # a node without Chrome still answers fetch/search calls
        summary.update(companion_connected=False, error=f"{type(exc).__name__}: {exc}")
    try:
        sessions = browser_tools.sessions_overview()
        summary.update(sessions_open=sessions["sessions_open"], max_sessions=sessions["max_sessions"])
    except Exception:
        pass
    return summary


def local_status() -> dict[str, Any]:
    """``/wsn/info``: this node's identity, location and browser state."""
    status = identity.local_identity(_version())
    status["place"] = identity.place(status.get("public"))
    status["browser"] = browser_summary()
    return status


# -- images survive JSON as base64 ------------------------------------------------


def encode(value: Any) -> Any:
    if isinstance(value, Image):
        content = value.to_image_content()
        return {IMAGE_KEY: {"data": content.data, "mime_type": content.mimeType}}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        image = value.get(IMAGE_KEY)
        if isinstance(image, dict) and len(value) == 1:
            mime = str(image.get("mime_type") or "image/png")
            return Image(data=base64.b64decode(str(image.get("data") or "")),
                         format=mime.split("/", 1)[-1])
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


# -- forwarding ------------------------------------------------------------------


def wants_remote(node: str | None) -> bool:
    return node is not None and not config.is_local_target(node)


async def forward(node: str, tool: str, arguments: dict[str, Any]) -> Any:
    """Run one ``web_action``/``web_info`` call on another computer."""
    entry = config.resolve(node)
    result = decode(await asyncio.to_thread(client.call, entry, tool, arguments))
    if isinstance(result, dict):
        result = {**result, "via_node": {"name": entry.name, "address": entry.address}}
    return result


def denied_actions() -> frozenset[str]:
    configured = os.getenv(DENY_ENV)
    if configured is None:
        return DEFAULT_DENY
    return frozenset(item.strip().lower() for item in configured.split(",") if item.strip())


def _refuse_denied(actions: Any) -> None:
    denied = denied_actions()
    aliases = tuple(getattr(_facade, "_ACTION_KEY_ALIASES", ()))
    for step in actions if isinstance(actions, list) else []:
        if not isinstance(step, dict):
            continue
        keys = ("action",) if "action" in step else aliases
        for key in keys:
            name = str(step.get(key) or "").strip().lower()
            if name in denied:
                raise PermissionError(
                    f"Action {name!r} is disabled on this node ({DENY_ENV}); run it on that "
                    "computer itself."
                )


def node_call(loop: asyncio.AbstractEventLoop) -> Any:
    """The ``call`` a NodeServer runs: the facade's own tools, on this node's loop."""

    def call(tool: str, arguments: dict[str, Any], caller: str) -> Any:
        if arguments.get("node") not in (None, "") and not config.is_local_target(arguments["node"]):
            raise ValueError("A node does not relay to further nodes; call that node directly.")
        allowed = _ACTION_KEYS if tool == "web_action" else _INFO_KEYS
        unknown = sorted(set(arguments) - set(allowed) - {"node"})
        if unknown:
            raise ValueError(f"{tool}: unknown argument(s) {unknown}; allowed: {list(allowed)}")
        kept = {key: arguments[key] for key in allowed if key in arguments}
        if tool == "web_action":
            _refuse_denied(kept.get("actions"))
            coroutine = _facade.web_action(**kept)
        else:
            coroutine = _facade.web_info(**kept)
        LOGGER.info("Node call %s from %s", tool, caller)
        return encode(asyncio.run_coroutine_threadsafe(coroutine, loop).result())

    return call


# -- the MCP wrappers --------------------------------------------------------------


async def browser_nodes(
    discover: bool = False,
    refresh: bool = False,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """This computer's LAN/public IP and location, plus every other computer (node) to drive.

    discover=true broadcasts a probe on the LAN; refresh=true re-reads the public IP.
    Each node row says online/unpaired/offline, its IPs, location and browser state.
    """
    timeout = min(15.0, max(0.5, float(timeout_seconds)))
    extra = {"browser": browser_summary()}
    return await asyncio.to_thread(
        overview.overview, version=_version(), local_extra=extra, discover=discover,
        refresh_public=refresh, timeout=timeout,
    )


async def browser_node_manage(
    op: Literal["add", "remove", "list", "ping"] = "list",
    name: str | None = None,
    address: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Save, forget, list, or check another computer that node='<name>' runs calls on.

    add needs address (IP or host[:port], default port 8766) and the key shown by that
    computer's `wsn --node` or companion popup; name defaults to the address.
    """
    def run() -> dict[str, Any]:
        if op == "list":
            return {"success": True, "nodes": [entry.public() for entry in config.known_nodes()]}
        if op == "add":
            if not address:
                raise ValueError("nodes op='add' needs address (e.g. 192.168.1.115)")
            entry = config.add_node(name or config.normalize_address(address).split(":")[0],
                                    address, key)
            row = overview.probe(entry)
            return {"success": True, "saved": entry.public(), "status": row}
        if op == "remove":
            removed = config.remove_node(name or address)
            return {"success": removed, "removed": name or address,
                    **({} if removed else {"error": "No saved node by that name."})}
        target = name or address
        if not target:
            raise ValueError("nodes op='ping' needs name or address")
        row = overview.probe(config.resolve(target))
        return {"success": row.get("state") == "online", "status": row}

    return await asyncio.to_thread(run)


ACTION_SPECS = (
    ("nodes", browser_node_manage, "session", "Save/remove/ping a computer for node='<name>'."),
)


# -- `wsn --node` ------------------------------------------------------------------


def _banner(server: NodeServer, key: str) -> str:
    addresses = identity.lan_addresses() or ["<this computer's IP>"]
    lines = [
        f"Web Search Neo node {server.name!r} (v{server.version}) is listening on port {server.port}.",
        "Reach it at: " + ", ".join(f"{address}:{server.port}" for address in addresses),
        f"Pairing key: {config.display_key(key)}",
        "On another computer: web_action nodes op='add' name='<name>' "
        f"address='{addresses[0]}' key='<pairing key>', then node='<name>'.",
        "LAN discovery: " + ("off (" + server.discovery_error + ")" if server.discovery_error
                             else f"UDP {server.discovery_port}"),
        "Stop: wsn --node --stop (or Ctrl+C).",
    ]
    return "\n".join(lines)


def serve(arguments: list[str]) -> int:
    """``wsn --node``: publish this machine's tools to the LAN until stopped."""
    if "--stop" in arguments:
        stopped = client.stop_local()
        print("The node was asked to stop." if stopped else "No node is running on this machine.")
        return 0
    if "--key" in arguments:
        print(config.display_key(config.load_or_create_key()))
        return 0
    plugins.load_plugins()
    key = config.load_or_create_key()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="wsn-node-loop", daemon=True).start()
    try:
        browser_tools.start_current_chrome_bridge()
    except Exception as exc:  # fetch and search still work without a browser
        LOGGER.warning("Node starts without the Chrome bridge: %s: %s", type(exc).__name__, exc)
    server = NodeServer(call=node_call(loop), info=local_status, key=key, name=config.node_name(),
                        version=_version(), host=config.listen_host(), port=config.listen_port(),
                        discovery_port=config.discovery_port())
    try:
        server.start()
    except OSError as exc:
        print(f"Cannot listen on {server.host}:{server.port}: {exc}. Is a node already running?",
              file=sys.stderr)
        return 1
    print(_banner(server, key), file=sys.stderr, flush=True)
    server.serve_forever()
    loop.call_soon_threadsafe(loop.stop)
    return 0
