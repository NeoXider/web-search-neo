"""A drag path that reads as a hand rather than as a metronome.

Slider vendors do not only check *where* the piece lands: TCaptcha and Geetest
score the movement itself, and the open-source slider solvers that keep working
against them (GeetestSliderCrack and its descendants) spend as much effort on the
trajectory as on finding the hole - accelerate, overshoot slightly, come back,
wobble a little in y, never two identical steps.

So this returns relative moves with the time to wait after each one: ``dx``/``dy``
are offsets from where the press happened, and the last point is exactly the
requested distance with ``dy`` back at zero, because a piece left one pixel short
is a failed captcha and a piece that overshoots and never corrects is a bot.
"""

from __future__ import annotations

import random

MIN_STEPS = 12
MAX_STEPS = 40
# How far past the hole a hand usually goes, in pixels: small, and proportional to
# the distance, because a long reach overshoots more than a short one.
OVERSHOOT_RATIO = 0.06
OVERSHOOT_MIN = 3.0
OVERSHOOT_MAX = 14.0


def _steps_for(distance: float) -> int:
    """More steps for a longer drag, within the range a hand actually produces."""
    return int(max(MIN_STEPS, min(MAX_STEPS, round(10 + distance / 8))))


def human_trajectory(
    distance: float,
    duration_seconds: float = 0.9,
    *,
    seed: int | None = None,
) -> list[tuple[float, float, float]]:
    """Relative moves and pauses for a drag of ``distance`` pixels to the right.

    The pauses sum to ``duration_seconds``. A real slider is never solved in under
    about half a second, so a shorter duration is treated as a request to hurry,
    not as permission to teleport.
    """
    target = max(1.0, float(distance))
    seconds = max(0.35, min(float(duration_seconds), 4.0))
    rng = random.Random(seed)
    steps = _steps_for(target)
    overshoot = min(OVERSHOOT_MAX, max(OVERSHOOT_MIN, target * OVERSHOOT_RATIO))
    peak_at = 0.72

    points: list[tuple[float, float]] = []
    previous_x = 0.0
    for index in range(1, steps + 1):
        ratio = index / steps
        if ratio <= peak_at:
            phase = ratio / peak_at
            eased = 1.0 - (1.0 - phase) ** 3  # fast out, slowing as it approaches
            x = (target + overshoot) * eased
            jitter = rng.uniform(-1.2, 1.2) if 0 < index < steps else 0.0
        else:
            phase = (ratio - peak_at) / (1.0 - peak_at)
            eased = phase * phase * (3.0 - 2.0 * phase)  # ease back onto the target
            x = (target + overshoot) - overshoot * eased
            jitter = rng.uniform(-0.5, 0.5) if index < steps else 0.0
        # A hand speeds up, passes the hole a little, then eases back onto it. The
        # only backward motion allowed is the correction itself, so a step that
        # retreats by more than a pixel or two is noise and gets dropped.
        x = min(max(x + jitter, previous_x - 1.5), target + overshoot + 2.0)
        previous_x = x
        # A wrist lifts a little mid-drag and lands; the ends are never off-axis.
        y = rng.uniform(0.4, 2.4) * (1.0 if 0.15 < ratio < 0.9 else 0.0)
        points.append((x, y))

    points[-1] = (target, 0.0)

    weights = [rng.uniform(0.7, 1.4) for _ in points]
    total = sum(weights)
    path: list[tuple[float, float, float]] = []
    for (dx, dy), weight in zip(points, weights):
        path.append((round(dx, 2), round(dy, 2), round(seconds * weight / total, 4)))
    return path
