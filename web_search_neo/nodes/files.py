"""Files that cross to another computer with a ``node=`` call.

A path names a file on the computer that reads it. So an ``upload`` sent to another
computer looked for the caller's file on that computer, and whatever the node saved - a
screenshot, a download, a ``save_to`` - stayed on its disk while the answer named a path
the caller could not open.

Now the caller packs each file an ``upload`` step names, when it exists on the caller,
into the request; the node writes them to a folder of its own and points the step there.
On the way back the node packs every file its answer names under its download folder
into the reply, and the caller writes them under its own download folder
(``from-nodes/<node>/``, the node's layout kept) and points the answer at them, keeping
the node's path as ``remote_path`` in ``node_files``. A path that is not a file on the
caller still goes to the node as a path on the node, as before.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import time
from typing import Any

UPLOAD_KEY = "__wsn_upload__"
FILES_KEY = "__wsn_files__"
# Raw bytes of one call, each way. Base64 grows them by a third: a request may be
# 64 MB (nodes.server.MAX_BODY_BYTES) and so may an answer (nodes.client.MAX_ANSWER_BYTES),
# which also carries the rest of the result.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_RETURN_BYTES = 32 * 1024 * 1024
UPLOADS_DIR = "node-uploads"
RECEIVED_DIR = "from-nodes"
UPLOAD_MAX_AGE_SECONDS = 24 * 3600
_PARTIAL_SUFFIXES = (".crdownload", ".tmp", ".partial", ".download")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_PATH_CHARS = 1024


def safe_name(name: object) -> str:
    """One path component that is valid on Windows and cannot climb out of its folder."""
    cleaned = _UNSAFE.sub("_", str(name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip(" .")
    return cleaned[:160] or "file"


def _megabytes(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB"


# -- upload: the caller's files go to the node ----------------------------------------


def pack_uploads(actions: Any, limit: int = MAX_UPLOAD_BYTES) -> Any:
    """The caller's side: carry each ``file_paths`` entry that is a file here as bytes."""
    if not isinstance(actions, list):
        return actions
    total = 0
    packed: list[Any] = []
    for step in actions:
        paths = step.get("file_paths") if isinstance(step, dict) else None
        if not isinstance(paths, list) or not any(Path(str(raw)).expanduser().is_file() for raw in paths):
            packed.append(step)
            continue
        carried: list[dict[str, str]] = []
        for raw in paths:
            path = Path(str(raw)).expanduser()
            if not path.is_file():
                carried.append({"path": str(raw)})
                continue
            total += path.stat().st_size
            if total > limit:
                raise ValueError(
                    f"upload: files sent to another computer are limited to {_megabytes(limit)} a call; "
                    f"{path.name} goes over it. Copy it to that computer and name its path there."
                )
            carried.append({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
        rest = {key: value for key, value in step.items() if key != "file_paths"}
        packed.append({**rest, UPLOAD_KEY: carried})
    return packed


def sweep(parent: Path, max_age: float = UPLOAD_MAX_AGE_SECONDS) -> None:
    """Drop upload folders a day old: a page reads an attached file when it sends the form."""
    try:
        entries = list(parent.iterdir()) if parent.is_dir() else []
    except OSError:
        return
    cutoff = time.time() - max_age
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def unpack_uploads(actions: Any, root: Path) -> Any:
    """The node's side: write carried files under ``root`` and name them in ``file_paths``."""
    if not isinstance(actions, list):
        return actions
    folder: Path | None = None
    unpacked: list[Any] = []
    for step in actions:
        carried = step.get(UPLOAD_KEY) if isinstance(step, dict) else None
        if carried is None:
            unpacked.append(step)
            continue
        if not isinstance(carried, list):
            raise ValueError("upload: the files sent with this step are malformed")
        if folder is None:
            sweep(root / UPLOADS_DIR)
            folder = root / UPLOADS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
        paths: list[str] = []
        for index, item in enumerate(carried):
            if isinstance(item, dict) and isinstance(item.get("path"), str) and "data" not in item:
                paths.append(item["path"])
                continue
            if not isinstance(item, dict) or not isinstance(item.get("data"), str):
                raise ValueError("upload: the files sent with this step are malformed")
            try:
                data = base64.b64decode(item["data"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"upload: a file sent with this step is not base64: {exc}") from None
            # A folder per file keeps two files of one name apart; the name is what the page sees.
            target = folder / str(index) / safe_name(item.get("name"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            paths.append(str(target))
        rest = {key: value for key, value in step.items() if key != UPLOAD_KEY}
        unpacked.append({**rest, "file_paths": paths})
    return unpacked


# -- saved files: the node's files come back ----------------------------------------


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _saved_file(text: str, root: Path) -> tuple[Path, PurePosixPath] | None:
    if not text or len(text) > _MAX_PATH_CHARS or "\n" in text:
        return None
    try:
        path = Path(text)
        if not path.is_absolute():
            return None
        resolved = path.resolve()
        relative = resolved.relative_to(root)
        if not relative.parts or relative.parts[0] == UPLOADS_DIR or not resolved.is_file():
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    if resolved.name.lower().endswith(_PARTIAL_SUFFIXES):
        return None
    return resolved, PurePosixPath(*relative.parts)


def pack_files(result: Any, root: Path, limit: int = MAX_RETURN_BYTES) -> dict[str, Any] | None:
    """The node's side: the files under ``root`` the answer names, as bytes for the caller."""
    root = root.resolve()
    named: dict[str, tuple[Path, PurePosixPath]] = {}
    for text in _strings(result):
        if text not in named:
            found = _saved_file(text, root)
            if found is not None:
                named[text] = found
    if not named:
        return None
    files: list[dict[str, Any]] = []
    too_large: list[dict[str, Any]] = []
    total = 0
    for text, (path, relative) in named.items():
        try:
            size = path.stat().st_size
            if total + size > limit:
                too_large.append({"remote_path": text, "bytes": size})
                continue
            data = path.read_bytes()
        except OSError:
            continue
        total += len(data)
        files.append({"remote_path": text, "relative": str(relative),
                      "data": base64.b64encode(data).decode("ascii")})
    return {"files": files, "too_large": too_large, "limit_bytes": limit}


def _replace(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, dict):
        return {key: _replace(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, mapping) for item in value]
    return value


def unpack_files(result: dict[str, Any], root: Path, node_name: str) -> dict[str, Any]:
    """The caller's side: write the node's files here and point the answer at them."""
    bundle = result.get(FILES_KEY)
    if bundle is None:
        return result
    result = {key: value for key, value in result.items() if key != FILES_KEY}
    if not isinstance(bundle, dict):
        return result
    target = (root / RECEIVED_DIR / safe_name(node_name)).resolve()
    mapping: dict[str, str] = {}
    saved: list[dict[str, Any]] = []
    for item in bundle.get("files") or []:
        if not isinstance(item, dict) or not isinstance(item.get("remote_path"), str):
            continue
        parts = [part for part in PurePosixPath(str(item.get("relative") or "")).parts
                 if part not in ("", ".", "..", "/")]
        if not parts:
            continue
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            continue
        local = target.joinpath(*(safe_name(part) for part in parts))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        mapping[item["remote_path"]] = str(local)
        saved.append({"path": str(local), "remote_path": item["remote_path"], "bytes": len(data)})
    summary: dict[str, Any] = {"saved_here": saved}
    too_large = [row for row in bundle.get("too_large") or [] if isinstance(row, dict)]
    if too_large:
        summary["left_on_node"] = too_large
        summary["note"] = (
            f"Files past {_megabytes(int(bundle.get('limit_bytes') or MAX_RETURN_BYTES))} a call "
            "stay on that computer at remote_path."
        )
    return {**_replace(result, mapping), "node_files": summary}
