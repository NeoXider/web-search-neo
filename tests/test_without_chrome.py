"""A machine without Chrome must still be a useful server.

Search, HTTP fetches, and the discovery topics never needed a browser. Only the
browser actions should fail, and they should say so in a way that tells the
caller what still works instead of leaking Selenium's own doubled message.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from selenium.common.exceptions import WebDriverException

import browser_tools
import main


class _OfflineBridge:
    """A bridge nothing ever connects to, as on a machine with no Chrome."""

    def status(self, wait_seconds: float = 0.0) -> dict:
        return {
            "connected": False,
            "host": "127.0.0.1",
            "port": 8765,
            "startup_error": None,
            "browser": {},
        }


@pytest.fixture
def no_chrome(monkeypatch):
    """Make driver creation fail the way a machine without Chrome fails."""
    monkeypatch.setattr(
        browser_tools.webdriver,
        "Chrome",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            WebDriverException("Message: unknown error: cannot find Chrome binary")
        ),
    )
    monkeypatch.setattr(browser_tools, "_browser_available", None, raising=False)
    monkeypatch.setattr(browser_tools, "_browser_error", None, raising=False)
    # Faking a missing Chrome binary is not enough: the companion route needs no
    # binary of ours at all. On a developer machine that really has the
    # extension installed, the live bridge answers this test process and Chrome
    # looks available again, so the second route has to be absent too.
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", _OfflineBridge)
    yield


def test_search_and_discovery_topics_do_not_need_a_browser(no_chrome):
    document = asyncio.run(main.web_info())
    assert document["public_tools"] == ["web_info", "web_action"]
    assert json.dumps(document)

    status = asyncio.run(main.web_info("search_status", {"check_live": False}))
    assert status["configured"]

    assert asyncio.run(main.web_info("time"))["year"] >= 2024


def test_fetch_actions_do_not_need_a_browser(no_chrome, local_site):
    result = asyncio.run(
        main.web_action(
            [
                {"action": "fetch_text", "url": f"{local_site.base_url}/page"},
                {"action": "fetch_links", "url": f"{local_site.base_url}/page"},
            ]
        )
    )
    assert result["success"] is True, result
    assert "Visible body text" in result["results"][0]["data"]
    assert result["results"][1]["data"]


def test_search_still_runs_without_a_browser(no_chrome, monkeypatch):
    import msp_search

    monkeypatch.setattr(msp_search, "ENGINE_ORDER", ["duckduckgo"])
    monkeypatch.setitem(
        msp_search.SEARCH_PROVIDERS,
        "duckduckgo",
        msp_search.FunctionSearchProvider(
            "duckduckgo",
            lambda *_args: [{"title": "T", "url": "https://example.test", "snippet": "s"}],
            "https://example.test/?q={query}",
        ),
    )
    result = asyncio.run(main.web_action([{"action": "search", "query": "anything"}]))
    assert result["success"] is True, result
    assert result["results"][0]["data"]["results"]


def test_opening_a_page_explains_the_missing_browser(no_chrome):
    result = asyncio.run(
        main.web_action(
            [
                {
                    "action": "open",
                    "url": "https://example.test",
                    "session_id": "no-chrome",
                    "profile_mode": "temporary",
                }
            ]
        )
    )
    error = result["results"][0]["error"]
    assert result["results"][0]["success"] is False
    assert "Chrome is unavailable" in error
    assert "not found on this machine" in error
    # The caller must learn what still works, not just that something broke.
    assert "search" in error and "fetch_text" in error
    # Selenium's raw text doubles its own prefix; that must not reach the caller.
    assert "Message: Message:" not in error


def test_browser_status_reports_the_reason_and_what_still_works(no_chrome):
    asyncio.run(
        main.web_action(
            [
                {
                    "action": "open",
                    "url": "https://example.test",
                    "session_id": "no-chrome-status",
                    "profile_mode": "temporary",
                }
            ]
        )
    )
    status = asyncio.run(main.web_info("browser_status"))
    assert status["available"] is False
    assert "Chrome is unavailable" in status["availability_error"]
    assert "do not" in status["next"]
    assert status["current_chrome"]["expected_version"]
