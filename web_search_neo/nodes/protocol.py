"""How a node request proves it knows the pairing key, without sending it.

Every request carries ``X-WSN-Time``, ``X-WSN-Nonce`` and ``X-WSN-Signature``:
an HMAC-SHA256 keyed with the pairing key over a direction label, the method,
the path, the time, the nonce and the SHA-256 of the body. The server refuses a
time more than :data:`MAX_SKEW_SECONDS` away from its own clock and a nonce it
has already accepted, so a captured request cannot be replayed. The answer is
signed the same way over the request's nonce, so a client also knows the reply
came from a holder of the key and belongs to its own request.
"""

from __future__ import annotations

from hashlib import sha256
import hmac
import secrets
import socket
import threading
import time
from typing import Mapping

PROTOCOL = 1
SERVICE = "web-search-neo-node"
HEADER_TIME = "X-WSN-Time"
HEADER_NONCE = "X-WSN-Nonce"
HEADER_SIGNATURE = "X-WSN-Signature"
HEADER_FROM = "X-WSN-From"
MAX_SKEW_SECONDS = 120
DISCOVERY_PROBE = b"WSN-DISCOVER/1"

_REQUEST_LABEL = "wsn-node-request"
_RESPONSE_LABEL = "wsn-node-response"


def _digest(body: bytes) -> str:
    return sha256(body or b"").hexdigest()


def _mac(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), sha256).hexdigest()


def request_signature(key: str, method: str, path: str, stamp: str, nonce: str, body: bytes) -> str:
    return _mac(key, "|".join((_REQUEST_LABEL, method.upper(), path, stamp, nonce, _digest(body))))


def response_signature(key: str, nonce: str, status: int, body: bytes) -> str:
    return _mac(key, "|".join((_RESPONSE_LABEL, nonce, str(int(status)), _digest(body))))


def sign_request(key: str, method: str, path: str, body: bytes = b"",
                 now: float | None = None) -> dict[str, str]:
    """Headers that authenticate one request."""
    stamp = str(int(time.time() if now is None else now))
    nonce = secrets.token_hex(16)
    return {
        HEADER_TIME: stamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: request_signature(key, method, path, stamp, nonce, body),
    }


class ReplayCache:
    """Nonces accepted within the clock window; each is good exactly once."""

    def __init__(self, window_seconds: float = MAX_SKEW_SECONDS * 2, limit: int = 20_000) -> None:
        self.window = window_seconds
        self.limit = limit
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, nonce: str, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        with self._lock:
            if len(self._seen) >= self.limit or len(self._seen) % 256 == 0:
                cutoff = moment - self.window
                self._seen = {key: at for key, at in self._seen.items() if at >= cutoff}
            if nonce in self._seen or len(self._seen) >= self.limit:
                return False
            self._seen[nonce] = moment
            return True


def verify_request(key: str, method: str, path: str, headers: Mapping[str, str], body: bytes,
                   replay: ReplayCache, now: float | None = None) -> str | None:
    """``None`` when the request is authentic, else the reason it is not."""
    stamp = str(headers.get(HEADER_TIME) or "")
    nonce = str(headers.get(HEADER_NONCE) or "")
    signature = str(headers.get(HEADER_SIGNATURE) or "")
    if not (stamp and nonce and signature):
        return "unsigned request"
    if not (8 <= len(nonce) <= 128) or not nonce.isalnum():
        return "malformed nonce"
    try:
        skew = abs((time.time() if now is None else now) - int(stamp))
    except ValueError:
        return "malformed time"
    if skew > MAX_SKEW_SECONDS:
        return f"clock skew {skew:.0f}s exceeds {MAX_SKEW_SECONDS}s (sync the computers' clocks)"
    expected = request_signature(key, method, path, stamp, nonce, body)
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii", "replace")):
        return "bad signature (the pairing key differs)"
    if not replay.accept(nonce, now):
        return "replayed nonce"
    return None


def verify_response(key: str, nonce: str, status: int, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = response_signature(key, nonce, status, body)
    return hmac.compare_digest(expected.encode("ascii"), str(signature).encode("ascii", "replace"))


def quiet_udp(udp: socket.socket) -> socket.socket:
    """Stop Windows from failing the next ``recvfrom`` after an ICMP port-unreachable.

    A probe broadcast to a subnet where nobody listens makes Windows report
    WSAECONNRESET on the *following* receive, which would end a discovery scan,
    or the node's responder, at the first silent address.
    """
    control = getattr(socket, "SIO_UDP_CONNRESET", None)
    if control is not None:
        try:
            udp.ioctl(control, False)
        except (OSError, ValueError):
            pass
    return udp
