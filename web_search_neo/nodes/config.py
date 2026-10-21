"""Who the other computers are, and this computer's own node settings.

Three sources name a node, in this order of precedence:

* the saved list, ``nodes.json`` in the per-user state directory, written by
  the ``nodes`` action and the companion popup (it holds pairing keys, so it is
  locked to the current account like the bridge token);
* ``WEB_SEARCH_NEO_NODES``, a ``name=host[:port]`` list separated by commas or
  semicolons, for a machine configured from its MCP config alone;
* a bare address: ``node="192.168.1.115"`` works without registering anything,
  with the shared key.

A node without a key of its own is called with the shared key - this machine's
``WEB_SEARCH_NEO_NODE_KEY`` or its own ``node-key`` file - so copying one key to
every machine pairs them all at once, and a discovered node needs no setup.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
from typing import Any

from web_search_neo import bridge_auth

DEFAULT_PORT = 8766
DEFAULT_DISCOVERY_PORT = 8767
KEY_FILE_NAME = "node-key"
NODES_FILE_NAME = "nodes.json"

DIR_ENV = "WEB_SEARCH_NEO_NODE_DIR"
KEY_ENV = "WEB_SEARCH_NEO_NODE_KEY"
NODES_ENV = "WEB_SEARCH_NEO_NODES"
NAME_ENV = "WEB_SEARCH_NEO_NODE_NAME"
HOST_ENV = "WEB_SEARCH_NEO_NODE_HOST"
PORT_ENV = "WEB_SEARCH_NEO_NODE_PORT"
DISCOVERY_ENV = "WEB_SEARCH_NEO_NODE_DISCOVERY_PORT"
# Sharing is on unless the user switched it off in the popup (stored), or this
# machine's environment says off by default.
SHARE_ENV = "WEB_SEARCH_NEO_NODE_SHARE"
_OFF_WORDS = frozenset({"0", "false", "no", "off"})

# What a caller may write to mean "here, not another machine".
LOCAL_TARGETS = frozenset({"", "local", "self", "this", "localhost", "127.0.0.1", "::1"})
MAX_NODES = 64

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_KEY = re.compile(r"^[0-9a-f]{32,128}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_LOCK = threading.Lock()


class NodeConfigError(ValueError):
    """A node name, address or key that cannot be used as given."""


def state_dir() -> Path:
    """The per-user directory node files live in (the bridge token's folder)."""
    override = os.getenv(DIR_ENV, "").strip()
    directory = Path(override) if override else bridge_auth.token_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def key_path() -> Path:
    return state_dir() / KEY_FILE_NAME


def nodes_path() -> Path:
    return state_dir() / NODES_FILE_NAME


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if 1 <= value <= 65535 else default


def listen_port() -> int:
    return _env_int(PORT_ENV, DEFAULT_PORT)


def discovery_port() -> int:
    return _env_int(DISCOVERY_ENV, DEFAULT_DISCOVERY_PORT)


def listen_host() -> str:
    return os.getenv(HOST_ENV, "").strip() or "0.0.0.0"


def node_name() -> str:
    """This machine's node name: ``WEB_SEARCH_NEO_NODE_NAME`` or the host name."""
    configured = os.getenv(NAME_ENV, "").strip()
    if configured and _NAME.match(configured):
        return configured
    host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname() or "node").strip("-.")
    return (host or "node")[:32]


# -- the pairing key ------------------------------------------------------------


def normalize_key(value: object) -> str:
    """A key as stored: lower-case hex, with the grouping dashes people paste removed."""
    text = re.sub(r"[\s-]", "", str(value or "")).lower()
    if not _KEY.match(text):
        raise NodeConfigError("A pairing key is 32-128 hexadecimal characters (dashes allowed).")
    return text


def is_key(value: object) -> bool:
    try:
        normalize_key(value)
    except NodeConfigError:
        return False
    return True


def local_key() -> str | None:
    """The shared key without minting one: the environment, then the key file."""
    configured = os.getenv(KEY_ENV, "").strip()
    if configured:
        return normalize_key(configured)
    try:
        stored = key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return stored if is_key(stored) else None


def load_or_create_key() -> str:
    """This machine's pairing key, minted (128 bits) on first use."""
    with _LOCK:
        existing = local_key()
        if existing:
            return existing
        key = secrets.token_hex(16)
        bridge_auth.write_private(key_path(), key)
        return key


def rotate_key() -> str:
    """Mint a new key; every paired machine must be given the new one."""
    if os.getenv(KEY_ENV, "").strip():
        raise NodeConfigError(f"The key comes from {KEY_ENV}; change it there.")
    with _LOCK:
        key = secrets.token_hex(16)
        bridge_auth.write_private(key_path(), key)
        return key


def display_key(key: str) -> str:
    """Groups of four, which is how a person reads a key aloud or types it."""
    return "-".join(key[index:index + 4] for index in range(0, len(key), 4))


# -- addresses --------------------------------------------------------------------


def normalize_address(text: object, default_port: int = DEFAULT_PORT) -> str:
    """``host:port`` from what a person types: a bare IP, a URL, or ``host:port``."""
    raw = str(text or "").strip()
    raw = re.sub(r"^https?://", "", raw, flags=re.I).split("/", 1)[0]
    if not raw:
        raise NodeConfigError("A node address is required, e.g. 192.168.1.115 or 192.168.1.115:8766.")
    port = default_port
    if raw.startswith("["):  # [IPv6]:port
        host, _, rest = raw[1:].partition("]")
        if rest.startswith(":"):
            port = _port(rest[1:])
        address_host = f"[{host}]"
        if not re.fullmatch(r"[0-9A-Fa-f:.]+", host or ""):
            raise NodeConfigError(f"Not an IPv6 address: {raw!r}")
        return f"{address_host}:{port}"
    if raw.count(":") == 1:
        raw, port_text = raw.split(":")
        port = _port(port_text)
    if not _HOST.match(raw):
        raise NodeConfigError(f"Not a host name or IPv4 address: {raw!r}")
    return f"{raw.lower()}:{port}"


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise NodeConfigError(f"Not a port number: {text!r}") from None
    if not 1 <= port <= 65535:
        raise NodeConfigError(f"Port out of range: {port}")
    return port


def split_address(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host.strip("[]"), int(port)


# -- the node list ------------------------------------------------------------------


@dataclass(frozen=True)
class NodeEntry:
    """One remote computer as this machine knows it."""

    name: str
    address: str
    key: str | None = None
    source: str = "saved"  # saved | env | address | discovered

    @property
    def url(self) -> str:
        return f"http://{self.address}"

    def effective_key(self) -> str | None:
        return self.key or local_key()

    def public(self) -> dict[str, Any]:
        """What may be shown or returned: everything but the key itself."""
        return {"name": self.name, "address": self.address, "source": self.source,
                "has_own_key": bool(self.key)}


def validate_name(name: object) -> str:
    text = str(name or "").strip()
    if not _NAME.match(text) or text.lower() in LOCAL_TARGETS:
        raise NodeConfigError(
            "A node name is 1-32 letters, digits, '.', '_' or '-' and cannot be "
            f"one of {sorted(t for t in LOCAL_TARGETS if t)}."
        )
    return text


def _read_store() -> dict[str, Any]:
    try:
        data = json.loads(nodes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"nodes": {}}
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
        return {"nodes": {}}
    return data


def _write_store(data: dict[str, Any]) -> None:
    bridge_auth.write_private(nodes_path(), json.dumps(data, indent=2, sort_keys=True) + "\n")


def saved_nodes() -> list[NodeEntry]:
    entries = []
    for name, record in sorted(_read_store()["nodes"].items(), key=lambda item: item[0].lower()):
        if not isinstance(record, dict):
            continue
        try:
            address = normalize_address(record.get("address"))
            key = normalize_key(record["key"]) if record.get("key") else None
            entries.append(NodeEntry(validate_name(name), address, key, "saved"))
        except NodeConfigError:
            continue  # a hand-edited entry that no longer parses is skipped, not fatal
    return entries


def env_nodes() -> list[NodeEntry]:
    entries = []
    for chunk in re.split(r"[;,]", os.getenv(NODES_ENV, "")):
        name, separator, address = chunk.strip().partition("=")
        if not separator:
            continue
        try:
            entries.append(NodeEntry(validate_name(name), normalize_address(address), None, "env"))
        except NodeConfigError:
            continue
    return entries


def known_nodes() -> list[NodeEntry]:
    """Saved nodes, then environment ones whose names are not already saved."""
    saved = saved_nodes()
    names = {entry.name.lower() for entry in saved}
    return saved + [entry for entry in env_nodes() if entry.name.lower() not in names]


def add_node(name: object, address: object, key: object = None) -> NodeEntry:
    entry = NodeEntry(
        validate_name(name), normalize_address(address),
        normalize_key(key) if key not in (None, "") else None, "saved",
    )
    with _LOCK:
        data = _read_store()
        existing = {item.lower(): item for item in data["nodes"]}
        previous = existing.get(entry.name.lower())
        if previous is None and len(data["nodes"]) >= MAX_NODES:
            raise NodeConfigError(f"At most {MAX_NODES} nodes can be saved.")
        if previous is not None:
            old = data["nodes"].pop(previous)
            if entry.key is None and isinstance(old, dict) and old.get("key"):
                # Re-adding to change the address keeps the key it was paired with.
                entry = NodeEntry(entry.name, entry.address, normalize_key(old["key"]), "saved")
        record: dict[str, Any] = {"address": entry.address}
        if entry.key:
            record["key"] = entry.key
        data["nodes"][entry.name] = record
        _write_store(data)
    return entry


def remove_node(name: object) -> bool:
    wanted = str(name or "").strip().lower()
    with _LOCK:
        data = _read_store()
        match = next((item for item in data["nodes"] if item.lower() == wanted), None)
        if match is None:
            return False
        data["nodes"].pop(match)
        _write_store(data)
    return True


def share_enabled() -> bool:
    """Whether this computer is shared: the popup's switch, on by default.

    A stored choice wins - the user turned it off (or on) in the popup. Without
    one, ``WEB_SEARCH_NEO_NODE_SHARE=0`` makes "off" the default for a machine
    that should never be reachable; otherwise other paired computers can drive
    it, which is what the feature is for. Nobody without the pairing key can.
    """
    stored = _read_store().get("share")
    if isinstance(stored, bool):
        return stored
    return os.getenv(SHARE_ENV, "1").strip().lower() not in _OFF_WORDS


def set_share(enabled: bool) -> None:
    with _LOCK:
        data = _read_store()
        data["share"] = bool(enabled)
        _write_store(data)


def is_local_target(target: object) -> bool:
    text = str(target or "").strip().lower()
    return text in LOCAL_TARGETS or text == node_name().lower()


def resolve(target: object) -> NodeEntry:
    """The node a ``node=`` argument names: a known name, else a bare address."""
    text = str(target or "").strip()
    known = known_nodes()
    for entry in known:
        if entry.name.lower() == text.lower():
            return entry
    try:
        address = normalize_address(text)
    except NodeConfigError:
        names = [entry.name for entry in known]
        raise NodeConfigError(
            f"Unknown node {text!r}. Known nodes: {names or 'none'}. Pass a saved name, an "
            "address such as 192.168.1.115, or read web_info(topic='nodes', "
            "params={'discover': true}) to find computers on this network."
        ) from None
    for entry in known:
        if entry.address == address:
            return entry
    return NodeEntry(text, address, None, "address")
