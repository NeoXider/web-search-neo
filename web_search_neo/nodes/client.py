"""Calling another computer's node: signed requests, verified answers, LAN discovery.

LAN calls bypass any configured HTTP proxy - a proxy cannot reach
``192.168.x.x`` and would only see the traffic - and every authenticated answer
must carry a valid signature over this request's nonce, or it is rejected.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from typing import Any
import urllib.error
import urllib.request

from web_search_neo.nodes import config, protocol
from web_search_neo.nodes.config import NodeEntry

TIMEOUT_ENV = "WEB_SEARCH_NEO_NODE_TIMEOUT"
# A forwarded web_action may legitimately wait for minutes (a challenge handed to
# a person, a long test_run), so the call timeout is generous; probes are not.
DEFAULT_CALL_TIMEOUT = 600.0
PROBE_TIMEOUT = 3.0
MAX_ANSWER_BYTES = 64 * 1024 * 1024

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class NodeError(RuntimeError):
    """A node could not be reached, refused the call, or answered unverifiably."""


def call_timeout() -> float:
    try:
        return max(5.0, float(os.getenv(TIMEOUT_ENV, "") or DEFAULT_CALL_TIMEOUT))
    except ValueError:
        return DEFAULT_CALL_TIMEOUT


def _exchange(entry: NodeEntry, method: str, path: str, payload: Any | None,
              timeout: float, signed: bool = True) -> Any:
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", protocol.HEADER_FROM: config.node_name()}
    key = entry.effective_key() if signed else None
    if signed:
        if not key:
            raise NodeError(
                f"No pairing key for node {entry.name!r}. Add it with web_action "
                "nodes op='add' (key=...), or set WEB_SEARCH_NEO_NODE_KEY to the shared key."
            )
        headers.update(protocol.sign_request(key, method, path, body))
    if body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(entry.url + path, data=body or None, headers=headers,
                                     method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status, raw, answer_headers = response.status, response.read(MAX_ANSWER_BYTES), response.headers
    except urllib.error.HTTPError as exc:
        status, raw, answer_headers = exc.code, exc.read(MAX_ANSWER_BYTES), exc.headers
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NodeError(f"Node {entry.name!r} at {entry.address} is unreachable: {reason}. "
                        "Is `wsn --node` running there and the port open in its firewall?") from None
    try:
        decoded = json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        raise NodeError(f"Node {entry.name!r} answered HTTP {status} with something other than JSON") from None
    if signed and status != 401:
        signature = answer_headers.get(protocol.HEADER_SIGNATURE) if answer_headers else None
        if not protocol.verify_response(key or "", headers[protocol.HEADER_NONCE], status, raw, signature):
            raise NodeError(f"Node {entry.name!r}'s answer is not signed with the pairing key; ignored.")
    if not signed:
        return decoded
    if not isinstance(decoded, dict) or not decoded.get("ok"):
        error = decoded.get("error") if isinstance(decoded, dict) else f"HTTP {status}"
        raise NodeError(f"Node {entry.name!r}: {error}")
    return decoded.get("result")


def call(entry: NodeEntry, tool: str, arguments: dict[str, Any], timeout: float | None = None) -> Any:
    """Run ``web_action``/``web_info`` on ``entry`` and return its result."""
    return _exchange(entry, "POST", "/wsn/call", {"tool": tool, "arguments": arguments},
                     call_timeout() if timeout is None else timeout)


def info(entry: NodeEntry, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    result = _exchange(entry, "GET", "/wsn/info", None, timeout)
    return result if isinstance(result, dict) else {}


def hello(entry: NodeEntry, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    result = _exchange(entry, "GET", "/wsn/hello", None, timeout, signed=False)
    if not isinstance(result, dict) or result.get("service") != protocol.SERVICE:
        raise NodeError(f"{entry.address} answered, but it is not a Web Search Neo node")
    return result


def stop_local(port: int | None = None, timeout: float = PROBE_TIMEOUT) -> bool:
    """Ask the node on this machine to exit; False when none answered."""
    entry = NodeEntry("local", f"127.0.0.1:{port or config.listen_port()}", None, "address")
    try:
        _exchange(entry, "POST", "/wsn/stop", {}, timeout)
    except NodeError:
        return False
    return True


def _broadcast_targets(addresses: list[str]) -> list[str]:
    targets = ["255.255.255.255"]
    for address in addresses:
        try:
            network = ipaddress.ip_network(f"{address}/24", strict=False)
        except ValueError:
            continue
        broadcast = str(network.broadcast_address)
        if broadcast not in targets:
            targets.append(broadcast)
    return targets


def discover(own_addresses: list[str], timeout: float = 1.2,
             port: int | None = None, extra_targets: list[str] | None = None) -> list[dict[str, Any]]:
    """Nodes that answer a broadcast probe on the LAN within ``timeout`` seconds."""
    discovery_port = port or config.discovery_port()
    udp = protocol.quiet_udp(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    found: dict[str, dict[str, Any]] = {}
    try:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind(("", 0))
        for target in _broadcast_targets(own_addresses) + list(extra_targets or []):
            try:
                udp.sendto(protocol.DISCOVERY_PROBE, (target, discovery_port))
            except OSError:
                continue
        deadline = time.monotonic() + max(0.1, timeout)
        while (remaining := deadline - time.monotonic()) > 0:
            udp.settimeout(remaining)
            try:
                data, peer = udp.recvfrom(4096)
            except ConnectionResetError:
                continue
            except (socket.timeout, OSError):
                break
            try:
                answer = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(answer, dict) or answer.get("service") != protocol.SERVICE:
                continue
            try:
                node_port = int(answer.get("port") or config.DEFAULT_PORT)
            except (TypeError, ValueError):
                continue
            address = f"{peer[0]}:{node_port}"
            found[address] = {
                "name": str(answer.get("name") or peer[0])[:32], "address": address,
                "version": str(answer.get("version") or "")[:32],
                "self": peer[0] in own_addresses or ipaddress.ip_address(peer[0]).is_loopback,
            }
    finally:
        udp.close()
    return sorted(found.values(), key=lambda item: item["address"])
