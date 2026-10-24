"""Real Chromium: the glide the page receives and the cursor a person sees.

Headless, with the session flagged visible so the overlays are drawn, as in
test_presence_browser.py. Checks what only a browser can show: the events of a
hover arrive as a path, the arrow animates by transform and settles on the
real position, a new target mid-glide does not make it jump, the caption and
the badge states exist and stay out of a screenshot.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from selenium.common.exceptions import WebDriverException

from web_search_neo import browser_tools, main, presence_badge

_CURSOR = """
const c = document.querySelector('[data-wsn-presence="cursor"]');
if (!c) return null;
const m = /translate3d\\(([-\\d.]+)px,\\s*([-\\d.]+)px/.exec(c.style.transform || '') || [0, '0', '0'];
const pill = c.shadowRoot.querySelector('div:last-child');
return {left: parseFloat(c.style.left), top: parseFloat(c.style.top),
        x: parseFloat(c.style.left) + parseFloat(m[1]), y: parseFloat(c.style.top) + parseFloat(m[2]),
        display: getComputedStyle(c).display, pill: pill ? pill.textContent : null,
        pillOpacity: pill ? pill.style.opacity : null};
"""


@pytest.fixture
def visible_page(local_site, monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_NEO_AGENT_PRESENCE", raising=False)
    monkeypatch.delenv("WEB_SEARCH_NEO_POINTER_GLIDE", raising=False)
    session_id = "presence-motion"
    try:
        browser_tools.open_page(local_site.base_url, session_id=session_id,
                                profile_mode="temporary", headless=True)
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    session = browser_tools._get_session(session_id)
    session.headless = False
    session.driver.execute_script("""
      document.body.innerHTML = '<button id="go" style="position:absolute;left:520px;top:300px;'
        + 'width:160px;height:50px">Sign in</button><div style="height:3000px"></div>';
      window.__moves = [];
      document.addEventListener('mousemove', e => window.__moves.push([e.clientX, e.clientY]));
    """)
    try:
        yield session_id, session
    finally:
        browser_tools.close_session(session_id)


def _act(step):
    result = asyncio.run(main.web_action([step]))
    assert result["success"], result
    return result


def test_a_hover_arrives_as_a_path_and_the_arrow_follows_it(visible_page):
    session_id, session = visible_page
    driver = session.driver
    _act({"action": "pointer", "pointer_action": "move", "x": 60, "y": 400,
          "session_id": session_id, "include_summary": False})
    assert driver.execute_script("return window.__moves.length") == 1  # the hot path: one event
    time.sleep(0.6)
    driver.execute_script("window.__moves = []")
    _act({"action": "pointer", "pointer_action": "hover", "x": 600, "y": 325,
          "session_id": session_id, "include_summary": False})
    moves = driver.execute_script("return window.__moves")
    assert len(moves) > 3 and moves[-1] == [600, 325]
    xs = [x for x, _y in moves]
    assert xs == sorted(xs)
    cursor = driver.execute_script(_CURSOR)
    # left/top are where the pointer is at once; the drawing settles there.
    assert (cursor["left"], cursor["top"]) == (600, 325)
    assert "Hover" in (cursor["pill"] or "") and "Sign in" in cursor["pill"]
    time.sleep(0.6)
    settled = driver.execute_script(_CURSOR)
    assert abs(settled["x"] - 600) < 0.6 and abs(settled["y"] - 325) < 0.6


def test_a_new_target_mid_glide_bends_instead_of_jumping(visible_page):
    session_id, session = visible_page
    driver = session.driver
    _act({"action": "pointer", "pointer_action": "move", "x": 40, "y": 40,
          "session_id": session_id, "include_summary": False})
    time.sleep(0.6)
    _act({"action": "pointer", "pointer_action": "move", "x": 900, "y": 600,
          "session_id": session_id, "include_summary": False})
    time.sleep(0.09)
    # Sampled in the page around the new note itself, and one frame later, so
    # nothing but the retarget can move the arrow between the readings.
    before, after, frame = driver.execute_script(
        "const read = () => (new Function(arguments[0]))();"
        "const before = read();"
        "window.__wsnPresence.note({action: 'pointer', flash: false, cursor: {x: 100, y: 650, tap: false}});"
        "const after = read();"
        "return new Promise(done => requestAnimationFrame(() => requestAnimationFrame("
        "  () => done([before, after, read()]))));",
        _CURSOR,
    )
    assert 40 < before["x"] < 900  # mid-glide, drawn by transform
    assert (after["left"], after["top"]) == (100, 650)
    # The reference jumped to the new target; the drawing did not.
    assert abs(after["x"] - before["x"]) < 0.6 and abs(after["y"] - before["y"]) < 0.6
    # Two frames on it has moved on smoothly, not leapt to the new target.
    step = ((frame["x"] - after["x"]) ** 2 + (frame["y"] - after["y"]) ** 2) ** 0.5
    assert 0 < step < 120
    time.sleep(0.7)
    settled = driver.execute_script(_CURSOR)
    assert abs(settled["x"] - 100) < 0.6 and abs(settled["y"] - 650) < 0.6


def test_a_scroll_is_captioned_in_a_corner_and_kept_out_of_screenshots(visible_page):
    session_id, session = visible_page
    driver = session.driver
    _act({"action": "scroll", "delta_y": 300, "session_id": session_id, "wait_seconds": 0})
    hud = driver.execute_script("""
      const h = document.querySelector('[data-wsn-presence="caption"]');
      return h ? {text: h.shadowRoot.textContent, display: getComputedStyle(h).display,
                  hidden: h.getAttribute('aria-hidden'), events: getComputedStyle(h).pointerEvents} : null;
    """)
    assert hud and "Scrolling ↓" in hud["text"] and "presence-motion" in hud["text"]
    assert hud == {**hud, "display": "block", "hidden": "true", "events": "none"}
    seen = {}
    real = driver.get_screenshot_as_png

    def capture():
        seen["display"] = driver.execute_script(
            "return getComputedStyle(document.querySelector('[data-wsn-presence=caption]')).display")
        return real()

    driver.get_screenshot_as_png = capture
    try:
        browser_tools.screenshot(session_id)
    finally:
        del driver.get_screenshot_as_png
    assert seen["display"] == "none"
    assert driver.execute_script(
        "return getComputedStyle(document.querySelector('[data-wsn-presence=caption]')).display") == "block"
    # The page readers never see the caption.
    assert "Scrolling" not in browser_tools.get_page_text(session_id).get("text", "")


def test_the_badge_animates_each_state_in_its_own_rhythm(visible_page):
    session_id, session = visible_page
    driver = session.driver
    read = "const s = window.__wsnPresence; return {phase: s.phase, pulse: !!s.pulse, shown: s.shown};"
    _act({"action": "pointer", "pointer_action": "click", "x": 600, "y": 325,
          "session_id": session_id, "include_summary": False})
    assert {k: v for k, v in driver.execute_script(read).items() if k != "shown"} == {
        "phase": "busy", "pulse": True}
    time.sleep(0.5)
    frames = driver.execute_script("return Object.keys(window.__wsnPresence.painted)")
    assert len([key for key in frames if key.startswith("agent/busy:")]) >= 2
    # Past the working seconds: still green, now breathing - a slower loop of its
    # own frames on the same single timer.
    time.sleep(presence_badge.BUSY_MS / 1000 + 2.5 * presence_badge.BREATHE_FRAME_MS / 1000)
    state = driver.execute_script(read)
    assert state["phase"] == "active" and state["pulse"] is True
    assert state["shown"].startswith("agent/active:")
    frames = driver.execute_script("return Object.keys(window.__wsnPresence.painted)")
    assert len([key for key in frames if key.startswith("agent/active:")]) >= 2


def test_reduced_motion_gets_a_still_ring_and_no_glide(visible_page):
    session_id, session = visible_page
    driver = session.driver
    driver.execute_cdp_cmd("Emulation.setEmulatedMedia",
                           {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
    _act({"action": "pointer", "pointer_action": "move", "x": 50, "y": 50,
          "session_id": session_id, "include_summary": False})
    _act({"action": "pointer", "pointer_action": "move", "x": 800, "y": 500,
          "session_id": session_id, "include_summary": False})
    time.sleep(0.05)
    cursor = driver.execute_script(_CURSOR)
    assert abs(cursor["x"] - 800) < 0.6 and abs(cursor["y"] - 500) < 0.6  # moved, not animated
    state = driver.execute_script("const s = window.__wsnPresence; return {phase: s.phase, pulse: !!s.pulse,"
                                  " shown: s.shown};")
    assert state["phase"] == "busy" and state["pulse"] is False and state["shown"] == "agent/busy:-1"
