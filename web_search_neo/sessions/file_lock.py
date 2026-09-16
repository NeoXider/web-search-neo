"""Cross-process exclusive access to a small JSON file, stdlib only.

A ``threading.Lock`` only serialises one server process, and several MCP
servers - one per agent - share a project directory. Two of them reading the
same ledger, each adding its entry and writing back, lose one entry; for a
one-time submit guard that lost entry is a duplicate submit. The lock here is an
OS lock on a sidecar ``.lock`` file, so it holds across processes, and writes go
through a uniquely named temporary file so two writers never share one.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import time

if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt
else:  # pragma: no cover - platform branch
    import fcntl

_LOCK_TIMEOUT_SECONDS = 30.0
_REPLACE_RETRY_SECONDS = 2.0
# A process-local gate in front of the OS lock: msvcrt byte locks are per
# handle, so two threads here must not both open one. A thread that already
# holds a path's lock re-enters without touching the OS lock again (a second
# handle would wait on the first one forever), tracked by a per-thread depth.
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


def _thread_lock(key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def exclusive(path: Path, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive lock on ``<path>.lock`` for the duration of the block.

    Re-entrant within one thread: a nested ``exclusive`` on the same path
    returns at once and the OS lock is released by the outermost block.
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(lock_path.resolve()))
    depths: dict[str, int] = _HELD.__dict__.setdefault("depths", {})
    if depths.get(key):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return
    gate = _thread_lock(key)
    deadline = time.monotonic() + timeout
    if not gate.acquire(timeout=max(timeout, 0.0)):
        raise TimeoutError(f"could not lock {lock_path} within {timeout:g}s")
    try:
        handle = open(lock_path, "a+b")
        try:
            while True:
                try:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"could not lock {lock_path} within {timeout:g}s") from None
                    time.sleep(0.05)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
    finally:
        gate.release()


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a unique temp file in the same directory, then replace.

    On Windows ``os.replace`` fails with PermissionError while another process
    (a reader, an indexer, antivirus) has the target open; that is transient, so
    it is retried briefly before giving up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
        while True:
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
