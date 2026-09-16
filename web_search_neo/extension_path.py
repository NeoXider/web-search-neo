"""Where the unpacked Chrome companion lives for this installation.

A git checkout keeps it in ``chrome-extension/`` next to the package and uses
it in place. An installed wheel carries it as ``web_search_neo/chrome_extension``
inside site-packages, which may be read-only or shared between users, while the
bridge secret has to be written into the extension folder. The installed copy is
therefore mirrored into a per-user directory and Chrome loads it from there.

The mirror is built in a temporary sibling and swapped into place, and it only
counts as current when its completion marker names the packaged version and a
fingerprint of the packaged files, so an interrupted copy is redone rather than
loaded half-finished. The shared packaged folder itself is never handed out:
the bridge secret must not be written where other accounts can read it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

LOGGER = logging.getLogger("web_search_neo.extension_path")

_PACKAGE_ROOT = Path(__file__).resolve().parent
CHECKOUT_DIR = _PACKAGE_ROOT.parent / "chrome-extension"
PACKAGED_DIR = _PACKAGE_ROOT / "chrome_extension"
# Files that belong to this machine, not to the shipped extension.
_LOCAL_FILES = frozenset({"bridge-token.js"})
MIRROR_MARKER = ".wsn-mirror-complete"


def _user_data_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "WebSearchNeo"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "web-search-neo"


def _version(directory: Path) -> str | None:
    try:
        return str(json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return None


def _shipped_files(source: Path) -> list[Path]:
    files = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_dir() or relative.name in _LOCAL_FILES or relative.name == MIRROR_MARKER:
            continue
        if "__pycache__" in relative.parts:
            continue
        files.append(path)
    return sorted(files)


def _fingerprint(source: Path) -> str:
    """Packaged version plus a hash of every shipped file's name and size."""
    digest = hashlib.sha256()
    for path in _shipped_files(source):
        digest.update(f"{path.relative_to(source).as_posix()}\0{path.stat().st_size}\n".encode("utf-8"))
    return f"{_version(source)}\n{digest.hexdigest()}\n"


def _is_current(target: Path, fingerprint: str) -> bool:
    try:
        return (target / MIRROR_MARKER).read_text(encoding="utf-8") == fingerprint
    except OSError:
        return False


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _mirror(source: Path, target: Path) -> Path:
    """Copy the packaged extension to ``target`` unless it is already current."""
    fingerprint = _fingerprint(source)
    if _is_current(target, fingerprint):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    retired: Path | None = None
    try:
        for path in _shipped_files(source):
            destination = staging / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        for name in _LOCAL_FILES:
            kept = target / name
            if kept.is_file():  # the machine-local secret survives an upgrade
                shutil.copy2(kept, staging / name)
        (staging / MIRROR_MARKER).write_text(fingerprint, encoding="utf-8")
        if target.exists():
            retired = target.with_name(f".{target.name}.{uuid.uuid4().hex}.old")
            os.replace(target, retired)
        os.replace(staging, target)
    except BaseException:
        if retired is not None and not target.exists():
            try:
                os.replace(retired, target)  # put the previous mirror back
                retired = None
            except OSError:
                pass
        _remove_tree(staging)
        raise
    if retired is not None:
        _remove_tree(retired)
    LOGGER.info("Companion extension %s mirrored to %s", _version(source), target)
    return target


def resolve_extension_dir() -> Path:
    """The folder Chrome should load the companion from.

    A failed mirror is logged and the per-user target is still returned, so the
    token write that follows fails loudly instead of landing in site-packages.
    """
    if (CHECKOUT_DIR / "manifest.json").is_file():
        return CHECKOUT_DIR.resolve()
    if (PACKAGED_DIR / "manifest.json").is_file():
        target = _user_data_root() / "extension"
        try:
            return _mirror(PACKAGED_DIR, target).resolve()
        except OSError as exc:
            LOGGER.error(
                "Could not mirror the companion extension to %s: %s; "
                "the shared packaged copy is not used because the bridge secret "
                "must not be written there",
                target,
                exc,
            )
            return target
    # Neither exists: keep the historical path so error messages name it.
    return CHECKOUT_DIR.resolve()


EXTENSION_DIR = resolve_extension_dir()
