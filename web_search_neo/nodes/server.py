"""The LAN side of a node: an HTTP endpoint for signed calls, and a discovery answer.

Endpoints (all JSON):

* ``GET /wsn/hello`` - unauthenticated; the service name, node name, version
  and port. Enough to say "a node lives here", nothing about the machine.
* ``GET /wsn/info`` - signed; :func:`info` (identity, location, browser state).
* ``POST /wsn/call`` - signed; ``{"tool": "web_action"|"web_info", "arguments":
  {...}}`` runs through the ``call`` callable and answers ``{"ok": true,
  "result": ...}`` or ``{"ok": false, "error": "..."}``.
* ``POST /wsn/stop`` - signed and loopback-only; how the companion's "Share this
  computer" switch turns a node it started back off.
* ``POST /wsn/pair/start|reveal|poll`` - open: the button pairing of
  :mod:`.pairing`, which hands the key over only after a person here allows it.
* ``GET /wsn/pair/waiting`` and ``POST /wsn/pair/decide`` - signed and
  loopback-only; how this computer's popup lists and answers those requests.

Discovery is one UDP datagram: a probe of :data:`protocol.DISCOVERY_PROBE` on
the discovery port is answered with the same facts ``/wsn/hello`` gives.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import socket
import threading
import time
from typing import Any, Callable

from web_search_neo.nodes import protocol
from web_search_neo.nodes.pairing import PairingDesk, PairingError

LOGGER = logging.getLogger("web_search_neo.nodes.server")
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_PAIR_BODY_BYTES = 8 * 1024
ALLOWED_TOOLS = frozenset({"web_action", "web_info"})
_AUTH_FAILURE_DELAY = 0.25
_PAIR_STEPS = {"/wsn/pair/start": "start", "/wsn/pair/reveal": "reveal", "/wsn/pair/poll": "poll"}
_LOCAL_POSTS = ("/wsn/stop", "/wsn/pair/decide")


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class NodeServer:
    """One machine's node endpoint; ``call`` and ``info`` come from the facade."""

    def __init__(
        self,
        *,
        call: Callable[[str, dict[str, Any], str], Any],
        info: Callable[[], dict[str, Any]],
        key: str,
        name: str,
        version: str = "",
        host: str = "0.0.0.0",
        port: int = 8766,
        discovery_port: int | None = 8767,
        on_pair_request: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.call = call
        self.info = info
        self.key = key
        self.name = name
        self.version = version
        self.host = host
        self.port = port
        self.discovery_port = discovery_port
        self.replay = protocol.ReplayCache()
        self.started_at = time.time()
        self._http: ThreadingHTTPServer | None = None
        self._udp: socket.socket | None = None
        self._stopped = threading.Event()
        self.discovery_error: str | None = None
        self.pairing = PairingDesk(name, lambda: self.key, notify=on_pair_request)

    # -- lifecycle -------------------------------------------------------------

    def hello(self) -> dict[str, Any]:
        return {"service": protocol.SERVICE, "protocol": protocol.PROTOCOL, "name": self.name,
                "version": self.version, "port": self.port}

    def start(self) -> None:
        server = self

        class Handler(_Handler):
            node = server

        self._http = ThreadingHTTPServer((self.host, self.port), Handler)
        self._http.daemon_threads = True
        self.port = int(self._http.server_address[1])
        threading.Thread(target=self._http.serve_forever, name="wsn-node-http", daemon=True).start()
        if self.discovery_port is not None:
            self._start_discovery()
        LOGGER.info("Node %s listening on %s:%s", self.name, self.host, self.port)

    def _start_discovery(self) -> None:
        udp = protocol.quiet_udp(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        try:
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.bind(("" if self.host in ("0.0.0.0", "") else self.host, self.discovery_port or 0))
        except OSError as exc:
            udp.close()
            self.discovery_error = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("LAN discovery is off: cannot bind UDP %s (%s)", self.discovery_port,
                           self.discovery_error)
            return
        udp.settimeout(0.5)
        self.discovery_port = int(udp.getsockname()[1])
        self._udp = udp
        threading.Thread(target=self._answer_discovery, name="wsn-node-discovery",
                         daemon=True).start()

    def _answer_discovery(self) -> None:
        udp = self._udp
        while udp is not None and not self._stopped.is_set():
            try:
                data, peer = udp.recvfrom(512)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                break
            if data.strip() != protocol.DISCOVERY_PROBE:
                continue
            try:
                udp.sendto(_json_bytes(self.hello()), peer)
            except OSError as exc:
                LOGGER.debug("Discovery answer to %s failed: %s", peer, exc)

    def serve_forever(self) -> None:
        if self._http is None:
            self.start()
        try:
            while not self._stopped.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stopped.set()
        if self._http is not None:
            http, self._http = self._http, None
            http.shutdown()
            http.server_close()
        if self._udp is not None:
            udp, self._udp = self._udp, None
            udp.close()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()


class _Handler(BaseHTTPRequestHandler):
    node: NodeServer
    server_version = "WebSearchNeoNode/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # stderr would be noise
        LOGGER.info("%s %s", self.client_address[0], fmt % args)

    def _send(self, status: int, payload: Any, nonce: str | None = None) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if nonce:
            self.send_header(protocol.HEADER_SIGNATURE,
                             protocol.response_signature(self.node.key, nonce, status, body))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self._send(413, {"ok": False, "error": f"body must be 0-{limit} bytes"})
            return None
        return self.rfile.read(length) if length else b""

    def _loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _local_only(self, nonce: str, what: str) -> bool:
        if self._loopback():
            return True
        self._send(403, {"ok": False, "error": f"{what} only from this node's own machine"}, nonce)
        return False

    def _authenticate(self, body: bytes) -> str | None:
        """The request nonce when authentic; otherwise answers 401 and returns None."""
        path = self.path.split("?", 1)[0]
        problem = protocol.verify_request(self.node.key, self.command, path, self.headers, body,
                                          self.node.replay)
        if problem is None:
            return str(self.headers.get(protocol.HEADER_NONCE))
        LOGGER.warning("Refused %s %s from %s: %s", self.command, path, self.client_address[0],
                       problem)
        time.sleep(_AUTH_FAILURE_DELAY)
        self._send(401, {"ok": False, "error": f"Node refused the request: {problem}."})
        return None

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = self.path.split("?", 1)[0]
        if path == "/wsn/hello":
            self._send(200, self.node.hello())
        elif path == "/wsn/pair/waiting":
            nonce = self._authenticate(b"")
            if nonce and self._local_only(nonce, "pairing requests are listed"):
                self._send(200, {"ok": True, "result": self.node.pairing.waiting()}, nonce)
        elif path == "/wsn/info":
            nonce = self._authenticate(b"")
            if nonce:
                try:
                    self._send(200, {"ok": True, "result": self.node.info()}, nonce)
                except Exception as exc:
                    self._send(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, nonce)
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in _PAIR_STEPS:
            self._pair_step(_PAIR_STEPS[path])
            return
        if path not in ("/wsn/call", *_LOCAL_POSTS):
            self._send(404, {"ok": False, "error": "not found"})
            return
        body = self._read_body()
        if body is None:
            return
        nonce = self._authenticate(body)
        if not nonce:
            return
        if path == "/wsn/stop":
            self._stop(nonce)
            return
        if path == "/wsn/pair/decide":
            self._decide(nonce, body)
            return
        try:
            request = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            self._send(400, {"ok": False, "error": "body is not JSON"}, nonce)
            return
        tool = request.get("tool") if isinstance(request, dict) else None
        arguments = request.get("arguments") if isinstance(request, dict) else None
        if tool not in ALLOWED_TOOLS or not isinstance(arguments, dict):
            self._send(400, {"ok": False, "error": f"tool must be one of {sorted(ALLOWED_TOOLS)} "
                                                   "with an arguments object"}, nonce)
            return
        caller = str(self.headers.get(protocol.HEADER_FROM) or self.client_address[0])[:64]
        try:
            result = self.node.call(tool, arguments, caller)
        except Exception as exc:
            self._send(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, nonce)
            return
        self._send(200, {"ok": True, "result": result}, nonce)

    def _stop(self, nonce: str) -> None:
        if not self._local_only(nonce, "a node can be stopped"):
            return
        self._send(200, {"ok": True, "result": {"stopping": True}}, nonce)
        threading.Thread(target=self.node.shutdown, name="wsn-node-stop", daemon=True).start()

    def _pair_step(self, step: str) -> None:
        """One open pairing step; nothing secret leaves before a person here allows it."""
        body = self._read_body(MAX_PAIR_BODY_BYTES)
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self._send(400, {"ok": False, "error": "body is not a JSON object"})
            return
        try:
            result = getattr(self.node.pairing, step)(payload, self.client_address[0])
        except PairingError as exc:
            LOGGER.info("Pairing %s from %s refused: %s", step, self.client_address[0], exc)
            self._send(400, {"ok": False, "error": str(exc)})
            return
        self._send(200, {"ok": True, "result": result})

    def _decide(self, nonce: str, body: bytes) -> None:
        if not self._local_only(nonce, "pairing requests are answered"):
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            result = self.node.pairing.decide(payload.get("request"), payload.get("allow") is True)
        except (ValueError, AttributeError, PairingError) as exc:
            self._send(200, {"ok": False, "error": str(exc) or "bad request"}, nonce)
            return
        self._send(200, {"ok": True, "result": result}, nonce)
