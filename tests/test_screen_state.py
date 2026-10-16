"""screen_state: what's on screen now, without a fresh full read.

page_text can miss a modal that arrived after the read, and a text search can
miss its buttons for the same reason. This topic re-reads the live DOM and
reports only the foreground - title, focused control, open modals with text
and buttons - so the next click targets what is actually up.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_search_neo import browser_tools, main, screen_state


def test_topic_is_wired_to_the_wrapper():
    assert main._TOPIC_HANDLERS["screen_state"] is main.browser_screen_state


def test_handler_reads_the_foreground(monkeypatch):
    seen = {}

    def fake_read(session_id="default"):
        seen["session_id"] = session_id
        return {"success": True, "session_id": session_id, "modals": []}

    monkeypatch.setattr(screen_state, "read", fake_read)
    result = asyncio.run(main.browser_screen_state(session_id="s1"))
    assert seen == {"session_id": "s1"}
    assert result["modals"] == []


def test_read_reports_modals_and_focus(monkeypatch):
    def fake_execute_js(script, session_id="default", **kwargs):
        assert "aria-modal" in script
        assert session_id == "s2"
        return {"success": True, "session_id": session_id,
                "value": {"title": "Chat", "url": "https://x.test/",
                          "focused": {"tag": "BUTTON", "role": None, "name": "Join"},
                          "modals": [{"role": "dialog", "name": "Invite",
                                      "text": "Join this group?", "buttons": ["Join"]}]}}

    monkeypatch.setattr(browser_tools, "execute_js", fake_execute_js)
    result = screen_state.read("s2")
    assert result["success"] is True
    assert result["title"] == "Chat"
    assert result["focused"] == {"tag": "BUTTON", "role": None, "name": "Join"}
    assert result["modals"][0]["buttons"] == ["Join"]


def test_read_passes_a_failed_run_through(monkeypatch):
    failure = {"success": False, "error": "no such session"}
    monkeypatch.setattr(
        browser_tools, "execute_js", lambda *args, **kwargs: failure
    )
    assert screen_state.read("gone") is failure


def test_read_tolerates_an_empty_page(monkeypatch):
    monkeypatch.setattr(
        browser_tools, "execute_js",
        lambda *args, **kwargs: {"success": True, "session_id": "s", "value": {}},
    )
    result = screen_state.read("s")
    assert result["modals"] == []
    assert result["focused"] is None
