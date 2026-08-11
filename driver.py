"""Backward-compatible Selenium driver factory."""

from __future__ import annotations

import warnings

from selenium import webdriver

from browser_tools import create_driver


def get_driver(log=None) -> webdriver.Chrome:
    """Create Chrome through Selenium Manager (webdriver-manager is not required)."""
    warnings.warn(
        "driver.get_driver() is deprecated; use browser_tools sessions instead",
        DeprecationWarning,
        stacklevel=2,
    )
    if log is not None:
        log.debug("Starting headless Chrome through Selenium Manager")
    return create_driver()
