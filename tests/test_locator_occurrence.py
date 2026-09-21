"""Occurrence selectors: ``css[N]`` picks the Nth match, never silently the first.

An ambiguous CSS match used to fill or click whichever control came first. The
trailing ``[N]`` (0-based document order) makes the choice explicit, and an N
past the match count is refused naming how many matched.
"""

from __future__ import annotations

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from web_search_neo import browser_tools
from web_search_neo.chrome_bridge import ChromeBridgeElement


@pytest.mark.parametrize(
    ("locator", "css", "index"),
    [
        ("input.qty[1]", "input.qty", 1),
        ("input.qty[0]", "input.qty", 0),
        ("#form input[12]", "#form input", 12),
        ("#email", "#email", None),
        ("input", "input", None),
        ("div:nth-child(2)", "div:nth-child(2)", None),
        ('input[value="[0]"]', 'input[value="[0]"]', None),
        ("input[", "input[", None),
    ],
)
def test_split_occurrence(locator, css, index):
    assert browser_tools._split_occurrence(locator) == (css, index)


class _OccElement:
    def __init__(self, ready=True):
        self.ready = ready

    def is_displayed(self):
        return self.ready

    def is_enabled(self):
        return self.ready


class _OccDriver:
    """Selenium-shaped: occurrence resolves through find_elements."""

    is_extension_bridge = False

    def __init__(self, count):
        self.elements = [_OccElement() for _ in range(count)]
        self.found_calls: list = []

    def find_elements(self, by, selector):
        self.found_calls.append((by, selector))
        return list(self.elements)

    def execute_script(self, script, *args):
        if "querySelectorAll" in script:
            return len(self.elements)
        return None


class _OccBridgeDriver:
    """Companion-shaped: no find_elements, only script probes."""

    is_extension_bridge = True

    def __init__(self, count):
        self.count = count

    def execute_script(self, script, *args):
        if "querySelectorAll" in script:
            return self.count
        return None


def test_nth_element_returns_the_indexed_match():
    driver = _OccDriver(3)
    element = browser_tools._nth_element(driver, "input.qty", 1)
    assert element is driver.elements[1]
    assert driver.found_calls == [(By.CSS_SELECTOR, "input.qty")]


def test_nth_element_out_of_range_names_the_count():
    driver = _OccDriver(2)
    with pytest.raises(ValueError, match=r"'input\.qty\[5\]' matches 2 element"):
        browser_tools._nth_element(driver, "input.qty", 5)


def test_resolve_element_routes_occurrence_to_nth_match():
    driver = _OccDriver(3)
    assert browser_tools._resolve_element(driver, "input.qty[2]") is driver.elements[2]


def test_resolve_element_without_suffix_still_takes_the_first():
    class _SingleDriver(_OccDriver):
        def find_element(self, by, selector):
            return self.elements[0]

    driver = _SingleDriver(3)
    assert browser_tools._resolve_element(driver, "input.qty") is driver.elements[0]


def test_bridge_nth_element_carries_selector_and_index():
    driver = _OccBridgeDriver(4)
    element = browser_tools._nth_element(driver, "input.qty", 2)
    assert isinstance(element, ChromeBridgeElement)
    assert element.selector == "input.qty"
    assert element.index == 2


def test_bridge_nth_element_out_of_range_names_the_count():
    driver = _OccBridgeDriver(1)
    with pytest.raises(ValueError, match=r"matches 1 element"):
        browser_tools._nth_element(driver, "input.qty", 3)


def test_wait_for_locator_occurrence_returns_when_ready():
    driver = _OccDriver(2)
    element = browser_tools._wait_for_locator(driver, "input.qty[1]", "clickable", 2.0)
    assert element is driver.elements[1]


def test_wait_for_locator_occurrence_times_out_naming_the_suffix():
    driver = _OccDriver(0)
    with pytest.raises(TimeoutException, match=r"input\.qty\[3\]"):
        browser_tools._wait_for_locator(driver, "input.qty[3]", "clickable", 0.3)
