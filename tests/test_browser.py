from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import struct
import time

import pytest
from selenium.common.exceptions import WebDriverException

import browser_tools
import main


def _open_or_skip(url: str, session_id: str, **kwargs):
    try:
        return browser_tools.open_page(url, session_id=session_id, **kwargs)
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")


def test_browser_full_form_upload_click_submit_and_screenshot(local_site, tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("verified resume payload", encoding="utf-8")
    opened = _open_or_skip(
        f"{local_site.base_url}/form?session=full-flow",
        "full-flow",
        width=800,
        height=600,
    )
    assert opened["session_id"] == "full-flow"
    assert opened["title"] == "Form full-flow"
    assert opened["viewport_width"] == 800
    assert opened["viewport_height"] == 600

    elements = browser_tools.get_page_elements("full-flow")
    assert {link["selector"] for link in elements["links"]} >= {"#fixture-link"}
    assert {button["selector"] for button in elements["buttons"]} >= {
        "#action-button",
        "#submit-button",
    }
    form = next(item for item in elements["forms"] if item["selector"] == "#application")
    by_selector = {field["selector"]: field for field in form["fields"]}
    assert by_selector["#candidate-name"]["label"] == "Candidate name"
    assert by_selector["#resume"]["type"] == "file"
    assert {item["value"] for item in by_selector["#role"]["options"]} == {
        "python",
        "unity",
    }

    filled = browser_tools.fill_fields(
        {
            "#candidate-name": "Neo Candidate",
            "#cover-letter": "Unity and C# experience",
            "#role": "unity",
            "#remote": True,
        },
        files={"#resume": str(resume)},
        session_id="full-flow",
    )
    assert filled["success"] is True
    assert set(filled["filled"]) == {
        "#candidate-name",
        "#cover-letter",
        "#role",
        "#remote",
    }
    assert filled["files_uploaded"] == ["#resume"]

    clicked = browser_tools.click("#action-button", "full-flow", wait_seconds=0)
    assert clicked["success"] is True
    session = browser_tools._get_session("full-flow")
    assert session.driver.find_element("css selector", "#click-state").text == "clicked"

    png = browser_tools.screenshot("full-flow", width=640, height=480, full_page=False)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1_000
    assert struct.unpack(">II", png[16:24]) == (640, 480)

    submitted = browser_tools.submit_form(
        "#application",
        "full-flow",
        submit_selector="#submit-button",
        wait_seconds=0.1,
    )
    assert submitted["success"] is True
    assert submitted["validation_passed"] is True
    assert submitted["submit_triggered"] is True
    assert submitted["title"] == "Submitted"
    assert submitted["url"].endswith("/submit")

    assert local_site.requests
    request = local_site.requests[-1]
    body = request.body.decode("utf-8", errors="replace")
    assert request.path == "/submit"
    assert request.headers["Content-Type"].startswith("multipart/form-data;")
    assert 'name="candidate_name"' in body and "Neo Candidate" in body
    assert 'name="role"' in body and "unity" in body
    assert 'name="remote"' in body and "on" in body
    assert 'filename="resume.txt"' in body and "verified resume payload" in body

    status = browser_tools.get_status("full-flow")
    assert status["available"] is True
    assert status["session_open"] is True
    closed = browser_tools.close_session("full-flow")
    assert closed == {"session_id": "full-flow", "closed": True, "active_sessions": []}
    assert browser_tools.get_status("full-flow")["session_open"] is False


def test_fill_fields_returns_partial_errors_without_losing_successful_fields(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "partial")

    result = browser_tools.fill_fields(
        {"#candidate-name": "Still filled", "#missing": "not found"},
        session_id="partial",
    )

    assert result["success"] is False
    assert result["filled"] == ["#candidate-name"]
    assert "#missing" in result["errors"]


def test_two_named_sessions_remain_independent_during_parallel_actions(local_site):
    _open_or_skip(f"{local_site.base_url}/form?session=alpha", "alpha")
    _open_or_skip(f"{local_site.base_url}/form?session=beta", "beta")

    def fill_and_read(session_id: str, value: str) -> tuple[str, str, str]:
        result = browser_tools.fill_fields(
            {"#candidate-name": value}, session_id=session_id
        )
        session = browser_tools._get_session(session_id)
        marker = session.driver.find_element("css selector", "#session-marker").text
        field_value = session.driver.find_element(
            "css selector", "#candidate-name"
        ).get_attribute("value")
        return result["session_id"], marker, field_value

    with ThreadPoolExecutor(max_workers=2) as executor:
        alpha_future = executor.submit(fill_and_read, "alpha", "Alice")
        beta_future = executor.submit(fill_and_read, "beta", "Bob")
        alpha = alpha_future.result(timeout=15)
        beta = beta_future.result(timeout=15)

    assert alpha == ("alpha", "alpha", "Alice")
    assert beta == ("beta", "beta", "Bob")
    assert browser_tools.get_status("alpha")["active_sessions"] == ["alpha", "beta"]
    assert browser_tools.get_status("beta")["active_sessions"] == ["alpha", "beta"]


def test_async_bulk_open_creates_two_independent_named_sessions(local_site):
    result = asyncio.run(
        main.browser_open_pages(
            [
                f"{local_site.base_url}/form?session=bulk-one",
                f"{local_site.base_url}/form?session=bulk-two",
            ],
            session_ids=["bulk-one", "bulk-two"],
            width=700,
            height=500,
        )
    )

    assert result["success_count"] == 2, result
    assert result["failure_count"] == 0
    assert [page["session_id"] for page in result["pages"]] == [
        "bulk-one",
        "bulk-two",
    ]
    assert [page["title"] for page in result["pages"]] == [
        "Form bulk-one",
        "Form bulk-two",
    ]
    assert browser_tools.get_status("bulk-one")["active_sessions"] == [
        "bulk-one",
        "bulk-two",
    ]


@pytest.mark.parametrize("session_id", ["", "contains space", "../escape", "x" * 65])
def test_session_id_is_validated(session_id):
    with pytest.raises(ValueError, match="session_id"):
        browser_tools.get_status(session_id)


def test_browser_requires_open_session_before_actions():
    with pytest.raises(ValueError, match="call browser_open_page first"):
        browser_tools.get_page_elements("missing")


def test_submit_reports_native_validation_failure(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "invalid-submit")
    result = browser_tools.submit_form("#application", "invalid-submit", wait_seconds=0)
    assert result["success"] is False
    assert result["validation_passed"] is False
    assert result["submit_triggered"] is False
    assert result["validation_errors"][0]["id"] == "candidate-name"


def test_selected_radio_cannot_be_falsely_reported_unchecked(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "radio")
    session = browser_tools._get_session("radio")
    session.driver.execute_script(
        "document.body.insertAdjacentHTML('beforeend', '<input id=radio-one type=radio name=choice checked>');"
    )
    result = browser_tools.fill_fields({"#radio-one": False}, session_id="radio")
    assert result["success"] is False
    assert "cannot be unchecked" in result["errors"]["#radio-one"]
    assert session.driver.find_element("css selector", "#radio-one").is_selected()


def test_separate_upload_tool_supports_file_input(local_site, tmp_path):
    upload = tmp_path / "cv.pdf"
    upload.write_bytes(b"%PDF-test")
    _open_or_skip(f"{local_site.base_url}/form", "upload")
    result = browser_tools.upload_file("#resume", [str(upload)], "upload")
    assert result["success"] is True
    assert result["files_uploaded"] == 1
    assert result["file_names"] == ["cv.pdf"]


def test_wait_for_dynamic_element(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "dynamic-wait")
    session = browser_tools._get_session("dynamic-wait")
    session.driver.execute_script(
        "setTimeout(() => document.body.insertAdjacentHTML('beforeend', "
        "'<button id=dynamic-button>Ready</button>'), 100);"
    )

    result = browser_tools.wait_for_element(
        "#dynamic-button", "dynamic-wait", state="clickable", timeout_seconds=2
    )

    assert result["success"] is True
    assert result["selector"] == "#dynamic-button"
    assert result["state"] == "clickable"
    assert result["tag"] == "button"


def test_wait_for_manual_challenge_resolution(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "challenge-resolved")
    session = browser_tools._get_session("challenge-resolved")
    session.driver.execute_script(
        "document.body.insertAdjacentHTML('afterbegin', "
        "'<div id=challenge>Verify you are human</div>');"
        "setTimeout(() => document.getElementById('challenge').remove(), 150);"
    )

    result = browser_tools.wait_for_challenge_resolution(
        "challenge-resolved", timeout_seconds=2, poll_interval_seconds=0.05
    )

    assert result["success"] is True
    assert result["challenge_seen"] is True
    assert result["resolved"] is True
    assert result["timed_out"] is False
    assert result["session_open"] is True


def test_wait_for_manual_challenge_timeout_keeps_session_open(local_site):
    _open_or_skip(f"{local_site.base_url}/form", "challenge-timeout")
    session = browser_tools._get_session("challenge-timeout")
    session.driver.execute_script(
        "document.body.insertAdjacentHTML('afterbegin', "
        "'<div id=challenge>Verify you are human</div>');"
    )

    result = browser_tools.wait_for_challenge_resolution(
        "challenge-timeout", timeout_seconds=0.15, poll_interval_seconds=0.05
    )

    assert result["success"] is False
    assert result["resolved"] is False
    assert result["timed_out"] is True
    assert result["session_open"] is True


def test_persistent_profile_reuses_browser_storage(local_site, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_PROFILE_ROOT", str(tmp_path / "profiles"))
    first = _open_or_skip(
        f"{local_site.base_url}/form",
        "persistent-one",
        profile_mode="persistent",
        profile_id="hh",
    )
    assert first["profile_mode"] == "persistent"
    assert first["profile_id"] == "hh"
    session = browser_tools._get_session("persistent-one")
    session.driver.execute_script("localStorage.setItem('authorization-proof', 'saved')")
    browser_tools.close_session("persistent-one")

    second = _open_or_skip(
        f"{local_site.base_url}/form",
        "persistent-two",
        profile_mode="persistent",
        profile_id="hh",
    )
    session = browser_tools._get_session("persistent-two")
    assert second["profile_mode"] == "persistent"
    assert session.driver.execute_script(
        "return localStorage.getItem('authorization-proof')"
    ) == "saved"
    with pytest.raises(RuntimeError, match="already in use"):
        browser_tools.open_page(
            f"{local_site.base_url}/form",
            "persistent-three",
            profile_mode="persistent",
            profile_id="hh",
        )


def test_profile_configuration_validates_attach_and_exclusive_profile(local_site):
    with pytest.raises(ValueError, match="local address"):
        browser_tools._profile_configuration(
            "attach", "attach", None, "192.168.1.10:9222"
        )
    mode, profile_id, address, key = browser_tools._profile_configuration(
        "attach", "attach", None, "127.0.0.1:9222"
    )
    assert (mode, profile_id, address, key) == (
        "attach",
        None,
        "127.0.0.1:9222",
        "attach:127.0.0.1:9222",
    )


def test_automatic_window_mode_defaults_attach_visible_and_owned_profiles_headless():
    assert browser_tools._resolve_headless("temporary", None) is True
    assert browser_tools._resolve_headless("persistent", None) is True
    assert browser_tools._resolve_headless("attach", None) is False
    assert browser_tools._resolve_headless("temporary", False) is False
    assert browser_tools._resolve_headless("persistent", False) is False
    assert browser_tools._resolve_headless("attach", True) is True


def test_latest_cached_chromedriver_uses_highest_version(tmp_path, monkeypatch):
    cache = tmp_path / "selenium"
    executable = "chromedriver.exe" if browser_tools.os.name == "nt" else "chromedriver"
    older = cache / "chromedriver" / "platform" / "150.0.1.1" / executable
    newer = cache / "chromedriver" / "platform" / "151.0.2.3" / executable
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    monkeypatch.setenv("SE_CACHE_PATH", str(cache))

    assert browser_tools._latest_cached_chromedriver() == newer


def test_canvas_game_probe_keyboard_pointer_and_drag(local_site):
    opened = _open_or_skip(f"{local_site.base_url}/game", "canvas-game")
    assert opened["headless"] is True
    assert opened["window_mode"] == "headless"

    probe = browser_tools.game_probe(
        "canvas-game", sample_seconds=0.15, include_console=False
    )
    assert probe["success"] is True
    assert probe["canvas_count"] == 1
    assert probe["canvases"][0]["selector"] == "#game-canvas"
    assert probe["canvases"][0]["context"] == "2d"
    assert probe["animation"]["frames"] >= 1
    assert probe["animation"]["fps"] > 0

    rect = probe["canvases"][0]["rect"]
    start_x = rect["x"] + 100
    start_y = rect["y"] + 100
    clicked = browser_tools.pointer_action(
        "click", start_x, start_y, "canvas-game", wait_seconds=0
    )
    assert clicked["success"] is True
    assert clicked["action"] == "click"

    keys = browser_tools.press_keys(
        ["SPACE"],
        "canvas-game",
        target_selector="#game-canvas",
        hold_seconds=0.01,
        repeat=2,
        wait_seconds=0,
    )
    assert keys["success"] is True
    assert keys["repeat"] == 2

    dragged = browser_tools.pointer_action(
        "drag",
        start_x,
        start_y,
        "canvas-game",
        end_x=start_x + 80,
        end_y=start_y + 40,
        duration_seconds=0.05,
        wait_seconds=0,
    )
    assert dragged["success"] is True
    assert dragged["action"] == "drag"

    session = browser_tools._get_session("canvas-game")
    events = session.driver.execute_script("return window.gameEvents")
    event_types = [event["type"] for event in events]
    assert event_types.count("pointerdown") >= 2
    assert event_types.count("pointerup") >= 2
    assert "pointermove" in event_types
    assert sum(
        event["type"] == "keydown" and event.get("code") == "Space"
        for event in events
    ) >= 2
    assert sum(
        event["type"] == "keyup" and event.get("code") == "Space"
        for event in events
    ) >= 2


@pytest.mark.parametrize(
    ("action", "kwargs", "message"),
    [
        ("unknown", {}, "action must be"),
        ("click", {"button": "extra"}, "button must be"),
        ("drag", {}, "drag requires"),
    ],
)
def test_game_pointer_rejects_invalid_actions(local_site, action, kwargs, message):
    _open_or_skip(f"{local_site.base_url}/game", "invalid-pointer")
    with pytest.raises(ValueError, match=message):
        browser_tools.pointer_action(
            action, 20, 20, "invalid-pointer", wait_seconds=0, **kwargs
        )


def test_game_keyboard_rejects_unknown_named_key(local_site):
    _open_or_skip(f"{local_site.base_url}/game", "invalid-key")
    with pytest.raises(ValueError, match="Unsupported key"):
        browser_tools.press_keys(["NOT_A_REAL_KEY"], "invalid-key")


def test_step_render_batches_multiple_held_keys_into_one_frame(local_site):
    _open_or_skip(f"{local_site.base_url}/game", "step-input")
    session = browser_tools._get_session("step-input")

    controlled = browser_tools.set_render_control("step", "step-input")
    assert controlled["mode"] == "step"
    assert controlled["input_advances_frame"] is True
    time.sleep(0.1)
    before = session.driver.execute_script("return window.frameCount")
    time.sleep(0.15)
    assert session.driver.execute_script("return window.frameCount") == before

    held = browser_tools.press_keys(
        ["W", "SHIFT", "SPACE"],
        "step-input",
        target_selector="#game-canvas",
        wait_seconds=0,
        action="hold",
    )
    assert held["held_keys"] == ["SHIFT", "SPACE", "W"]
    after_hold = session.driver.execute_script("return window.frameCount")
    assert after_hold == before + 1

    events = session.driver.execute_script("return window.gameEvents")
    key_down = [
        event for event in events if event["type"] == "keydown" and event["code"] in {"KeyW", "ShiftLeft", "Space"}
    ]
    assert {event["code"] for event in key_down} == {"KeyW", "ShiftLeft", "Space"}
    assert len({event["frame"] for event in key_down}) == 1
    assert not any(
        event["type"] == "keyup"
        and event["code"] in {"KeyW", "ShiftLeft", "Space"}
        for event in events
    )

    released = browser_tools.press_keys(
        ["W", "SHIFT", "SPACE"],
        "step-input",
        wait_seconds=0,
        action="release",
    )
    assert released["held_keys"] == []
    assert session.driver.execute_script("return window.frameCount") == after_hold + 1
    key_up = [
        event
        for event in session.driver.execute_script("return window.gameEvents")
        if event["type"] == "keyup" and event["code"] in {"KeyW", "ShiftLeft", "Space"}
    ]
    assert {event["code"] for event in key_up} == {"KeyW", "ShiftLeft", "Space"}
    assert len({event["frame"] for event in key_up}) == 1

    stepped = browser_tools.render_step(3, "step-input")
    assert stepped["frames"] == 3
    assert session.driver.execute_script("return window.frameCount") == after_hold + 4

    normal = browser_tools.set_render_control("normal", "step-input")
    assert normal["mode"] == "normal"


def test_mixed_key_and_pointer_batch_is_atomic_in_step_mode(local_site):
    _open_or_skip(f"{local_site.base_url}/game", "mixed-input")
    session = browser_tools._get_session("mixed-input")
    browser_tools.set_render_control("step", "mixed-input")
    browser_tools.press_keys(
        ["S"],
        "mixed-input",
        target_selector="#game-canvas",
        action="hold",
        wait_seconds=0,
    )
    before = session.driver.execute_script(
        "window.gameEvents = []; return window.frameCount;"
    )

    result = browser_tools.input_batch(
        key_actions=[
            {"key": "W", "action": "hold"},
            {"key": "S", "action": "release"},
            {"key": "SPACE", "action": "tap"},
            {"key": "E", "action": "tap"},
        ],
        pointer_actions=[
            {"action": "hover", "x": 200, "y": 150},
            {
                "action": "move",
                "x": 10,
                "y": -5,
                "coordinate_mode": "delta",
            },
        ],
        session_id="mixed-input",
        wait_seconds=0,
    )

    assert result["frame_advanced"] is True
    assert result["frames_advanced"] == 1
    assert result["held_keys"] == ["W"]
    assert result["pointer_actions"][0]["action"] == "hover"
    assert result["pointer_actions"][0]["x"] == 200
    assert result["pointer_actions"][0]["y"] == 150
    assert result["pointer_actions"][1]["coordinate_mode"] == "delta"
    assert result["pointer_actions"][1]["x"] == 210
    assert result["pointer_actions"][1]["y"] == 145
    assert session.driver.execute_script("return window.frameCount") == before + 1

    events = session.driver.execute_script("return window.gameEvents")
    relevant = [
        event
        for event in events
        if event["type"] in {"keydown", "keyup", "pointermove"}
    ]
    assert relevant
    assert {event["frame"] for event in relevant} == {before}
    keyboard = {(event["type"], event["code"]) for event in relevant if "code" in event}
    assert {
        ("keydown", "KeyW"),
        ("keyup", "KeyS"),
        ("keydown", "Space"),
        ("keyup", "Space"),
        ("keydown", "KeyE"),
        ("keyup", "KeyE"),
    } <= keyboard


def test_continuous_render_throttle_and_normal_restore(local_site):
    _open_or_skip(f"{local_site.base_url}/game", "throttled-game")
    session = browser_tools._get_session("throttled-game")
    controlled = browser_tools.set_render_control(
        "throttled", "throttled-game", target_fps=10
    )
    assert controlled["mode"] == "throttled"
    assert controlled["target_fps"] == 10

    start = session.driver.execute_script("return window.frameCount")
    time.sleep(0.65)
    throttled_delta = session.driver.execute_script("return window.frameCount") - start
    assert 3 <= throttled_delta <= 10

    browser_tools.set_render_control("normal", "throttled-game")
    restored_start = session.driver.execute_script("return window.frameCount")
    time.sleep(0.25)
    restored_delta = session.driver.execute_script("return window.frameCount") - restored_start
    assert restored_delta >= 5


def test_pointer_button_can_be_held_moved_and_released(local_site):
    _open_or_skip(f"{local_site.base_url}/game", "held-pointer")
    browser_tools.pointer_action(
        "press", 100, 100, "held-pointer", wait_seconds=0
    )
    assert browser_tools._get_session("held-pointer").held_buttons == {"left"}
    moved = browser_tools.pointer_action(
        "move", 180, 140, "held-pointer", wait_seconds=0
    )
    assert moved["held_buttons"] == ["left"]
    released = browser_tools.pointer_action(
        "release", 180, 140, "held-pointer", wait_seconds=0
    )
    assert released["held_buttons"] == []


def test_release_inputs_clears_every_held_key_and_button(local_site):
    _open_or_skip(f"{local_site.base_url}/game", "release-all")
    browser_tools.press_keys(
        ["A", "SHIFT"],
        "release-all",
        target_selector="#game-canvas",
        action="hold",
        wait_seconds=0,
    )
    browser_tools.pointer_action(
        "press", 120, 120, "release-all", wait_seconds=0
    )
    result = browser_tools.release_inputs("release-all")
    assert result["held_keys"] == []
    assert result["held_buttons"] == []
    session = browser_tools._get_session("release-all")
    assert session.held_keys == {}
    assert session.held_buttons == set()


def test_navigation_resets_render_gate_and_held_inputs(local_site):
    _open_or_skip(f"{local_site.base_url}/game", "navigation-reset")
    browser_tools.set_render_control("step", "navigation-reset")
    browser_tools.press_keys(
        ["W", "SHIFT"],
        "navigation-reset",
        target_selector="#game-canvas",
        action="hold",
        wait_seconds=0,
    )
    browser_tools.pointer_action(
        "press", 120, 120, "navigation-reset", wait_seconds=0
    )

    result = browser_tools.open_page(
        f"{local_site.base_url}/form?session=navigation-reset",
        "navigation-reset",
    )

    session = browser_tools._get_session("navigation-reset")
    assert result["title"] == "Form navigation-reset"
    assert session.render_mode == "normal"
    assert session.render_frame_selector is None
    assert session.held_keys == {}
    assert session.held_buttons == set()
