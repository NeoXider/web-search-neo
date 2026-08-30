"""Shared-secret authentication for the loopback bridge to the Chrome companion.

The bridge listens on 127.0.0.1, so any process running as the same user can
reach it. Origin checks alone do not help: a local process can claim any origin,
and the extension id is derivable from the public ``key`` in the manifest. Both
sides therefore prove knowledge of a per-user secret before any command flows.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from hashlib import sha256
from pathlib import Path

APP_DIR_NAME = "WebSearchNeo"
TOKEN_FILE_NAME = "bridge-token"
EXTENSION_DIR = (Path(__file__).resolve().parents[1] / "chrome-extension").resolve()
EXTENSION_TOKEN_FILE = EXTENSION_DIR / "bridge-token.js"

_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def token_path() -> Path:
    """Return the per-user token file path, creating its directory if needed."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    directory = base / APP_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / TOKEN_FILE_NAME


def is_token(value: object) -> bool:
    """Return True for a well-formed bridge token (64 lowercase hex chars)."""
    return isinstance(value, str) and bool(_TOKEN_PATTERN.match(value))


def _write_private(path: Path, text: str) -> None:
    """Create or replace a file that only the owning account may read."""
    # O_CREAT with mode 0o600 avoids the window a chmod-after-write would leave.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name != "nt":
        # The mode above is ignored when the file already existed.
        os.chmod(path, 0o600)


def load_or_create_token() -> str:
    """Read the machine-local bridge secret, minting one on first use."""
    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if is_token(existing):
        return existing
    token = secrets.token_hex(32)
    # Windows has no chmod: the file inherits the user profile ACL, which keeps
    # other accounts out but not other processes of this same user.
    _write_private(path, token)
    return token


def write_extension_token(token: str) -> Path:
    """Mirror the secret into the unpacked extension so setup stays hands-free."""
    if not is_token(token):
        raise ValueError("Refusing to write a malformed bridge token")
    contents = f'export const BRIDGE_TOKEN = "{token}";\n'
    path = EXTENSION_TOKEN_FILE
    try:
        if path.read_text(encoding="utf-8") == contents:
            return path
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, contents)
    return path


def sign(token: str, nonce: str) -> str:
    """Return the hex HMAC-SHA256 of ``nonce`` keyed with ``token``."""
    return hmac.new(token.encode("utf-8"), str(nonce).encode("utf-8"), sha256).hexdigest()


def verify(token: str, nonce: str, proof: object) -> bool:
    """Check a peer's proof for ``nonce`` in constant time."""
    if not isinstance(proof, str):
        return False
    try:
        candidate = proof.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(sign(token, nonce).encode("ascii"), candidate)


def token_matches(expected: str, received: object) -> bool:
    """Compare a presented token against the local one in constant time."""
    if not isinstance(received, str):
        return False
    return hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))
