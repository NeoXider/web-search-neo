"""Wheel, buttons, drag, modifiers, touch, pointer lock, and named keys.

Every browser test drives the real ``tests/fixtures/games/pointer.html`` harness,
which mirrors each DOM event it receives into ``window.__input``. Nothing here is
asserted against a screenshot or a pixel: the page reports what it actually got.
"""

from __future__ import annotations

import math
import time

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools
from web_search_neo import key_table


POINTER_FIXTURE = "/fixtures/games/pointer.html"


def _open_or_skip(url: str, session_id: str, **kwargs):
    # Keep the deterministic suite in the background while production defaults visible.
    kwargs.setdefault("headless", True)
    kwargs.setdefault("profile_mode", "temporary")
    try:
        return browser_tools.open_page(url, session_id=session_id, **kwargs)
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def _open_pointer_fixture(local_site, session_id: str):
    _open_or_skip(f"{local_site.base_url}{POINTER_FIXTURE}", session_id)
    return browser_tools._get_session(session_id)


def _pad_rect(session) -> dict[str, float]:
    return session.driver.execute_script(
        "const r = document.getElementById('pad').getBoundingClientRect();"
        "return {x: r.x, y: r.y, width: r.width, height: r.height};"
    )


def _input(session, projection: str):
    """Read a projection of ``window.__input`` (``s`` is the state object)."""
    return session.driver.execute_script(f"const s = window.__input; return {projection};")


def _wait_for_input(session, projection: str, timeout: float = 3.0):
    """Poll ``window.__input`` until a projection turns truthy.

    ``pointerlockchange`` is delivered in a task of its own, so the page state
    can lag one turn behind ``document.pointerLockElement``.
    """
    deadline = time.monotonic() + timeout
    while True:
        value = _input(session, projection)
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.02)


def test_scroll_with_selector_targets_the_container_holding_the_element(local_site):
    _open_or_skip(
        f"{local_site.base_url}/fixtures/games/scroll_container.html", "scroll-selector"
    )
    session = browser_tools._get_session("scroll-selector")
    try:
        before = session.driver.execute_script("return window.__panelScrollTop();")
        result = browser_tools.scroll_page(
            400, session_id="scroll-selector", selector="#marker", wait_seconds=0
        )
        assert result["success"] is True
        assert result["selector"] == "#marker"
        assert result["before"]["scroll_y"] == 0
        # A wheel event is applied by the compositor, not by the call that sent
        # it, so a single read right after dispatch races the scroll on a slow
        # or headless machine. Polling waits for the same outcome instead of
        # asserting a weaker one: a container that never moves still fails.
        after = before
        deadline = time.monotonic() + 5.0
        while after <= before and time.monotonic() < deadline:
            time.sleep(0.02)
            after = session.driver.execute_script("return window.__panelScrollTop();")
        assert after > before
    finally:
        browser_tools.close_session("scroll-selector")


def test_scroll_selector_and_frame_selector_are_mutually_exclusive(local_site):
    _open_or_skip(
        f"{local_site.base_url}/fixtures/games/scroll_container.html", "scroll-selector-refusal"
    )
    try:
        with pytest.raises(ValueError, match="mutually exclusive"):
            browser_tools.scroll_page(100, session_id="scroll-selector-refusal", selector="#marker", frame_selector="#panel")
    finally:
        browser_tools.close_session("scroll-selector-refusal")


def test_wheel_carries_both_axes_and_drives_the_zoom_model(local_site):
    session = _open_pointer_fixture(local_site, "wheel-input")
    try:
        rect = _pad_rect(session)
        center_x = rect["x"] + rect["width"] / 2
        center_y = rect["y"] + rect["height"] / 2

        result = browser_tools.pointer_action(
            "wheel",
            center_x,
            center_y,
            "wheel-input",
            delta_x=15,
            delta_y=-240,
            wait_seconds=0,
        )
        assert result["success"] is True
        assert result["action"] == "wheel"
        assert result["delta_x"] == 15
        assert result["delta_y"] == -240

        scrolled = _input(
            session,
            "{events: s.wheelEvents, dx: s.wheelDeltaX, dy: s.wheelDeltaY,"
            " zoom: s.zoom, mode: s.wheelMode}",
        )
        assert scrolled["events"] == 1
        assert scrolled["dx"] == pytest.approx(15, abs=1)
        assert scrolled["dy"] == pytest.approx(-240, abs=1)
        assert scrolled["mode"] == 0  # DOM_DELTA_PIXEL
        assert scrolled["zoom"] > 1.0

        # Scrolling the other way has to move the derived value back down.
        browser_tools.pointer_action(
            "wheel",
            center_x,
            center_y,
            "wheel-input",
            delta_x=-15,
            delta_y=240,
            wait_seconds=0,
        )
        undone = _input(
            session, "{events: s.wheelEvents, dx: s.wheelDeltaX, dy: s.wheelDeltaY, zoom: s.zoom}"
        )
        assert undone["events"] == 2
        assert undone["dx"] == pytest.approx(0, abs=1)
        assert undone["dy"] == pytest.approx(0, abs=1)
        assert undone["zoom"] < scrolled["zoom"]
    finally:
        browser_tools.close_session("wheel-input")


def test_secondary_buttons_and_double_click_are_counted_separately(local_site):
    session = _open_pointer_fixture(local_site, "button-input")
    try:
        rect = _pad_rect(session)
        x = rect["x"] + 120
        y = rect["y"] + 120

        right = browser_tools.pointer_action(
            "click", x, y, "button-input", button="right", wait_seconds=0
        )
        assert right["button"] == "right"
        browser_tools.pointer_action(
            "click", x + 10, y, "button-input", button="middle", wait_seconds=0
        )
        double = browser_tools.pointer_action(
            "double_click", x + 20, y, "button-input", wait_seconds=0
        )
        assert double["action"] == "double_click"

        state = _input(
            session,
            "{left: s.leftClicks, middle: s.middleClicks, right: s.rightClicks,"
            " dbl: s.dblClicks, aux: s.auxClicks, context: s.contextMenus,"
            " clickCount: s.clickCount, total: s.totalClicks}",
        )
        assert state["right"] == 1
        assert state["middle"] == 1
        assert state["context"] == 1  # the right button opens a context menu
        assert state["aux"] == 2  # right + middle surface as auxclick, never as click
        assert state["dbl"] == 1
        assert state["left"] == 2  # a double click is two primary presses
        assert state["clickCount"] == 2  # detail reached 2, so it was a real dblclick
        assert state["total"] == 4
    finally:
        browser_tools.close_session("button-input")


def test_drag_moves_through_intermediate_points(local_site):
    session = _open_pointer_fixture(local_site, "drag-input")
    try:
        rect = _pad_rect(session)
        start_x = rect["x"] + 40
        start_y = rect["y"] + 40
        end_x = start_x + 200
        end_y = start_y + 100

        dragged = browser_tools.pointer_action(
            "drag",
            start_x,
            start_y,
            "drag-input",
            end_x=end_x,
            end_y=end_y,
            duration_seconds=0.2,
            wait_seconds=0,
        )
        assert dragged["success"] is True
        assert dragged["end_x"] == pytest.approx(end_x)
        assert dragged["end_y"] == pytest.approx(end_y)

        drag = _input(
            session,
            "{drags: s.drags, steps: s.lastDragSteps, distance: s.lastDragDistance,"
            " path: s.lastDragPath, startX: s.lastDragStartX, startY: s.lastDragStartY,"
            " endX: s.lastDragEndX, endY: s.lastDragEndY, type: s.lastDragPointerType}",
        )
        assert drag["drags"] == 1
        assert drag["type"] == "mouse"
        # A teleporting drag would report exactly two path samples (down + up).
        assert len(drag["path"]) > 2
        assert drag["steps"] >= 2
        assert (drag["startX"], drag["startY"]) == pytest.approx((40, 40))
        assert (drag["endX"], drag["endY"]) == pytest.approx((240, 140))
        # Straight line, so the travelled distance is the length of the segment.
        assert drag["distance"] == pytest.approx(math.hypot(200, 100), abs=1)
        xs = [point["x"] for point in drag["path"]]
        assert xs == sorted(xs)
        assert any(40 < value < 240 for value in xs)
    finally:
        browser_tools.close_session("drag-input")


def test_held_modifiers_reach_the_mouse_event(local_site):
    session = _open_pointer_fixture(local_site, "modifier-input")
    try:
        rect = _pad_rect(session)
        x = rect["x"] + 200
        y = rect["y"] + 140
        session.driver.execute_script(
            "window.__modifiers = [];"
            "document.getElementById('pad').addEventListener('mousedown', event =>"
            "  window.__modifiers.push({shift: event.shiftKey, ctrl: event.ctrlKey,"
            "    alt: event.altKey, meta: event.metaKey}));"
        )

        held = browser_tools.press_keys(
            ["SHIFT"], "modifier-input", action="hold", wait_seconds=0
        )
        assert held["held_keys"] == ["SHIFT"]
        shift_click = browser_tools.pointer_action(
            "click", x, y, "modifier-input", wait_seconds=0
        )
        assert shift_click["modifiers"] == key_table.MODIFIER_BITS["Shift"]
        browser_tools.press_keys(["SHIFT"], "modifier-input", action="release", wait_seconds=0)

        browser_tools.press_keys(["CONTROL"], "modifier-input", action="hold", wait_seconds=0)
        ctrl_click = browser_tools.pointer_action(
            "click", x, y, "modifier-input", wait_seconds=0
        )
        assert ctrl_click["modifiers"] == key_table.MODIFIER_BITS["Control"]
        browser_tools.press_keys(["CONTROL"], "modifier-input", action="release", wait_seconds=0)

        observed = session.driver.execute_script("return window.__modifiers;")
        assert len(observed) == 2
        assert observed[0] == {"shift": True, "ctrl": False, "alt": False, "meta": False}
        assert observed[1] == {"shift": False, "ctrl": True, "alt": False, "meta": False}
        assert browser_tools._get_session("modifier-input").held_keys == {}
    finally:
        browser_tools.close_session("modifier-input")


def test_touch_emulation_enables_tap_swipe_and_multitouch(local_site):
    session = _open_pointer_fixture(local_site, "touch-input")
    try:
        # Feature detection is decided while the document loads, so the tool
        # reloads the page; everything below runs against the fresh document.
        emulated = browser_tools.set_touch_emulation(
            "touch-input", enabled=True, max_touch_points=5
        )
        assert emulated["success"] is True
        assert emulated["touch_enabled"] is True
        assert emulated["reloaded"] is True
        assert emulated["ontouchstart"] is True
        assert emulated["max_touch_points"] == 5
        assert session.touch_enabled is True
        assert _input(session, "s.maxDeviceTouchPoints") == 5

        rect = _pad_rect(session)
        x = rect["x"] + 100
        y = rect["y"] + 100

        tapped = browser_tools.touch_action("tap", [{"x": x, "y": y}], "touch-input", wait_seconds=0)
        assert tapped["success"] is True
        assert tapped["points"] == 1
        after_tap = _input(
            session, "{starts: s.touchStarts, ends: s.touchEnds, taps: s.taps, moves: s.touchMoves}"
        )
        assert after_tap == {"starts": 1, "ends": 1, "taps": 1, "moves": 0}

        browser_tools.touch_action(
            "swipe",
            [{"x": x, "y": y, "end_x": x + 200, "end_y": y + 60}],
            "touch-input",
            steps=8,
            duration_seconds=0.05,
            wait_seconds=0,
        )
        after_swipe = _input(
            session, "{starts: s.touchStarts, moves: s.touchMoves, ends: s.touchEnds, taps: s.taps}"
        )
        assert after_swipe["starts"] == 2
        assert after_swipe["moves"] >= 4  # a swipe is a stream, not a jump
        assert after_swipe["ends"] == 2
        assert after_swipe["taps"] == 1  # a moved finger is not a tap

        browser_tools.touch_action(
            "press",
            [{"x": x, "y": y, "id": 0}, {"x": x + 80, "y": y + 40, "id": 1}],
            "touch-input",
            wait_seconds=0,
        )
        while_pressed = _input(session, "{active: s.activeTouches, max: s.maxTouchPoints}")
        browser_tools.touch_action("release", None, "touch-input", wait_seconds=0)
        assert while_pressed["active"] == 2
        assert while_pressed["max"] >= 2
        assert _input(session, "s.activeTouches") == 0

        disabled = browser_tools.set_touch_emulation("touch-input", enabled=False)
        assert disabled["touch_enabled"] is False
        assert session.touch_enabled is False
    finally:
        browser_tools.close_session("touch-input")


def test_pointer_lock_accumulates_unclamped_relative_movement(local_site):
    session = _open_pointer_fixture(local_site, "lock-input")
    try:
        acquired = browser_tools.pointer_lock("acquire", "lock-input", selector="#pad")
        if not acquired["locked"]:
            pytest.skip(f"Pointer lock is unavailable in this browser: {acquired}")
        assert acquired["success"] is True
        assert acquired["element"] == "pad"
        assert session.pointer_locked is True
        assert _wait_for_input(session, "s.locked") is True
        assert _input(session, "{changes: s.lockChanges, error: s.lockError}") == {
            "changes": 1,
            "error": None,
        }

        # Anchor the pointer at a known absolute spot first, so the deltas below
        # are measured from a position both sides agree on.
        rect = _pad_rect(session)
        browser_tools.pointer_action(
            "move", rect["x"] + 200, rect["y"] + 140, "lock-input", wait_seconds=0
        )
        baseline = _input(session, "{x: s.lockDelta.x, y: s.lockDelta.y, moves: s.lockMoves}")

        # Two of these overshoot the 1440x900 viewport on purpose: a locked
        # pointer has no position, so a big turn must not be clamped away.
        deltas = [(120, -40), (-900, 320), (1500, -260), (-40, 12)]
        for delta_x, delta_y in deltas:
            moved = browser_tools.pointer_action(
                "move",
                delta_x,
                delta_y,
                "lock-input",
                coordinate_mode="relative",
                wait_seconds=0,
            )
            assert moved["coordinate_mode"] == "relative"
            assert moved["delta_x"] == delta_x
            assert moved["delta_y"] == delta_y

        accumulated = _input(session, "{x: s.lockDelta.x, y: s.lockDelta.y, moves: s.lockMoves}")
        assert accumulated["moves"] == baseline["moves"] + len(deltas)
        assert accumulated["x"] - baseline["x"] == sum(item[0] for item in deltas)
        assert accumulated["y"] - baseline["y"] == sum(item[1] for item in deltas)

        released = browser_tools.pointer_lock("release", "lock-input")
        assert released["success"] is True
        assert released["locked"] is False
        assert session.pointer_locked is False
        assert _wait_for_input(session, "s.locked === false") is True
        assert browser_tools.pointer_lock("status", "lock-input")["locked"] is False
    finally:
        browser_tools.close_session("lock-input")


def test_frames_advanced_counts_only_the_frames_the_gate_really_released(local_site):
    _open_pointer_fixture(local_site, "frame-count")
    try:
        # Nothing is gated in normal mode, so the page draws on its own schedule
        # and the call released no frame at all.
        loose = browser_tools.press_keys(
            ["W"], "frame-count", action="tap", wait_seconds=0
        )
        assert loose["render_mode"] == "normal"
        assert loose["frames_advanced"] == 0

        batched = browser_tools.input_batch(
            key_actions=[{"key": "W", "action": "tap"}],
            session_id="frame-count",
            wait_seconds=0,
        )
        assert batched["frame_advanced"] is False
        assert batched["frames_advanced"] == 0

        browser_tools.set_render_control("throttled", "frame-count", target_fps=20)
        throttled = browser_tools.press_keys(
            ["W"], "frame-count", action="tap", wait_seconds=0
        )
        assert throttled["render_mode"] == "throttled"
        assert throttled["frames_advanced"] == 0

        browser_tools.set_render_control("step", "frame-count")
        stepped = browser_tools.press_keys(
            ["W"], "frame-count", action="tap", hold_frames=2, wait_seconds=0
        )
        assert stepped["frames_advanced"] == 2
        held = browser_tools.press_keys(
            ["W"], "frame-count", action="hold", wait_seconds=0
        )
        assert held["frames_advanced"] == 1
        browser_tools.press_keys(["W"], "frame-count", action="release", wait_seconds=0)
    finally:
        browser_tools.set_render_control("normal", "frame-count")
        browser_tools.close_session("frame-count")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("F5", {"key": "F5", "code": "F5", "keyCode": 116, "location": 0}),
        ("NUMPAD1", {"key": "1", "code": "Numpad1", "keyCode": 97, "location": 3}),
        ("META", {"key": "Meta", "code": "MetaLeft", "keyCode": 91, "location": 1}),
    ],
)
def test_named_keys_arrive_with_the_physical_code(local_site, name, expected):
    session = _open_pointer_fixture(local_site, "named-keys")
    try:
        browser_tools.press_keys([name], "named-keys", hold_seconds=0.01, wait_seconds=0)
        events = _input(
            session,
            "s.keys.filter(k => k.type === 'keydown' || k.type === 'keyup')"
            " .map(k => ({type: k.type, key: k.key, code: k.code,"
            "             keyCode: k.keyCode, location: k.location, repeat: k.repeat}))",
        )
        assert [item["type"] for item in events] == ["keydown", "keyup"]
        for item in events:
            assert item["repeat"] is False
            assert {key: item[key] for key in expected} == expected
    finally:
        browser_tools.close_session("named-keys")


def test_release_lifts_the_key_that_was_held_whatever_its_case(local_site):
    session = _open_pointer_fixture(local_site, "case-keys")
    try:
        browser_tools.press_keys(["w"], "case-keys", action="hold", wait_seconds=0)
        assert browser_tools._get_session("case-keys").held_keys == {"W": "w"}
        assert _input(
            session, "s.keys.filter(k => k.type === 'keyup' && k.code === 'KeyW').length"
        ) == 0

        released = browser_tools.press_keys(
            ["W"], "case-keys", action="release", wait_seconds=0
        )
        assert released["action"] == "release"
        assert released["held_keys"] == []
        assert browser_tools._get_session("case-keys").held_keys == {}
        lifted = _input(
            session,
            "s.keys.filter(k => k.type === 'keyup' && k.code === 'KeyW')"
            " .map(k => ({key: k.key, code: k.code}))",
        )
        assert lifted == [{"key": "w", "code": "KeyW"}]
    finally:
        browser_tools.close_session("case-keys")


@pytest.mark.parametrize(
    ("raw", "shifted", "expected"),
    [
        ("w", False, ("w", "KeyW", 87, 0)),
        ("w", True, ("W", "KeyW", 87, 0)),
        ("W", False, ("w", "KeyW", 87, 0)),
        ("W", True, ("W", "KeyW", 87, 0)),
        ("5", False, ("5", "Digit5", 53, 0)),
        ("%", False, ("%", "Digit5", 53, 0)),
        (",", False, (",", "Comma", 188, 0)),
        ("<", False, ("<", "Comma", 188, 0)),
        (".", False, (".", "Period", 190, 0)),
        ("/", False, ("/", "Slash", 191, 0)),
        ("F1", False, ("F1", "F1", 112, 0)),
        ("F5", False, ("F5", "F5", 116, 0)),
        ("F12", False, ("F12", "F12", 123, 0)),
        ("NUMPAD0", False, ("0", "Numpad0", 96, 3)),
        ("NUMPAD1", False, ("1", "Numpad1", 97, 3)),
        ("NUMPAD9", False, ("9", "Numpad9", 105, 3)),
        ("DECIMAL", False, (".", "NumpadDecimal", 110, 3)),
        ("SHIFT", False, ("Shift", "ShiftLeft", 16, 1)),
        ("CONTROL", False, ("Control", "ControlLeft", 17, 1)),
        ("ALT", False, ("Alt", "AltLeft", 18, 1)),
        ("META", False, ("Meta", "MetaLeft", 91, 1)),
        ("SPACE", False, (" ", "Space", 32, 0)),
        ("LEFT", False, ("ArrowLeft", "ArrowLeft", 37, 0)),
        ("ENTER", False, ("Enter", "Enter", 13, 0)),
    ],
)
def test_resolve_key_matches_the_us_layout(raw, shifted, expected):
    assert key_table.resolve_key(raw, shifted=shifted) == expected


def test_resolve_key_accepts_the_selenium_private_use_characters():
    for name in ("F5", "NUMPAD1", "META", "SHIFT"):
        assert key_table.resolve_key(key_table.SELENIUM_KEYS[name]) == key_table.resolve_key(name)


def test_resolve_key_reports_non_latin_letters_without_a_physical_code():
    key, code, key_code, location = key_table.resolve_key("щ")
    assert (key, code, key_code, location) == ("щ", "", 0, 0)
