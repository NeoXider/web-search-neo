"""Verify hidden Selenium launch paths without creating any child process."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from web_search_neo import browser_tools


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch flags")
@pytest.mark.parametrize("cached,retry", [(False, False), (True, False), (True, True)])
def test_every_driver_path_supplies_a_hidden_service(monkeypatch, cached, retry):
    services, launches = [], []
    monkeypatch.setattr(browser_tools, "_latest_cached_chromedriver",
                        lambda: Path("cached-driver.exe") if cached else None)

    def service(**kwargs):
        services.append(kwargs)
        return SimpleNamespace(options=kwargs)

    driver = SimpleNamespace(execute_script=lambda *_: "Chrome/153",
                             set_page_load_timeout=lambda *_: None,
                             set_script_timeout=lambda *_: None)

    def chrome(*, service, options):
        launches.append(service)
        if retry and len(launches) == 1:
            raise RuntimeError("Cached driver is incompatible")
        return driver

    monkeypatch.setattr(browser_tools, "Service", service)
    monkeypatch.setattr(browser_tools.webdriver, "Chrome", chrome)
    assert browser_tools.create_driver(headless=True, profile_mode="temporary") is driver
    assert len(services) == (2 if retry else 1)
    assert services[0]["executable_path"] == ("cached-driver.exe" if cached else None)
    for options in services:
        assert options["popen_kw"] == {"creation_flags": 0x08000000}
    if retry:
        assert "executable_path" not in services[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch flags")
def test_real_selenium_service_has_one_startupinfo_argument(monkeypatch):
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common import service as service_module

    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=999)

    monkeypatch.setattr(service_module.subprocess, "Popen", popen)
    service = Service(executable_path="fixture-driver.exe",
                      popen_kw=browser_tools._driver_popen_kwargs())
    try:
        service._start_process("fixture-driver.exe")
        assert len(calls) == 1
        assert calls[0][1]["creationflags"] & 0x08000000
        assert calls[0][1]["startupinfo"].wShowWindow == 0
    finally:
        # No real process exists; suppress Service's ordinary process cleanup.
        service.process = None
