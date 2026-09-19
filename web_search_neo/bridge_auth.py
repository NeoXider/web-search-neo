"""Shared-secret authentication for the loopback bridge to the Chrome companion.

The bridge listens on 127.0.0.1, so any process running as the same user can
reach it. Origin checks alone do not help: a local process can claim any origin,
and the extension id is derivable from the public ``key`` in the manifest. Both
sides therefore prove knowledge of a per-user secret before any command flows.

The secret itself never crosses the socket (protocol 2): each side sends a
nonce and answers the other's nonce with an HMAC, with a direction label mixed
in so that one side's proof can never be replayed as the other's.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from hashlib import sha256
from pathlib import Path

from web_search_neo.extension_path import EXTENSION_DIR

APP_DIR_NAME = "WebSearchNeo"
TOKEN_FILE_NAME = "bridge-token"
EXTENSION_TOKEN_FILE = EXTENSION_DIR / "bridge-token.js"

LOGGER = logging.getLogger("web_search_neo.bridge.auth")

_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# A reader that races a writer of an older release (which truncated in place)
# can see an empty or partial file for a moment; that is not a missing token.
_READ_ATTEMPTS = 5
_READ_RETRY_SECONDS = 0.05
_CREATE_NO_WINDOW = 0x08000000

# Files already locked down by this process, keyed by identity so a replaced
# file is checked again; each process re-applies the ACL once per file.
_RESTRICTED: set[tuple[str, int, int, int]] = set()
_RESTRICTED_LOCK = threading.Lock()

SERVER_PROOF_LABEL = "wsn-bridge-server"
CLIENT_PROOF_LABEL = "wsn-bridge-client"


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


def _windows_account() -> str | None:
    user = os.environ.get("USERNAME")
    if not user:
        return None
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def restrict_to_current_user(path: Path) -> bool:
    """Make ``path`` readable by the current account only; True on success.

    POSIX gets mode 0600. On Windows ``chmod`` is a no-op, and a file under the
    checkout inherits ``Users:(RX)``/``Authenticated Users:(M)``, so the ACL is
    replaced instead: inheritance off, full control for this account alone.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return True
        except OSError as exc:
            LOGGER.warning("Could not restrict %s to its owner: %s", path, exc)
            return False
    account = _windows_account()
    if account is None:
        LOGGER.warning("Could not restrict %s: USERNAME is not set", path)
        return False
    try:
        completed = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("Could not restrict %s to %s: %s", path, account, exc)
        return False
    if completed.returncode != 0:
        LOGGER.warning(
            "icacls could not restrict %s to %s (exit %s): %s",
            path,
            account,
            completed.returncode,
            (completed.stdout or completed.stderr or "").strip()[:200],
        )
        return False
    # Newer icacls builds (Windows Server 2022 images included) keep explicit
    # entries - SYSTEM, Administrators, owner rights - after /inheritance:r;
    # strip every ACE that is not this account so the token stays single-user.
    user_name = account.rsplit("\\", 1)[-1].lower()
    for _ in range(3):
        check = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        if check.returncode != 0:
            break
        foreign = []
        for line in check.stdout.splitlines():
            entry = line.replace(str(path), "").strip()
            if ":" not in entry or "Successfully" in entry:
                continue
            principal = entry.split(":(", 1)[0]
            if principal.rsplit("\\", 1)[-1].lower() != user_name:
                foreign.append(principal)
        if not foreign:
            break
        args = ["icacls", str(path)]
        for principal in foreign:
            args += ["/remove:g", principal]
        subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    return True


def _file_identity(path: Path) -> tuple[str, int, int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), status.st_ino, status.st_mtime_ns, status.st_size)


def ensure_private(path: Path) -> bool:
    """Re-apply the owner-only ACL/mode to an existing secret file.

    Files written by an older release, copied in by hand, or restored from a
    backup can still carry the inherited ``Users`` entries, so every process
    locks each token file down again the first time it loads it (and again
    whenever the file is replaced). A failure is logged at warning level.
    """
    identity = _file_identity(path)
    if identity is None:
        return False
    with _RESTRICTED_LOCK:
        if identity in _RESTRICTED:
            return True
    if not restrict_to_current_user(path):
        return False
    with _RESTRICTED_LOCK:
        _RESTRICTED.add(_file_identity(path) or identity)
    return True


def _write_private(path: Path, text: str, *, exclusive: bool = False) -> None:
    """Atomically create or replace a file meant for the owning account only.

    The content goes into a uniquely named sibling that is locked down *before*
    anything secret is written to it, then moved into place, so no reader ever
    sees a half-written file. Locking down is best effort: if ``icacls`` or
    ``chmod`` fails, a warning is logged and the file keeps the ACL it
    inherited from its folder.

    ``exclusive`` refuses to replace an existing file and raises
    :class:`FileExistsError` instead, which is how two processes minting at
    once agree on one winner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            restrict_to_current_user(Path(temporary))
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if not exclusive:
            os.replace(temporary, path)
        elif os.name == "nt":
            os.rename(temporary, path)  # never overwrites on Windows
        else:
            os.link(temporary, path)  # never overwrites on POSIX
            os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_token(path: Path) -> str | None:
    """The stored token, ``None`` when the file does not exist, ``""`` when unusable.

    Only a missing file is "absent". Any other read failure is retried briefly
    and then raised: minting a fresh secret because a concurrent writer held the
    file for a moment would silently lock out every peer holding the real one.
    """
    for attempt in range(_READ_ATTEMPTS):
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError:
            if attempt == _READ_ATTEMPTS - 1:
                raise
        else:
            if is_token(content):
                return content
        time.sleep(_READ_RETRY_SECONDS)
    return ""


def load_or_create_token() -> str:
    """Read the machine-local bridge secret, minting one only if there is none."""
    path = token_path()
    existing = _read_token(path)
    if existing:
        ensure_private(path)
        return existing
    token = secrets.token_hex(32)
    if existing is None:
        try:
            _write_private(path, token, exclusive=True)
        except FileExistsError:
            # Another process minted first; its secret is the one everyone uses.
            winner = _read_token(path)
            if winner:
                ensure_private(path)
                return winner
            _write_private(path, token)
    else:
        LOGGER.warning("The bridge token file %s held no usable token; minting a new one", path)
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
            ensure_private(path)
            return path
    except OSError:
        pass
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


def server_proof_message(client_nonce: str, server_nonce: str) -> str:
    """What the daemon signs: the peer's nonce, bound to its own."""
    return f"{SERVER_PROOF_LABEL}|{client_nonce}|{server_nonce}"


def client_proof_message(server_nonce: str, client_nonce: str) -> str:
    """What a peer signs: the daemon's nonce, bound to its own."""
    return f"{CLIENT_PROOF_LABEL}|{server_nonce}|{client_nonce}"


def token_matches(expected: str, received: object) -> bool:
    """Compare a presented token against the local one in constant time."""
    if not isinstance(received, str):
        return False
    return hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))
