"""The plain-words caption the page shows for each action.

What it may say is as important as what it says: a caption is on screen for
anyone looking at the tab, so typed text, filled values and URL paths must
never reach it.
"""

from __future__ import annotations

import pytest

from web_search_neo import agent_presence
from web_search_neo.presence_captions import caption_for, short


@pytest.mark.parametrize("action,arguments,expected", [
    ("click", {"selector": "#go"}, ("Click", True)),
    ("click", {"text": "Sign in"}, ("Click · Sign in", False)),
    ("click_text", {"text": "Continue"}, ("Click · Continue", False)),
    ("pointer", {"pointer_action": "hover"}, ("Hover", True)),
    ("pointer", {"pointer_action": "move"}, ("Move", False)),
    ("pointer", {"pointer_action": "click", "button": "right"}, ("Right-click", True)),
    ("pointer", {"pointer_action": "drag"}, ("Drag", True)),
    ("pointer", {"pointer_action": "wheel", "delta_y": -300}, ("Scrolling ↑", False)),
    ("scroll", {"delta_y": 400}, ("Scrolling ↓", False)),
    ("scroll", {"delta_y": 0, "delta_x": -50}, ("Scrolling ←", False)),
    ("type_text", {"text": "hunter2", "selector": "#password"}, ("Typing…", False)),
    ("fill", {"fields": {"#a": "x", "#b": "y"}}, ("Filling 2 fields", False)),
    ("fill", {"fields": {"#a": "x"}}, ("Filling a field", False)),
    ("upload", {"file_paths": ["a", "b", "c"]}, ("Uploading 3 files", False)),
    ("wait", {"selector": "#results"}, ("Waiting for #results", False)),
    ("wait", {"selector": ".spinner", "state": "hidden"}, ("Waiting for .spinner to go away", False)),
    ("wait", {"seconds": 2}, ("Waiting 2s", False)),
    ("wait", {"script": "return 1"}, ("Waiting for the page", False)),
    ("open", {"url": "https://example.com/account?token=secret"}, ("Opening example.com", False)),
    ("press_keys", {"keys": ["ENTER"]}, ("Keys · Enter", False)),
    ("press_keys", {"keys": ["Control+Shift+K"]}, ("Keys · Control+Shift+K", False)),
    ("press_keys", {"keys": ["CONTROL", "a"]}, ("Keys · Control+A", False)),
    ("touch", {"touch_action": "swipe"}, ("Swipe", False)),
    ("look", {}, ("Reading the page", False)),
    ("perf_report", {}, ("Perf report", False)),
])
def test_each_action_reads_as_plain_words(action, arguments, expected):
    assert caption_for(action, arguments) == expected


def test_typed_characters_never_reach_the_screen():
    # A lone character pressed as a key is typing - and could be a password.
    assert caption_for("press_keys", {"keys": ["p"]}) == ("Pressing keys", False)
    assert caption_for("press_keys", {"keys": ["s", "e", "c"]}) == ("Pressing keys", False)
    assert "hunter2" not in str(caption_for("type_text", {"text": "hunter2"}))
    assert "secret" not in str(caption_for("fill", {"fields": {"#p": "secret"}}))
    assert "token" not in str(caption_for("navigate", {"url": "https://a.test/x?token=1"}))


def test_bookkeeping_says_nothing():
    for action in ("close", "close_all", "step", "render", "attach_tab", "stealth", ""):
        assert caption_for(action, {}) == (None, False)


def test_long_details_are_cut_with_an_ellipsis():
    assert short("a" * 100, 10) == "a" * 9 + "…"
    assert short("  two\n words ") == "two words"
    text, _named = caption_for("click_text", {"text": "x" * 200})
    assert len(text) <= len("Click · ") + 32 and text.endswith("…")


def test_the_payload_carries_the_caption_and_asks_for_a_name_only_when_useful():
    click = agent_presence.payload_for("click", {"selector": "#go"})
    assert click["caption"] == "Click" and click["captionTarget"] is True
    typed = agent_presence.payload_for("type_text", {"text": "secret", "selector": "#q"})
    assert typed["caption"] == "Typing…" and "captionTarget" not in typed
    assert "secret" not in str(typed)
    assert "caption" not in agent_presence.payload_for("close", {})
