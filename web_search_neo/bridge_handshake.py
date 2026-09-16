"""The bridge's mutual challenge-response handshake (protocol 2).

Protocol 1 had every peer send the raw machine secret in its first frame, so
whatever answered on the bridge port first - including a squatter that bound it
before the daemon - was handed the token before it had proved anything. Now the
secret never crosses the wire, and the daemon proves itself first::

    peer   -> daemon  {"type": "hello", "protocol": 2, "role": ..., "nonce": Nc, ...}
    daemon -> peer    {"type": "challenge", "protocol": 2, "nonce": Ns,
                       "proof": HMAC(token, "wsn-bridge-server|Nc|Ns")}
    peer   -> daemon  {"type": "auth", "proof": HMAC(token, "wsn-bridge-client|Ns|Nc")}
    daemon -> peer    {"type": "hello_ack", "protocol": 2, ...}

A peer that cannot verify the daemon's proof hangs up without sending anything
derived from the secret. The direction labels keep one side's proof from being
replayed as the other's, and the fresh nonces on both sides keep any proof from
being replayed at all. The chrome extension implements the peer side in
``chrome-extension/bridge-auth.js``; the MCP client uses :func:`client_handshake`.

A protocol-1 hello is refused with a close reason that names the protocol this
daemon speaks and tells the user to reload the companion; the MCP client reads
that number from the reason (see :func:`client_handshake`).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Callable

from web_search_neo import bridge_auth

LOGGER = logging.getLogger("web_search_neo.bridge.daemon")

HANDSHAKE_TIMEOUT = 5.0
MIN_NONCE_CHARS = 16
MAX_NONCE_CHARS = 256
TOKEN_MISMATCH_REASON = "Companion token mismatch; reload the extension on chrome://extensions"
ROLES = ("extension", "client")


class HandshakeFailure(Exception):
    """The peer side of the handshake failed; the socket is already closed.

    ``refused_protocol`` is set when the daemon turned the hello down because
    it speaks another protocol revision, which is the one failure a client
    should stop retrying on.
    """

    def __init__(self, message: str, refused_protocol: str | None = None) -> None:
        super().__init__(message)
        self.refused_protocol = refused_protocol


def _close(connection: Any, code: int, reason: str) -> None:
    try:
        connection.close(code=code, reason=reason)
    except Exception as exc:
        LOGGER.warning("Closing a bridge socket failed: %s: %s", type(exc).__name__, exc)


def _usable_nonce(value: object) -> bool:
    return isinstance(value, str) and MIN_NONCE_CHARS <= len(value) <= MAX_NONCE_CHARS


def _request_origin(websocket: Any) -> str | None:
    request = getattr(websocket, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    return headers.get("Origin")


def authenticate_peer(
    websocket: Any,
    *,
    protocol: int,
    token_source: Callable[[], str],
    extension_origin: str,
) -> tuple[dict[str, Any], str] | None:
    """Run the daemon's side; return ``(hello, token)`` or close and return None."""
    try:
        first = json.loads(websocket.recv(timeout=HANDSHAKE_TIMEOUT))
    except (TypeError, ValueError):
        first = None
    except Exception as exc:
        LOGGER.warning("A bridge peer never sent a hello: %s: %s", type(exc).__name__, exc)
        return None
    if not isinstance(first, dict):
        LOGGER.warning("Rejected a bridge client whose first frame was not a JSON object")
        _close(websocket, 1008, "Companion hello must be a JSON object")
        return None
    if first.get("type") != "hello" or first.get("protocol") != protocol:
        # The number is in the reason on purpose: this close is the only thing a
        # peer of another revision ever receives. A protocol-1 companion also
        # sent its raw token here; it is dropped unread.
        LOGGER.warning(
            "Rejected a bridge hello for protocol %r (this daemon speaks %s)",
            first.get("protocol"),
            protocol,
        )
        _close(
            websocket,
            1008,
            f"Expected Web Search Neo bridge protocol {protocol}; "
            "update and reload the companion on chrome://extensions",
        )
        return None
    role = first.get("role")
    if role not in ROLES:
        _close(websocket, 1008, "Unknown bridge role; update the companion")
        return None
    if role == "extension" and _request_origin(websocket) != extension_origin:
        LOGGER.warning("Rejected an extension hello without the companion's Origin")
        _close(websocket, 1008, "Extension role requires the companion origin")
        return None
    client_nonce = first.get("nonce")
    if not _usable_nonce(client_nonce):
        LOGGER.warning("Rejected a bridge client whose hello carried no usable nonce")
        _close(websocket, 1008, "Companion hello must carry a nonce")
        return None
    token = token_source()
    server_nonce = secrets.token_hex(16)
    try:
        websocket.send(
            json.dumps(
                {
                    "type": "challenge",
                    "protocol": protocol,
                    "nonce": server_nonce,
                    "proof": bridge_auth.sign(
                        token, bridge_auth.server_proof_message(client_nonce, server_nonce)
                    ),
                }
            )
        )
        answer = json.loads(websocket.recv(timeout=HANDSHAKE_TIMEOUT))
    except (TypeError, ValueError):
        answer = None
    except Exception as exc:
        # The usual cause is a peer that could not verify our proof - it holds
        # another secret - and hung up without answering, as it should.
        LOGGER.warning(
            "A bridge peer left during the challenge (token mismatch?): %s: %s",
            type(exc).__name__,
            exc,
        )
        return None
    proof = answer.get("proof") if isinstance(answer, dict) and answer.get("type") == "auth" else None
    if not bridge_auth.verify(
        token, bridge_auth.client_proof_message(server_nonce, client_nonce), proof
    ):
        LOGGER.warning(
            "Rejected a bridge %s that did not prove it knows the companion token "
            "(claimed browser: %r)",
            role,
            dict(first.get("browser") or {}),
        )
        _close(websocket, 1008, TOKEN_MISMATCH_REASON)
        return None
    return first, token


def client_handshake(
    connection: Any, *, token: str, protocol: int, fields: dict[str, Any]
) -> dict[str, Any]:
    """Run a peer's side and return the daemon's ``hello_ack``.

    Raises :class:`HandshakeFailure` after closing the socket. The raw token is
    never sent, and nothing derived from it is sent before the daemon proved it
    holds the same secret.
    """
    client_nonce = secrets.token_hex(16)
    stage = "hello"
    try:
        connection.send(
            json.dumps(
                {**fields, "type": "hello", "protocol": protocol, "nonce": client_nonce}
            )
        )
        challenge = json.loads(connection.recv(timeout=HANDSHAKE_TIMEOUT))
        if not isinstance(challenge, dict) or challenge.get("type") != "challenge":
            _close(connection, 1008, "Expected a bridge daemon challenge")
            raise HandshakeFailure("The peer on the bridge port is not a bridge daemon")
        server_nonce = challenge.get("nonce")
        if not _usable_nonce(server_nonce) or not bridge_auth.verify(
            token,
            bridge_auth.server_proof_message(client_nonce, server_nonce),
            challenge.get("proof"),
        ):
            _close(connection, 1008, TOKEN_MISMATCH_REASON)
            raise HandshakeFailure(
                "The peer on the bridge port did not prove it knows the companion token"
            )
        stage = "auth"
        connection.send(
            json.dumps(
                {
                    "type": "auth",
                    "proof": bridge_auth.sign(
                        token, bridge_auth.client_proof_message(server_nonce, client_nonce)
                    ),
                }
            )
        )
        acknowledgement = json.loads(connection.recv(timeout=HANDSHAKE_TIMEOUT))
    except HandshakeFailure:
        raise
    except (TypeError, ValueError) as exc:
        _close(connection, 1008, "Bridge daemon handshake frames must be JSON")
        raise HandshakeFailure(f"The bridge daemon answered with junk: {exc}") from exc
    except Exception as exc:
        # A peer that accepts and then says nothing must not leave an abandoned
        # socket (and its reader thread) behind on every retry.
        _close(connection, 1002, "The bridge daemon did not finish the hello")
        refusal = re.search(r"bridge protocol (\d+)", str(exc))
        if refusal and stage == "hello":
            raise HandshakeFailure(str(exc), refused_protocol=refusal.group(1)) from exc
        if stage == "auth" and "token mismatch" in str(exc).lower():
            raise HandshakeFailure(
                "The bridge daemon refused this server's proof of the companion token"
            ) from exc
        raise HandshakeFailure(
            f"The peer on the bridge port did not answer the hello: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(acknowledgement, dict) or acknowledgement.get("type") != "hello_ack":
        _close(connection, 1008, "Expected a bridge daemon hello_ack")
        raise HandshakeFailure("The peer on the bridge port is not a bridge daemon")
    return acknowledgement
