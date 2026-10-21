"""Eased, bounded pointer paths: where an approach or a drag passes, and when.

Pure geometry and timing, no browser. ``pointer_motion`` dispatches the points
over CDP, and the presence overlay in the page replays the same arc, so the
ghost cursor a person watches and the events the page receives agree.

Two rules shape it:

* an *approach* - the move that brings the pointer onto a click or a hover -
  bows a little and eases in and out, the way a wrist moves, and is bounded in
  steps and in time so it can never become the slow part of a call;
* a *drag* stays exactly on the straight line it was asked for (a drawing
  surface would otherwise draw a curve) and only its timing is eased.

The overlay script carries a copy of ``bend_for``, ``hermite`` and the curve,
so a change here is a change to ``presence_overlay`` as well.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Callable, Mapping

GLIDE_ENV = "WEB_SEARCH_NEO_POINTER_GLIDE"

# Below this a glide is invisible to a person and pointless to a page.
MIN_DISTANCE = 3.0
# Fitts-shaped: a short hop is quick, a reach across the window takes longer,
# and nothing takes long. The cap keeps an approach well inside the 0.5 s a
# click already waits for the page to settle.
APPROACH_BASE_MS = 50.0
APPROACH_SLOPE_MS = 28.0
APPROACH_SCALE_PX = 16.0
APPROACH_MIN_MS = 60.0
APPROACH_MAX_MS = 220.0
# About one event per display frame, and never more than MAX_STEPS of them:
# each one is a DevTools round trip.
FRAME_MS = 16.0
MIN_STEPS = 3
MAX_STEPS = 12
# How far the arc bows away from the straight line at its widest.
BEND_RATIO = 0.06
BEND_MAX = 24.0
# The fastest a retargeted ease may leave its start; above 3 the cubic is no
# longer monotonic and the pointer would run backwards.
MAX_INITIAL_SPEED = 3.0


def glide_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """``WEB_SEARCH_NEO_POINTER_GLIDE=0`` (or false/no/off) restores teleporting."""
    value = (os.environ if environ is None else environ).get(GLIDE_ENV, "")
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def hermite(t: float, initial_speed: float = 0.0) -> float:
    """Progress 0..1 along a cubic that starts at ``initial_speed`` and stops dead.

    With no initial speed this is smoothstep: zero speed at both ends. A glide
    that interrupts another passes the speed it inherited, so a retarget bends
    away instead of stopping first.
    """
    u = min(1.0, max(0.0, float(t)))
    s = min(MAX_INITIAL_SPEED, max(0.0, float(initial_speed)))
    return s * (u * u * u - 2.0 * u * u + u) + (3.0 * u * u - 2.0 * u * u * u)


def approach_ms(distance: float) -> float:
    """How long an approach of ``distance`` CSS pixels takes, in milliseconds."""
    spread = APPROACH_SLOPE_MS * math.log2(1.0 + max(0.0, float(distance)) / APPROACH_SCALE_PX)
    return min(APPROACH_MAX_MS, max(APPROACH_MIN_MS, APPROACH_BASE_MS + spread))


def bend_for(x0: float, y0: float, x1: float, y1: float) -> float:
    """Signed bow in pixels along the left-hand normal ``(-dy, dx)`` of the move.

    A hand pivots at the wrist below the mouse, so a sideways move arcs upward
    whichever way it goes; the sign flips with the horizontal direction to keep
    the bow on top.
    """
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    if distance < MIN_DISTANCE:
        return 0.0
    size = min(BEND_MAX, distance * BEND_RATIO)
    return -size if dx > 0 else size


def curve_point(
    x0: float, y0: float, x1: float, y1: float, bend: float, t: float
) -> tuple[float, float]:
    """The point at parameter ``t`` on the quadratic arc whose widest bow is ``bend``."""
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    if distance == 0.0:
        return x0, y0
    # A quadratic Bezier passes half way to its control point, so the control
    # sits twice as far out as the bow should be.
    cx = (x0 + x1) / 2.0 - dy / distance * 2.0 * bend
    cy = (y0 + y1) / 2.0 + dx / distance * 2.0 * bend
    u = 1.0 - t
    return (
        u * u * x0 + 2.0 * u * t * cx + t * t * x1,
        u * u * y0 + 2.0 * u * t * cy + t * t * y1,
    )


@dataclass(frozen=True)
class Glide:
    """A planned approach: the arc, its length in time, and the points on it.

    ``points`` are ``(x, y, at_seconds)`` measured from the start of the glide,
    and exclude the end itself - the caller's own event lands there.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    bend: float
    seconds: float
    points: tuple[tuple[float, float, float], ...]

    @property
    def ms(self) -> int:
        return int(round(self.seconds * 1000.0))


def _finite(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def plan_approach(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    bounds: tuple[float, float] | None = None,
) -> Glide | None:
    """The approach from ``start`` to ``end``, or None when there is nothing to glide.

    ``bounds`` is the window (width, height); intermediate points are kept
    inside it, because CDP drops an event outside the window without a word.
    """
    (x0, y0), (x1, y1) = start, end
    if not _finite(x0, y0, x1, y1):
        return None
    distance = math.hypot(x1 - x0, y1 - y0)
    if distance < MIN_DISTANCE:
        return None
    ms = approach_ms(distance)
    steps = max(MIN_STEPS, min(MAX_STEPS, int(round(ms / FRAME_MS))))
    bend = bend_for(x0, y0, x1, y1)
    width, height = bounds if bounds else (0.0, 0.0)
    points = []
    for index in range(1, steps):
        t = index / steps
        x, y = curve_point(x0, y0, x1, y1, bend, hermite(t))
        if width > 0:
            x = min(max(x, 0.0), width - 1.0)
        if height > 0:
            y = min(max(y, 0.0), height - 1.0)
        points.append((round(x, 2), round(y, 2), round(ms * t / 1000.0, 4)))
    return Glide((float(x0), float(y0)), (float(x1), float(y1)), round(bend, 2), ms / 1000.0, tuple(points))


def run(
    glide: Glide,
    move: Callable[[float, float], None],
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Send each point on its own schedule, then wait out the last step.

    Pauses are measured against the clock, not added up, so a slow round trip
    eats into the next pause instead of stretching the glide: the whole thing
    costs ``glide.seconds`` or the time the events themselves take, whichever
    is longer.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    began = clock()
    for x, y, at in glide.points:
        lag = at - (clock() - began)
        if lag > 0:
            sleep(lag)
        move(x, y)
    rest = glide.seconds - (clock() - began)
    if rest > 0:
        sleep(rest)


def drag_points(
    start: tuple[float, float], end: tuple[float, float], steps: int
) -> list[tuple[float, float]]:
    """``steps`` points on the straight segment, eased in time, the last exactly ``end``."""
    (x0, y0), (x1, y1) = start, end
    count = max(1, int(steps))
    points = []
    for index in range(1, count + 1):
        progress = hermite(index / count)
        points.append((x0 + (x1 - x0) * progress, y0 + (y1 - y0) * progress))
    points[-1] = (x1, y1)
    return points
