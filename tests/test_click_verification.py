"""click() reports whether the page observably moved after the click.

A click that returns success:true while the URL and title sit still is the
oldest automation lie there is - the report that motivated this file. These
tests pin the pre/post comparison without a browser.
"""

from __future__ import annotations

import pytest

from web_search_neo import browser_tools


class _ClickedElement:
    def __init__(self, driver):
        self.driver = driver
        self.clicks = 0

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.clicks += 1
        if self.driver.navigates_to is not None:
            self.driver.state.update(self.driver.navigates_to)


class _ClickDriver:
    """Serves the pre-click probe and the presence rect from mutable state."""

    def __init__(self, url="https://example.test/a", title="A", navigates_to=None):
        self.state = {"url": url, "title": title}
        self.navigates_to = navigates_to
        self.element = _ClickedElement(self)
        self.fail_pre_probe = False

    def execute_script(self, script, *args):
        if "location.href" in script:
            if self.fail_pre_probe:
                raise RuntimeError("probe failed")
            return dict(self.state)
        if "getBoundingClientRect" in script:
            return {"x": 10.0, "y": 20.0, "top": True}
        return None

    def find_element(self, by, selector):
        return self.element

    def quit(self):
        return None


def _register(driver, session_id):
    session = browser_tools.BrowserSession(driver=driver, headless=True)
    browser_tools._sessions[session_id] = session
    return session


def _summary_from(driver):
    def _summary(_driver, session_id):
        return {"session_id": session_id, **driver.state}

    return _summary


def test_click_reports_page_changed_on_navigation(monkeypatch):
    driver = _ClickDriver(
        navigates_to={"url": "https://example.test/b", "title": "B"}
    )
    _register(driver, "click-nav")
    monkeypatch.setattr(browser_tools, "_page_summary", _summary_from(driver))

    result = browser_tools.click("#go", session_id="click-nav", wait_seconds=0)

    assert result["success"] is True
    assert driver.element.clicks == 1
    assert result["page_changed"] is True
    assert "no_observable_change" not in result


def test_click_flags_no_observable_change_when_page_sits_still(monkeypatch):
    driver = _ClickDriver()
    _register(driver, "click-still")
    monkeypatch.setattr(browser_tools, "_page_summary", _summary_from(driver))

    result = browser_tools.click("#menu", session_id="click-still", wait_seconds=0)

    assert result["success"] is True
    assert driver.element.clicks == 1
    assert result["page_changed"] is False
    assert result["no_observable_change"] is True
    assert "page_text/page_elements" in result["change_note"]


def test_click_reports_unknown_change_when_pre_probe_fails(monkeypatch):
    driver = _ClickDriver()
    driver.fail_pre_probe = True
    _register(driver, "click-unknown")
    monkeypatch.setattr(browser_tools, "_page_summary", _summary_from(driver))

    result = browser_tools.click("#go", session_id="click-unknown", wait_seconds=0)

    assert result["success"] is True
    assert result["page_changed"] is None
    assert "no_observable_change" not in result
