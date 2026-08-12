"""Playing a real game through the step-mode frame gate.

The platformer fixture runs exactly one physics tick per released frame and never
reads wall-clock time, so a correct gate reproduces the same trajectory on every
machine: the level is finished with the same ticks, jumps, and deaths each run.
"""

from __future__ import annotations

import time

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

import browser_tools


PLATFORMER = "/fixtures/games/platformer.html"
IFRAME_HOST = "/fixtures/games/iframe_host.html"
WEBGL = "/fixtures/games/webgl.html"

FRAME_DELTA_MS = 1000 / 60
MAX_PLAYTHROUGH_STEPS = 300

_GAME_STATE_SCRIPT = """
const g = window.__game;
return {frame: g.frame, tick: g.tick, x: g.x, y: g.y, onGround: g.onGround,
        won: g.won, deaths: g.deaths, keys: g.keysDown};
"""


def _open_or_skip(url: str, session_id: str, **kwargs):
    # Keep the deterministic suite in the background while production defaults visible.
    kwargs.setdefault("headless", True)
    kwargs.setdefault("profile_mode", "temporary")
    try:
        return browser_tools.open_page(url, session_id=session_id, **kwargs)
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def _open_fixture(local_site, path: str, session_id: str):
    _open_or_skip(f"{local_site.base_url}{path}", session_id)
    return browser_tools._get_session(session_id)


def _in_frame(session, frame_selector: str | None, script: str, *args):
    driver = session.driver
    driver.switch_to.default_content()
    if frame_selector:
        driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, frame_selector))
    try:
        return driver.execute_script(script, *args)
    finally:
        driver.switch_to.default_content()


def _read_game(session, frame_selector: str | None = None) -> dict:
    return _in_frame(session, frame_selector, _GAME_STATE_SCRIPT)


def _game_events(session, frame_selector: str | None = None) -> list[dict]:
    return _in_frame(
        session,
        frame_selector,
        "return window.__game.events.filter(e => e.type === 'jump'"
        " || e.type === 'land' || e.type === 'death' || e.type === 'win');",
    )


def test_step_mode_playthrough_finishes_the_level_without_dying(local_site):
    session = _open_fixture(local_site, PLATFORMER, "playthrough")
    try:
        control = browser_tools.set_render_control("step", "playthrough")
        assert control["mode"] == "step"
        assert control["input_advances_frame"] is True
        assert control["time_frozen"] is True

        # Hold right for the whole run; the pit can only be cleared with a
        # running jump, so horizontal speed has to survive across frames.
        held = browser_tools.press_keys(
            ["RIGHT"],
            "playthrough",
            target_selector="#game",
            action="hold",
            wait_seconds=0,
        )
        assert held["held_keys"] == ["RIGHT"]

        state = _read_game(session)
        assert state["keys"] == ["ArrowRight"]
        steps = 0
        while steps < MAX_PLAYTHROUGH_STEPS and not state["won"]:
            if state["onGround"]:
                browser_tools.press_keys(
                    ["SPACE"],
                    "playthrough",
                    action="tap",
                    focus_mode="none",
                    wait_seconds=0,
                )
            else:
                browser_tools.render_step(1, "playthrough")
            state = _read_game(session)
            steps += 1

        assert state["won"] is True, f"gave up after {steps} frames at {state}"
        assert state["deaths"] == 0
        assert steps < MAX_PLAYTHROUGH_STEPS
        assert state["tick"] == state["frame"]  # one released frame, one tick

        events = _game_events(session)
        assert [item["type"] for item in events].count("win") == 1
        assert sum(item["type"] == "jump" for item in events) >= 2
        assert not any(item["type"] == "death" for item in events)

        released = browser_tools.release_inputs("playthrough")
        assert released["held_keys"] == []
        assert browser_tools.set_render_control("normal", "playthrough")["mode"] == "normal"
    finally:
        browser_tools.close_session("playthrough")


def test_step_mode_freezes_the_page_clock_between_released_frames(local_site):
    session = _open_fixture(local_site, PLATFORMER, "virtual-clock")
    driver = session.driver
    try:
        browser_tools.set_render_control("step", "virtual-clock")

        first = driver.execute_script("return performance.now();")
        time.sleep(0.25)
        second = driver.execute_script("return performance.now();")
        assert second == first, "the virtual clock moved without a released frame"

        date_before = driver.execute_script("return Date.now();")
        delta_count = driver.execute_script("return window.__game.realDeltas.length;")
        stepped = browser_tools.render_step(6, "virtual-clock")
        assert stepped["frames"] == 6

        third = driver.execute_script("return performance.now();")
        assert third - second == pytest.approx(6 * FRAME_DELTA_MS, abs=0.01)
        date_after = driver.execute_script("return Date.now();")
        assert date_after - date_before == pytest.approx(6 * FRAME_DELTA_MS, abs=1)

        # The frame timestamps the game itself sees have to be just as constant.
        deltas = driver.execute_script(
            "return window.__game.realDeltas.slice(arguments[0]);", delta_count
        )
        assert len(deltas) == 6
        # deltas[0] straddles the switch into step mode; the rest are pure gate.
        assert all(item == pytest.approx(FRAME_DELTA_MS, abs=0.001) for item in deltas[1:])

        custom = browser_tools.set_render_control(
            "step", "virtual-clock", frame_delta_ms=50.0
        )
        assert custom["frame_delta_ms"] == pytest.approx(50.0)
        before_custom = driver.execute_script("return performance.now();")
        browser_tools.render_step(4, "virtual-clock")
        after_custom = driver.execute_script("return performance.now();")
        assert after_custom - before_custom == pytest.approx(200.0, abs=0.01)
    finally:
        browser_tools.close_session("virtual-clock")


def test_gated_timers_only_fire_when_a_frame_is_released(local_site):
    session = _open_fixture(local_site, PLATFORMER, "gated-timers")
    driver = session.driver
    try:
        browser_tools.set_render_control("step", "gated-timers", gate_timers=True)
        driver.execute_script(
            "window.__ticks = 0;"
            "window.__timer = setInterval(() => { window.__ticks += 1; }, 10);"
            "window.__once = 0;"
            "setTimeout(() => { window.__once += 1; }, 5);"
        )

        time.sleep(0.3)  # 30 interval periods of real time, and nothing may run
        assert driver.execute_script("return [window.__ticks, window.__once];") == [0, 0]

        browser_tools.render_step(1, "gated-timers")
        assert driver.execute_script("return [window.__ticks, window.__once];") == [1, 1]

        browser_tools.render_step(4, "gated-timers")
        # 16.67ms per frame is above the 10ms period, so exactly one run per frame.
        assert driver.execute_script("return window.__ticks;") == 5
        assert driver.execute_script("return window.__once;") == 1

        # Leaving step mode hands the queued interval back to the real scheduler.
        browser_tools.set_render_control("normal", "gated-timers")
        resumed = driver.execute_script("return window.__ticks;")
        time.sleep(0.2)
        assert driver.execute_script("return window.__ticks;") > resumed
    finally:
        browser_tools.close_session("gated-timers")


def test_gate_timers_freezes_an_interval_registered_before_step_mode(local_site):
    """A game registers its timers on load, long before the agent gates frames.

    KNOWN FAILURE - real defect in the render bootstrap. ``state.installTimers``
    only replaces ``window.setInterval``/``setTimeout`` when step mode is entered,
    so anything scheduled earlier keeps running on Chrome's real scheduler and
    the "each released frame is a fixed frame_delta_ms" promise does not hold for
    it.
    """
    session = _open_fixture(local_site, PLATFORMER, "legacy-timers")
    driver = session.driver
    try:
        driver.execute_script(
            "window.__ticks = 0;"
            "window.__timer = setInterval(() => { window.__ticks += 1; }, 10);"
        )
        browser_tools.set_render_control("step", "legacy-timers", gate_timers=True)

        frozen = driver.execute_script("return window.__ticks;")
        time.sleep(0.3)
        assert driver.execute_script("return window.__ticks;") == frozen, (
            "an interval registered before step mode kept ticking on real time"
        )

        browser_tools.render_step(4, "legacy-timers")
        assert driver.execute_script("return window.__ticks;") == frozen + 4
    finally:
        browser_tools.close_session("legacy-timers")


def test_tapped_key_is_still_held_while_the_frame_runs(local_site):
    session = _open_fixture(local_site, PLATFORMER, "tap-jump")
    try:
        browser_tools.set_render_control("step", "tap-jump")
        before = _read_game(session)
        assert before["onGround"] is True

        tapped = browser_tools.press_keys(
            ["SPACE"],
            "tap-jump",
            target_selector="#game",
            action="tap",
            wait_seconds=0,
        )
        assert tapped["frames_advanced"] == 1
        assert tapped["hold_frames"] == 1
        assert tapped["held_keys"] == []  # released again after the frame ran

        after = _read_game(session)
        assert after["tick"] == before["tick"] + 1
        # A key that was lifted before the frame ran would leave the player put.
        assert after["onGround"] is False
        assert after["y"] < before["y"]
        jumps = [item for item in _game_events(session) if item["type"] == "jump"]
        assert len(jumps) == 1
        assert jumps[0]["tick"] == before["tick"] + 1
    finally:
        browser_tools.close_session("tap-jump")


def test_render_step_reinstalls_the_gate_after_the_document_is_replaced(local_site):
    session = _open_fixture(local_site, PLATFORMER, "gate-recovery")
    try:
        browser_tools.set_render_control("step", "gate-recovery")
        browser_tools.render_step(2, "gate-recovery")

        session.driver.refresh()  # a level restart drops every injected script

        recovered = browser_tools.render_step(3, "gate-recovery")
        assert recovered["success"] is True
        assert recovered.get("gate_reinstalled") is True
        assert recovered["frames"] == 3
        assert recovered["frame_count"] == 3  # a fresh gate, counting from zero
        assert session.render_mode == "step"

        before = _read_game(session)
        browser_tools.render_step(2, "gate-recovery")
        assert _read_game(session)["tick"] == before["tick"] + 2
    finally:
        browser_tools.close_session("gate-recovery")


def test_iframe_game_is_stepped_and_reachable_by_pointer(local_site):
    session = _open_fixture(local_site, IFRAME_HOST, "iframe-game")
    try:
        host = browser_tools.game_probe(
            "iframe-game", sample_seconds=0.2, include_console=False
        )
        assert host["canvas_count"] == 0
        assert host["iframe_count"] == 1
        assert host["iframes"][0]["selector"] == "#game-frame"

        inside = browser_tools.game_probe(
            "iframe-game",
            frame_selector="#game-frame",
            sample_seconds=0.2,
            include_console=False,
        )
        assert inside["canvas_count"] == 1
        assert inside["canvases"][0]["selector"] == "#game"
        assert inside["canvases"][0]["context"] == "2d"

        control = browser_tools.set_render_control(
            "step", "iframe-game", frame_selector="#game-frame"
        )
        assert control["mode"] == "step"
        assert control["frame_selector"] == "#game-frame"

        idle = _read_game(session, "#game-frame")
        time.sleep(0.2)
        gated = _read_game(session, "#game-frame")
        assert gated["tick"] == idle["tick"], "the gate did not reach into the frame"

        browser_tools.press_keys(
            ["RIGHT"],
            "iframe-game",
            target_selector="#game",
            frame_selector="#game-frame",
            action="hold",
            wait_seconds=0,
        )
        browser_tools.render_step(6, "iframe-game")
        moved = _read_game(session, "#game-frame")
        assert moved["keys"] == ["ArrowRight"]
        assert moved["tick"] == gated["tick"] + 7  # one frame for the input, six stepped
        assert moved["x"] == gated["x"] + 7 * 5
        browser_tools.press_keys(
            ["RIGHT"], "iframe-game", frame_selector="#game-frame", action="release", wait_seconds=0
        )

        _in_frame(
            session,
            "#game-frame",
            "window.__hits = [];"
            "document.getElementById('game').addEventListener('mousedown', event =>"
            "  window.__hits.push({x: Math.round(event.offsetX), y: Math.round(event.offsetY)}));",
        )
        canvas = _in_frame(
            session,
            "#game-frame",
            "const r = document.getElementById('game').getBoundingClientRect();"
            "return {x: r.x, y: r.y, width: r.width, height: r.height};",
        )
        browser_tools.pointer_action(
            "click",
            canvas["x"] + 1 + 100,
            canvas["y"] + 1 + 50,
            "iframe-game",
            frame_selector="#game-frame",
            wait_seconds=0,
        )
        hits = _in_frame(session, "#game-frame", "return window.__hits;")
        assert len(hits) == 1, "the click missed the canvas inside the offset frame"
        assert 0 <= hits[0]["x"] <= 640
        assert 0 <= hits[0]["y"] <= 360
    finally:
        browser_tools.close_session("iframe-game")


def test_iframe_pointer_lands_on_the_exact_frame_local_coordinate(local_site):
    """KNOWN FAILURE - real defect in ``pointer_action``/``_frame_offset``.

    Both offset frame-local coordinates by ``getBoundingClientRect()``, which is
    the frame's *border box* origin. CDP input is expressed in top-level page
    pixels, so the offset has to be the frame's *content* origin - border and
    padding included. The host fixture publishes that value in
    ``window.__frameOffset`` (``rect + borderLeft/borderTop``); with its 4px
    border every pointer aimed into the frame lands 4px up and to the left.
    """
    session = _open_fixture(local_site, IFRAME_HOST, "iframe-aim")
    try:
        offset = session.driver.execute_script("return window.__frameOffset;")
        assert offset["borderLeft"] == 4  # the fixture is bordered on purpose
        _in_frame(
            session,
            "#game-frame",
            "window.__hits = [];"
            "document.getElementById('game').addEventListener('mousedown', event =>"
            "  window.__hits.push({x: Math.round(event.offsetX), y: Math.round(event.offsetY)}));",
        )
        canvas = _in_frame(
            session,
            "#game-frame",
            "const r = document.getElementById('game').getBoundingClientRect();"
            "const cs = getComputedStyle(document.getElementById('game'));"
            "return {x: r.x + parseFloat(cs.borderLeftWidth),"
            "        y: r.y + parseFloat(cs.borderTopWidth)};",
        )
        browser_tools.pointer_action(
            "click",
            canvas["x"] + 100,
            canvas["y"] + 50,
            "iframe-aim",
            frame_selector="#game-frame",
            wait_seconds=0,
        )
        hits = _in_frame(session, "#game-frame", "return window.__hits;")
        assert hits[0] == {"x": 100, "y": 50}, (
            "pointer_action added the iframe border-box origin "
            f"({offset['rectX']}, {offset['rectY']}) instead of its content origin "
            f"({offset['x']}, {offset['y']})"
        )
    finally:
        browser_tools.close_session("iframe-aim")


def test_webgl_game_is_probed_and_played_to_the_win(local_site):
    session = _open_fixture(local_site, WEBGL, "webgl-game")
    driver = session.driver
    try:
        probe = browser_tools.game_probe(
            "webgl-game", sample_seconds=0.2, include_console=True
        )
        assert probe["success"] is True
        assert probe["canvas_count"] == 1
        canvas = probe["canvases"][0]
        assert canvas["selector"] == "#gl"
        assert canvas["context"] == "webgl2"
        assert canvas["visible"] is True
        assert probe["animation"]["available"] is True
        assert probe["animation"]["frames"] >= 1
        assert driver.execute_script("return window.__game.error;") is None
        assert driver.execute_script("return window.__game.context;") == "webgl2"

        browser_tools.set_render_control("step", "webgl-game")
        for key in ("1", "2", "3"):
            browser_tools.press_keys(
                [key], "webgl-game", target_selector="#gl", action="tap", wait_seconds=0
            )
        state = driver.execute_script(
            "const g = window.__game;"
            "return {won: g.won, seen: g.colorsSeen, color: g.color, frame: g.frame};"
        )
        assert state["seen"] == ["red", "green", "blue"]
        assert state["color"] == "blue"
        assert state["won"] is True

        browser_tools.set_render_control("normal", "webgl-game")
    finally:
        browser_tools.close_session("webgl-game")
