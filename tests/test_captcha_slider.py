"""Slider captchas are measured and dragged on this machine, not bought.

TCaptcha and Geetest publish no sitekey, so there is nothing a solving service can
be given: the answer is a drag, and the vendor grades the movement. What this file
holds the new code to is therefore the three things that decide whether a drag is
right - where the hole is, how far the handle has to travel, and whether the path
looks like a hand - plus one end-to-end run against a page that really removes its
widget when something is released at the measured offset.

The fixtures are synthetic pictures rather than screenshots of a live vendor: the
measurement has to be checked against a known answer, and a real TCaptcha puzzle is
different every time it loads.
"""

from __future__ import annotations

import base64
import io

import numpy as np
import pytest
from PIL import Image

from web_search_neo import captcha_solvers
from web_search_neo.captcha_solvers.images import crop_png, find_outline_pair, image_size, match_piece

HOLE_X, PIECE_X, PIECE_W, PIECE_H = 178, 4, 52, 56
WIDGET_W, WIDGET_H = 300, 180


def _texture(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    tex = rng.integers(60, 200, size=(WIDGET_H, WIDGET_W, 3)).astype("int16")
    tex[:, 1:] = (tex[:, 1:] + tex[:, :-1]) // 2
    return tex


def _outline(arr: np.ndarray, x: int, y: int, width: int, height: int, value: int) -> None:
    arr[y:y + height, x] = value
    arr[y:y + height, x + width - 1] = value
    arr[y:y + 1, x:x + width] = value
    arr[y + height - 1:y + height, x:x + width] = value


def _png(arr) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.asarray(arr).astype("uint8")).save(buffer, format="PNG")
    return buffer.getvalue()


def _widget_picture(seed: int = 7) -> bytes:
    """A slider background with the piece at the start and the hole to its right."""
    tex = _texture(seed)
    _outline(tex, HOLE_X, 30, PIECE_W, PIECE_H, 8)
    _outline(tex, PIECE_X, 30, PIECE_W, PIECE_H, 246)
    return _png(tex)


def _hole_only_picture() -> bytes:
    """The background as the vendor serves it: the hole is there, the piece is not.

    The hole holds an inverted copy of the piece's own texture, which is what makes
    the silhouette matchable - the same edge structure in a different place - and the
    patch's home on the track is flattened so there is exactly one place it fits.
    """
    tex = _texture()
    patch = tex[30:30 + PIECE_H, PIECE_X:PIECE_X + PIECE_W].copy()
    tex[30:30 + PIECE_H, PIECE_X:PIECE_X + PIECE_W] = 128
    tex[30:30 + PIECE_H, HOLE_X:HOLE_X + PIECE_W] = 255 - patch
    return _png(tex)


def _piece_picture() -> bytes:
    """The cut-out piece as its own image."""
    return _png(_texture()[30:30 + PIECE_H, PIECE_X:PIECE_X + PIECE_W])


def test_notch_finder_locates_the_hole_and_the_distance():
    found = captcha_solvers.detect_gap(_widget_picture())
    assert found["ok"] is True
    assert found["method"] == "notch"
    assert abs(found["gap_x"] - HOLE_X) <= 2, found
    assert abs(found["piece_x"] - PIECE_X) <= 2, found
    # The distance is what the drag has to travel; getting it from the left border
    # instead of from where the piece actually sits would overshoot by PIECE_X.
    assert abs(found["distance"] - (HOLE_X - PIECE_X)) <= 3, found
    assert found["confidence"] > 0.5, found


def test_crop_of_a_device_pixel_capture_measures_the_same_offset():
    shot = _widget_picture()
    scaled = Image.open(io.BytesIO(shot)).resize((WIDGET_W * 2, WIDGET_H * 2), Image.NEAREST)
    cropped = crop_png(_png(scaled), 0, 0, WIDGET_W, WIDGET_H, 2.0)
    found = captcha_solvers.detect_gap(cropped)
    assert found["ok"] is True
    # Measured in device pixels, then scaled back by the crop's own ratio: the
    # answer has to come out in the CSS pixels the pointer actions are given.
    css = found["distance"] * (WIDGET_W / float(found["image_width"]))
    assert abs(css - (HOLE_X - PIECE_X)) <= 4, found


def test_template_matching_finds_the_hole_when_only_the_hole_is_left():
    pytest.importorskip("cv2")
    found = match_piece(_hole_only_picture(), _piece_picture())
    assert found["ok"] is True, found
    assert found["method"] == "template"
    assert abs(found["gap_x"] - HOLE_X) <= 3, found
    assert abs(found["gap_y"] - 30) <= 3, found


def _plain_texture(seed: int = 11) -> np.ndarray:
    """A photo that never reaches the white the widget strokes its shapes with."""
    rng = np.random.default_rng(seed)
    tex = rng.integers(40, 150, size=(WIDGET_H, WIDGET_W, 3)).astype("int16")
    tex[:, 1:] = (tex[:, 1:] + tex[:, :-1]) // 2
    return tex


def test_outline_pair_reads_the_strokes_the_widget_draws_itself():
    """A live TCaptcha strokes both the piece and its hole with a light line.

    That is a different signature from the notch search and it was the one that
    measured a real puzzle to the pixel while the edge search under-measured it, so
    both shapes have to be readable as a pair: same width, same height, same row.
    """
    pytest.importorskip("cv2")
    tex = _plain_texture()
    _outline(tex, HOLE_X, 30, PIECE_W, PIECE_H, 246)
    _outline(tex, PIECE_X, 30, PIECE_W, PIECE_H, 240)
    found = captcha_solvers.detect_gap(_png(tex))
    assert found["ok"] is True, found
    assert found["method"] == "outline_pair", found
    assert abs(found["gap_x"] - HOLE_X) <= 1, found
    assert abs(found["piece_x"] - PIECE_X) <= 1, found
    assert abs(found["distance"] - (HOLE_X - PIECE_X)) <= 1, found
    assert found["confidence"] > 0.9, found


def test_a_third_shape_in_the_same_row_is_reported_as_unsure():
    """A rival pair lowers the outline reading, and an unsure reading does not get forced."""
    pytest.importorskip("cv2")
    tex = _plain_texture()
    _outline(tex, HOLE_X, 30, PIECE_W, PIECE_H, 246)
    _outline(tex, PIECE_X, 30, PIECE_W, PIECE_H, 240)
    _outline(tex, HOLE_X + 70, 31, PIECE_W, PIECE_H, 242)
    outlined = captcha_solvers.images.find_outline_pair(_png(tex))
    assert outlined["ok"] is True, outlined
    assert outlined["confidence"] <= 0.6, outlined
    # With the outline reading unsure, the widget's strokes no longer settle one
    # answer, so the caller keeps its own inference instead of a forced reading.
    assert captcha_solvers.detect_gap(_png(tex))["method"] != "outline_pair"


def test_photo_highlights_are_not_shapes_and_a_dim_stroke_is_read():
    """A real photo carries bright patches the size window cannot rule out alone.

    In a live capture the hole's stroke sat around 158 where the photo behind it was
    dark, and a lit part of the same photo was a solid block the same size as the
    piece - so the stroke has to be readable below 165, and a patch that fills its
    own box has to stay out of the pair while a ring does not.
    """
    pytest.importorskip("cv2")
    tex = _plain_texture(seed=5)
    tex[30:30 + PIECE_H, 240:240 + PIECE_W] = 215
    _outline(tex, HOLE_X, 30, PIECE_W, PIECE_H + 8, 158)
    _outline(tex, PIECE_X, 30, PIECE_W, PIECE_H, 158)
    outlined = find_outline_pair(_png(tex))
    assert outlined["ok"] is True, outlined
    assert abs(outlined["distance"] - (HOLE_X - PIECE_X)) <= 1, outlined
    assert outlined["confidence"] > 0.9, outlined
    assert captcha_solvers.detect_gap(_png(tex))["method"] == "outline_pair"


def test_a_picture_with_no_hole_says_so_instead_of_guessing():
    flat = _png(np.full((WIDGET_H, WIDGET_W, 3), 128, dtype="uint8"))
    found = captcha_solvers.detect_gap(flat)
    assert found["ok"] is False
    assert found["reason"]
    assert image_size(flat) == (WIDGET_W, WIDGET_H)
    # A box with nothing in it is not a crop, and a box smaller than the smallest
    # piece worth measuring is widened to one rather than returned as noise.
    assert crop_png(flat, WIDGET_W + 20, 0, 40, 40) == b""
    assert image_size(crop_png(flat, 0, 0, 3, 3)) == (8, 8)


def test_trajectory_overshoots_and_lands_on_the_exact_distance():
    for distance in (40, 174, 420):
        path = captcha_solvers.human_trajectory(distance, 0.9, seed=distance)
        xs = [point[0] for point in path]
        ys = [point[1] for point in path]
        pauses = [point[2] for point in path]
        assert xs[-1] == pytest.approx(distance), path[-3:]
        assert ys[0] == 0.0 and ys[-1] == 0.0, path[:2]
        # A hand passes the hole slightly and eases back onto it; a monotone ramp
        # that stops dead is the signature these vendors score as a bot.
        assert max(xs) > distance + 1.0, max(xs)
        assert max(xs[i] - xs[i + 1] for i in range(len(xs) - 1)) <= 1.6, path
        assert all(pause > 0 for pause in pauses)
        assert sum(pauses) == pytest.approx(0.9, abs=0.02)


def test_solve_loop_measures_drags_and_confirms_the_widget_left():
    calls = {"locate": 0, "drag": [], "cleared": 0}

    def locate() -> dict:
        calls["locate"] += 1
        return {
            "vendor": "tcaptcha", "live": True, "solved": False,
            "root": [10, 20, WIDGET_W + 20, WIDGET_H + 60],
            "bg": [0, 0, WIDGET_W, WIDGET_H],
            "handle": [4, WIDGET_H + 10, 44, 34],
            "dpr": 1, "viewport": [WIDGET_W, WIDGET_H + 80], "url": "https://example.test/auth",
        }

    def capture() -> bytes:
        return _widget_picture()

    def drag(start, path) -> None:
        calls["drag"].append((start, list(path)))

    def cleared() -> dict:
        calls["cleared"] += 1
        return {"cleared": True, "vendor": "tcaptcha", "evidence": "no live slider widget on the page"}

    outcome = captcha_solvers.solve_slider(
        locate, capture, drag, cleared, lambda seconds: None, budget_seconds=20.0
    )
    assert outcome["solved"] is True, outcome
    assert outcome["vendor"] == "tcaptcha"
    assert abs(outcome["distance_px"] - (HOLE_X - PIECE_X)) <= 4, outcome
    assert len(calls["drag"]) == 1
    start, path = calls["drag"][0]
    assert start == (4 + 22.0, WIDGET_H + 10 + 17.0)
    assert path[-1][0] == pytest.approx(HOLE_X - PIECE_X, abs=4)
    assert calls["cleared"] >= 1


def test_an_unsure_measurement_is_reported_instead_of_dragged_on():
    calls = {"drag": 0}

    def locate() -> dict:
        return {
            "vendor": "tcaptcha", "live": True, "solved": False,
            "bg": [0, 0, WIDGET_W, WIDGET_H], "handle": [4, 200, 44, 34],
            "dpr": 1, "viewport": [WIDGET_W, 260], "url": "https://example.test/auth",
        }

    def drag(start, path) -> None:
        calls["drag"] += 1

    outcome = captcha_solvers.solve_slider(
        locate,
        lambda: _png(np.full((WIDGET_H, WIDGET_W, 3), 128, dtype="uint8")),
        drag,
        lambda: {"cleared": False},
        lambda seconds: None,
        budget_seconds=10.0,
    )
    assert outcome["solved"] is False
    assert "measured" in outcome["reason"] or "unsure" in outcome["reason"], outcome
    assert calls["drag"] == 0


def test_local_solve_is_on_until_an_operator_switches_it_off(monkeypatch):
    monkeypatch.delenv(captcha_solvers.slider.LOCAL_SOLVE_ENV, raising=False)
    assert captcha_solvers.local_solve_enabled() is True
    monkeypatch.setenv(captcha_solvers.slider.LOCAL_SOLVE_ENV, "0")
    assert captcha_solvers.local_solve_enabled() is False


def test_vendor_scripts_name_the_widgets_they_are_meant_to_find():
    assert set(captcha_solvers.SLIDER_VENDORS) >= {"tcaptcha", "geetest4"}
    for name, spec in captcha_solvers.SLIDER_VENDORS.items():
        for key in ("root", "frame", "bg", "handle", "success"):
            assert isinstance(spec[key], list) and spec[key], (name, key)
    assert "#tcaptcha_transform_dy" in captcha_solvers.SLIDER_LOCATE_SCRIPT
    assert "tencent-captcha-dy__" in captcha_solvers.SLIDER_LOCATE_SCRIPT
    assert "geetest_slider_button" in captcha_solvers.SLIDER_LOCATE_SCRIPT
    # Tencent renames these classes between releases; a substring selector survives
    # the rename and an exact name does not.
    assert '[class*=' in captcha_solvers.SLIDER_LOCATE_SCRIPT
    assert "turing.captcha.qcloud.com" in captcha_solvers.SLIDER_CLEARED_SCRIPT


def test_page_probe_reports_a_tcaptcha_widget_as_a_captcha_widget(local_site):
    from selenium.common.exceptions import WebDriverException

    from web_search_neo import browser_tools

    try:
        browser_tools.open_page(
            f"{local_site.base_url}/page", session_id="slider-probe",
            headless=True, profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    session = browser_tools._get_session("slider-probe")
    html = """
      <h1>Sign in</h1><p>Email and password are required to continue.</p>
      <div id="tcaptcha_transform_dy" style="position:fixed;left:20px;top:20px;width:340px;
           height:260px;background:#fff">
        <div class="tencent-captcha-dy__bg-placeholder tencent-captcha-dy__bg-placeholder--slider"
             style="width:300px;height:180px"></div>
        <div class="tencent-captcha-dy__slider-block" style="width:300px;height:40px"></div>
      </div>
    """
    session.driver.execute_script("document.body.innerHTML = arguments[0];", html)
    status = browser_tools._challenge_status(session.driver)
    joined = " ".join(status["captcha_widgets"])
    assert "tcaptcha" in joined.lower(), status
    assert "tencent-captcha-dy__" in joined, status

    # The leftover node an auto-passed TCaptcha leaves behind has a box and zero
    # opacity. It is not a gate, and it is not reported as one: TCaptcha is
    # deliberately absent from INVISIBLE_CAPABLE.
    session.driver.execute_script(
        "document.getElementById('tcaptcha_transform_dy').style.opacity = '0';"
    )
    hidden_status = browser_tools._challenge_status(session.driver)
    assert "tencent-captcha-dy__" not in " ".join(hidden_status["captcha_widgets"])
    assert hidden_status["invisible_challenge_pending"] is False


def test_a_widget_that_only_clears_at_the_measured_distance_gets_solved(local_site):
    """End to end: see the widget, measure it, drag it, and the page admits it."""
    from selenium.common.exceptions import WebDriverException

    from web_search_neo import browser_tools

    try:
        browser_tools.open_page(
            f"{local_site.base_url}/slider-captcha",
            session_id="slider-live", headless=True, profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")

    result = browser_tools.solve_captcha(
        mode="solve", session_id="slider-live", timeout_seconds=40.0, poll_seconds=1.0
    )
    assert result.get("solved_by") == "local_slider", result
    assert result["captcha_present"] is False, result
    assert abs(result["slider"]["distance_px"] - 174) <= 8, result["slider"]

    driver = browser_tools._get_session("slider-live").driver
    # The page only removes its own widget when a mouseup lands within 8 px of the
    # distance it was built around, so this is the measurement being checked by the
    # other side rather than by our own arithmetic.
    assert driver.execute_script("return document.getElementById('slider-state').textContent;") == "solved"
    drag = driver.execute_script("return window.__drag || null;")
    assert drag and abs(drag["distance"] - 174) <= 8, drag
