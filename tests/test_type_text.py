"""Canned-driver coverage for the type_text action.

One CDP Input.insertText per call instead of a per-key event storm, which is
what React controlled inputs need to see as a real edit. No Chrome here: each
driver records exactly what would cross the wire.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from selenium.webdriver.common.by import By

from web_search_neo import browser_tools
from web_search_neo import main
from web_search_neo.contract import notes as contract_notes


class _TypedElement:
    def __init__(self, tag="input"):
        self.tag_name = tag
        self.keys: list[str] = []

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def send_keys(self, *parts):
        self.keys.extend(parts)


class _TypeDriver:
    """Answers focus queries and locate requests with canned elements."""

    is_extension_bridge = False

    def __init__(self):
        self.focused = _TypedElement()
        self.located: dict[str, _TypedElement] = {}
        self.find_calls: list[tuple[str, str]] = []

    def execute_script(self, script, *args):
        if "document.activeElement" in script:
            return self.focused
        return {"url": "https://example.test/form", "title": "Form"}

    def find_element(self, by, selector):
        self.find_calls.append((by, selector))
        if (by, str(selector).strip()) not in {
            (By.CSS_SELECTOR, key) for key in self.located
        }:
            from selenium.common.exceptions import NoSuchElementException

            raise NoSuchElementException(str(selector))
        return self.located[str(selector).strip()]

    def quit(self):
        return None


def _register(driver: _TypeDriver, session_id: str):
    session = browser_tools.BrowserSession(driver=driver, headless=True)
    browser_tools._sessions[session_id] = session
    return session


def test_type_text_without_selector_types_into_focused_element():
    driver = _TypeDriver()
    _register(driver, "tt-focus")
    started = time.monotonic()
    result = browser_tools.type_text("hello", session_id="tt-focus")
    assert result["success"] is True
    assert result["typed_into"] == "focused_element"
    assert result["inserted"] == 5
    assert driver.focused.keys == ["hello"]
    # No locating happened: the focused element came from document.activeElement.
    assert driver.find_calls == []
    assert time.monotonic() - started < 5


def test_type_text_with_selector_locates_first():
    driver = _TypeDriver()
    target = _TypedElement()
    driver.located["#name"] = target
    _register(driver, "tt-sel")
    result = browser_tools.type_text(
        "Ada", session_id="tt-sel", selector="#name"
    )
    assert result["success"] is True
    assert result["typed_into"] == "#name"
    assert result["inserted"] == 3
    assert target.keys == ["Ada"]


def test_type_text_blank_selector_treats_the_focus_as_target():
    driver = _TypeDriver()
    _register(driver, "tt-blank")
    result = browser_tools.type_text(
        "x", session_id="tt-blank", selector="   "
    )
    assert result["typed_into"] == "focused_element"
    assert driver.focused.keys == ["x"]
    # A blank target must not spend ten seconds locating nothing.
    assert driver.find_calls == []


def test_type_text_refuses_empty_text():
    driver = _TypeDriver()
    _register(driver, "tt-empty")
    with pytest.raises(ValueError, match="non-empty"):
        browser_tools.type_text("", session_id="tt-empty")


def test_type_text_registered_in_contract():
    assert "type_text" in main._ACTIONS
    spec = main._ACTIONS["type_text"]
    assert spec.group == "page"
    schema = asyncio.run(main.web_info("action_schema", {"action": "type_text"}))
    props = set(schema["input_schema"]["properties"])
    assert props == {"action", "text", "session_id", "selector"}


def test_type_text_notes_document_the_single_insert_command():
    assert contract_notes._ACTION_NOTES["type_text"]
    wait_notes = contract_notes._ACTION_NOTES["wait"]
    assert "sleep" in wait_notes
