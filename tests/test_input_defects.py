"""Input defects: frame transforms, key aliases, stuck presses, the virtual clock.

Every browser test here drives a real fixture and asserts what the page reports
about the events it received, never what the tool says it sent. The frame cases
use ``tests/fixtures/games/frame_transforms.html``, which holds four copies of the
pointer harness behind four different transforms, and
``tests/fixtures/games/frame_properties.html``, which holds copies behind the
individual transform properties, CSS ``zoom``, a clip and an overlay; the clock
cases use ``tests/fixtures/games/timers.html``, which records the clock each
callback saw.
"""

from __future__ import annotations

import time

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from web_search_neo import browser_tools


POINTER_FIXTURE = "/fixtures/games/pointer.html"
FRAME_FIXTURE = "/fixtures/games/frame_transforms.html"
PROPERTY_FIXTURE = "/fixtures/games/frame_properties.html"
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
        # The named pairs collapse to one spelling in _normalize_game_key, so
        # only the last two reach _held_slot as genuinely different strings and
        # need the US-layout `code` to be recognised as the same physical key:
        # a letter spelled in either case, and a digit released by the character
        # its own key produces with Shift down.
        ("CTRL", "CONTROL", "ControlLeft"),
        ("LEFT", "ARROW_LEFT", "ArrowLeft"),
        (" ", "SPACE", "Space"),
        ("ESC", "ESCAPE", "Escape"),
        ("w", "W", "KeyW"),
        ("2", "@", "Digit2"),
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


def _scroll_page(session, offset: float) -> None:
    session.driver.execute_script("window.scrollTo(0, arguments[0]);", offset)


@pytest.mark.parametrize(
    ("frame_selector", "scroll_y", "shape"),
    [
        ("#rotprop", 0, "rotate: 20deg"),
        ("#scaleprop", 0, "scale: 0.5"),
        ("#zoomed", 1900, "zoom: 2"),
    ],
)
def test_a_click_lands_where_it_was_aimed_under_a_property_transform_or_zoom(
    local_site, frame_selector, scroll_y, shape
):
    """The individual transform properties and `zoom` move a frame just as much
    as `transform` does, and neither of them shows up in the computed
    `transform` the frame map used to read."""
    session = _open(local_site, PROPERTY_FIXTURE, "frame-properties")
    try:
        origin = _pad_origin(session, frame_selector)
        _scroll_page(session, scroll_y)
        result = browser_tools.pointer_action(
            "click",
            origin["x"] + 120,
            origin["y"] + 70,
            "frame-properties",
            frame_selector=frame_selector,
            wait_seconds=0,
        )
        assert result["success"] is True
        landed = _in_frame(
            session,
            frame_selector,
            "const s = window.__input;"
            "return {downs: s.pointerDowns, x: s.dragStartX, y: s.dragStartY};",
        )
        assert landed["downs"] == 1, f"the click never reached the frame with {shape}"
        assert (landed["x"], landed["y"]) == pytest.approx((120, 70), abs=1.0)
    finally:
        browser_tools.close_session("frame-properties")


def test_a_frame_the_browser_would_not_hit_refuses_the_click_instead_of_missing(
    local_site,
):
    """A frame clipped by an ancestor, or covered by something painted over it,
    is inside the window at every point of it, so only a hit test can tell that
    the event would go somewhere else."""
    session = _open(local_site, PROPERTY_FIXTURE, "frame-blocked")
    try:
        clipped = _pad_origin(session, "#clipped")
        covered = _pad_origin(session, "#covered")
        _scroll_page(session, 2900)

        with pytest.raises(ValueError) as clip_failure:
            browser_tools.pointer_action(
                "click",
                clipped["x"] + 120,
                clipped["y"] + 70,
                "frame-blocked",
                frame_selector="#clipped",
                wait_seconds=0,
            )
        assert "instead of the frame" in str(clip_failure.value)
        assert _in_frame(session, "#clipped", "return window.__input.pointerDowns;") == 0

        with pytest.raises(ValueError) as cover_failure:
            browser_tools.pointer_action(
                "click",
                covered["x"] + 120,
                covered["y"] + 70,
                "frame-blocked",
                frame_selector="#covered",
                wait_seconds=0,
            )
        # The message names what was in the way rather than claiming the point
        # is off-window, which is what it used to say when it said anything.
        assert "banner" in str(cover_failure.value)
        assert _in_frame(session, "#covered", "return window.__input.pointerDowns;") == 0

        # The parts of both frames the browser really would hit keep working.
        browser_tools.pointer_action(
            "click",
            clipped["x"] + 38,
            clipped["y"] + 20,
            "frame-blocked",
            frame_selector="#clipped",
            wait_seconds=0,
        )
        assert _in_frame(session, "#clipped", "return window.__input.pointerDowns;") == 1
        browser_tools.pointer_action(
            "click",
            covered["x"] + 438,
            covered["y"] + 70,
            "frame-blocked",
            frame_selector="#covered",
            wait_seconds=0,
        )
        assert _in_frame(session, "#covered", "return window.__input.pointerDowns;") == 1
    finally:
        browser_tools.close_session("frame-blocked")


def test_a_key_stream_that_dies_mid_delivery_leaves_the_key_on_the_books(
    local_site, monkeypatch
):
    """A partial delivery has to over-report, never under-report: a key the page
    holds and the session has forgotten can never be lifted by anything."""
    session = _open(local_site, POINTER_FIXTURE, "batch-partial")
    try:
        real = browser_tools._perform_key_events

        def half_delivered(driver, events):
            # The bridge sends one Input.dispatchKeyEvent per event and
            # ActionChains.perform can fail part way through too, so a stream
            # can land in the page up to the point where it died.
            real(driver, events[:1])
            raise WebDriverException("the key stream died mid-delivery")

        monkeypatch.setattr(browser_tools, "_perform_key_events", half_delivered)
        with pytest.raises(WebDriverException):
            browser_tools.input_batch(
                key_actions=[
                    {"key": "SHIFT", "action": "hold"},
                    {"key": "W", "action": "hold"},
                ],
                session_id="batch-partial",
                wait_seconds=0,
            )
        monkeypatch.undo()

        assert [item["type"] for item in _key_events(session, "ShiftLeft")] == ["keydown"]
        assert "SHIFT" in session.held_keys
        assert browser_tools._session_modifiers(session) == 8

        browser_tools.release_inputs("batch-partial")
        assert session.held_keys == {}
        assert [item["type"] for item in _key_events(session, "ShiftLeft")] == [
            "keydown",
            "keyup",
        ]
    finally:
        browser_tools.close_session("batch-partial")


def test_tapping_a_key_this_session_already_holds_is_refused(local_site):
    session = _open(local_site, POINTER_FIXTURE, "tap-held")
    try:
        browser_tools.press_keys(["SHIFT"], "tap-held", action="hold", wait_seconds=0)
        with pytest.raises(ValueError) as failure:
            browser_tools.press_keys(["SHIFT"], "tap-held", action="tap", wait_seconds=0)
        assert "already held" in str(failure.value)

        # The hold is untouched: still down in the page, still on the books, and
        # still colouring every mouse event this session sends.
        assert [item["type"] for item in _key_events(session, "ShiftLeft")] == ["keydown"]
        assert sorted(session.held_keys) == ["SHIFT"]
        assert browser_tools._session_modifiers(session) == 8

        released = browser_tools.press_keys(
            ["SHIFT"], "tap-held", action="release", wait_seconds=0
        )
        assert released["held_keys"] == []
        assert [item["type"] for item in _key_events(session, "ShiftLeft")] == [
            "keydown",
            "keyup",
        ]
    finally:
        browser_tools.close_session("tap-held")


def test_a_batched_tap_of_a_key_that_is_already_held_is_refused(local_site):
    session = _open(local_site, POINTER_FIXTURE, "batch-tap-held")
    try:
        browser_tools.press_keys(["CTRL"], "batch-tap-held", action="hold", wait_seconds=0)
        with pytest.raises(ValueError) as failure:
            browser_tools.input_batch(
                # Spelled differently on purpose: the same physical key.
                key_actions=[{"key": "CONTROL", "action": "tap"}],
                session_id="batch-tap-held",
                wait_seconds=0,
            )
        assert "already held" in str(failure.value)

        # Nothing was sent, so the key the caller was holding is still down
        # rather than lifted by a tap that never pressed it.
        assert [item["type"] for item in _key_events(session, "ControlLeft")] == ["keydown"]
        assert sorted(session.held_keys) == ["CTRL"]

        # A batch that releases the hold first may tap it in the same call.
        browser_tools.input_batch(
            key_actions=[
                {"key": "CTRL", "action": "release"},
                {"key": "CONTROL", "action": "tap"},
            ],
            session_id="batch-tap-held",
            wait_seconds=0,
        )
        assert [item["type"] for item in _key_events(session, "ControlLeft")] == [
            "keydown",
            "keyup",
            "keydown",
            "keyup",
        ]
        assert session.held_keys == {}
    finally:
        browser_tools.close_session("batch-tap-held")


def _input_calls(session_id: str, frame_selector: str) -> dict[str, object]:
    """One call of every input path, all naming the same frame the same way."""
    return {
        "pointer_action": lambda: browser_tools.pointer_action(
            "click", 40, 20, session_id, frame_selector=frame_selector, wait_seconds=0
        ),
        "touch_action": lambda: browser_tools.touch_action(
            "tap", [{"x": 40, "y": 20}], session_id,
            frame_selector=frame_selector, wait_seconds=0,
        ),
        "press_keys": lambda: browser_tools.press_keys(
            ["W"], session_id, frame_selector=frame_selector,
            action="hold", focus_mode="none", wait_seconds=0,
        ),
        "input_batch": lambda: browser_tools.input_batch(
            key_actions=[{"key": "W", "action": "hold"}],
            pointer_actions=[{"action": "hover", "x": 40, "y": 20}],
            session_id=session_id,
            frame_selector=frame_selector,
            wait_seconds=0,
        ),
        # Not input itself, but its canvas rects are aimed at with the same
        # string, so it has to name the same frame or the aim is somewhere else.
        "game_probe": lambda: browser_tools.game_probe(
            session_id, frame_selector=frame_selector, sample_seconds=0.1,
            include_console=False,
        ),
    }


def test_an_ambiguous_frame_selector_is_refused_by_every_input_path(local_site):
    """One call must not read one string two ways: the keys of a batch went
    through the strict resolver while its pointer entries took the first CSS
    match, so a selector naming four frames could type into one and click in
    another."""
    session = _open(local_site, FRAME_FIXTURE, "frame-ambiguous")
    try:
        for name, call in _input_calls("frame-ambiguous", "iframe").items():
            with pytest.raises(ValueError) as failure:
                call()
            assert "matches 4 elements" in str(failure.value), name
        # Refused before anything was sent, in every one of them.
        assert session.held_keys == {}
        for frame_selector in ("#plain", "#scaled", "#rotated", "#nested"):
            assert _in_frame(
                session,
                frame_selector,
                "const s = window.__input;"
                "return s.pointerDowns + s.keyDowns + s.touchStarts + s.moves;",
            ) == 0
    finally:
        browser_tools.close_session("frame-ambiguous")


@pytest.mark.parametrize("frame_selector", ["ref:0123abcd:1", "#page >>> #plain"])
def test_a_frame_locator_input_cannot_aim_through_is_refused_up_front(
    local_site, frame_selector
):
    """A ref handle and a piercing path name a document, not a box in the page,
    and input is aimed by coordinate. Half the paths used to accept them and the
    other half refuse, inside one call."""
    session = _open(local_site, FRAME_FIXTURE, "frame-locator")
    try:
        for name, call in _input_calls("frame-locator", frame_selector).items():
            with pytest.raises(ValueError) as failure:
                call()
            assert "Pass a CSS selector" in str(failure.value), name
        assert session.held_keys == {}
        assert _in_frame(session, "#plain", "return window.__input.keyDowns;") == 0
    finally:
        browser_tools.close_session("frame-locator")


def _pad_point(session, dx: float, dy: float) -> dict[str, float]:
    return session.driver.execute_script(
        "const r = document.getElementById('pad').getBoundingClientRect();"
        "return {x: r.x + arguments[0], y: r.y + arguments[1]};",
        dx,
        dy,
    )


def test_a_swipe_that_ends_outside_the_viewport_plants_no_finger(local_site):
    session = _open(local_site, POINTER_FIXTURE, "swipe-bounds")
    try:
        browser_tools.set_touch_emulation("swipe-bounds", enabled=True)
        viewport = session.driver.execute_script(
            "return {width: window.innerWidth, height: window.innerHeight};"
        )
        start = _pad_point(session, 40, 40)
        with pytest.raises(ValueError) as failure:
            browser_tools.touch_action(
                "swipe",
                [
                    {
                        "x": start["x"],
                        "y": start["y"],
                        "end_x": viewport["width"] + 200,
                        "end_y": start["y"],
                    }
                ],
                "swipe-bounds",
                wait_seconds=0,
            )
        assert "outside the selected" in str(failure.value)

        # The end point is refused before the finger goes down, so there is no
        # finger left planted for every later gesture to fight with.
        assert session.held_touches == {}
        assert _input(session, "s.touchStarts") == 0
        assert _input(session, "s.activeTouches") == 0

        browser_tools.touch_action(
            "tap", [{"x": start["x"], "y": start["y"]}], "swipe-bounds", wait_seconds=0
        )
        assert _input(session, "s.taps") == 1
        assert _input(session, "s.activeTouches") == 0
    finally:
        browser_tools.close_session("swipe-bounds")


def test_pressing_a_touch_id_that_is_already_down_is_refused(local_site):
    session = _open(local_site, POINTER_FIXTURE, "touch-double-press")
    try:
        browser_tools.set_touch_emulation("touch-double-press", enabled=True)
        first = _pad_point(session, 40, 40)
        second = _pad_point(session, 200, 120)
        browser_tools.touch_action(
            "press", [{"x": first["x"], "y": first["y"], "id": 0}],
            "touch-double-press", wait_seconds=0,
        )
        with pytest.raises(ValueError) as failure:
            browser_tools.touch_action(
                "press", [{"x": second["x"], "y": second["y"], "id": 0}],
                "touch-double-press", wait_seconds=0,
            )
        assert "already down" in str(failure.value)

        # Chrome drops a touchStart for an id that is already down, so a session
        # that recorded the new position would aim every later id-only move or
        # release at a place the page never had the finger.
        assert _input(session, "s.touchStarts") == 1
        assert session.held_touches[0]["x"] == pytest.approx(first["x"], abs=0.5)
        assert session.held_touches[0]["y"] == pytest.approx(first["y"], abs=0.5)

        browser_tools.touch_action("release", [{"id": 0}], "touch-double-press", wait_seconds=0)
        assert _input(session, "s.activeTouches") == 0
        assert session.held_touches == {}
    finally:
        browser_tools.close_session("touch-double-press")


_CLOCK_PROBE_SCRIPT = """
window.__clock = {samples: 0, minDelta: Infinity, minFrameDelta: Infinity,
                  backwards: 0, last: performance.now(), lastFrame: null,
                  lastDate: Date.now()};
requestAnimationFrame(function tick(stamp) {
  const clock = window.__clock;
  const now = performance.now();
  clock.minDelta = Math.min(clock.minDelta, now - clock.last);
  clock.last = now;
  const date = Date.now();
  if (date < clock.lastDate) clock.backwards += 1;
  clock.lastDate = date;
  if (clock.lastFrame !== null) {
    clock.minFrameDelta = Math.min(clock.minFrameDelta, stamp - clock.lastFrame);
  }
  clock.lastFrame = stamp;
  clock.samples += 1;
  requestAnimationFrame(tick);
});
"""

_CLOCK_READ_SCRIPT = """
const clock = window.__clock;
return {samples: clock.samples, backwards: clock.backwards,
        min_delta: clock.minDelta === Infinity ? null : clock.minDelta,
        min_frame_delta: clock.minFrameDelta === Infinity ? null : clock.minFrameDelta};
"""


def _clock_report(session, at_least: int | None = None) -> dict[str, float]:
    """Read the page's own clock record, waiting for real frames if asked."""
    deadline = time.monotonic() + 5.0
    while True:
        report = session.driver.execute_script(_CLOCK_READ_SCRIPT)
        if at_least is None or report["samples"] >= at_least or time.monotonic() > deadline:
            return report
        time.sleep(0.02)


def test_the_page_clock_never_runs_backwards_across_a_mode_change(local_site):
    """Stepping faster than the wall clock puts the page's clock ahead of the
    native one. Swapping the native clock back in drops it, and a game that
    measures a delta across that moment integrates backwards.

    Four rounds, because a mode change lands at a different point in the
    browser's own frame cycle every time and only some of those points expose
    the smaller half of this: a frame is dated from when it began, so the first
    native frame after the change can carry a stamp from before the change.
    """
    session = _open(local_site, TIMER_FIXTURE, "clock-monotonic")
    try:
        session.driver.execute_script(_CLOCK_PROBE_SCRIPT)
        highest = 0.0
        for round_index in range(4):
            browser_tools.set_render_control(
                "step", "clock-monotonic", frame_delta_ms=100
            )
            entered = session.driver.execute_script("return performance.now();")
            assert entered >= highest, f"entering step mode dropped the clock (round {round_index})"

            browser_tools.render_step(
                60 if not round_index else 10, "clock-monotonic", include_summary=False
            )
            stepped = session.driver.execute_script("return performance.now();")
            assert stepped >= entered

            browser_tools.set_render_control("normal", "clock-monotonic")
            restored = session.driver.execute_script("return performance.now();")
            assert restored >= stepped, "the page clock fell back to the native one"

            # And the page's own frames, which is where the jump is felt: a
            # negative delta snaps every tween and integrates physics backwards,
            # whether it is six seconds or six milliseconds.
            samples = _clock_report(session)["samples"]
            report = _clock_report(session, at_least=samples + 3)
            assert report["samples"] >= samples + 3, "the page stopped drawing frames"
            assert report["min_delta"] >= 0, f"performance.now() went backwards (round {round_index})"
            assert report["min_frame_delta"] >= 0, f"a frame stamp went backwards (round {round_index})"
            assert report["backwards"] == 0, "Date.now() went backwards"
            highest = restored
    finally:
        browser_tools.set_render_control("normal", "clock-monotonic")
        browser_tools.close_session("clock-monotonic")


def test_a_frame_stamped_before_the_mode_change_cannot_undercut_the_last_one(
    local_site,
):
    """The half of the clock jump that depends on timing, made to happen.

    Whether the browser hands back a frame it had already dated before the mode
    change is a race the test cannot win reliably, so the gate is asked to stamp
    such a frame directly - which is exactly what its rAF wrapper does with the
    timestamp the browser gives it.
    """
    session = _open(local_site, TIMER_FIXTURE, "clock-late-frame")
    try:
        browser_tools.set_render_control("step", "clock-late-frame", frame_delta_ms=100)
        browser_tools.render_step(20, "clock-late-frame", include_summary=False)
        browser_tools.set_render_control("normal", "clock-late-frame")
        observed = session.driver.execute_script(
            """
            const state = window.__webSearchNeoRenderControl;
            const last = state.lastStamp;
            // A frame that began 25ms before the browser was asked for one.
            const late = state.stamp(last - 25);
            return {last: last, late: late, now: performance.now(),
                    next: state.stamp(last + 10)};
            """
        )
        assert observed["late"] >= observed["last"]
        # The page's clock moves with the stamp rather than trailing it, so a
        # game measuring "how long since this frame began" gets no negative.
        assert observed["now"] >= observed["late"]
        assert observed["next"] >= observed["late"]
    finally:
        browser_tools.set_render_control("normal", "clock-late-frame")
        browser_tools.close_session("clock-late-frame")


class _Nowhere:
    """A driver that accepts a frame reset and nothing else."""

    class _SwitchTo:
        def default_content(self) -> None:
            return None

    def __init__(self) -> None:
        self.switch_to = self._SwitchTo()

    def quit(self) -> None:
        """The autouse session cleanup tears this down like any other."""
        return None


def _staged_session(session_id: str) -> browser_tools.BrowserSession:
    session = browser_tools.BrowserSession(driver=_Nowhere(), headless=True)
    browser_tools._sessions[session_id] = session
    return session


def test_a_hold_whose_dispatch_dies_still_records_the_keys(monkeypatch):
    """`input_batch` learned this; `press_keys` had the same hole.

    Any event in a stream may have landed, so a hold that raised has to assume
    the keys are down: `release_inputs` can lift a key the session knows about
    and cannot lift one it never heard of.
    """
    session = _staged_session("keys-die")
    monkeypatch.setattr(
        browser_tools,
        "_perform_key_events",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("transport died")),
    )

    with pytest.raises(RuntimeError, match="transport died"):
        browser_tools.press_keys(
            ["SHIFT", "W"], session_id="keys-die", action="hold", focus_mode="none"
        )

    assert sorted(session.held_keys) == ["SHIFT", "W"]


def test_a_release_that_never_went_does_not_forget_the_keys(monkeypatch):
    """The mirror image, and the one direction nothing recovers from."""
    session = _staged_session("keys-stay")
    session.held_keys.update({"SHIFT": "Shift", "W": "w"})
    monkeypatch.setattr(
        browser_tools,
        "_perform_key_events",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("transport died")),
    )

    with pytest.raises(RuntimeError, match="transport died"):
        browser_tools.press_keys(
            ["SHIFT", "W"], session_id="keys-stay", action="release", focus_mode="none"
        )

    assert sorted(session.held_keys) == ["SHIFT", "W"]


def test_an_input_action_told_to_narrow_a_frame_is_not_sent_to_the_outline():
    """The advice has to fit the path: an input action cannot aim through the
    outline's verified-unique `frame` path, so telling it to use one loops."""
    reader = browser_tools._ambiguous_frame_message("iframe", 4, css_only=False)
    aimer = browser_tools._ambiguous_frame_message("iframe", 4, css_only=True)

    assert "matches 4 elements" in reader and "matches 4 elements" in aimer
    assert "page_outline" in reader
    assert "cannot aim through it" in aimer
