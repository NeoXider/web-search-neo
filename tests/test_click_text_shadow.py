"""click_text reaches open shadow roots and same-origin frames, and stays strict.

The fixture holds a button in an open shadow root, one two shadow roots deep, one
whose label is projected through a slot, one in a same-origin (srcdoc) frame, one
in a shadow root inside that frame, duplicates across those documents, a button
under an aria-hidden host, and a cross-origin frame (localhost against 127.0.0.1).
Every click is a real CDP mouse event aimed at the reported point, so the page's
own listener firing proves the point - frame border, padding and offset included.

The companion (current Chrome) path is exercised offline: a ChromeBridgeDriver is
driven through a bridge that hands its ``cdp.send`` calls to the same headless
Chrome, so the bridge's own script wrapper and input parameters are what run.
"""

from __future__ import annotations

import time

import pytest

from web_search_neo import browser_tools
from web_search_neo.chrome_bridge import ChromeBridgeDriver

SESSION = "shadow-click"


def _open_or_skip(local_site, session_id: str = SESSION):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(
            f"{local_site.base_url}/fixtures/perception/shadow_click.html",
            session_id=session_id, width=1024, height=768, headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    driver = browser_tools._get_session(session_id).driver
    deadline = time.time() + 10
    ready = "const d = document.getElementById('frame').contentDocument;" \
            "return !!(d && d.getElementById('frame-host') && d.getElementById('frame-host').shadowRoot);"
    while not driver.execute_script(ready):
        assert time.time() < deadline, "the srcdoc frame never loaded"
        time.sleep(0.1)
    return driver


def _clicks(driver) -> list[str]:
    return driver.execute_script("return window.wsnClicks.slice()")


@pytest.fixture
def page(local_site):
    # conftest closes every session around each test, so each test opens its own.
    return _open_or_skip(local_site)


TARGETS = [
    # text, logged click, frame, shadow_path, matched_selector
    ("Shadow save", "shadow-save", None, "#host >>> #shadow-save", "#host >>> #shadow-save"),
    ("Nested shadow save", "nested-save", None,
     "#outer-host >>> #inner-host >>> #nested-save", "#outer-host >>> #inner-host >>> #nested-save"),
    ("Projected go", "projected", None, "#projected >>> #projected-inner", "#projected >>> #projected-inner"),
    ("Frame save", "frame-save", "#frame", None, "#frame >>> #frame-save"),
    ("Frame shadow save", "frame-shadow-save", "#frame",
     "#frame-host >>> #frame-shadow-save", "#frame >>> #frame-host >>> #frame-shadow-save"),
]


@pytest.mark.parametrize(("text", "logged", "frame", "shadow_path", "selector"), TARGETS)
def test_click_text_clicks_inside_shadow_roots_and_same_origin_frames(
    page, text, logged, frame, shadow_path, selector
):
    before = len(_clicks(page))
    answer = browser_tools.click_text(text, session_id=SESSION, role="button", wait_seconds=0)

    assert _clicks(page)[before:] == [logged], answer
    assert answer["success"] is True
    assert answer["matched_text"] == text
    assert answer["matched_selector"] == selector
    assert answer["frame"] == frame
    assert answer["shadow_path"] == shadow_path
    # The cross-origin frame could not be searched, and the answer says so.
    assert answer["cross_origin_frames"] == 1
    assert "cross-origin" in answer["frames_note"]


def test_a_reported_selector_is_one_find_and_click_accept_back(page):
    answer = browser_tools.click_text("Frame shadow save", session_id=SESSION, wait_seconds=0)
    before = len(_clicks(page))
    browser_tools.click(answer["matched_selector"], session_id=SESSION, wait_seconds=0)
    assert _clicks(page)[before:] == ["frame-shadow-save"]


def test_duplicates_across_documents_are_refused_with_the_count(page):
    before = len(_clicks(page))
    with pytest.raises(ValueError, match="found 2") as twin:
        browser_tools.click_text("Twin", session_id=SESSION, wait_seconds=0)
    assert "#host >>> #shadow-twin" in str(twin.value)
    assert "#top-twin" in str(twin.value)

    with pytest.raises(ValueError, match="found 2") as echo:
        browser_tools.click_text("Echo", session_id=SESSION, wait_seconds=0)
    assert "'frame': '#frame'" in str(echo.value)
    assert "#host >>> #shadow-echo" in str(echo.value)
    assert _clicks(page)[before:] == []

    narrowed = browser_tools.click_text(
        "Twin", session_id=SESSION, selector="#shadow-twin", wait_seconds=0
    )
    assert narrowed["shadow_path"] == "#host >>> #shadow-twin"
    assert _clicks(page)[before:] == ["shadow-twin"]


def test_hidden_hosts_and_cross_origin_frames_are_out_of_reach_honestly(page):
    with pytest.raises(ValueError, match="found 0"):
        browser_tools.click_text("Hidden save", session_id=SESSION, wait_seconds=0)

    with pytest.raises(ValueError, match="found 0") as remote:
        browser_tools.click_text("Remote save", session_id=SESSION, wait_seconds=0)
    assert "1 cross-origin frame(s) were not searched" in str(remote.value)
    assert "frame_selector" in str(remote.value)

    # Named explicitly, the cross-origin frame is searched from inside.
    answer = browser_tools.click_text(
        "Remote save", session_id=SESSION, frame_selector="#remote", wait_seconds=0
    )
    assert answer["frame"] == "#remote"
    page.switch_to.frame(page.find_element("css selector", "#remote"))
    try:
        assert page.execute_script("return document.getElementById('remote-log').textContent") == "remote-save"
    finally:
        page.switch_to.default_content()


def test_an_overlay_above_a_frame_is_named_not_clicked(page):
    page.execute_script(
        "const r = document.getElementById('frame').getBoundingClientRect();"
        "const cover = document.createElement('div'); cover.id = 'cover';"
        "Object.assign(cover.style, {position: 'fixed', left: r.left + 'px', top: r.top + 'px',"
        " width: r.width + 'px', height: r.height + 'px', background: 'rgba(0,0,0,.2)'});"
        "document.body.appendChild(cover);"
        "document.getElementById('frame').scrollIntoView({block: 'center'});"
        "const q = document.getElementById('frame').getBoundingClientRect();"
        "cover.style.left = q.left + 'px'; cover.style.top = q.top + 'px';"
    )
    before = len(_clicks(page))
    try:
        with pytest.raises(ValueError, match="covered by"):
            browser_tools.click_text("Frame save", session_id=SESSION, wait_seconds=0)
        assert _clicks(page)[before:] == []
    finally:
        page.execute_script("document.getElementById('cover').remove()")


class _SameChromeBridge:
    """The companion bridge's calls, answered by the Selenium-driven Chrome."""

    def __init__(self, selenium_driver) -> None:
        self.selenium = selenium_driver
        self.methods: list[str] = []

    def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
        params = params or {}
        if method == "tabs.create":
            return {"id": 7, "url": "about:blank", "title": "", "group": params.get("group")}
        if method == "events.subscribe":
            return {"started_at": 0, "seq": 0, "domains": params.get("domains", [])}
        if method == "tabs.get":
            return {"id": 7, "url": self.selenium.current_url, "title": self.selenium.title}
        if method == "frames.resolve":
            return {"sessionId": None, "sameOrigin": True}
        if method == "cdp.send":
            self.methods.append(params["method"])
            return self.selenium.execute_cdp_cmd(params["method"], params.get("params") or {})
        raise AssertionError(f"unexpected bridge call {method}")


def test_the_companion_path_walks_and_clicks_the_same_way(local_site, monkeypatch):
    driver = _open_or_skip(local_site, "shadow-click-bridge")
    try:
        bridge = _SameChromeBridge(driver)
        companion = ChromeBridgeDriver(bridge=bridge)
        session = browser_tools._get_session("shadow-click-bridge")
        monkeypatch.setattr(session, "driver", companion)

        answer = browser_tools.click_text(
            "Frame shadow save", session_id="shadow-click-bridge", wait_seconds=0
        )
        assert answer["frame"] == "#frame"
        assert answer["shadow_path"] == "#frame-host >>> #frame-shadow-save"
        nested = browser_tools.click_text(
            "Nested shadow save", session_id="shadow-click-bridge", wait_seconds=0
        )
        assert nested["shadow_path"] == "#outer-host >>> #inner-host >>> #nested-save"
        with pytest.raises(ValueError, match="found 2"):
            browser_tools.click_text("Echo", session_id="shadow-click-bridge", wait_seconds=0)
        # A same-origin frame named explicitly runs the matcher under the bridge's
        # frame rebinding, and the point is still carried out to the page.
        framed = browser_tools.click_text(
            "Frame save", session_id="shadow-click-bridge", frame_selector="#frame", wait_seconds=0
        )
        assert framed["frame"] == "#frame"

        assert "Input.dispatchMouseEvent" in bridge.methods
        assert _clicks(driver)[-3:] == ["frame-shadow-save", "nested-save", "frame-save"]
    finally:
        monkeypatch.undo()
        browser_tools.close_session(session_id="shadow-click-bridge")
