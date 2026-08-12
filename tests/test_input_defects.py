"""Input defects: frame transforms, key aliases, stuck presses, the virtual clock.

Every browser test here drives a real fixture and asserts what the page reports
about the events it received, never what the tool says it sent. The frame cases
use ``tests/fixtures/games/frame_transforms.html``, which holds four copies of the
pointer harness behind four different transforms; the clock cases use
``tests/fixtures/games/timers.html``, which records the clock each callback saw.
"""

from __future__ import annotations

import time

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

import browser_tools


POINTER_FIXTURE = "/fixtures/games/pointer.html"
FRAME_FIXTURE = "/fixtures/games/frame_transforms.html"
TIMER_FIXTURE = "/fixtures/games/timers.html"


def _open_or_skip(url: str, session_id: str, **kwargs):
    kwargs.setdefault("headless", True)
    kwargs.setdefault("profile_mode", "temporary")
    try:
        return browser_tools.open_page(url, session_id=session_id, **kwargs)
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def _open(local_site, fixture: str, session_id: str):
    _open_or_skip(f"{local_site.base_url}{fixture}", session_id)
    return browser_tools._get_session(session_id)


def _input(session, projection: str):
    return session.driver.execute_script(f"const s = window.__input; return {projection};")


def _in_frame(session, frame_selector: str, script: str):
    """Read something from inside one frame and come back to the top document."""
    driver = session.driver
    driver.switch_to.default_content()
    driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, frame_selector))
    try:
        return driver.execute_script(script)
    finally:
        driver.switch_to.default_content()


def _pad_origin(session, frame_selector: str) -> dict[str, float]:
    return _in_frame(
        session,
        frame_selector,
        "const r = document.getElementById('pad').getBoundingClientRect();"
        "return {x: r.x, y: r.y};",
    )


def _key_events(session, code: str) -> list[dict[str, str]]:
    return _input(
        session,
        f"s.keys.filter(k => k.code === '{code}' && (k.type === 'keydown' || k.type === 'keyup'))"
        " .map(k => ({type: k.type, key: k.key, code: k.code}))",
    )


@pytest.mark.parametrize(
    ("frame_selector", "shape"),
    [
        ("#plain", "no transform"),
        ("#scaled", "scale(0.5)"),
        ("#rotated", "rotate(20deg)"),
        ("#nested", "a scaled ancestor"),
    ],
)
def test_a_click_inside_a_transformed_frame_lands_where_it_was_aimed(
    local_site, frame_selector, shape
):
    session = _open(local_site, FRAME_FIXTURE, "frame-transform")
    try:
        origin = _pad_origin(session, frame_selector)
        browser_tools.pointer_action(
            "click",
            origin["x"] + 120,
            origin["y"] + 70,
            "frame-transform",
            frame_selector=frame_selector,
            wait_seconds=0,
        )
        landed = _in_frame(
            session,
            frame_selector,
            "const s = window.__input;"
            "return {downs: s.pointerDowns, x: s.dragStartX, y: s.dragStartY};",
        )
        assert landed["downs"] == 1, f"the click never reached the frame with {shape}"
        # The harness reports pad-relative coordinates, so the aim is exact.
        assert (landed["x"], landed["y"]) == pytest.approx((120, 70), abs=1.0)
    finally:
        browser_tools.close_session("frame-transform")


def test_a_frame_scrolled_out_of_the_window_refuses_the_click_instead_of_missing(
    local_site,
):
    session = _open(local_site, FRAME_FIXTURE, "frame-clipped")
    try:
        origin = _pad_origin(session, "#plain")
        # Push the top of the frame above the window; its own viewport still
        # reports the same coordinates, so only the page mapping can catch this.
        session.driver.execute_script("window.scrollTo(0, 400);")
        with pytest.raises(ValueError) as failure:
            browser_tools.pointer_action(
                "click",
                origin["x"] + 40,
                origin["y"] + 20,
                "frame-clipped",
                frame_selector="#plain",
                wait_seconds=0,
            )
        assert "outside" in str(failure.value)
        assert "Scroll the frame into view" in str(failure.value)
        assert _in_frame(session, "#plain", "return window.__input.pointerDowns;") == 0

        # The part of the frame that is still on screen keeps working.
        session.driver.execute_script("window.scrollTo(0, 0);")
        browser_tools.pointer_action(
            "click",
            origin["x"] + 40,
            origin["y"] + 20,
            "frame-clipped",
            frame_selector="#plain",
            wait_seconds=0,
        )
        assert _in_frame(session, "#plain", "return window.__input.pointerDowns;") == 1
    finally:
        browser_tools.close_session("frame-clipped")


@pytest.mark.parametrize(
    ("pressed", "released", "code"),
    [
        ("CTRL", "CONTROL", "ControlLeft"),
        ("LEFT", "ARROW_LEFT", "ArrowLeft"),
        (" ", "SPACE", "Space"),
        ("ESC", "ESCAPE", "Escape"),
    ],
)
def test_a_key_held_under_one_name_is_released_under_any_of_its_names(
    local_site, pressed, released, code
):
    session = _open(local_site, POINTER_FIXTURE, "key-alias")
    try:
        browser_tools.press_keys([pressed], "key-alias", action="hold", wait_seconds=0)
        assert [item["type"] for item in _key_events(session, code)] == ["keydown"]

        result = browser_tools.press_keys(
            [released], "key-alias", action="release", wait_seconds=0
        )
        assert result["held_keys"] == []
        assert session.held_keys == {}
        assert browser_tools._session_modifiers(session) == 0
        assert [item["type"] for item in _key_events(session, code)] == [
            "keydown",
            "keyup",
        ]
    finally:
        browser_tools.close_session("key-alias")


def test_holding_the_same_key_under_two_names_presses_it_once(local_site):
    session = _open(local_site, POINTER_FIXTURE, "key-alias-hold")
    try:
        browser_tools.press_keys(["LEFT"], "key-alias-hold", action="hold", wait_seconds=0)
        held = browser_tools.press_keys(
            ["ARROW_LEFT"], "key-alias-hold", action="hold", wait_seconds=0
        )
        assert held["held_keys"] == ["LEFT"]
        assert [item["type"] for item in _key_events(session, "ArrowLeft")] == ["keydown"]

        released = browser_tools.press_keys(
            ["LEFT"], "key-alias-hold", action="release", wait_seconds=0
        )
        assert released["held_keys"] == []
        assert [item["type"] for item in _key_events(session, "ArrowLeft")] == [
            "keydown",
            "keyup",
        ]
    finally:
        browser_tools.close_session("key-alias-hold")


def test_a_batch_that_never_reaches_the_page_holds_no_keys(local_site):
    session = _open(local_site, POINTER_FIXTURE, "batch-abort")
    try:
        with pytest.raises(Exception):
            browser_tools.input_batch(
                key_actions=[
                    {"key": "SHIFT", "action": "hold"},
                    {"key": "W", "action": "tap"},
                ],
                session_id="batch-abort",
                frame_selector="#there-is-no-such-frame",
                wait_seconds=0,
            )
        assert session.held_keys == {}
        assert session.fresh_keys == set()
        assert browser_tools._session_modifiers(session) == 0
        assert _key_events(session, "ShiftLeft") == []

        # A modifier the page never received must not colour the events that
        # follow, and a key the session wrongly believes is down would never be
        # pressed again - it would look to the tool like it was already held.
        pad = session.driver.execute_script(
            "const r = document.getElementById('pad').getBoundingClientRect();"
            "return {x: r.x + 60, y: r.y + 40};"
        )
        clicked = browser_tools.pointer_action(
            "click", pad["x"], pad["y"], "batch-abort", wait_seconds=0
        )
        assert clicked["modifiers"] == 0
        held = browser_tools.press_keys(
            ["W"], "batch-abort", action="hold", wait_seconds=0
        )
        assert held["held_keys"] == ["W"]
        assert [item["type"] for item in _key_events(session, "KeyW")] == ["keydown"]
    finally:
        browser_tools.close_session("batch-abort")


def _break_the_gate(session) -> None:
    """Leave a gate in place that fails the moment a frame is asked for."""
    session.driver.execute_script(
        "window.__webSearchNeoRenderControl = {mode: 'step',"
        " step: function () { throw new Error('the gate is gone'); }};"
    )


def test_a_tap_is_lifted_even_when_the_frame_advance_fails(local_site):
    session = _open(local_site, POINTER_FIXTURE, "tap-stuck")
    try:
        browser_tools.set_render_control("step", "tap-stuck")
        _break_the_gate(session)
        with pytest.raises(Exception):
            browser_tools.press_keys(
                ["W"], "tap-stuck", action="tap", hold_frames=1, wait_seconds=0
            )
        assert [item["type"] for item in _key_events(session, "KeyW")] == [
            "keydown",
            "keyup",
        ]
        assert session.held_keys == {}
    finally:
        browser_tools.close_session("tap-stuck")


def test_a_batched_tap_is_lifted_even_when_the_frame_advance_fails(local_site):
    session = _open(local_site, POINTER_FIXTURE, "batch-tap-stuck")
    try:
        browser_tools.set_render_control("step", "batch-tap-stuck")
        _break_the_gate(session)
        with pytest.raises(Exception):
            browser_tools.input_batch(
                key_actions=[{"key": "SPACE", "action": "tap"}],
                session_id="batch-tap-stuck",
                wait_seconds=0,
            )
        assert [item["type"] for item in _key_events(session, "Space")] == [
            "keydown",
            "keyup",
        ]
        assert session.held_keys == {}
    finally:
        browser_tools.close_session("batch-tap-stuck")


def test_releasing_one_finger_leaves_the_others_on_the_page(local_site):
    session = _open(local_site, POINTER_FIXTURE, "touch-partial")
    try:
        browser_tools.set_touch_emulation("touch-partial", enabled=True)
        pad = session.driver.execute_script(
            "const r = document.getElementById('pad').getBoundingClientRect();"
            "return {x: r.x, y: r.y};"
        )
        first = {"x": pad["x"] + 40, "y": pad["y"] + 40, "id": 0}
        second = {"x": pad["x"] + 160, "y": pad["y"] + 90, "id": 1}
        browser_tools.touch_action("press", [first, second], "touch-partial", wait_seconds=0)
        assert _input(session, "s.activeTouches") == 2

        lifted = browser_tools.touch_action(
            "release", [{"id": 0}], "touch-partial", wait_seconds=0
        )
        assert lifted["active_touches"] == [1]
        assert _input(session, "s.activeTouches") == 1
        assert sorted(session.held_touches) == [1]

        # The surviving finger is still a real one: Chrome refuses to move a
        # point it thinks was already released.
        moved = browser_tools.touch_action(
            "move",
            [{"x": pad["x"] + 200, "y": pad["y"] + 120, "id": 1}],
            "touch-partial",
            wait_seconds=0,
        )
        assert moved["success"] is True
        assert _input(session, "s.activeTouches") == 1
    finally:
        browser_tools.close_session("touch-partial")


def test_a_tap_does_not_lift_a_finger_that_is_still_pressed(local_site):
    session = _open(local_site, POINTER_FIXTURE, "touch-tap-mix")
    try:
        browser_tools.set_touch_emulation("touch-tap-mix", enabled=True)
        pad = session.driver.execute_script(
            "const r = document.getElementById('pad').getBoundingClientRect();"
            "return {x: r.x, y: r.y};"
        )
        browser_tools.touch_action(
            "press",
            [{"x": pad["x"] + 40, "y": pad["y"] + 40, "id": 0}],
            "touch-tap-mix",
            wait_seconds=0,
        )
        browser_tools.touch_action(
            "tap",
            [{"x": pad["x"] + 200, "y": pad["y"] + 120, "id": 3}],
            "touch-tap-mix",
            wait_seconds=0,
        )
        assert _input(session, "s.activeTouches") == 1
        assert sorted(session.held_touches) == [0]
    finally:
        browser_tools.close_session("touch-tap-mix")


def test_release_inputs_lifts_touch_points_as_well_as_keys(local_site):
    session = _open(local_site, POINTER_FIXTURE, "touch-release-all")
    try:
        browser_tools.set_touch_emulation("touch-release-all", enabled=True)
        pad = session.driver.execute_script(
            "const r = document.getElementById('pad').getBoundingClientRect();"
            "return {x: r.x, y: r.y};"
        )
        browser_tools.touch_action(
            "press",
            [
                {"x": pad["x"] + 40, "y": pad["y"] + 40, "id": 0},
                {"x": pad["x"] + 160, "y": pad["y"] + 90, "id": 1},
            ],
            "touch-release-all",
            wait_seconds=0,
        )
        browser_tools.press_keys(["W"], "touch-release-all", action="hold", wait_seconds=0)
        assert _input(session, "s.activeTouches") == 2

        released = browser_tools.release_inputs("touch-release-all")
        assert released["held_touches"] == []
        assert released["held_keys"] == []
        assert session.held_touches == {}
        assert _input(session, "s.activeTouches") == 0
    finally:
        browser_tools.close_session("touch-release-all")


def test_a_touch_outside_the_viewport_is_refused_instead_of_dropped(local_site):
    session = _open(local_site, POINTER_FIXTURE, "touch-bounds")
    try:
        browser_tools.set_touch_emulation("touch-bounds", enabled=True)
        viewport = session.driver.execute_script(
            "return {width: window.innerWidth, height: window.innerHeight};"
        )
        with pytest.raises(ValueError) as failure:
            browser_tools.touch_action(
                "tap",
                [{"x": viewport["width"] + 300, "y": viewport["height"] + 300}],
                "touch-bounds",
                wait_seconds=0,
            )
        assert "outside the selected" in str(failure.value)
        with pytest.raises(ValueError):
            browser_tools.touch_action(
                "tap", [{"x": -50, "y": 20}], "touch-bounds", wait_seconds=0
            )
        assert _input(session, "s.touchStarts") == 0
        assert session.held_touches == {}
    finally:
        browser_tools.close_session("touch-bounds")


def _lock_delta(session, moves: int | None = None) -> dict[str, float]:
    """Read the locked-pointer totals, waiting for a move that is still in flight.

    A dispatched CDP event is answered before the renderer has run it, so the
    page can be one turn behind the call that caused it.
    """
    deadline = time.monotonic() + 3.0
    while True:
        state = _input(session, "{x: s.lockDelta.x, y: s.lockDelta.y, moves: s.lockMoves}")
        if moves is None or state["moves"] >= moves or time.monotonic() > deadline:
            return state
        time.sleep(0.02)


def test_the_first_relative_move_carries_the_delta_and_nothing_else(local_site):
    session = _open(local_site, POINTER_FIXTURE, "relative-start")
    try:
        acquired = browser_tools.pointer_lock("acquire", "relative-start", selector="#pad")
        if not acquired["locked"]:
            pytest.skip(f"Pointer lock is unavailable in this browser: {acquired}")
        # pointerlockchange arrives in a task of its own, and the harness only
        # counts raw movement once it has seen it.
        deadline = time.monotonic() + 3.0
        while not _input(session, "s.locked") and time.monotonic() < deadline:
            time.sleep(0.02)
        before = _lock_delta(session)
        browser_tools.pointer_action(
            "move", 3, 2, "relative-start", coordinate_mode="relative", wait_seconds=0
        )
        after = _lock_delta(session, before["moves"] + 1)
        assert after["moves"] == before["moves"] + 1
        # Anything else means the pointer was warped somewhere first and the game
        # read the warp as a turn.
        assert (after["x"] - before["x"], after["y"] - before["y"]) == (3, 2)

        # Nor may a long run snap back: turning far enough in one direction used
        # to trip a recentring guard, and the warp back to the middle of the
        # viewport reached the game as one enormous turn.
        for _ in range(3):
            browser_tools.pointer_action(
                "move",
                40_000,
                0,
                "relative-start",
                coordinate_mode="relative",
                wait_seconds=0,
            )
        before = _lock_delta(session, after["moves"] + 3)
        browser_tools.pointer_action(
            "move", -4, 6, "relative-start", coordinate_mode="relative", wait_seconds=0
        )
        after = _lock_delta(session, before["moves"] + 1)
        assert (after["x"] - before["x"], after["y"] - before["y"]) == (-4, 6)
    finally:
        browser_tools.close_session("relative-start")


def _timer_report(session) -> dict[str, float]:
    return session.driver.execute_script("return window.__report();")


def test_gated_timers_keep_their_period_across_a_released_frame(local_site):
    session = _open(local_site, TIMER_FIXTURE, "clock-intervals")
    try:
        browser_tools.set_render_control("step", "clock-intervals", frame_delta_ms=100)
        session.driver.execute_script("window.__resetTimers();")
        start = _timer_report(session)
        browser_tools.render_step(5, "clock-intervals", include_summary=False)
        report = _timer_report(session)

        # Five frames of 100ms each: half a second of the page's own time.
        assert report["now"] - start["now"] == pytest.approx(500, abs=1)
        assert report["frames"] == 5
        # A 5ms interval owes ~100 ticks in half a second. Rescheduling it from
        # the end of the frame instead of from its own deadline used to hand it
        # exactly one tick per frame, which made it as slow as the frame rate.
        assert report["fast"] >= 80
        assert report["slow"] == pytest.approx(5, abs=1)
        assert report["fast"] > 10 * report["slow"]
        # And it ticks along the frame rather than all at once at the end of it.
        assert report["fast_distinct"] >= 40
        assert report["late"] == 1  # the 250ms one-shot fired, once
    finally:
        browser_tools.set_render_control("normal", "clock-intervals")
        browser_tools.close_session("clock-intervals")


def test_a_zero_delay_timer_chain_is_bounded_and_moves_the_clock(local_site):
    session = _open(local_site, TIMER_FIXTURE, "clock-chain")
    try:
        browser_tools.set_render_control("step", "clock-chain", frame_delta_ms=1000 / 60)
        session.driver.execute_script("window.__resetTimers();")
        start = _timer_report(session)
        browser_tools.render_step(10, "clock-chain", include_summary=False)
        report = _timer_report(session)

        # A `setTimeout(loop, 0)` chain used to run a fixed 64 ticks per frame,
        # every one of them reading the same instant, so the game ran ten times
        # too fast while its own clock said no time had passed at all.
        assert report["chain"] < 30 * report["frames"]
        assert report["chain_distinct"] >= 20
        # The ticks are spread across the ten frames of virtual time, not piled
        # up on their boundaries.
        assert report["chain_span"] == pytest.approx(report["now"] - start["now"], abs=25)
    finally:
        browser_tools.set_render_control("normal", "clock-chain")
        browser_tools.close_session("clock-chain")


_PORT_SPY_SCRIPT = """
delete window.__webSearchNeoRenderControl;
window.__ports = 0;
window.__timers = 0;
const RealChannel = MessageChannel;
window.MessageChannel = function () {
  const channel = new RealChannel();
  const post = channel.port2.postMessage.bind(channel.port2);
  channel.port2.postMessage = value => { window.__ports += 1; return post(value); };
  return channel;
};
const realTimeout = window.setTimeout;
window.setTimeout = function (...args) { window.__timers += 1; return realTimeout.apply(window, args); };
"""


def test_stepping_yields_through_a_message_port_not_a_throttled_timer(local_site):
    session = _open(local_site, POINTER_FIXTURE, "step-yield")
    try:
        # The gate is installed before any page script, so the only way to see
        # which primitive it yields with is to watch them and re-arm it.
        session.driver.execute_script(_PORT_SPY_SCRIPT)
        browser_tools.set_render_control("step", "step-yield")
        stepped = browser_tools.render_step(6, "step-yield", include_summary=False)
        counts = session.driver.execute_script(
            "return {ports: window.__ports, timers: window.__timers};"
        )
        assert stepped["frames"] == 6
        # Five yields between six frames. A hidden tab clamps a timer to about a
        # second, which is what made sixty stepped frames take a minute.
        assert counts["ports"] >= 5
        assert counts["timers"] == 0
    finally:
        browser_tools.set_render_control("normal", "step-yield")
        browser_tools.close_session("step-yield")


def test_stepping_still_works_without_a_message_channel(local_site):
    session = _open(local_site, POINTER_FIXTURE, "step-yield-fallback")
    try:
        session.driver.execute_script(
            "delete window.__webSearchNeoRenderControl; window.MessageChannel = undefined;"
        )
        browser_tools.set_render_control("step", "step-yield-fallback")
        stepped = browser_tools.render_step(4, "step-yield-fallback", include_summary=False)
        assert stepped["frames"] == 4
        assert stepped["success"] is True
    finally:
        browser_tools.set_render_control("normal", "step-yield-fallback")
        browser_tools.close_session("step-yield-fallback")
