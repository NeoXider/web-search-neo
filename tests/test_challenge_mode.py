from __future__ import annotations

import asyncio

import pytest

from web_search_neo import main


CHALLENGE_RESPONSE = {
    "success": False,
    "query": "query",
    "requested_engine": "duckduckgo",
    "engine_used": None,
    "fallback_used": False,
    "cached": False,
    "elapsed_ms": 10,
    "results": [],
    "errors": {"duckduckgo": {"kind": "challenge"}},
    "challenge_recoveries": [
        {
            "provider": "duckduckgo",
            "browser_url": "https://duckduckgo.com/?q=query",
            "suggested_arguments": {
                "session_id": "search-duckduckgo",
                "headless": False,
            },
        }
    ],
}


def test_search_challenge_mode_defaults_to_immediate_fallback(monkeypatch):
    calls = []

    def fake_search(*args):
        calls.append(args)
        return {"success": True, "engine_used": "brave", "results": []}

    monkeypatch.setattr(main.msp_search, "search_web", fake_search)
    response = asyncio.run(main.search_web("query"))

    assert response["challenge_mode"] == "fallback"
    assert response["engine_used"] == "brave"
    assert calls[0][3] is True


def test_manual_challenge_returns_open_resolved_session(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main.msp_search,
        "search_web",
        lambda *args: calls.append(args) or dict(CHALLENGE_RESPONSE),
    )
    monkeypatch.setattr(
        main.browser_tools,
        "open_page",
        lambda *_args: {"session_id": "search-duckduckgo"},
    )
    monkeypatch.setattr(
        main.browser_tools,
        "wait_for_challenge_resolution",
        lambda *_args: {
            "resolved": True,
            "timed_out": False,
            "challenge_seen": True,
            "waited_seconds": 4.2,
            "url": "https://duckduckgo.com/?q=query",
            "title": "query at DuckDuckGo",
        },
    )

    response = asyncio.run(
        main.search_web("query", challenge_mode="manual", manual_timeout_seconds=180)
    )

    assert len(calls) == 1
    assert calls[0][3] is False
    assert response["success"] is True
    assert response["outcome"] == "manual_browser_ready"
    assert response["result_source"] == "browser_session"
    assert response["engine_used"] == "duckduckgo"
    assert response["manual_challenge"]["session_open"] is True
    assert response["manual_challenge"]["timeout_seconds"] == 180
    assert response["next_tools"] == ["web_info", "web_action"]
    assert response["next_calls"][0]["arguments"]["topic"] == "page_elements"


def test_manual_challenge_timeout_closes_window_and_falls_back(monkeypatch):
    search_calls = []
    close_calls = []

    def fake_search(*args):
        search_calls.append(args)
        if len(search_calls) == 1:
            return dict(CHALLENGE_RESPONSE)
        return {"success": True, "engine_used": "brave", "results": []}

    monkeypatch.setattr(main.msp_search, "search_web", fake_search)
    monkeypatch.setattr(
        main.browser_tools,
        "open_page",
        lambda *_args: {"session_id": "search-duckduckgo"},
    )
    monkeypatch.setattr(
        main.browser_tools,
        "wait_for_challenge_resolution",
        lambda *_args: {
            "resolved": False,
            "timed_out": True,
            "challenge_seen": True,
            "waited_seconds": 180.0,
            "url": "https://duckduckgo.com/?q=query",
            "title": "Challenge",
        },
    )
    monkeypatch.setattr(
        main.browser_tools,
        "close_session",
        lambda session_id: close_calls.append(session_id)
        or {"closed": True, "session_id": session_id},
    )

    response = asyncio.run(main.search_web("query", challenge_mode="manual"))

    assert [call[3] for call in search_calls] == [False, True]
    assert close_calls == ["search-duckduckgo"]
    assert response["success"] is True
    assert response["engine_used"] == "brave"
    assert response["outcome"] == "fallback_after_manual_timeout"
    assert response["manual_challenge"]["fallback_continued"] is True
    assert response["manual_challenge"]["session_open"] is False


def test_search_rejects_unknown_challenge_mode():
    with pytest.raises(ValueError, match="challenge_mode"):
        asyncio.run(main.search_web("query", challenge_mode="automatic"))
