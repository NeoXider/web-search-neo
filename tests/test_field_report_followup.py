"""Regression coverage for the 1.18.4 follow-up to the field report.

Tab following after Chrome replaces a tab (and the tabs it must never take),
persistent (parked) sessions and every way re-attaching them is refused,
verified clicks, type_text focus rules and keys mode (canvas games, Cyrillic),
the execute_js result contract, cookie paging and filtered clears, the session-
cap roster and orphan release, wait seconds, the screenshot action, action
aliases, stable locator hints, title_pending, SPA shells, and the companion's
code hash. Most drivers here are fakes that record what would cross the wire;
the focus, click-probe, locator and React checks at the end run in real Chrome.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest

from web_search_neo import browser_tools, chrome_bootstrap, main
from web_search_neo.actions import scripts as script_actions
from web_search_neo.actions import verification
from web_search_neo.bridge_daemon import BridgeDaemon, _Route
from web_search_neo.cdp import tab_follow
from web_search_neo.chrome_bridge import ChromeBridge, ChromeBridgeError
from web_search_neo.fetch import content as fetch_content
from web_search_neo.sessions import parking

RUN = "run-1"
GROUP = browser_tools.DEFAULT_TAB_GROUP


# ---------------------------------------------------------------- fakes


class _SwitchTo:
    def default_content(self) -> None:
        return None


class _Companion:
    """A companion that knows which tabs exist, where they are, and what replaced what."""

    def __init__(self, alive=(), replaced=None, tabs=None, resolve=True):
        self.alive = set(alive)
        self.replaced = dict(replaced or {})
        self.tabs = dict(tabs or {})  # id -> {"group", "url"}
        self.resolve = resolve
        self.calls: list[tuple[str, dict]] = []
        self.followed: dict[int, int] = {}

    def request(self, method, params=None, **_kwargs):
        params = params or {}
        self.calls.append((method, params))
        tab = params.get("tabId")
        if method == "tabs.get":
            if tab in self.alive:
                return {"id": tab, **self.tabs.get(tab, {"group": GROUP, "url": "https://example.test/"})}
            raise ChromeBridgeError(f"No tab with id {tab}")
        if method == "tabs.resolve":
            if not self.resolve:
                raise ChromeBridgeError("Unknown bridge method: tabs.resolve")
            successor = self.replaced.get(tab)
            return {"tabId": tab, "alive": tab in self.alive, "replaced_by": successor,
                    "successor_alive": None if successor is None else successor in self.alive}
        if method == "tabs.remove":
            self.alive.discard(tab)
            return {"removed": True, "id": tab}
        return {}

    def take_followed(self, tab_id):
        return self.followed.pop(tab_id, None)


class _Tab:
    def __init__(self, tab_id=42, bridge=None):
        self.tab_id = tab_id
        self.bridge = bridge or _Companion(alive={tab_id})
        self.actual_tab_group = GROUP
        self.is_extension_bridge = True
        self.switch_to = _SwitchTo()
        self.calls: list[str] = []
        self.title = "Page"
        self.url = "https://example.test/"

    def _start_capture(self):
        self.calls.append(f"capture {self.tab_id}")

    def get(self, url):
        self.calls.append(f"get {url}")

    def quit(self):
        self.calls.append("quit")
        return {"detached": True, "id": self.tab_id}

    def close_tab(self):
        self.calls.append("close_tab")
        return {"removed": True}

    def execute_script(self, script, *_args):
        if script.strip() == "return document.readyState":
            return "complete"
        return {"url": self.url, "title": self.title}

    def execute_cdp_cmd(self, *_args, **_kwargs):
        return {}


def _register(session_id, driver, **overrides):
    fields = {
        "driver": driver, "headless": False, "profile_mode": "current",
        "current_tab_id": driver.tab_id, "owns_browser": True, "owns_tab": True,
        "browser_run": RUN, "tab_group": GROUP,
    }
    fields.update(overrides)
    session = browser_tools.BrowserSession(**fields)
    browser_tools._sessions[session_id] = session
    return session


@pytest.fixture
def companion(monkeypatch):
    """The user's Chrome as the parked-session checks see it."""
    fake = _Companion()
    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    monkeypatch.setattr(browser_tools, "_claim_tab", lambda tab_id: {})
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", lambda tab_id: None)
    monkeypatch.setattr(browser_tools, "_current_browser_run", lambda: RUN)
    for name in [record["session_id"] for record in parking.list_records()]:
        parking.forget(name)
    parking.take_expired()
    yield
    for name in [record["session_id"] for record in parking.list_records()]:
        parking.forget(name)
    parking.take_expired()


# ------------------------------------------------ 2. tab replaced by Chrome


def test_successor_comes_only_from_chromes_replacement_record():
    assert tab_follow.find_successor(_Companion(alive={77}, replaced={42: 77}), 42) == 77
    # A recorded successor that is itself gone is no successor.
    assert tab_follow.find_successor(_Companion(alive=set(), replaced={42: 77}), 42) is None
    # An older companion without tabs.resolve: nothing is guessed.
    assert tab_follow.find_successor(_Companion(alive={77}, replaced={42: 77}, resolve=False), 42) is None


def test_a_child_tab_of_the_lost_tab_is_never_taken_over():
    # The agent's tab closed after opening a payment popup (tab 51) and the user
    # has tab 60 on the same URL: neither is a replacement, so the session drops.
    bridge = _Companion(alive={51, 60}, tabs={51: {"group": GROUP, "url": "https://pay.test/"}})
    driver = _Tab(42, bridge)
    session = _register("lost", driver)
    session.last_url = "https://pay.test/"
    translated = browser_tools.translate_stale_tab_error("lost", ChromeBridgeError("No tab with given id 42"))
    assert isinstance(translated, ValueError) and "lost its tab" in str(translated)
    assert "lost" not in browser_tools._sessions and driver.tab_id == 42


def test_a_replaced_tab_is_followed_and_keeps_its_ownership():
    bridge = _Companion(alive={77}, replaced={42: 77})
    driver = _Tab(42, bridge)
    session = _register("follow", driver)

    translated = browser_tools.translate_stale_tab_error(
        "follow", ChromeBridgeError("No tab with given id 42")
    )

    assert isinstance(translated, browser_tools.SessionTabFollowed)
    assert translated.new_tab == 77 and "Chrome itself replaced it" in str(translated)
    # The same page in a new tab object: the session still owns what it opened.
    assert driver.tab_id == 77 and session.current_tab_id == 77 and session.owns_tab is True
    assert session.pending_notice == {"tab_followed": {"from": 42, "to": 77}}

def test_a_successor_held_elsewhere_drops_the_session(monkeypatch):
    bridge = _Companion(alive={77}, replaced={42: 77})
    _register("other", _Tab(77, bridge))
    _register("follow-dup", _Tab(42, bridge))
    translated = browser_tools.translate_stale_tab_error(
        "follow-dup", ChromeBridgeError("No tab with given id 42")
    )
    assert isinstance(translated, ValueError) and "driven by another session" in str(translated)
    assert "follow-dup" not in browser_tools._sessions

    def refused(tab_id):
        raise RuntimeError(f"Chrome tab {tab_id} is already being driven by another agent.")

    monkeypatch.setattr(browser_tools, "_claim_tab", refused)
    bridge2 = _Companion(alive={88}, replaced={43: 88})
    driver = _Tab(43, bridge2)
    _register("claimed-away", driver)
    translated = browser_tools.translate_stale_tab_error(
        "claimed-away", ChromeBridgeError("No tab with given id 43")
    )
    assert isinstance(translated, ValueError) and "could not be claimed" in str(translated)
    assert "claimed-away" not in browser_tools._sessions and driver.tab_id == 43
    # The same refusal on the companion's own report fails the next call loudly.
    driver2 = _Tab(44, _Companion(alive={99}))
    _register("reported-away", driver2)
    driver2.bridge.followed[44] = 99
    with pytest.raises(ValueError, match="could not be claimed"):
        browser_tools._get_session("reported-away")
    assert "reported-away" not in browser_tools._sessions

def test_a_redirect_the_companion_reported_moves_the_session_on_the_next_call():
    bridge = _Companion(alive={77})
    driver = _Tab(42, bridge)
    session = _register("reported", driver)
    bridge.followed[42] = 77
    assert browser_tools._get_session("reported") is session
    assert session.current_tab_id == 77 and driver.tab_id == 77 and session.owns_tab is True


def test_the_bridge_client_records_tab_followed_from_an_answer():
    client = ChromeBridge.__new__(ChromeBridge)
    client._followed = {}
    import threading
    client._state_lock = threading.Lock()
    client._note_followed({"from": 42, "to": 77})
    assert client.take_followed(42) == 77 and client.take_followed(42) is None


def test_the_daemon_relays_tab_followed_to_the_client():
    sent = []

    class _Client:
        def send_quietly(self, frame):
            sent.append(frame)

    daemon = BridgeDaemon.__new__(BridgeDaemon)
    import threading
    daemon._lock = threading.Lock()
    socket = object()
    daemon._routes = {"r1": _Route(client=_Client(), client_id="c1", extension=socket)}
    daemon._deliver_result(socket, {"id": "r1", "result": {"ok": 1}, "tab_followed": {"from": 1, "to": 2}})
    assert sent == [{"type": "result", "id": "c1", "result": {"ok": 1}, "tab_followed": {"from": 1, "to": 2}}]


def test_a_read_topic_is_answered_from_the_followed_tab(monkeypatch):
    bridge = _Companion(alive={77}, replaced={42: 77})
    driver = _Tab(42, bridge)
    _register("follow-read", driver)
    calls = {"n": 0}

    def flaky_text(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChromeBridgeError("No tab with given id 42")
        return {"text": "hello", "tab": driver.tab_id}

    monkeypatch.setattr(browser_tools, "get_page_text", flaky_text)
    answer = asyncio.run(main.web_info("page_text", {"session_id": "follow-read"}))
    assert answer["text"] == "hello" and answer["tab"] == 77 and calls["n"] == 2


def test_execute_js_and_writes_are_never_repeated_after_following(monkeypatch):
    bridge = _Companion(alive={77}, replaced={42: 77})
    _register("follow-js", _Tab(42, bridge))
    runs = {"js": 0, "reload": 0}

    def run_js(*_args, **_kwargs):
        runs["js"] += 1
        raise ChromeBridgeError("No tab with given id 42")

    monkeypatch.setattr(browser_tools, "execute_js", run_js)
    with pytest.raises(browser_tools.SessionTabFollowed):
        asyncio.run(main.web_info("execute_js", {"script": "return 1;", "session_id": "follow-js"}))
    assert runs["js"] == 1

    bridge2 = _Companion(alive={88}, replaced={43: 88})
    _register("follow-reload", _Tab(43, bridge2))

    def reload(*_args, **_kwargs):
        runs["reload"] += 1
        raise ChromeBridgeError("No tab with given id 43")

    monkeypatch.setattr(browser_tools, "reload_page", reload)
    result = asyncio.run(main.web_action([{"action": "reload", "session_id": "follow-reload"}]))
    assert runs["reload"] == 1 and "SessionTabFollowed" in result["results"][0]["error"]


def test_a_second_dead_tab_after_the_repeat_is_translated_too(monkeypatch):
    bridge = _Companion(alive={77}, replaced={42: 77})
    driver = _Tab(42, bridge)
    _register("twice", driver)

    def always_dead(**_kwargs):
        raise ChromeBridgeError(f"No tab with given id {driver.tab_id}")

    monkeypatch.setattr(browser_tools, "get_page_text", always_dead)
    bridge.alive.discard(77)  # the successor dies before the repeat lands
    original_find = tab_follow.find_successor
    monkeypatch.setattr(browser_tools, "_find_successor_tab",
                        lambda b, t: 77 if t == 42 else original_find(b, t))
    with pytest.raises(ValueError, match="lost its tab"):
        asyncio.run(main.web_info("page_text", {"session_id": "twice"}))


# ---------------------------------------------- B. persistent sessions


def _park(session_id="keep", tab_id=42, **record):
    stored = {"tab_id": tab_id, "browser_run": RUN, "agent_label": "bot", "owns_tab": True,
              "tab_group": GROUP, "label_tab": True, "url": "https://example.test/work"}
    stored.update(record)
    return parking.remember(session_id, stored)


def test_parking_registry_round_trip_and_hardened_expiry(monkeypatch):
    parking.remember("p1", {"tab_id": 5})
    assert parking.lookup("p1")["tab_id"] == 5
    assert parking.forget("p1") is True and parking.lookup("p1") is None
    path = parking.registry_path()
    now = time.time()
    path.write_text(json.dumps({
        "old": {"tab_id": 1, "updated_at": now - 10 * 86400},
        "future": {"tab_id": 2, "updated_at": now + 86400},
        "garbage": {"tab_id": 3, "updated_at": "yesterday"},
        "fresh": {"tab_id": 4, "updated_at": now},
    }), encoding="utf-8")
    assert [r["session_id"] for r in parking.list_records()] == ["fresh"]
    assert sorted(r["session_id"] for r in parking.take_expired()) == ["future", "garbage", "old"]
    assert parking.take_expired() == []
    for value in ("0", "-5", "nan", "junk"):
        monkeypatch.setenv("WEB_SEARCH_NEO_PARKED_SESSION_TTL", value)
        assert parking.ttl_seconds() == parking.DEFAULT_PARKED_TTL_SECONDS
    monkeypatch.setenv("WEB_SEARCH_NEO_PARKED_SESSION_TTL", "1")
    assert parking.ttl_seconds() == parking.MIN_PARKED_TTL_SECONDS


def test_exit_parks_a_persisted_tab_and_an_explicit_reattach_resumes_it(monkeypatch, companion):
    driver = _Tab(42)
    session = _register("keep", driver, persist=True, agent_label="bot", label_tab=False)
    session.last_url = "https://user:pw@example.test/work?token=abc#frag"
    _register("plain", _Tab(43))

    outcome = browser_tools._close_everything_at_exit()

    assert outcome["parked_sessions"] == ["keep"]
    assert "close_tab" not in driver.calls and "quit" in driver.calls
    record = parking.lookup("keep")
    assert record["tab_id"] == 42 and record["owns_tab"] is True and record["label_tab"] is False
    assert "pw" not in record["url"] and "abc" not in record["url"] and "title" not in record
    assert parking.lookup("plain") is None and browser_tools._sessions == {}

    # A later client: nothing is picked up implicitly ...
    with pytest.raises(ValueError, match="reattach"):
        browser_tools._get_session("keep")
    # ... only an explicit reattach, and only after every identity check passed.
    companion.alive.add(42)
    companion.tabs[42] = {"group": GROUP, "url": "https://example.test/elsewhere"}
    replacement = _Tab(42)
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: replacement)
    labelled = []
    monkeypatch.setattr(browser_tools, "_apply_tab_label",
                        lambda s, sid, label_tab=True: labelled.append(label_tab))
    answer = browser_tools.reattach_session("keep")
    adopted = browser_tools._sessions["keep"]
    assert answer["reattached"] is True and adopted.persist and adopted.owns_tab
    assert labelled == [False]  # label_tab=False survives the round trip

    browser_tools.close_session("keep")
    assert parking.lookup("keep") is None


@pytest.mark.parametrize("change,reason", [
    ({"group": "Someone else's group"}, "left the agent's tab group"),
    ({"url": "https://bank.test/"}, "different site"),
])
def test_reattach_refuses_a_tab_that_is_no_longer_provably_ours(companion, change, reason):
    _park()
    companion.alive.add(42)
    companion.tabs[42] = {"group": GROUP, "url": "https://example.test/", **change}
    answer = browser_tools.reattach_session("keep")
    assert answer["success"] is False and reason in answer["error"]
    assert parking.lookup("keep") is None and "keep" not in browser_tools._sessions
    assert ("tabs.remove", {"tabId": 42}) not in companion.calls


def test_reattach_refuses_when_the_chrome_run_cannot_be_confirmed(monkeypatch, companion):
    _park()
    companion.alive.add(42)
    monkeypatch.setattr(browser_tools, "_current_browser_run", lambda: None)  # bridge down
    assert "cannot be confirmed" in browser_tools.reattach_session("keep")["error"]
    _park(browser_run="an-older-run")
    monkeypatch.setattr(browser_tools, "_current_browser_run", lambda: RUN)
    assert "restarted" in browser_tools.reattach_session("keep")["error"]
    assert parking.lookup("keep") is None


def test_reattach_refuses_a_tab_another_client_drives_and_keeps_its_record(monkeypatch, companion):
    _park()
    companion.alive.add(42)

    def busy(tab_id):
        raise RuntimeError(f"Chrome tab {tab_id} is already being driven by another agent.")

    monkeypatch.setattr(browser_tools, "_claim_tab", busy)
    assert "live in another MCP client" in browser_tools.reattach_session("keep")["error"]
    assert parking.lookup("keep") is not None


def test_closing_a_parked_session_closes_only_a_tab_the_server_opened(companion):
    companion.alive.update({42, 43})
    _park("mine", 42, owns_tab=True)
    _park("borrowed", 43, owns_tab=False)
    closed = browser_tools.close_session("mine")["retired_parked"]
    kept = browser_tools.close_session("borrowed")["retired_parked"]
    assert closed["tab_closed"] is True and 42 not in companion.alive
    assert kept["tab_closed"] is False and 43 in companion.alive
    assert "only tabs the server opened" in kept["left_open_reason"]
    assert parking.lookup("mine") is None and parking.lookup("borrowed") is None


def test_an_expired_record_closes_its_server_opened_tab(companion):
    companion.alive.add(42)
    _park("stale", 42)
    records = json.loads(parking.registry_path().read_text(encoding="utf-8"))
    records["stale"]["updated_at"] = time.time() - 10 * 86400
    parking.registry_path().write_text(json.dumps(records), encoding="utf-8")
    overview = browser_tools.sessions_overview()
    assert overview["parked_expired"][0]["tab_closed"] is True and 42 not in companion.alive


def test_persist_needs_current_chrome_and_a_real_session_id(monkeypatch):
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(1))
    with pytest.raises(ValueError, match="profile_mode='current'"):
        browser_tools.open_page(
            "https://example.test/", session_id="tmp", profile_mode="temporary", persist=True
        )
    with pytest.raises(ValueError, match="not 'default'"):
        browser_tools.open_page("https://example.test/", persist=True)
    assert browser_tools._sessions == {}


def test_open_and_attach_publish_persist_reattach_and_attach_alias():
    schema = asyncio.run(main.web_info("action_schema", {"action": "open"}))
    assert schema["input_schema"]["properties"]["persist"]["default"] is False
    attach = asyncio.run(main.web_info("action_schema", {"action": "attach_tab"}))
    assert "persist" not in attach["input_schema"]["properties"]
    alias = asyncio.run(main.web_info("action_schema", {"action": "attach"}))
    assert alias["action"] == "attach_tab"
    assert "reattach" in main._ACTIONS


def test_the_attach_alias_dispatches_attach_tab(monkeypatch):
    seen = {}

    def attach(tab_id, session_id, agent_label, label_tab):
        seen.update(tab_id=tab_id, session_id=session_id)
        return {"success": True}

    monkeypatch.setattr(browser_tools, "attach_current_tab", attach)
    result = asyncio.run(main.web_action(
        [{"action": "attach", "tab_id": 7, "session_id": "a"}]
    ))
    assert result["success"] is True and result["results"][0]["action"] == "attach_tab"
    assert seen == {"tab_id": 7, "session_id": "a"}


# ------------------------------------------------ 9. cap roster and orphans


def test_the_cap_error_lists_every_holder_redacted_and_how_to_release(monkeypatch):
    monkeypatch.setattr(browser_tools, "effective_max_sessions", lambda: (2, "default"))
    first = _register("a1", _Tab(1), agent_label="alpha")
    first.last_url = "https://a.test/reset?token=s3cret"
    _register("a2", _Tab(2))
    with pytest.raises(RuntimeError) as refusal:
        browser_tools._create_session(
            "a3", 1440, 900, False, "current", None, None, None, GROUP
        )
    text = str(refusal.value)
    assert "Holders:" in text and "a1 (agent=alpha, tab=1" in text and "url=https://a.test/reset" in text
    assert "s3cret" not in text and "idle_for_seconds" in text


def test_close_all_idle_for_seconds_releases_only_orphans():
    old = _register("old", _Tab(1))
    old.last_used = time.monotonic() - 3600
    _register("fresh", _Tab(2))
    answer = browser_tools.close_all_sessions(scope="all", idle_for_seconds=600)
    assert answer["closed_sessions"] == ["old"]
    assert [row["session_id"] for row in answer["kept_sessions"]] == ["fresh"]
    assert "fresh" in browser_tools._sessions


def test_close_all_says_why_a_session_was_kept():
    # 1.19 (BUG-15): an anonymous session kept by the idle filter was called
    # "another agent's", with scope='all' - which closes other agents' too - as the cure.
    old = _register("old-anon", _Tab(1))
    old.last_used = time.monotonic() - 3600
    _register("fresh-anon", _Tab(2))
    answer = browser_tools.close_all_sessions(idle_for_seconds=600)
    assert answer["kept_sessions"] == [
        {"session_id": "fresh-anon", "agent_label": None, "kept_because": "used_recently"}]
    assert "other agents" not in answer["scope_note"] and "idle_for_seconds" in answer["scope_note"]


# ------------------------------------------------------ A. wait seconds


def test_wait_seconds_needs_no_session_and_refuses_a_selector():
    started = time.monotonic()
    answer = browser_tools.wait_for_element(session_id="nobody", seconds=0.2)
    assert answer["success"] is True and answer["slept_seconds"] == pytest.approx(0.2)
    assert time.monotonic() - started >= 0.15  # Windows clock ticks are ~15 ms
    with pytest.raises(ValueError, match="plain delay"):
        browser_tools.wait_for_element("#x", seconds=1)
    result = asyncio.run(main.web_action([{"action": "wait", "seconds": 0.1}]))
    assert result["success"] is True


# ------------------------------------------- 1 + D. type_text focus and keys


class _KeyDriver:
    is_extension_bridge = True
    tab_id = 3

    def __init__(self, focused):
        self.focused = focused
        self.events: list[dict] = []
        self.inserted: list[str] = []

    def perform_key_events(self, events):
        self.events.extend(events)

    def execute_script(self, script, *_args):
        if script == verification.FOCUSED_EDITABLE_SCRIPT:
            return self.focused
        if "document.activeElement" in script:
            return {"type": "object"}
        return {"url": "https://game.test/", "title": "Game"}

    def execute_cdp_cmd(self, command, params):
        if command == "Input.insertText":
            self.inserted.append(params["text"])
        return {}

    def quit(self):
        return None


def _typed(events):
    return "".join(e["key"] for e in events if e["type"] == "down" and len(e["key"]) == 1)


def test_text_key_events_keep_cyrillic_and_shift_capitals():
    events = verification.text_key_events("Привет Ab\n")
    shift = verification.key_table.SELENIUM_KEYS["SHIFT"]
    downs = [e["key"] for e in events if e["type"] == "down"]
    assert downs[:6] == list("Привет")
    assert downs[6] == verification.key_table.SELENIUM_KEYS["SPACE"]
    assert downs[7:9] == [shift, "A"] and downs[9] == "b"
    assert downs[-1] == verification.key_table.SELENIUM_KEYS["ENTER"]
    assert len([e for e in events if e["type"] == "up"]) == len(downs)


def test_type_text_keys_mode_presses_each_character_into_the_focus():
    driver = _KeyDriver({"editable": True, "tag": "input", "focused": True})
    _register("keys", driver, profile_mode="temporary")
    answer = browser_tools.type_text("Да", session_id="keys", mode="keys")
    assert answer["mode_used"] == "keys" and _typed(driver.events) == "Да"
    assert driver.inserted == []
    with pytest.raises(ValueError, match="at most 500"):
        browser_tools.type_text("x" * 501, session_id="keys", mode="keys")


def test_keys_mode_refuses_when_nothing_has_focus():
    driver = _KeyDriver({"editable": False, "tag": "body", "focused": False, "in_frame": False})
    _register("keys-nofocus", driver, profile_mode="temporary")
    with pytest.raises(ValueError, match="bare page"):
        browser_tools.type_text("hi", session_id="keys-nofocus", mode="keys")
    assert driver.events == []


def test_a_focused_canvas_switches_to_keys_on_its_own():
    driver = _KeyDriver({"editable": False, "tag": "canvas", "canvas": True, "focused": True})
    _register("canvas", driver, profile_mode="temporary")
    answer = browser_tools.type_text("ёж", session_id="canvas")
    assert answer["mode_used"] == "keys" and "canvas" in answer["mode_note"]
    assert _typed(driver.events) == "ёж" and driver.inserted == []


def test_insert_refuses_nothing_focused_and_read_only_controls():
    driver = _KeyDriver({"editable": False, "tag": "body", "focused": False})
    _register("nofocus", driver, profile_mode="temporary")
    with pytest.raises(ValueError, match="Nothing editable has focus"):
        browser_tools.type_text("hi", session_id="nofocus")
    driver.focused = {"editable": True, "tag": "input", "focused": True, "readonly": True}
    with pytest.raises(ValueError, match="read-only"):
        browser_tools.type_text("hi", session_id="nofocus")
    with pytest.raises(ValueError, match="mode must be"):
        browser_tools.type_text("hi", session_id="nofocus", mode="paste")


def test_a_cross_origin_frame_is_unknown_not_refused():
    driver = _KeyDriver({"editable": None, "tag": "iframe", "in_frame": True, "cross_origin": True})
    _register("xframe", driver, profile_mode="temporary")
    browser_tools.type_text("Съешь ещё", session_id="xframe")
    assert driver.inserted == ["Съешь ещё"]


# ---------------------------------------------------- 7. click verification


class _ProbeDriver:
    def __init__(self, evidence, armed=True):
        self.evidence = evidence
        self.armed = armed

    def execute_script(self, script, *args):
        if script == verification.CLICK_ARM_SCRIPT:
            return {"url": "u", "title": "t", "armed": self.armed}
        if script == verification.CLICK_COLLECT_SCRIPT:
            return self.evidence
        return None


def _effects(evidence, *, measurable=True, url="u"):
    driver = _ProbeDriver(evidence)
    return verification.collect_click_effects(
        driver, object(), verification.arm_click_probe(driver), {"url": url, "title": "t"},
        measurable=measurable,
    )


def test_only_changes_around_the_target_verify_a_click():
    near = _effects({"mutations": 9, "near_target": 2, "focus_before": "a", "focus_after": "a",
                     "target_attached": True})
    assert near["verified"] is True and near["post_state"]["dom_mutations_near_target"] == 2
    elsewhere = _effects({"mutations": 40, "near_target": 0, "focus_before": "a", "focus_after": "a",
                          "target_attached": True})
    # 1.19: a change elsewhere is an effect of low confidence - neither "verified"
    # (it may be page noise) nor "nothing happened" (it may be the modal it opened).
    assert elsewhere["verified"] is None and elsewhere["effect_detected"] is True
    assert elsewhere["effect_confidence"] == "low" and "no_observable_change" not in elsewhere
    assert "Do not repeat the click" in elsewhere["change_note"]
    assert "retry" not in elsewhere["change_note"]
    still = _effects({"mutations": 0, "near_target": 0, "focus_before": "a", "focus_after": "a",
                      "target_attached": True})
    assert still["verified"] is False and still["no_observable_change"] is True
    opened = _effects({"mutations": 3, "near_target": 0, "focus_before": "a", "focus_after": "a",
                       "target_attached": True, "dialog_open": True, "dialog_before": False})
    assert opened["verified"] is True and opened["post_state"]["dialog_opened"] is True


def test_frame_or_shadow_targets_are_never_claimed_unverified():
    quiet = {"mutations": 0, "near_target": 0, "focus_before": "a", "focus_after": "a",
             "target_attached": True}
    assert _effects(quiet, measurable=False)["verified"] is None
    assert _effects({**quiet, "scoped": True})["verified"] is None
    assert _effects(quiet, measurable=False, url="v")["verified"] is True


def test_focus_landing_on_the_target_itself_is_not_an_effect():
    fields = _effects({"mutations": 0, "near_target": 0, "focus_before": "body#",
                       "focus_after": "button#go", "focus_on_target": True, "target_attached": True})
    assert fields["verified"] is False
    moved = _effects({"mutations": 0, "near_target": 0, "focus_before": "body#",
                      "focus_after": "input#q", "target_attached": True})
    assert moved["verified"] is True


def test_a_detached_target_counts_as_an_effect():
    fields = _effects({"mutations": 0, "near_target": None, "focus_before": "a", "focus_after": "a",
                       "target_attached": False})
    assert fields["verified"] is True


def test_the_probe_is_hidden_and_always_disconnected():
    assert "Symbol.for('wsn.clickProbe')" in verification.CLICK_ARM_SCRIPT
    assert "enumerable: false" in verification.CLICK_ARM_SCRIPT
    assert "__wsnClickProbe" not in verification.CLICK_ARM_SCRIPT + verification.CLICK_COLLECT_SCRIPT
    assert re.search(r"\} finally \{\s*if \(probe && probe.observer\) probe.observer.disconnect\(\);",
                     verification.CLICK_COLLECT_SCRIPT)


# ------------------------------------------------- 3 + 12. execute_js


class _ScriptDriver:
    def __init__(self, value):
        self.value = value

    def execute_script(self, script, *_args):
        return self.value


@pytest.mark.parametrize("value,kind", [
    ({"a": [1, 2]}, "object"), ([1], "array"), ("x", "string"), (3, "number"),
    (True, "boolean"), (None, "null"),
])
def test_execute_js_reports_one_json_contract(value, kind):
    answer = script_actions.execute(
        _ScriptDriver(value), "return 1;", page_summary=dict,
        wait_until_ready=lambda *_: None, describe_error=str,
    )
    assert answer["value_type"] == kind
    assert json.loads(answer["value_json"]) == answer["value"]


def test_execute_js_says_when_the_script_forgot_return():
    answer = script_actions.execute(
        _ScriptDriver(None), "document.title", page_summary=dict,
        wait_until_ready=lambda *_: None, describe_error=str,
    )
    assert "return" in answer["value_note"]


def test_execute_js_explains_an_unserialisable_result():
    class _Cyclic:
        def execute_script(self, *_args):
            raise RuntimeError("Object reference chain is too long")

    answer = script_actions.execute(
        _Cyclic(), "return window;", page_summary=dict,
        wait_until_ready=lambda *_: None, describe_error=str,
    )
    assert answer["success"] is False and "plain object" in answer["error"]


def test_execute_js_counts_cross_origin_frames():
    class _Page(_Tab):
        def execute_script(self, script, *_args):
            if script == browser_tools._CROSS_ORIGIN_FRAMES_SCRIPT:
                return 2
            if "querySelector('#inside')" in script:
                return None
            if "return document.title" in script:
                return "T"
            return {"url": "https://example.test/", "title": "T"}

    _register("frames", _Page(), profile_mode="temporary")
    empty = browser_tools.execute_js(
        "return document.querySelector('#inside');", session_id="frames", report_frames=True
    )
    assert empty["value"] is None and empty["cross_origin_frames"] == 2
    assert "frame_selector" in empty["frames_note"]
    found = browser_tools.execute_js("return document.title;", session_id="frames", report_frames=True)
    assert found["value"] == "T" and "cross_origin_frames" not in found


# ------------------------------------------------------- 6. cookies


class _CookieDriver(_Tab):
    def __init__(self, count):
        super().__init__(1)
        self.jar = [{"name": f"c{i:04d}", "domain": f"d{i % 7}.test", "path": "/"} for i in range(count)]

    def execute_cdp_cmd(self, command, *_args, **_kwargs):
        return {"cookies": list(reversed(self.jar))} if command == "Storage.getCookies" else {}


def test_cookie_paging_reaches_every_cookie():
    _register("jar", _CookieDriver(1263), profile_mode="temporary")
    seen: list[str] = []
    offset = 0
    while offset is not None:
        page = browser_tools.cookies("get", session_id="jar", limit=500, offset=offset)
        assert page["total"] == 1263
        seen += [f"{c['domain']}|{c['name']}" for c in page["cookies"]]
        offset = page["next_offset"]
    assert len(seen) == 1263 and len(set(seen)) == 1263


def test_the_companion_allows_the_filtered_cookie_delete():
    source = (_EXTENSION / "service-worker.js").read_text(encoding="utf-8")
    assert '"Network.deleteCookies",' in source


# ------------------------------------------------------- screenshot


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (320).to_bytes(4, "big") + (200).to_bytes(4, "big") + b"rest"


def test_screenshot_action_saves_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(browser_tools, "screenshot", lambda *a, **k: _PNG)
    result = asyncio.run(main.web_action([{"action": "screenshot", "session_id": "shot"}]))
    data = result["results"][0]["data"]
    assert result["success"] is True and data["size_bytes"] == len(_PNG)
    assert (data["image_width"], data["image_height"]) == (320, 200)
    assert Path(data["saved_to"]).read_bytes() == _PNG
    schema = asyncio.run(main.web_info("action_schema", {"action": "screenshot"}))
    assert "params_schema" in schema and "overwrite" in schema["action_form"]["input_schema"]["properties"]


def test_screenshot_paths_are_checked_before_the_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    captures = []
    monkeypatch.setattr(browser_tools, "screenshot", lambda *a, **k: captures.append(1) or _PNG)
    (tmp_path / "keep.png").write_bytes(b"precious")
    with pytest.raises(ValueError, match="overwrite=true"):
        browser_tools.save_screenshot("s", path="keep.png")
    with pytest.raises(ValueError, match=r"\.png"):
        browser_tools.save_screenshot("s", path="notes.txt")
    with pytest.raises(ValueError, match="inside the download directory"):
        browser_tools.save_screenshot("s", path=str(tmp_path.parent / "escape.png"))
    assert captures == [] and (tmp_path / "keep.png").read_bytes() == b"precious"
    browser_tools.save_screenshot("s", path="keep.png", overwrite=True)
    assert (tmp_path / "keep.png").read_bytes() == _PNG


# ------------------------------------------------ 11. title_pending


def test_an_empty_title_is_pending_only_on_an_html_page():
    driver = _Tab(1)
    driver.title = ""
    _register("untitled", driver, profile_mode="temporary")
    assert browser_tools._page_summary(driver, "untitled")["title_pending"] is True
    driver.url = "about:blank"
    assert "title_pending" not in browser_tools._page_summary(driver, "untitled")
    driver.url = "https://example.test/"
    driver.execute_script = lambda *_a: {"url": driver.url, "title": "", "content_type": "application/json"}
    assert "title_pending" not in browser_tools._page_summary(driver, "untitled")
    driver.execute_script = lambda *_a: {"url": driver.url, "title": "Ready"}
    assert "title_pending" not in browser_tools._page_summary(driver, "untitled")


# --------------------------------------------------- 4. SPA shells


def test_a_title_only_page_with_scripts_is_flagged_with_the_browser_call():
    html = "<html><head><title>GitHub</title><script src='/x/app-4f3a.js'></script></head><body></body></html>"
    assert fetch_content.is_spa_shell(html, "GitHub") is True
    notice = fetch_content._spa_notice("https://github.com/someone")
    assert '"action":"open"' in notice and "page_text" in notice


# -------------------------------------------- contract text is updated


def test_the_contract_documents_the_new_behaviour():
    notes = main._action_documentation()
    assert "mode='keys'" in notes["type_text"]["mode"]
    assert "Neither false nor null means 'click again'" in notes["click"]["verification"]
    assert "reattach" in notes["open"]["persist"] and "never 'default'" in notes["open"]["persist"]
    assert "never is" in notes["open"]["tab_followed"]
    assert "seconds=N" in notes["wait"]["sleep"]
    assert "idle_for_seconds" in notes["close_all"]["orphans"]
    assert "confirm_clear_all" in notes["cookies"]["paging"]
    assert "framework_resynced" in notes["fill"]["typing_react"]
    assert "canvas_text" in main._capabilities()["recipes"]


# ------------------------------------------ companion code

NODE = shutil.which("node")
_EXTENSION = Path(__file__).resolve().parents[1] / "chrome-extension"


def test_the_code_hash_covers_every_module_the_worker_runs():
    js = (_EXTENSION / "code-hash.js").read_text(encoding="utf-8")
    listed = re.findall(r'"([a-z-]+\.js)"', js[js.index("CODE_FILES"):js.index("];")])
    assert tuple(listed) == chrome_bootstrap.CODE_FILES
    reachable, pending = set(), ["service-worker.js"]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending += re.findall(r'from "\./([a-z-]+\.js)"', (_EXTENSION / name).read_text(encoding="utf-8"))
    assert reachable - {"bridge-token.js"} == set(listed)


@pytest.mark.skipif(NODE is None, reason="node is required to exercise the companion modules")
def test_the_companion_redirects_only_replaced_tabs_and_hashes_its_code():
    follow = (_EXTENSION / "tab-follow.js").as_uri()
    hashing = (_EXTENSION / "code-hash.js").as_uri()
    body = f"""
import * as fs from "node:fs";
const listeners = {{}};
const on = name => ({{addListener: fn => {{ listeners[name] = fn; }}}});
const store = {{}};
const tabs = {{onReplaced: on('replaced'), onCreated: on('created'),
  get: async id => {{ if (id === 7) return {{id}}; throw new Error('No tab'); }}}};
const session = {{get: async k => ({{[k]: store[k]}}), set: async items => Object.assign(store, items)}};
const {{createTabFollower}} = await import({json.dumps(follow)});
const follower = createTabFollower(tabs, session);
listeners.replaced(7, 5);
if (listeners.created) listeners.created({{id: 9, openerTabId: 3}});
const redirected = await follower.redirect({{tabId: 5, method: 'x'}});
const child = await follower.redirect({{tabId: 3}});
const resolved = await follower.resolve(5);
const reloaded = createTabFollower({{}}, session);
const {{selfCodeHash}} = await import({json.dumps(hashing)});
const dir = {json.dumps(str(_EXTENSION))};
const hash = await selfCodeHash(
  async url => {{ const b = fs.readFileSync(dir + '/' + url);
    return {{arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)}}; }},
  name => name,
  bytes => Buffer.from(bytes).toString('hex'));
console.log(JSON.stringify({{redirected, child, resolved, hash,
  afterEviction: await reloaded.redirect({{tabId: 5}})}}));
"""
    output = subprocess.run(
        [NODE, "--input-type=module", "-e", body], capture_output=True, text=True, timeout=60
    )
    assert output.returncode == 0, output.stderr
    answer = json.loads(output.stdout.strip().splitlines()[-1])
    assert answer["redirected"]["tabId"] == 7
    assert answer["child"]["tabId"] == 3  # an opened tab is never a replacement
    assert answer["resolved"] == {"tabId": 5, "alive": False, "replaced_by": 7, "successor_alive": True}
    assert answer["afterEviction"]["tabId"] == 7
    assert answer["hash"] == chrome_bootstrap.expected_code_hash()


def test_the_service_worker_reports_redirects_on_the_answer():
    source = (_EXTENSION / "service-worker.js").read_text(encoding="utf-8")
    assert 'import {createTabFollower} from "./tab-follow.js";' in source
    assert '"tabs.resolve"' in source and "tabFollower.redirect(raw)" in source
    assert "payload.tab_followed = {from: Number(raw.tabId), to: params.tabId}" in source
    assert "opener" not in (_EXTENSION / "tab-follow.js").read_text(encoding="utf-8").split("export")[1]


# ------------------------------------------- in a real browser


def _open_fixture_or_skip(local_site, session_id, name="react_like.html"):
    from selenium.common.exceptions import WebDriverException

    try:
        browser_tools.open_page(
            f"{local_site.base_url}/fixtures/perception/{name}",
            session_id=session_id, width=1024, height=768, headless=True,
            profile_mode="temporary",
        )
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    return browser_tools._get_session(session_id).driver


def test_page_elements_offer_stable_locators_on_a_react_like_page(local_site):
    _open_fixture_or_skip(local_site, "stable-hints")
    answer = browser_tools.get_page_elements(session_id="stable-hints", category="interactive")
    rows = {row.get("name"): row for row in answer["interactive"]}
    save = rows["Save"]
    assert save["testid"] == "save-button" and "stable_selector" not in save
    assert save["suggested_locator"] == {"selector": save["selector"]}
    assert "suggested_locator" not in rows["Name"]  # a stable plain selector needs no hint
    edit = rows["Edit"]
    assert edit["stable_selector"] is False and ":nth-of-type(" in edit["selector"]
    assert edit["suggested_locator"] == {"text": "Edit", "role": "button"}


def test_fill_resyncs_a_controlled_input_that_missed_the_keystrokes(local_site):
    _open_fixture_or_skip(local_site, "react-resync")
    answer = browser_tools.fill_fields({"#name": "Ада"}, session_id="react-resync")
    assert answer["success"] is True and answer["framework_resynced"] == ["#name"]
    driver = browser_tools._get_session("react-resync").driver
    assert driver.execute_script("return window.__changes;") == ["Ада"]


def test_focus_is_found_through_shadow_roots_and_same_origin_frames(local_site):
    driver = _open_fixture_or_skip(local_site, "focus-probe", "focus_and_probe.html")

    def focus(script):
        driver.execute_script(script)
        return verification.focused_editable(driver)

    assert focus("document.activeElement.blur(); document.body.focus();")["focused"] is False
    assert focus("document.getElementById('plain').focus();")["editable"] is True
    assert focus("document.getElementById('ro').focus();")["readonly"] is True
    shadow = focus("document.getElementById('editor').shadowRoot.getElementById('shadow-input').focus();")
    assert shadow["editable"] is True and shadow["in_shadow"] is True
    framed = focus("const f = document.getElementById('frame'); f.focus();"
                   "f.contentDocument.getElementById('inner').focus();")
    assert framed["editable"] is True and framed["in_frame"] is True


def test_a_click_is_judged_by_changes_around_its_target_not_page_noise(local_site):
    _open_fixture_or_skip(local_site, "click-probe", "focus_and_probe.html")
    quiet = browser_tools.click("#noop", session_id="click-probe", wait_seconds=0.3)
    assert quiet["post_state"]["dom_mutations"] > 0  # the ticker kept ticking
    assert quiet["verified"] is None and quiet["effect_confidence"] == "low"  # noise never verifies
    pressed = browser_tools.click("#toggle", session_id="click-probe", wait_seconds=0.3)
    assert pressed["verified"] is True and pressed["post_state"]["dom_mutations_near_target"] >= 1
    shadow = browser_tools.click("#editor >>> #shadow-input", session_id="click-probe", wait_seconds=0.1)
    assert shadow["verified"] is None
    probe_left = browser_tools._get_session("click-probe").driver.execute_script(
        "return Object.keys(window).filter(k => k.toLowerCase().includes('clickprobe'));"
    )
    assert probe_left == []


def test_a_borrowed_tab_is_never_parked_or_reattached(companion):
    refused = asyncio.run(main.web_action(
        [{"action": "attach_tab", "tab_id": 50, "session_id": "borrow", "persist": True}]
    ))
    assert refused["success"] is False and "persist" in refused["results"][0]["error"]
    # Even a record claiming a borrowed tab (written by an older build) is refused.
    _park("borrow", 50, owns_tab=False)
    companion.alive.add(50)
    answer = browser_tools.reattach_session("borrow")
    assert answer["success"] is False and "only tabs the server opened" in answer["error"]
    assert parking.lookup("borrow") is None and 50 in companion.alive
    session = _register("borrowed-live", _Tab(51), persist=True, owns_tab=False)
    browser_tools._remember_parked("borrowed-live", session)
    assert parking.lookup("borrowed-live") is None




# ------------------------------------------------ round 2 regressions


def test_an_expired_record_never_touches_a_live_persist_session(monkeypatch, companion):
    """N1 repro: a live persist session whose record passed the TTL, then browser_status."""
    driver = _Tab(42)
    session = _register("live", driver, persist=True)
    session.last_url = "https://example.test/work"
    companion.alive.add(42)
    released = []
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", lambda tab_id: released.append(tab_id))
    browser_tools._remember_parked("live", session)
    records = json.loads(parking.registry_path().read_text(encoding="utf-8"))
    records["live"]["updated_at"] = time.time() - 10 * 86400
    parking.registry_path().write_text(json.dumps(records), encoding="utf-8")

    overview = browser_tools.sessions_overview()

    assert 42 in companion.alive and ("tabs.remove", {"tabId": 42}) not in companion.calls
    assert released == [] and "live" in browser_tools._sessions
    assert "parked_expired" not in overview  # a live session's record is not "expired"
    assert parking.lookup("live") is not None  # put back, not lost


def test_using_a_persist_session_keeps_its_record_fresh(monkeypatch):
    session = _register("fresh-use", _Tab(42), persist=True)
    session.last_url = "https://example.test/"
    browser_tools._remember_parked("fresh-use", session)
    first = parking.lookup("fresh-use")["updated_at"]
    session.parked_at -= 10_000  # long enough ago to be due
    browser_tools._get_session("fresh-use")
    assert parking.lookup("fresh-use")["updated_at"] > first
    stamp = parking.lookup("fresh-use")["updated_at"]
    browser_tools._get_session("fresh-use")  # throttled: no rewrite right away
    assert parking.lookup("fresh-use")["updated_at"] == stamp


def test_attaching_a_sessions_own_parked_tab_reattaches_it(monkeypatch, companion):
    _park("own", 42)
    companion.alive.add(42)
    companion.tabs[42] = {"group": GROUP, "url": "https://example.test/else"}
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(42))
    answer = browser_tools.attach_current_tab(42, session_id="own")
    assert answer["reattached"] is True and 42 in companion.alive
    assert browser_tools._sessions["own"].owns_tab is True


def test_a_parked_tab_now_on_another_site_is_named_not_abandoned(companion):
    _park("moved", 42)
    companion.alive.add(42)
    companion.tabs[42] = {"group": GROUP, "url": "https://elsewhere.test/"}
    answer = browser_tools.reattach_session("moved")
    assert answer["success"] is False and answer["left_open_tab"]["tab_id"] == 42
    assert "close_tabs" in answer["left_open_tab"]["note"] and 42 in companion.alive
    # An explicit close of such a parked session closes the server's tab.
    _park("moved2", 43)
    companion.alive.add(43)
    companion.tabs[43] = {"group": GROUP, "url": "https://elsewhere.test/"}
    assert browser_tools.close_session("moved2")["retired_parked"]["tab_closed"] is True
    assert 43 not in companion.alive


@pytest.mark.parametrize("focused,allowed", [
    ({"editable": False, "tag": "button", "focused": True, "control": True}, False),
    ({"editable": False, "tag": "a", "focused": True, "control": True, "in_frame": True}, False),
    ({"editable": False, "tag": "div", "focused": True, "tabindex": True}, True),
    ({"editable": None, "tag": "x-editor", "focused": True, "custom_element": True}, True),
    ({"editable": False, "tag": "body", "focused": False, "in_frame": True}, True),
])
def test_keys_go_only_where_typing_makes_sense(focused, allowed):
    driver = _KeyDriver(focused)
    _register("keys-target", driver, profile_mode="temporary")
    if allowed:
        browser_tools.type_text("ok", session_id="keys-target", mode="keys")
        assert _typed(driver.events) == "ok"
    else:
        with pytest.raises(ValueError, match="button or link"):
            browser_tools.type_text("ok", session_id="keys-target", mode="keys")
        assert driver.events == []


def test_a_click_that_never_happened_disarms_its_probe(monkeypatch):
    scripts = []

    class _Failing(_Tab):
        def execute_script(self, script, *args):
            scripts.append(script)
            return super().execute_script(script, *args)

    driver = _Failing(1)
    _register("disarm", driver, profile_mode="temporary")

    class _Element:
        tag_name = "button"

        def click(self):
            raise RuntimeError("element click intercepted")

    monkeypatch.setattr(browser_tools, "_wait_for_locator", lambda *a, **k: _Element())
    with pytest.raises(RuntimeError, match="intercepted"):
        browser_tools.click("#go", session_id="disarm", wait_seconds=0)
    assert verification.CLICK_ARM_SCRIPT in scripts
    assert scripts[-1] == verification.CLICK_DISARM_SCRIPT


def test_the_contract_keeps_a_real_margin_under_its_budget():
    assert len(json.dumps(main._capabilities())) < 14_000



# ------------------------------------------------ round 3 (1.18.5)


@pytest.mark.parametrize("domain", ["com", ".com", "ru", "co.uk", ".co.uk", "github.io", "com.br"])
def test_clearing_cookies_of_a_public_suffix_needs_confirmation(domain):
    class _Jar(_Tab):
        def execute_cdp_cmd(self, command, *_args, **_kwargs):
            return {"cookies": [{"name": "sid", "domain": ".shop.co.uk", "path": "/"}]} \
                if command == "Storage.getCookies" else {}

    driver = _Jar(1)
    _register("suffix", driver, profile_mode="temporary")
    with pytest.raises(ValueError, match="public suffix"):
        browser_tools.cookies("clear", session_id="suffix", domain=domain)
    assert browser_tools.cookies("clear", session_id="suffix", domain="shop.co.uk")["deleted"] >= 0


def test_the_public_suffix_list_keeps_real_sites_clearable():
    from web_search_neo.actions.cookie_scope import is_public_suffix

    for site in ("example.com", "shop.co.uk", "me.github.io", "yandex.ru", "a.b.c.org",
                 "localhost", "127.0.0.1"):
        assert is_public_suffix(site) is False, site
    for suffix in ("com", "co.uk", "github.io", "intranet", "co.jp"):
        assert is_public_suffix(suffix) is True, suffix


def test_a_click_whose_settle_fails_still_disarms_its_probe(monkeypatch):
    scripts = []

    class _Page(_Tab):
        def execute_script(self, script, *args):
            scripts.append(script)
            return super().execute_script(script, *args)

    driver = _Page(1)
    _register("settle", driver, profile_mode="temporary")

    class _Element:
        tag_name = "button"

        def click(self):
            return None

    monkeypatch.setattr(browser_tools, "_wait_for_locator", lambda *a, **k: _Element())

    def broken_summary(*_args):
        raise RuntimeError("the page went away")

    monkeypatch.setattr(browser_tools, "_page_summary", broken_summary)
    with pytest.raises(RuntimeError, match="went away"):
        browser_tools.click("#go", session_id="settle", wait_seconds=0)
    assert verification.CLICK_ARM_SCRIPT in scripts
    assert scripts[-1] == verification.CLICK_DISARM_SCRIPT


def test_keys_into_a_tabindex_surface_carry_a_shortcut_warning():
    driver = _KeyDriver({"editable": False, "tag": "div", "focused": True, "tabindex": True})
    _register("hotkeys", driver, profile_mode="temporary")
    answer = browser_tools.type_text("gg", session_id="hotkeys", mode="keys")
    assert "keyboard shortcut" in answer["keys_warning"]
    canvas = _KeyDriver({"editable": False, "tag": "canvas", "canvas": True, "focused": True})
    _register("game", canvas, profile_mode="temporary")
    assert "keys_warning" not in browser_tools.type_text("gg", session_id="game", mode="keys")


def test_stop_tells_a_silent_listener_from_a_free_port(monkeypatch):
    import socket as socket_module
    import threading

    from web_search_neo.chrome_bridge import ChromeBridge

    server = socket_module.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    stop = threading.Event()

    def swallow():
        server.settimeout(0.2)
        held = []
        while not stop.is_set():
            try:
                held.append(server.accept()[0])  # accept, never answer
            except OSError:
                continue
        for conn in held:
            conn.close()

    thread = threading.Thread(target=swallow, daemon=True)
    thread.start()
    client = ChromeBridge(port=port, spawn=False, connect_timeout=0.5, start_timeout=0.2)
    try:
        started = time.monotonic()
        assert client.stop_daemon("test") is False
        assert "did not complete the bridge handshake" in client.stop_problem
        assert time.monotonic() - started < 25
    finally:
        client.shutdown()
        stop.set()
        thread.join(timeout=5)
        server.close()
    free = ChromeBridge(port=port, spawn=False, connect_timeout=0.5, start_timeout=0.2)
    try:
        assert free.stop_daemon("test") is False and free.stop_problem is None
    finally:
        free.shutdown()


# ------------------------------------------------ companion after a server update


class _StatusBridge:
    """A companion one version behind whose reload either takes or does not."""

    def __init__(self, connected=True, version="0.0.1", code_hash="old", claims=(), takes=True):
        self.connected = connected
        self.version = version
        self.code_hash = code_hash
        self.claims = list(claims)
        self.takes = takes
        self.reloads = 0

    def status(self, _wait=0.0):
        return {"connected": self.connected,
                "browser": {"extension_version": self.version, "code_hash": self.code_hash},
                "daemon": {"linked": True, "claims": self.claims}}

    def request(self, method, params=None, timeout=None):
        assert method == "runtime.reload"
        self.reloads += 1
        if self.takes:
            self.version = chrome_bootstrap.expected_extension_version()
            self.code_hash = chrome_bootstrap.expected_code_hash()
        return {"version": self.version}


@pytest.fixture
def refresh_state(monkeypatch, tmp_path):
    from web_search_neo import companion_refresh

    monkeypatch.setenv("WEB_SEARCH_NEO_COMPANION_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(chrome_bootstrap, "_SELF_UPDATE_POLL_SECONDS", 0.0)
    return companion_refresh


def test_an_outdated_companion_is_reloaded_only_when_nobody_drives(refresh_state):
    busy = _StatusBridge(claims=[{"tab_id": 5, "holder": "other"}])
    assert refresh_state.refresh_stale_companion(busy)["self_update"] == "deferred"
    local = _StatusBridge()
    assert refresh_state.refresh_stale_companion(local, local_sessions=1)["self_update"] == "deferred"
    assert busy.reloads == local.reloads == 0
    idle = _StatusBridge()
    assert refresh_state.refresh_stale_companion(idle)["self_update"] == "done"
    assert idle.reloads == 1 and refresh_state.refresh_stale_companion(idle) is None
    assert refresh_state.refresh_stale_companion(_StatusBridge(connected=False)) is None


def test_a_hash_only_difference_is_reported_never_reloaded(refresh_state):
    same_version = _StatusBridge(version=chrome_bootstrap.expected_extension_version(), code_hash="edited")
    answer = refresh_state.refresh_stale_companion(same_version)
    assert answer["self_update"] == "not_attempted" and answer["manual_steps"]
    assert same_version.reloads == 0


def test_a_reload_that_does_not_take_is_never_looped(refresh_state, monkeypatch):
    monkeypatch.setattr(refresh_state, "AUTO_RELOAD_INTERVAL_SECONDS", 0.0)
    stuck = _StatusBridge(takes=False)  # e.g. a second checkout's folder on the same port
    first = refresh_state.refresh_stale_companion(stuck)
    assert first["self_update"] == "ineffective" and first["manual_steps"] and stuck.reloads == 1
    for _ in range(3):
        again = refresh_state.refresh_stale_companion(stuck)
        assert again["self_update"] == "ineffective"
    assert stuck.reloads == 1  # the same build is not reloaded again
    other = _StatusBridge(version="0.0.2", takes=False)
    refresh_state.refresh_stale_companion(other)
    assert other.reloads == 1  # a different combination is judged afresh


def test_the_reload_pause_holds_across_server_processes(refresh_state, tmp_path):
    script = tmp_path / "one_process.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from web_search_neo import chrome_bootstrap, companion_refresh\n"
        "chrome_bootstrap._SELF_UPDATE_POLL_SECONDS = 0.0\n"
        "class B:\n"
        "    reloads = 0\n"
        "    def status(self, _w=0.0):\n"
        "        return {'connected': True, 'browser': {'extension_version': '0.0.1', 'code_hash': 'x'},\n"
        "                'daemon': {'linked': True, 'claims': []}}\n"
        "    def request(self, method, params=None, timeout=None):\n"
        "        B.reloads += 1\n"
        "        return {'version': '0.0.1'}\n"
        "chrome_bootstrap._reload_companion = lambda bridge, expected: (bridge.request('runtime.reload'), {'self_update': 'done'})[1]\n"
        "companion_refresh._reload_companion = chrome_bootstrap._reload_companion\n"
        "companion_refresh.refresh_stale_companion(B())\n"
        "print(B.reloads)\n",
        encoding="utf-8",
    )
    import os as _os
    import sys as _sys

    env = dict(_os.environ)
    runs = [subprocess.run([_sys.executable, str(script)], capture_output=True, text=True,
                           timeout=120, env=env) for _ in range(2)]
    assert [r.returncode for r in runs] == [0, 0], [r.stderr for r in runs]
    assert [r.stdout.strip().splitlines()[-1] for r in runs] == ["1", "0"]


def test_browser_tabs_explains_a_companion_that_has_not_reconnected_yet(monkeypatch):
    monkeypatch.setattr(browser_tools, "list_current_chrome_tabs",
                        lambda wait: {"connected": False, "tabs": [], "daemon": {"linked": True}})
    answer = browser_tools.get_current_tabs(0)
    assert "wait_seconds=75" in answer["companion_note"] and "Reconnect" in answer["companion_note"]



def test_clearing_a_cookie_filter_that_reaches_many_hosts_needs_confirmation():
    class _Wide(_Tab):
        def execute_cdp_cmd(self, command, *_args, **_kwargs):
            if command == "Storage.getCookies":
                return {"cookies": [{"name": "sid", "domain": f".shop{i}.example.org", "path": "/"}
                                    for i in range(6)]}
            return {}

    _register("wide", _Wide(1), profile_mode="temporary")
    with pytest.raises(ValueError, match="6 different hosts"):
        browser_tools.cookies("clear", session_id="wide", domain="example.org")
    assert browser_tools.cookies("clear", session_id="wide", domain="example.org",
                                 confirm_clear_all=True)["success"] is False  # fake never deletes


@pytest.mark.parametrize("suffix", ["kiev.ua", "in.ua", "pp.ua", "ca.us", "eu.org", "ngrok.io",
                                    "s3.amazonaws.com", "myshopify.com", "blogspot.com", "blogspot.ru"])
def test_the_extended_suffix_list(suffix):
    from web_search_neo.actions.cookie_scope import is_public_suffix

    assert is_public_suffix(suffix) is True
    assert is_public_suffix("shop." + suffix) is False


# ------------------------------------------------ 1.18.5 audit leftovers (B1-B4)


def test_a_damaged_state_file_is_treated_as_absent(refresh_state):
    path = refresh_state.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_auto_reload": time.time() + 10 ** 7, "ineffective": "junk"}),
                    encoding="utf-8")
    bridge = _StatusBridge()
    assert refresh_state.refresh_stale_companion(bridge)["self_update"] == "done"
    path.write_text(json.dumps({"last_auto_reload": "yesterday", "ineffective": [1, None]}),
                    encoding="utf-8")
    assert refresh_state._read(path) == {"last_auto_reload": 0.0, "ineffective": []}


def test_only_an_older_companion_is_reloaded(refresh_state):
    newer = _StatusBridge(version="99.0.0")
    answer = refresh_state.refresh_stale_companion(newer)
    assert answer["self_update"] == "not_attempted" and newer.reloads == 0
    odd = _StatusBridge(version="dev-build")
    assert refresh_state.refresh_stale_companion(odd)["self_update"] == "not_attempted"


def test_claims_that_appear_just_before_the_reload_defer_it(refresh_state):
    class _Late(_StatusBridge):
        def status(self, wait=0.0):
            answer = super().status(wait)
            if wait:  # the re-read right before the reload
                answer["daemon"]["claims"] = [{"tab_id": 9, "holder": "late"}]
            return answer

    late = _Late()
    assert refresh_state.refresh_stale_companion(late)["self_update"] == "deferred"
    assert late.reloads == 0


def test_the_daemon_refuses_a_companion_reload_while_tabs_are_claimed():
    import threading as _threading

    daemon = BridgeDaemon.__new__(BridgeDaemon)
    daemon._lock = _threading.Lock()
    daemon._claims = {5: object()}
    holder, refusal = daemon._claim_check(object(), "runtime.reload", {})
    assert holder is None and "driving a tab" in refusal
    daemon._claims = {}
    assert daemon._claim_check(object(), "runtime.reload", {}) == (None, None)


def test_tldextract_is_asked_about_private_domains(monkeypatch):
    import sys as _sys
    import types

    from web_search_neo.actions import cookie_scope

    seen = {}

    class _Extract:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __call__(self, name):
            return types.SimpleNamespace(domain="", suffix=name)

    monkeypatch.setitem(_sys.modules, "tldextract", types.SimpleNamespace(TLDExtract=_Extract))
    assert cookie_scope._library_says("example.pages.example") is True
    assert seen["include_psl_private_domains"] is True

    class _Broken:
        def __init__(self, **_kwargs):
            raise RuntimeError("snapshot missing")

    monkeypatch.setitem(_sys.modules, "tldextract", types.SimpleNamespace(TLDExtract=_Broken))
    monkeypatch.setitem(_sys.modules, "publicsuffix2", types.SimpleNamespace(
        get_tld=lambda name, strict=True: name))
    assert cookie_scope._library_says("anything.example") is True
