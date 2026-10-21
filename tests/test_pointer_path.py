"""Pointer paths: the eased, bounded approach and the eased straight drag.

The maths is pure (``actions/pointer_path.py``); the second half drives
``browser_tools`` with a driver double that records every DevTools call, to pin
down which gestures glide, which stay instant for the game hot path, and that
the presence overlay is told about a glide before its first event.
"""

from __future__ import annotations

import math

import pytest

from web_search_neo import agent_presence, browser_tools, pointer_motion
from web_search_neo.actions import pointer_path


# --- the curves --------------------------------------------------------------


@pytest.mark.parametrize("speed", [0.0, 0.5, 1.5, 2.5, 3.0])
def test_hermite_runs_from_zero_to_one_without_going_backwards(speed):
    values = [pointer_path.hermite(i / 100, speed) for i in range(101)]
    assert values[0] == 0.0 and values[-1] == pytest.approx(1.0)
    assert all(b >= a - 1e-12 for a, b in zip(values, values[1:]))
    # It leaves at the speed it was given and arrives at rest.
    assert (values[1] - values[0]) * 100 == pytest.approx(speed, abs=0.05)
    assert (values[-1] - values[-2]) * 100 < 0.05


def test_hermite_without_speed_is_smoothstep():
    assert pointer_path.hermite(0.5) == pytest.approx(0.5)
    assert pointer_path.hermite(0.25) == pytest.approx(0.15625)
    # Out-of-range input and speeds are clamped, never extrapolated.
    assert pointer_path.hermite(-1) == 0.0 and pointer_path.hermite(2) == 1.0
    assert pointer_path.hermite(0.5, 99) == pointer_path.hermite(0.5, pointer_path.MAX_INITIAL_SPEED)


def test_approach_time_grows_with_distance_and_stays_bounded():
    times = [pointer_path.approach_ms(d) for d in (0, 5, 40, 150, 600, 2000, 10_000)]
    assert times == sorted(times)
    assert times[0] == pointer_path.APPROACH_MIN_MS
    assert times[-1] == pointer_path.APPROACH_MAX_MS <= 250


@pytest.mark.parametrize("dx", [300.0, -300.0])
def test_a_sideways_move_bows_upward_whichever_way_it_goes(dx):
    x0, y0, x1, y1 = 400.0, 300.0, 400.0 + dx, 300.0
    bend = pointer_path.bend_for(x0, y0, x1, y1)
    middle = pointer_path.curve_point(x0, y0, x1, y1, bend, 0.5)
    assert middle[1] < y0  # screen y grows downward
    assert y0 - middle[1] == pytest.approx(abs(bend))
    assert abs(bend) == pytest.approx(min(pointer_path.BEND_MAX, 300 * pointer_path.BEND_RATIO))


def test_the_bow_is_capped_and_absent_on_a_tiny_move():
    assert abs(pointer_path.bend_for(0, 0, 5000, 0)) == pointer_path.BEND_MAX
    assert pointer_path.bend_for(10, 10, 11, 11) == 0.0


def test_the_curve_starts_and_ends_exactly_on_its_points():
    assert pointer_path.curve_point(1, 2, 300, 40, 12, 0.0) == pytest.approx((1, 2))
    assert pointer_path.curve_point(1, 2, 300, 40, 12, 1.0) == pytest.approx((300, 40))
    assert pointer_path.curve_point(5, 5, 5, 5, 12, 0.3) == (5, 5)


# --- planning and running an approach ----------------------------------------


def test_no_glide_for_a_hop_too_short_to_see_or_a_point_that_is_not_one():
    assert pointer_path.plan_approach((10, 10), (11, 11)) is None
    assert pointer_path.plan_approach((float("nan"), 0), (100, 100)) is None
    assert pointer_path.plan_approach((0, 0), (float("inf"), 100)) is None


def test_an_approach_is_bounded_eased_and_stops_short_of_its_target():
    glide = pointer_path.plan_approach((40, 500), (900, 120), bounds=(1000, 700))
    assert glide is not None
    assert pointer_path.MIN_STEPS - 1 <= len(glide.points) <= pointer_path.MAX_STEPS - 1
    assert glide.seconds * 1000 <= pointer_path.APPROACH_MAX_MS
    assert glide.ms == round(glide.seconds * 1000)
    times = [at for _x, _y, at in glide.points]
    assert times == sorted(times) and 0 < times[0] and times[-1] < glide.seconds
    # The caller's own event lands on the target; the glide never sends it.
    assert (glide.points[-1][0], glide.points[-1][1]) != (900, 120)
    # Progress along the chord only ever moves forward, slow-fast-slow.
    chord = (860.0, -380.0)
    length = math.hypot(*chord)
    progress = [((x - 40) * chord[0] + (y - 500) * chord[1]) / length for x, y, _ in glide.points]
    assert progress == sorted(progress)
    gaps = [b - a for a, b in zip([0.0] + progress, progress + [length])]
    assert gaps[0] < max(gaps) and gaps[-1] < max(gaps)
    for x, y, _at in glide.points:
        assert 0 <= x <= 999 and 0 <= y <= 699


def test_points_near_the_edge_are_kept_inside_the_window():
    glide = pointer_path.plan_approach((5, 2), (700, 2), bounds=(800, 600))
    assert all(y >= 0 for _x, y, _at in glide.points)


def test_run_keeps_its_own_schedule_and_a_slow_event_eats_into_the_pause():
    glide = pointer_path.plan_approach((0, 300), (600, 300))
    now = {"t": 100.0}
    slept: list[float] = []
    moved: list[tuple[float, float]] = []

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    def move(x, y):
        moved.append((x, y))
        now["t"] += 0.03  # a round trip slower than a frame

    pointer_path.run(glide, move, sleep=sleep, clock=lambda: now["t"])
    assert moved == [(x, y) for x, y, _ in glide.points]
    # Never stretched: the slow events used the pauses up instead of adding to them.
    assert now["t"] - 100.0 == pytest.approx(max(glide.seconds, 0.03 * len(moved)), abs=0.031)


def test_a_drag_stays_on_its_line_and_ends_exactly_there():
    points = pointer_path.drag_points((40.0, 40.0), (240.0, 140.0), 9)
    assert len(points) == 9 and points[-1] == (240.0, 140.0)
    for x, y in points:
        assert (y - 40.0) == pytest.approx((x - 40.0) / 2.0)  # on the segment
    xs = [x for x, _ in points]
    assert xs == sorted(xs)
    steps = [b - a for a, b in zip([40.0] + xs, xs)]
    assert steps[0] < max(steps) and steps[-1] < max(steps)  # eased, not uniform
    # Two steps (no duration) are exactly the old midpoint and end.
    assert pointer_path.drag_points((0, 0), (10, 20), 2) == [(5.0, 10.0), (10, 20)]


@pytest.mark.parametrize("value,expected", [
    (None, True), ("1", True), ("0", False), ("off", False), ("False", False), ("no", False),
])
def test_the_glide_switch(value, expected):
    environ = {} if value is None else {pointer_path.GLIDE_ENV: value}
    assert pointer_path.glide_enabled(environ) is expected


# --- which gestures glide ------------------------------------------------------


class _InputDriver:
    """Records DevTools input and page scripts in one ordered log."""

    is_extension_bridge = True

    def __init__(self):
        self.log: list[tuple[str, object]] = []

    def execute_cdp_cmd(self, command, params):
        self.log.append((command, dict(params)))
        return {}

    def execute_script(self, script, *args):
        self.log.append(("script", args[0] if args else script))
        return True

    def moves(self):
        return [(p["x"], p["y"]) for c, p in self.log
                if c == "Input.dispatchMouseEvent" and p["type"] == "mouseMoved"]

    def leads(self):
        return [p for c, p in self.log if c == "script" and isinstance(p, dict) and "lead" in p]


@pytest.fixture
def pointer_session(monkeypatch):
    monkeypatch.delenv(pointer_path.GLIDE_ENV, raising=False)
    monkeypatch.setattr(pointer_path.time, "sleep", lambda _seconds: None)
    driver = _InputDriver()
    session = browser_tools.BrowserSession(driver=driver, headless=True, profile_mode="temporary")
    session.pointer_x, session.pointer_y = 100.0, 500.0
    browser_tools._sessions["glide-case"] = session
    frame_map = browser_tools._FrameMap(page_width=1000.0, page_height=700.0)
    monkeypatch.setattr(browser_tools, "_pointer_context",
                        lambda _driver, _frame: (frame_map, {"width": 1000.0, "height": 700.0}))
    yield session
    browser_tools._sessions.pop("glide-case", None)


def _pointer(action, x, y, **kwargs):
    return browser_tools.pointer_action(action, x, y, "glide-case", include_summary=False, **kwargs)


def test_hover_glides_from_where_the_pointer_was(pointer_session):
    _pointer("hover", 800, 150)
    moves = pointer_session.driver.moves()
    assert len(moves) > 2
    assert moves[-1] == (800, 150)
    # Every intermediate point lies between the two ends: an approach, not a detour.
    for x, _y in moves[:-1]:
        assert 100 < x < 800
    assert (pointer_session.pointer_x, pointer_session.pointer_y) == (800, 150)


@pytest.mark.parametrize("action", ["move", "click", "press", "double_click"])
def test_the_game_hot_path_still_lands_in_one_event(pointer_session, action):
    _pointer(action, 800, 150)
    assert pointer_session.driver.moves() == [(800, 150)]


def test_a_click_by_coordinates_through_click_glides(pointer_session, monkeypatch):
    monkeypatch.setattr(browser_tools, "_page_summary", lambda _driver, session_id: {"session_id": session_id})
    browser_tools.click(x=800, y=150, session_id="glide-case", wait_seconds=0)
    moves = pointer_session.driver.moves()
    assert len(moves) > 2 and moves[-1] == (800, 150)
    pressed = [p for c, p in pointer_session.driver.log
               if c == "Input.dispatchMouseEvent" and p["type"] == "mousePressed"]
    assert len(pressed) == 1 and (pressed[0]["x"], pressed[0]["y"]) == (800, 150)


def test_the_switch_turns_the_glide_off(pointer_session, monkeypatch):
    monkeypatch.setenv(pointer_path.GLIDE_ENV, "0")
    _pointer("hover", 800, 150)
    assert pointer_session.driver.moves() == [(800, 150)]


def test_no_glide_from_an_unknown_origin_a_lock_or_a_stepped_page(pointer_session):
    pointer_session.pointer_x = pointer_session.pointer_y = 0.0  # never moved
    _pointer("hover", 800, 150)
    assert pointer_session.driver.moves() == [(800, 150)]
    for setup in ({"pointer_locked": True}, {"render_mode": "step"}):
        pointer_session.driver.log.clear()
        pointer_session.pointer_x, pointer_session.pointer_y = 100.0, 500.0
        session = browser_tools.BrowserSession(driver=pointer_session.driver, headless=True)
        session.pointer_x, session.pointer_y = 100.0, 500.0
        for key, value in setup.items():
            setattr(session, key, value)
        pointer_motion.approach(session, "hover", (800.0, 150.0), (800.0, 150.0), None, 0.0,
                                lambda x, y: pointer_session.driver.log.append(("move", (x, y))), glide=True)
        assert pointer_session.driver.log == []


def test_relative_moves_never_glide(pointer_session):
    _pointer("hover", 30, -20, coordinate_mode="delta")
    assert len(pointer_session.driver.moves()) == 1


def test_a_timed_drag_approaches_then_drags_straight(pointer_session):
    _pointer("drag", 300, 300, end_x=500, end_y=400, duration_seconds=0.2)
    log = pointer_session.driver.log
    kinds = [p["type"] for c, p in log if c == "Input.dispatchMouseEvent"]
    pressed_at = kinds.index("mousePressed")
    moves = pointer_session.driver.moves()
    approach, dragged = moves[:pressed_at], moves[pressed_at:]
    assert len(approach) > 2 and approach[-1] == (300, 300)
    assert dragged[-1] == (500, 400)
    for x, y in dragged:
        assert y - 300 == pytest.approx((x - 300) / 2)  # the drag itself never bows
    assert kinds[-1] == "mouseReleased"


def test_an_untimed_drag_in_a_batch_does_not_approach(pointer_session):
    browser_tools.input_batch(pointer_actions=[
        {"action": "drag", "x": 300, "y": 300, "end_x": 500, "end_y": 400},
    ], session_id="glide-case", include_summary=False)
    moves = pointer_session.driver.moves()
    assert moves[0] == (300, 300) and len(moves) == 3  # start, midpoint, end


# --- the overlay hears about a glide before its first event -------------------


def test_the_cursor_is_told_the_path_before_the_events(pointer_session, monkeypatch):
    monkeypatch.delenv(agent_presence.PRESENCE_ENV, raising=False)
    pointer_session.headless = False
    _pointer("hover", 800, 150)
    log = pointer_session.driver.log
    lead_at = next(i for i, (c, p) in enumerate(log) if c == "script" and isinstance(p, dict))
    first_move = next(i for i, (c, p) in enumerate(log) if c == "Input.dispatchMouseEvent")
    assert lead_at < first_move
    (payload,) = pointer_session.driver.leads()
    plan = pointer_path.plan_approach((100.0, 500.0), (800.0, 150.0), bounds=(1000.0, 700.0))
    assert payload["lead"] == {"tap": False, "ms": plan.ms, "bend": plan.bend, "fromX": 100.0, "fromY": 500.0}
    assert payload["cursor"] == {"x": 800.0, "y": 150.0, "tap": False}
    assert payload["caption"] == "Hover" and payload["flash"] is False


def test_a_lead_describes_the_drag_that_follows(pointer_session, monkeypatch):
    monkeypatch.delenv(agent_presence.PRESENCE_ENV, raising=False)
    pointer_session.headless = False
    _pointer("drag", 300, 300, end_x=500, end_y=400, duration_seconds=0.2)
    (payload,) = pointer_session.driver.leads()
    assert payload["lead"]["dragX"] == 500 and payload["lead"]["dragY"] == 400
    assert payload["lead"]["dragMs"] == 200
    assert payload["caption"] == "Drag"


def test_no_lead_when_presence_is_off_or_the_page_is_headless(pointer_session, monkeypatch):
    _pointer("hover", 800, 150)  # headless
    monkeypatch.setenv(agent_presence.PRESENCE_ENV, "0")
    pointer_session.headless = False
    _pointer("hover", 100, 500)
    assert pointer_session.driver.leads() == []
    # The real glide does not depend on the overlay: both hovers glided.
    assert len(pointer_session.driver.moves()) > 4
