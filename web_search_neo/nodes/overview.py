"""One picture of this computer and every other one: what the topic and the popup show."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any

from web_search_neo.nodes import client, config, firewall, identity
from web_search_neo.nodes.config import NodeEntry

HOW = (
    "web_action(node='<name>', actions=[...]) and web_info(node='<name>', topic=...) run on "
    "that computer, in its Chrome. Pair one with web_action nodes op='pair' (address; no key: "
    "the user allows it on that computer, then call again), or op='add' with its key; "
    "params.discover=true scans the LAN. sharing.firewall says whether Windows Firewall lets "
    "the LAN reach this computer."
)


def sharing_state(lan_ips: list[str], include_key: bool = False) -> dict[str, Any]:
    """Whether a node server runs on this machine, and where others reach it."""
    port = config.listen_port()
    state: dict[str, Any] = {"enabled": config.share_enabled(), "port": port}
    try:
        answer = client.hello(NodeEntry("local", f"127.0.0.1:{port}", None, "address"), timeout=1.0)
    except client.NodeError:
        state["running"] = False
    else:
        state.update(running=True, version=answer.get("version"), name=answer.get("name"))
    state["reach_at"] = [f"{address}:{port}" for address in lan_ips]
    if include_key:
        key = config.load_or_create_key()
        state["pair_key"] = config.display_key(key)
    return state


def probe(entry: NodeEntry, timeout: float = client.PROBE_TIMEOUT) -> dict[str, Any]:
    """One node's live status: online and paired, reachable but unpaired, or offline."""
    row = entry.public()
    started = time.monotonic()
    try:
        details = client.info(entry, timeout)
    except client.NodeError as exc:
        row["error"] = str(exc)
        try:
            hello = client.hello(entry, timeout=min(timeout, 1.5))
        except client.NodeError:
            row.update(state="offline", online=False)
        else:
            row.update(state="unpaired", online=True, version=hello.get("version"),
                       reported_name=hello.get("name"))
        return row
    row.update(state="online", online=True,
               latency_ms=round((time.monotonic() - started) * 1000))
    for key in ("version", "hostname", "os", "lan_ips", "primary_ip", "public", "browser",
                "sessions"):
        if key in details:
            row[key] = details[key]
    if details.get("name") and details.get("name") != entry.name:
        row["reported_name"] = details["name"]
    row["place"] = identity.place(details.get("public"))
    return row


def overview(*, version: str = "", local_extra: dict[str, Any] | None = None,
             discover: bool = False, refresh_public: bool = False,
             include_key: bool = False, timeout: float = client.PROBE_TIMEOUT,
             firewall_wait: bool = True) -> dict[str, Any]:
    """This machine, its sharing state, and every known (and optionally discovered) node.

    While this computer is shared, ``sharing.firewall`` says whether Windows Firewall
    lets the LAN in; ``firewall_wait=False`` answers from the cache and reads it later.
    """
    me = identity.local_identity(version, include_public=True, refresh=refresh_public)
    if local_extra:
        me.update(local_extra)
    me["place"] = identity.place(me.get("public"))
    entries = config.known_nodes()
    found: list[dict[str, Any]] = []
    if discover:
        found = client.discover(me["lan_ips"], timeout=min(timeout, 1.5))
        known_addresses = {entry.address for entry in entries}
        known_names = {entry.name.lower() for entry in entries}
        for item in found:
            if item["self"] or item["address"] in known_addresses:
                continue
            try:
                name = config.validate_name(item["name"])
            except config.NodeConfigError:
                name = ""
            if not name or name.lower() in known_names:
                name = item["address"].split(":")[0]
            entries.append(NodeEntry(name, item["address"], None, "discovered"))
            known_names.add(name.lower())
    rows: list[dict[str, Any]] = []
    if entries:
        with ThreadPoolExecutor(max_workers=min(8, len(entries))) as pool:
            rows = list(pool.map(lambda entry: probe(entry, timeout), entries))
    sharing = sharing_state(me["lan_ips"], include_key=include_key)
    if sharing.get("enabled") or sharing.get("running"):
        sharing["firewall"] = firewall.state(me.get("primary_ip") or "", wait=firewall_wait)
    return {
        "self": me,
        "sharing": sharing,
        "nodes": rows,
        "discovered": len(found) if discover else None,
        "how": HOW,
    }
