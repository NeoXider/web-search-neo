"""Download-location confinement and log-safe URLs for the fetch tools."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DOWNLOAD_DIR_ENV = "WEB_SEARCH_NEO_DOWNLOAD_DIR"
DEFAULT_DOWNLOAD_SUBDIR = "downloads"

# A query parameter whose lowercased name contains any of these is masked.
_SENSITIVE_PARAM_PARTS = (
    "token",
    "key",
    "sig",
    "secret",
    "password",
    "passwd",
    "pwd",
    "auth",
    "code",
    "session",
    "credential",
    "jwt",
    "ticket",
    "otp",
)
REDACTED = "REDACTED"


def download_root() -> Path:
    """The only directory ``save_to`` may write into.

    ``WEB_SEARCH_NEO_DOWNLOAD_DIR`` when set, otherwise ``downloads/`` under the
    server's current working directory.
    """
    configured = os.getenv(DOWNLOAD_DIR_ENV, "").strip()
    root = Path(configured).expanduser() if configured else Path.cwd() / DEFAULT_DOWNLOAD_SUBDIR
    return root.resolve()


def resolve_save_path(save_to: str) -> Path:
    """Map ``save_to`` into the download root, refusing anything that escapes it.

    Relative paths are taken relative to the root; absolute ones must already
    lie inside it. Symlinks are resolved before the containment check.
    """
    root = download_root()
    requested = Path(str(save_to)).expanduser()
    candidate = requested if requested.is_absolute() else root / requested
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"save_to is not a usable path: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(
            f"save_to must stay inside the download directory {root} "
            f"(set {DOWNLOAD_DIR_ENV} to change it); refused {save_to!r}"
        ) from None
    if resolved == root:
        raise ValueError("save_to must name a file, not the download directory itself")
    return resolved


def write_download(save_to: str, data: bytes, *, overwrite: bool = False) -> Path:
    """Write ``data`` under the download root; an existing file needs ``overwrite``."""
    path = resolve_save_path(save_to)
    if path.is_dir():
        raise ValueError(f"save_to names a directory: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb" if overwrite else "xb") as handle:
            handle.write(data)
    except FileExistsError:
        raise ValueError(
            f"save_to already exists: {path}; pass overwrite=true to replace it"
        ) from None
    except OSError as exc:
        raise ValueError(f"save_to could not be written: {exc}") from exc
    return path


def _sensitive(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _SENSITIVE_PARAM_PARTS)


def redact_url(url: str) -> str:
    """A URL fit for the log: no userinfo, no fragment, sensitive params masked."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "<unparseable url>"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode(
            [(name, REDACTED if _sensitive(name) else value) for name, value in pairs],
            safe=REDACTED,
        )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
