"""Frames for one bridge client, written by a thread of its own.

The daemon answers every client from the one thread that reads the companion's
socket. Writing to a websocket blocks when the peer does not read, so a single
client that hung - a stopped debugger, a suspended process, a full pipe - used
to hold that thread, and with it the answers of every other agent sharing the
browser. They saw their own commands time out for a reason that had nothing to
do with them.

An outbox takes the frame and returns at once. Its writer thread sends frames in
the order they were queued, so a slow client delays nobody but itself, and one
that falls hopelessly behind is disconnected instead of growing without bound.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)

# A single frame may be 64 MB (a screenshot); four of them waiting is a peer that is not reading.
MAX_PENDING_BYTES = 256 * 1024 * 1024


class Outbox:
    """An ordered, non-blocking way to send text frames to one connection."""

    def __init__(self, max_pending_bytes: int = MAX_PENDING_BYTES) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._max_pending_bytes = max_pending_bytes
        self._pending_bytes = 0
        self._connection: Any = None
        self._label = "peer"
        self._thread: threading.Thread | None = None
        self._closed = False

    def put(self, connection: Any, text: str, label: str = "") -> None:
        """Queue one frame for ``connection``; raises ``ConnectionError`` once the peer is gone."""
        with self._lock:
            if self._closed:
                raise ConnectionError(f"{label or self._label} is gone")
            if self._pending_bytes + len(text) > self._max_pending_bytes:
                self._closed = True
                waiting = self._pending_bytes
                threading.Thread(target=self._drop, args=(connection,), daemon=True).start()
                raise ConnectionError(
                    f"{label or self._label} stopped reading: {waiting} bytes were already "
                    "waiting for it, so it was disconnected"
                )
            self._pending_bytes += len(text)
            self._idle.clear()
            if self._thread is None:
                self._connection, self._label = connection, label or self._label
                self._thread = threading.Thread(
                    target=self._run, name="web-search-neo-bridge-outbox", daemon=True
                )
                self._thread.start()
        self._queue.put(text)

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            try:
                self._connection.send(text)
            except Exception as exc:
                LOGGER.warning(
                    "Could not write to bridge client %s: %s: %s", self._label, type(exc).__name__, exc
                )
                with self._lock:
                    self._closed = True
                    self._pending_bytes = 0
                    self._idle.set()
                return
            with self._lock:
                self._pending_bytes -= len(text)
                if self._pending_bytes <= 0:
                    self._pending_bytes = 0
                    self._idle.set()

    @staticmethod
    def _drop(connection: Any) -> None:
        try:
            connection.close(code=1011, reason="Bridge client stopped reading")
        except Exception:
            pass

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued so far has been written; False if it has not by ``timeout``."""
        return self._idle.wait(timeout)

    def close(self) -> None:
        """Stop the writer once it has written what is already queued."""
        with self._lock:
            self._closed = True
        self._queue.put(None)
