"""How long to wait for Chrome on a machine that is busy with other work.

The bridge's built-in limits - ten seconds for a capture subscription, fifteen
for a script - are right for an idle machine. On one whose CPU is pegged by a
build, a render or a model the same healthy Chrome needs three times that, and
a fixed limit told the agent the bridge had timed out when nothing was broken
(measured 2026-11-11 with sixteen cores at 100 %: `open` of a heavy page failed
five times in a row, each time at a different command).

Two measurements decide how patient to be, and neither is a guess:

* **How slowly Chrome has just been answering.** The answer times of the
  cheapest commands - the ones that run no page code and wait for no load - are
  kept for a few minutes. While the slowest is over ``SLOW_SECONDS`` every
  built-in limit starts out at ``FACTOR`` times that.
* **How busy the machine was while we waited.** When a built-in limit runs out,
  the CPU time the whole machine spent during that very wait is read. If it was
  above ``BUSY_CPU`` the answer is late, not lost, and the wait goes on, one
  more limit at a time, up to ``FACTOR`` limits in all.

On an idle machine nothing changes: a page stuck in an endless loop uses one
core, the machine is not busy, and the command fails at its usual limit. A
limit a caller chose - ``timeout_seconds`` of a script, an explicit CDP timeout,
the short teardown limit - is a plain number and is kept to the second; only a
limit wrapped in :class:`BuiltIn` may grow.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from typing import Any, Callable

# Commands whose answer time says how busy Chrome is, not how heavy a page is: none of
# them runs page code or waits for a load. `tabs.create` is left out because a companion
# older than 1.30 sits ten seconds on a blank tab for a reason that is not load.
PROBE_METHODS = frozenset({"debugger.attach", "tabs.get", "tabs.list", "tabs.activate", "events.subscribe"})


class BuiltIn(float):
    """A limit the server chose for itself, so it may be extended; a caller's own is a plain float."""


def timeout_message(method: str, waited: float, busy: float | None = None) -> str:
    """What an agent reads when the companion never answered a command."""
    head = f"Chrome bridge command '{method}' timed out after {waited:.0f} s: "
    if busy is not None:
        return head + (
            f"the wait was extended because this machine's CPU was at {busy:.0%}, and the answer still "
            "did not come. Chrome is slow rather than broken: retry when the other work has finished."
        )
    return head + (
        "the companion did not answer. Chrome on a busy machine is slow rather than broken, "
        "so retry once before giving up."
    )


def _cpu_times() -> tuple[float, float] | None:
    """Cumulative (busy, total) CPU time of the whole machine; None where it cannot be read."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None

            def ticks(value: Any) -> int:
                return (value.dwHighDateTime << 32) | value.dwLowDateTime

            total = ticks(kernel) + ticks(user)  # kernel time includes idle time
            return float(total - ticks(idle)), float(total)
        with open("/proc/stat", encoding="ascii") as handle:  # Linux: "cpu user nice system idle iowait ..."
            fields = [float(value) for value in handle.readline().split()[1:9]]
        return sum(fields) - fields[3] - fields[4], sum(fields)
    except Exception:
        return None


def _load_average() -> float | None:
    """The one-minute load per core, where the kernel keeps one (macOS, Linux); else None."""
    try:
        return min(os.getloadavg()[0] / (os.cpu_count() or 1), 1.0)
    except (AttributeError, OSError):
        return None


class CpuLoad:
    """How busy the whole machine was between two looks, 0..1; None when it cannot be told."""

    MIN_INTERVAL = 0.5  # two looks closer than this share one answer

    def __init__(
        self,
        read: Callable[[], tuple[float, float] | None] = _cpu_times,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read, self._now = read, now
        self._lock = threading.Lock()
        self._last: tuple[float, float, float] | None = None  # when, busy, total
        self._value: float | None = None

    def look(self) -> float | None:
        """The share of CPU time used since the previous look."""
        sample = self._read()
        if sample is None:
            return _load_average()
        when = self._now()
        with self._lock:
            if self._last is None:
                self._last = (when, *sample)
            elif when - self._last[0] >= self.MIN_INTERVAL and sample[1] > self._last[2]:
                self._value = (sample[0] - self._last[1]) / (sample[1] - self._last[2])
                self._last = (when, *sample)
            return self._value


class Patience:
    """Waits for the companion's answers; see the module text for how long and why."""

    SLOW_SECONDS = 3.0
    FACTOR = 4.0
    MAX_SECONDS = 120.0
    WINDOW_SECONDS = 180.0
    BUSY_CPU = 0.8

    def __init__(self, now: Callable[[], float] = time.monotonic, load: CpuLoad | None = None) -> None:
        self._now = now
        self._load = load or CpuLoad(now=now)
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, float]] = deque(maxlen=256)

    def clock(self) -> float:
        """The time ``wait`` measures from; read it before the command is sent."""
        return self._now()

    def note(self, method: str, seconds: float) -> None:
        """Remember how long ``method`` took; only the probe commands count."""
        if method in PROBE_METHODS:
            with self._lock:
                self._samples.append((self._now(), float(seconds)))

    def slowest(self) -> float:
        """The slowest probe answer of the last few minutes, in seconds."""
        horizon = self._now() - self.WINDOW_SECONDS
        with self._lock:
            while self._samples and self._samples[0][0] < horizon:
                self._samples.popleft()
            return max((seconds for _, seconds in self._samples), default=0.0)

    def floor(self) -> float:
        """The least a built-in limit should be right now; 0 while Chrome is quick."""
        slowest = self.slowest()
        return min(slowest * self.FACTOR, self.MAX_SECONDS) if slowest >= self.SLOW_SECONDS else 0.0

    def stretch(self, seconds: float) -> BuiltIn:
        return BuiltIn(max(float(seconds), self.floor()))

    def wait(
        self, answered: threading.Event, method: str, timeout: float, started: float | None = None
    ) -> None:
        """Wait for ``answered``; raise ``TimeoutError`` saying what happened if it never comes."""
        started = self._now() if started is None else started
        limit = max(0.1, float(timeout))
        extendable = isinstance(timeout, BuiltIn)
        if extendable:
            self._load.look()  # the mark that "while we waited" is measured from
        longest = max(limit, min(limit * self.FACTOR, self.MAX_SECONDS))
        waited, reason = 0.0, None
        while True:
            step = min(limit, longest - waited)
            if answered.wait(step):
                self.note(method, self._now() - started)
                return
            waited += step
            busy = self._load.look() if extendable else None
            if busy is None or busy < self.BUSY_CPU or waited >= longest:
                break
            reason = busy  # the machine was busy for the whole of that wait: the answer is late, not lost
        self.note(method, waited)
        raise TimeoutError(timeout_message(method, waited, reason))


def patient(bridge: Any, seconds: float) -> BuiltIn:
    """``seconds`` as a built-in limit: longer while ``bridge`` has seen Chrome answer slowly."""
    patience = getattr(bridge, "patience", None)
    return patience.stretch(seconds) if isinstance(patience, Patience) else BuiltIn(seconds)
