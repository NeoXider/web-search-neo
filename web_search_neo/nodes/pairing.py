"""Pairing without typing a key: B hands its key to A once a person at B allows it.

A is the computer that wants to drive B. B's node serves three open endpoints:

1. ``POST /wsn/pair/start`` ``{name, commit}`` - A's name and the SHA-256 of its
   Diffie-Hellman public value. B answers ``{request, name, pub}``.
2. ``POST /wsn/pair/reveal`` ``{request, pub}`` - B checks the commitment,
   derives the shared secret and a six-digit code from it and both public
   values, and asks its person (a Chrome notification and the popup) whether
   A may drive this computer. A shows the same code on its own screen.
3. ``POST /wsn/pair/poll`` ``{request, proof}`` - pending, denied or expired;
   once allowed, B's key sealed with the shared secret, handed over once.

Committing before revealing keeps a machine in the middle from choosing its
half after seeing A's, so it can make the two codes match only by luck (one in
a million). The person who compares the codes and presses Allow is what makes
this safe, and nothing is handed over until they do. The key crosses the wire
once, encrypted with a secret nobody else on the network can compute.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import hmac
import secrets
import threading
import time
from typing import Any, Callable

from web_search_neo.nodes import client, config
from web_search_neo.nodes.config import NodeEntry

# RFC 3526 group 14: the 2048-bit MODP safe prime, generator 2.
_P = int("".join("""
FFFFFFFF FFFFFFFF C90FDAA2 2168C234 C4C6628B 80DC1CD1 29024E08 8A67CC74
020BBEA6 3B139B22 514A0879 8E3404DD EF9519B3 CD3A431B 302B0A6D F25F1437
4FE1356D 6D51C245 E485B576 625E7EC6 F44C42E9 A637ED6B 0BFF5CB6 F406B7ED
EE386BFB 5A899FA5 AE9F2411 7C4B1FE6 49286651 ECE45B3D C2007CB8 A163BF05
98DA4836 1C55D39A 69163FA8 FD24CF5F 83655D23 DCA3AD96 1C62F356 208552BB
9ED52907 7096966D 670C354E 4ABC9804 F1746C08 CA18217C 32905E46 2E36CE3B
E39E772C 180E8603 9B2783A2 EC07A28F B5C55DF0 6F4C52C9 DE2BCBF6 95581718
3995497C EA956AE5 15D22618 98FA0510 15728E5A 8AACAA68 FFFFFFFF FFFFFFFF
""".split()), 16)
_G = 2
_WIDTH = 256  # bytes in one group element
_LABEL = "wsn-pair/1"

REQUEST_TTL_SECONDS = 180.0  # time a person has to walk over and press Allow
MAX_WAITING = 4
START_INTERVAL_SECONDS = 2.0  # per caller address: a flood cannot bury a real request
ATTEMPT_TTL_SECONDS = REQUEST_TTL_SECONDS + 30.0


class PairingError(RuntimeError):
    """A pairing step that cannot go on; the message says why in plain words."""


def _element(value: int) -> bytes:
    return value.to_bytes(_WIDTH, "big")


def new_secret() -> tuple[int, int]:
    """A one-time private exponent and its public value."""
    private = secrets.randbelow(_P - 3) + 2
    return private, pow(_G, private, _P)


def peer_value(text: object) -> int:
    try:
        value = int(str(text or ""), 16)
    except ValueError:
        raise PairingError("malformed public value") from None
    if not 2 <= value <= _P - 2:
        raise PairingError("public value out of range")
    return value


def commitment(public: int) -> str:
    return sha256(_element(public)).hexdigest()


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, sha256).digest()


@dataclass(frozen=True)
class Session:
    """What both computers derive once both public values are known."""

    master: bytes

    def _sub(self, label: str) -> bytes:
        return _hmac(self.master, label.encode("ascii"))

    @property
    def code(self) -> str:
        """Six digits, grouped as people read them aloud: ``482 913``."""
        digits = f"{int.from_bytes(self._sub('code')[:8], 'big') % 1_000_000:06d}"
        return f"{digits[:3]} {digits[3:]}"

    def proof(self, request: str) -> str:
        return _hmac(self._sub("proof"), request.encode("utf-8")).hex()

    def _stream(self, length: int) -> bytes:
        key, out, counter = self._sub("seal"), b"", 0
        while len(out) < length:
            out += _hmac(key, counter.to_bytes(4, "big"))
            counter += 1
        return out[:length]

    def seal(self, secret: str) -> dict[str, str]:
        data = secret.encode("utf-8")
        sealed = bytes(a ^ b for a, b in zip(data, self._stream(len(data))))
        return {"sealed": sealed.hex(), "tag": _hmac(self._sub("tag"), sealed).hex()}

    def unseal(self, box: dict[str, Any]) -> str:
        try:
            sealed = bytes.fromhex(str(box.get("sealed") or ""))
            tag = bytes.fromhex(str(box.get("tag") or ""))
        except ValueError:
            raise PairingError("the sealed key is malformed") from None
        if not sealed or not hmac.compare_digest(tag, _hmac(self._sub("tag"), sealed)):
            raise PairingError("the sealed key does not belong to this pairing; nothing was saved")
        return bytes(a ^ b for a, b in zip(sealed, self._stream(len(sealed)))).decode("utf-8")


def derive(private: int, peer_public: int, *, request: str, a_name: str, b_name: str,
           a_public: int, b_public: int) -> Session:
    shared = pow(peer_public, private, _P)
    transcript = "|".join((_LABEL, request, a_name, b_name, format(a_public, "x"),
                           format(b_public, "x")))
    return Session(_hmac(_element(shared), transcript.encode("utf-8")))


# -- B: the computer being asked --------------------------------------------------


@dataclass
class _Request:
    id: str
    name: str
    address: str
    commit: str
    private: int
    public: int
    created: float
    session: Session | None = None
    state: str = "await_reveal"  # -> waiting -> allowed | denied


class PairingDesk:
    """Pairing requests waiting for the person at this computer."""

    def __init__(self, own_name: str, secret: Callable[[], str],
                 notify: Callable[[dict[str, Any]], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.own_name = own_name
        self.secret = secret
        self.notify = notify
        self.clock = clock
        self._requests: dict[str, _Request] = {}
        self._last_start: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for request_id in [key for key, item in self._requests.items()
                           if now - item.created > REQUEST_TTL_SECONDS]:
            self._requests.pop(request_id, None)
        self._last_start = {address: at for address, at in self._last_start.items()
                            if now - at < START_INTERVAL_SECONDS}

    def start(self, payload: dict[str, Any], address: str) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()[:32] or address
        commit = str(payload.get("commit") or "").lower()
        if len(commit) != 64 or any(ch not in "0123456789abcdef" for ch in commit):
            raise PairingError("commit must be a SHA-256 hex digest")
        now = self.clock()
        with self._lock:
            self._prune(now)
            if address in self._last_start:
                raise PairingError("one pairing request every few seconds; try again shortly")
            if len(self._requests) >= MAX_WAITING:
                raise PairingError("too many pairing requests are waiting on this computer")
            self._last_start[address] = now
            private, public = new_secret()
            request = _Request(secrets.token_hex(8), name, address, commit, private, public, now)
            self._requests[request.id] = request
        return {"request": request.id, "name": self.own_name, "pub": format(public, "x")}

    def reveal(self, payload: dict[str, Any], address: str) -> dict[str, Any]:
        with self._lock:
            self._prune(self.clock())
            request = self._requests.get(str(payload.get("request") or ""))
            if request is None or request.address != address or request.state != "await_reveal":
                raise PairingError("no such pairing request (it may have expired)")
            a_public = peer_value(payload.get("pub"))
            if not hmac.compare_digest(commitment(a_public), request.commit):
                self._requests.pop(request.id, None)
                raise PairingError("the revealed value does not match the commitment")
            request.session = derive(request.private, a_public, request=request.id,
                                     a_name=request.name, b_name=self.own_name,
                                     a_public=a_public, b_public=request.public)
            request.state = "waiting"
            shown = self._public(request)
        if self.notify is not None:
            try:
                self.notify(shown)
            except Exception:  # the popup still lists it
                pass
        return {"waiting": True}

    def poll(self, payload: dict[str, Any], address: str) -> dict[str, Any]:
        with self._lock:
            self._prune(self.clock())
            request = self._requests.get(str(payload.get("request") or ""))
            if request is None or request.session is None or request.address != address:
                return {"state": "expired"}
            if not hmac.compare_digest(request.session.proof(request.id),
                                       str(payload.get("proof") or "")):
                raise PairingError("bad proof")
            if request.state == "waiting":
                return {"state": "waiting"}
            self._requests.pop(request.id, None)  # an answer is handed over once
            if request.state == "denied":
                return {"state": "denied"}
            return {"state": "allowed", "name": self.own_name,
                    **request.session.seal(self.secret())}

    def _public(self, request: _Request) -> dict[str, Any]:
        return {"request": request.id, "name": request.name, "address": request.address,
                "code": request.session.code if request.session else "",
                "expires_in": max(0, round(REQUEST_TTL_SECONDS - (self.clock() - request.created)))}

    def waiting(self) -> list[dict[str, Any]]:
        """What the popup lists: requests a person still has to answer."""
        with self._lock:
            self._prune(self.clock())
            return [self._public(item) for item in self._requests.values() if item.state == "waiting"]

    def decide(self, request_id: object, allow: bool) -> dict[str, Any]:
        with self._lock:
            self._prune(self.clock())
            request = self._requests.get(str(request_id or ""))
            if request is None or request.state != "waiting":
                raise PairingError("that pairing request is no longer waiting (it may have expired)")
            request.state = "allowed" if allow else "denied"
            return {"decided": request.state, "name": request.name, "address": request.address}


# -- A: the computer asking ---------------------------------------------------------


@dataclass
class _Attempt:
    id: str
    entry: NodeEntry
    request: str
    peer_name: str
    session: Session
    created: float


_ATTEMPTS: dict[str, _Attempt] = {}
_ATTEMPTS_LOCK = threading.Lock()


def _post(entry: NodeEntry, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    answer = client.open_post(entry, path, payload, timeout)
    if not isinstance(answer, dict) or not answer.get("ok"):
        error = str((answer or {}).get("error") if isinstance(answer, dict) else answer)
        if error == "not found":
            raise PairingError(
                f"{entry.address} runs an older Web Search Neo without button pairing. Update it "
                "(the Update button in its panel), or add it with the key its panel shows."
            )
        raise PairingError(f"{entry.address} refused the pairing: {error}")
    result = answer.get("result")
    return result if isinstance(result, dict) else {}


def begin(address: object, timeout: float = client.PROBE_TIMEOUT) -> dict[str, Any]:
    """Ask the computer at ``address`` to pair; the answer carries the code to compare."""
    entry = NodeEntry("pairing", config.normalize_address(address), None, "address")
    private, public = new_secret()
    me = config.node_name()
    started = _post(entry, "/wsn/pair/start", {"name": me, "commit": commitment(public)}, timeout)
    request = str(started.get("request") or "")
    peer_name = str(started.get("name") or "")[:32]
    b_public = peer_value(started.get("pub"))
    session = derive(private, b_public, request=request, a_name=me, b_name=peer_name,
                     a_public=public, b_public=b_public)
    _post(entry, "/wsn/pair/reveal", {"request": request, "pub": format(public, "x")}, timeout)
    attempt = _Attempt(secrets.token_hex(8), entry, request, peer_name, session, time.time())
    with _ATTEMPTS_LOCK:
        now = time.time()
        for key in [key for key, item in _ATTEMPTS.items() if now - item.created > ATTEMPT_TTL_SECONDS]:
            _ATTEMPTS.pop(key, None)
        _ATTEMPTS[attempt.id] = attempt
    return {"attempt": attempt.id, "code": session.code, "name": peer_name,
            "address": entry.address, "state": "waiting", "expires_in": round(REQUEST_TTL_SECONDS)}


def _save_name(peer_name: str, address: str) -> str:
    for entry in config.saved_nodes():
        if entry.address == address:
            return entry.name  # re-pairing a saved computer keeps its name
    taken = {entry.name.lower(): entry.address for entry in config.saved_nodes()}
    try:
        name = config.validate_name(peer_name)
    except config.NodeConfigError:
        name = ""
    if not name or taken.get(name.lower(), address) != address:
        name = address.split(":")[0]
    return name


def check(attempt_id: object, timeout: float = client.PROBE_TIMEOUT) -> dict[str, Any]:
    """Ask once whether the person at the other computer has answered; save on Allow."""
    with _ATTEMPTS_LOCK:
        attempt = _ATTEMPTS.get(str(attempt_id or ""))
    if attempt is None:
        return {"state": "expired"}
    answer = _post(attempt.entry, "/wsn/pair/poll",
                   {"request": attempt.request, "proof": attempt.session.proof(attempt.request)},
                   timeout)
    state = str(answer.get("state") or "expired")
    base = {"attempt": attempt.id, "name": attempt.peer_name, "address": attempt.entry.address}
    if state == "waiting":
        return {**base, "state": "waiting", "code": attempt.session.code}
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.pop(attempt.id, None)
    if state != "allowed":
        return {**base, "state": state if state == "denied" else "expired"}
    key = config.normalize_key(attempt.session.unseal(answer))
    entry = config.add_node(_save_name(attempt.peer_name, attempt.entry.address),
                            attempt.entry.address, key)
    return {**base, "state": "paired", "saved": entry.public()}


def attempt_for(address: str) -> str | None:
    """The attempt still open with ``address``, so asking twice finishes instead of restarting."""
    with _ATTEMPTS_LOCK:
        now = time.time()
        return next((item.id for item in _ATTEMPTS.values() if item.entry.address == address
                     and now - item.created < ATTEMPT_TTL_SECONDS), None)


def cancel(attempt_id: object) -> bool:
    with _ATTEMPTS_LOCK:
        return _ATTEMPTS.pop(str(attempt_id or ""), None) is not None
