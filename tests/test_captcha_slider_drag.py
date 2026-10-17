"""The slider drag now goes through the page, not just the DevTools pointer.

TCaptcha's slider ignores CDP Input.dispatchMouseEvent beyond a few pixels: a real
100 px CDP drag moved its groove only 22 px, while the same path dispatched as
in-page PointerEvents drove it the whole way. ``_slider_solve`` therefore runs the
trajectory through ``SLIDER_DRAG_SCRIPT`` when the widget gave a handle selector,
and only falls back to the CDP pointer when that dispatch did not happen. These
tests hold that wiring to its two branches without a browser.
"""

from __future__ import annotations

from types import SimpleNamespace

import web_search_neo.browser_tools as browser_tools


class _FakeSession:
    def __init__(self) -> None:
        self.lock = _NullLock()
        self.driver = object()


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc) -> bool:
        return False


def _install(monkeypatch, *, handle_selector, dragged):
    """Fake the four primitives _slider_solve reaches for, recording which drag ran."""
    seen = {"cdp": [], "in_page": [], "scripts": []}

    def fake_execute_js(script, args=None, session_id="default", **kwargs):
        seen["scripts"].append(script[:24])
        if "SLIDER" in script or "handleSelector" in script or "getBoundingClientRect" in script:
            pass
        if script is browser_tools._captcha_solvers.SLIDER_LOCATE_SCRIPT:
            return {"value": {
                "vendor": "tcaptcha", "live": True, "solved": False,
                "bg": [100.0, 100.0, 300.0, 180.0], "handle": [110.0, 150.0, 40.0, 40.0],
                "handle_selector": handle_selector, "viewport": [1000, 800], "dpr": 1,
            }}
        if script is browser_tools._captcha_solvers.SLIDER_DRAG_SCRIPT:
            seen["in_page"].append(args)
            return {"value": {"dragged": dragged}}
        if script is browser_tools._captcha_solvers.SLIDER_CLEARED_SCRIPT:
            return {"value": {"cleared": True, "vendor": "tcaptcha"}}
        return {"value": {}}

    def fake_pointer(session, action, x, y, viewport, frame_map=None):
        seen["cdp"].append((action, x, y))

    monkeypatch.setattr(browser_tools, "_get_session", lambda session_id: _FakeSession())
    monkeypatch.setattr(browser_tools, "execute_js", fake_execute_js)
    monkeypatch.setattr(browser_tools, "_pointer_dispatch", fake_pointer)
    monkeypatch.setattr(browser_tools, "_pointer_context", lambda driver, _ctx: ({}, {"width": 1000, "height": 800}))
    monkeypatch.setattr(browser_tools, "screenshot", lambda session_id="default": b"png")
    from web_search_neo.captcha_solvers import slider as _slider
    monkeypatch.setattr(_slider, "detect_gap", lambda picture, piece=None, **kwargs: {
        "ok": True, "distance": 150.0, "image_width": 300.0, "confidence": 0.9, "method": "notch",
    })
    monkeypatch.setattr(_slider, "crop_png", lambda shot, *a, **k: b"crop")
    monkeypatch.setattr(_slider, "image_size", lambda shot: (1000, 800))
    return seen


def test_the_drag_prefers_the_in_page_pointer_dispatch(monkeypatch):
    seen = _install(monkeypatch, handle_selector=".tencent-captcha-dy__slider-block", dragged=True)
    out = browser_tools._slider_solve("s", budget_seconds=10.0)
    assert out["solved"] is True
    # The in-page drag ran and the CDP pointer never touched the handle.
    assert len(seen["in_page"]) == 1
    assert seen["cdp"] == []


def test_the_drag_falls_back_to_cdp_when_the_page_drag_did_not_happen(monkeypatch):
    seen = _install(monkeypatch, handle_selector=".tencent-captcha-dy__slider-block", dragged=False)
    out = browser_tools._slider_solve("s", budget_seconds=10.0)
    # The in-page dispatch was tried, then the CDP press/move/release path ran.
    assert len(seen["in_page"]) == 1
    assert seen["cdp"] and seen["cdp"][0][0] == "press" and seen["cdp"][-1][0] == "release"


def test_the_drag_skips_the_page_dispatch_without_a_handle_selector(monkeypatch):
    seen = _install(monkeypatch, handle_selector=None, dragged=True)
    out = browser_tools._slider_solve("s", budget_seconds=10.0)
    # No selector means nothing to dispatch to: straight to the CDP pointer.
    assert seen["in_page"] == []
    assert seen["cdp"] and seen["cdp"][0][0] == "press"
