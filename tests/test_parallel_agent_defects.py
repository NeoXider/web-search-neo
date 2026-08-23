"""What broke when several agents shared one MCP server for a day.

Every test here comes from one run: about a hundred job applications filed by
five agents through a single server. Four things went wrong, none of them a
crash - a fifth agent that could never get a session, one agent's ``close_all``
ending the other four's work, a ``page_elements`` answer too large to receive,
and no way at all to find out which agent was where.
"""

from __future__ import annotations

import json
import threading

import pytest

import bridge_daemon
import browser_tools
import main


class _SwitchTo:
    def default_content(self) -> None:
        return None


class _Driver:
    """Enough of a driver for the perception topics: a summary and one payload."""

    def __init__(self, payload: object = None, url: str = "https://example.test/") -> None:
        self.payload = payload
        self.url = url
        self.tab_id = 1
        self.switch_to = _SwitchTo()

    def execute_script(self, script: str, *_args: object) -> object:
        if "location.href" in script:
            return {
                "url": self.url,
                "title": "Vacancy",
                "viewport_width": 1440,
                "viewport_height": 900,
                "page_width": 1440,
                "page_height": 4000,
                "ready_state": "complete",
                "challenge": {},
            }
        return self.payload

    def quit(self) -> dict[str, object]:
        return {"detached": True, "id": self.tab_id}

    def close_tab(self) -> dict[str, bool]:
        return {"removed": True}


def _register(session_id: str, driver: object = None, **overrides) -> browser_tools.BrowserSession:
    fields = {
        "driver": driver if driver is not None else _Driver(),
        "headless": False,
        "profile_mode": "current",
        "current_tab_id": 1,
        "owns_browser": True,
        "owns_tab": False,
    }
    fields.update(overrides)
    session = browser_tools.BrowserSession(**fields)
    browser_tools._sessions[session_id] = session
    return session


# ---------------------------------------------------------------------------
# 1. The cap that stopped the fifth agent working at all
# ---------------------------------------------------------------------------


def test_the_default_cap_leaves_room_for_a_realistic_fan_out(monkeypatch):
    """Four was the number that turned a five-agent run into a four-agent run."""
    monkeypatch.delenv("WEB_SEARCH_NEO_MAX_SESSIONS", raising=False)
    assert browser_tools._max_sessions() >= 8


def test_the_companion_popup_can_raise_the_cap(monkeypatch):
    """The setting has to reach the server, or the popup is decoration.

    The extension cannot call into this process, but its hello already describes
    the browser and the daemon already relays that description to every server.
    The number rides there, so this asserts the reading end of a real channel.
    """
    monkeypatch.delenv("WEB_SEARCH_NEO_MAX_SESSIONS", raising=False)

    class _Bridge:
        def browser_info(self) -> dict[str, object]:
            return {"name": "Chrome", "max_sessions": 12}

    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: _Bridge())
    assert browser_tools.effective_max_sessions() == (12, "companion")


def test_the_daemon_relays_the_popup_setting_to_every_server():
    """The other half of the channel: what the extension puts in its hello has to
    survive the daemon, or the reading end above is reading nothing."""
    relayed = bridge_daemon.browser_state(
        {
            "browser": {
                "name": "Chrome",
                "extension_version": "1.6.0",
                "browser_run": "a" * 32,
                "max_sessions": 12,
            }
        }
    )

    assert relayed["max_sessions"] == 12
    assert relayed[bridge_daemon.BROWSER_RUN_KEY] == "a" * 32


def test_the_server_environment_outranks_the_popup(monkeypatch):
    """A number deployed into the server's environment was said about the server."""
    monkeypatch.setenv("WEB_SEARCH_NEO_MAX_SESSIONS", "3")
    monkeypatch.setattr(browser_tools, "MAX_SESSIONS", 3)

    class _Bridge:
        def browser_info(self) -> dict[str, object]:
            return {"max_sessions": 40}

    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: _Bridge())
    assert browser_tools.effective_max_sessions() == (3, "environment")


@pytest.mark.parametrize("value", [0, -5, "many", None, 10_000, True])
def test_a_nonsense_popup_value_never_becomes_the_cap(monkeypatch, value):
    monkeypatch.delenv("WEB_SEARCH_NEO_MAX_SESSIONS", raising=False)

    class _Bridge:
        def browser_info(self) -> dict[str, object]:
            return {"max_sessions": value}

    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: _Bridge())
    cap, _source = browser_tools.effective_max_sessions()
    assert 1 <= cap <= browser_tools.MAX_SESSIONS_CEILING


def test_a_bridge_that_cannot_be_asked_is_not_an_answer_of_zero(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_NEO_MAX_SESSIONS", raising=False)

    def _explode() -> object:
        raise RuntimeError("no daemon")

    monkeypatch.setattr(browser_tools, "get_chrome_bridge", _explode)
    assert browser_tools.effective_max_sessions() == (browser_tools.MAX_SESSIONS, "default")


def test_the_refusal_at_the_cap_still_names_the_way_out(monkeypatch):
    monkeypatch.setattr(browser_tools, "effective_max_sessions", lambda: (2, "default"))
    _register("a", agent_label="alpha")
    _register("b", agent_label="beta")
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Driver())

    with pytest.raises(RuntimeError) as refusal:
        browser_tools._create_session("c", 1440, 900, False, "current")

    message = str(refusal.value)
    assert "Maximum of 2" in message
    assert "WEB_SEARCH_NEO_MAX_SESSIONS" in message
    # And now who is holding the slots, which is what a blocked agent has to
    # know to decide whether to wait or to complain.
    assert "alpha" in message and "beta" in message


# ---------------------------------------------------------------------------
# 2. close_all, which used to end everybody's work
# ---------------------------------------------------------------------------


def test_close_all_leaves_other_agents_sessions_running():
    _register("mine", agent_label="filer-1")
    _register("theirs", agent_label="filer-2")

    result = browser_tools.close_all_sessions(agent_label="filer-1")

    assert result["closed_sessions"] == ["mine"]
    assert result["kept_sessions"] == [{"session_id": "theirs", "agent_label": "filer-2"}]
    assert result["active_sessions"] == ["theirs"]
    assert "theirs" in browser_tools._sessions


def test_close_all_says_what_it_left_standing():
    """A tidy-up that silently skipped four sessions is as bad as one that
    silently closed them."""
    _register("mine", agent_label="filer-1")
    _register("theirs", agent_label="filer-2")

    result = browser_tools.close_all_sessions(agent_label="filer-1")

    assert "scope='all'" in result["scope_note"]
    assert "1 session(s)" in result["scope_note"]


def test_an_unlabelled_caller_owns_only_unlabelled_sessions():
    _register("anonymous")
    _register("labelled", agent_label="filer-2")

    result = browser_tools.close_all_sessions()

    assert result["closed_sessions"] == ["anonymous"]
    assert sorted(browser_tools._sessions) == ["labelled"]


def test_scope_all_is_still_available_and_still_closes_everything():
    _register("one", agent_label="filer-1")
    _register("two", agent_label="filer-2")

    result = browser_tools.close_all_sessions(scope="all")

    assert result["closed_sessions"] == ["one", "two"]
    assert result["kept_sessions"] == []
    assert browser_tools._sessions == {}


def test_include_foreign_is_the_same_door_by_another_name():
    _register("one", agent_label="filer-1")
    _register("two", agent_label="filer-2")

    result = browser_tools.close_all_sessions(agent_label="filer-1", include_foreign=True)

    assert result["scope"] == "all"
    assert browser_tools._sessions == {}


def test_a_scope_nobody_defined_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="scope must be"):
        browser_tools.close_all_sessions(scope="everything")


def test_process_exit_still_closes_everybody():
    """Ownership stops meaning anything once the interpreter is going down."""
    _register("one", agent_label="filer-1")
    _register("two", agent_label="filer-2")

    browser_tools._close_everything_at_exit()

    assert browser_tools._sessions == {}


# ---------------------------------------------------------------------------
# 3. The 83,616-character page_elements answer
# ---------------------------------------------------------------------------


def _fat_elements(count: int = 300) -> dict[str, object]:
    links = [
        {
            "selector": f"a[data-index='{index}']",
            "text": "Respond to this vacancy " * 12,
            "href": f"https://example.test/vacancy/{index}",
            "visible": True,
        }
        for index in range(count)
    ]
    return {
        "links": links,
        "forms": [],
        "fields": [],
        "buttons": [],
        "iframes": [],
        "found": {"links": count, "forms": 0, "fields": 0, "buttons": 0, "iframes": 0},
        "returned": {"links": count, "forms": 0, "fields": 0, "buttons": 0, "iframes": 0},
        "range": {
            "links": {"start": 0, "end": count, "next_offset": None, "has_more": False},
        },
        "offset": 0,
        "limit": 1000,
        "truncated": False,
    }


def test_page_elements_no_longer_returns_more_than_a_context_can_hold():
    _register("reader", _Driver(_fat_elements()))

    result = browser_tools.get_page_elements("reader", limit=1000)

    assert result["budget_truncated"] is True
    assert result["chars_returned"] <= result["char_budget"]
    assert result["chars_before_budget"] > result["char_budget"]
    assert len(json.dumps(result, ensure_ascii=False)) < 25_000


def test_a_clipped_page_elements_answer_says_where_to_continue():
    _register("reader", _Driver(_fat_elements()))

    result = browser_tools.get_page_elements("reader", limit=1000)

    kept = len(result["links"])
    assert 0 < kept < 300
    assert result["returned"]["links"] == kept
    # The paging numbers describe what was sent, not what was collected: a
    # next_offset past the budget cut would skip the entries it dropped.
    assert result["range"]["links"]["end"] == kept
    assert result["range"]["links"]["next_offset"] == kept
    assert result["range"]["links"]["has_more"] is True
    assert "max_chars" in result["budget_note"]


def test_a_small_page_is_untouched_and_says_so():
    payload = _fat_elements(count=2)
    _register("reader", _Driver(payload))

    result = browser_tools.get_page_elements("reader")

    assert result["budget_truncated"] is False
    assert len(result["links"]) == 2
    assert result["range"]["links"]["next_offset"] is None


def test_a_bigger_budget_returns_more(monkeypatch):
    _register("reader", _Driver(_fat_elements()))

    small = browser_tools.get_page_elements("reader", limit=1000, max_chars=3_000)
    large = browser_tools.get_page_elements("reader", limit=1000, max_chars=60_000)

    assert len(large["links"]) > len(small["links"])


def test_the_budget_shares_itself_between_the_categories():
    """A page whose buttons matter is not helped by a budget spent on links."""
    payload = _fat_elements(count=40)
    payload["buttons"] = [
        {"selector": f"button[data-index='{index}']", "text": "Submit " * 20, "visible": True}
        for index in range(40)
    ]
    payload["found"]["buttons"] = 40
    _register("reader", _Driver(payload))

    result = browser_tools.get_page_elements("reader", limit=1000, max_chars=4_000)

    assert result["links"] and result["buttons"]


def test_page_outline_text_is_bounded_too(monkeypatch):
    _register("reader", _Driver())
    monkeypatch.setattr(
        browser_tools.page_perception,
        "outline",
        lambda *a, **k: {"outline": "node line\n" * 20_000, "limit": 200, "format": "text"},
    )

    result = browser_tools.get_page_outline("reader", max_chars=5_000)

    assert result["budget_truncated"] is True
    assert result["chars_returned"] <= 5_000
    assert result["outline"].endswith("node line")


def test_find_is_bounded_too(monkeypatch):
    _register("reader", _Driver())
    monkeypatch.setattr(
        browser_tools.page_perception,
        "find",
        lambda *a, **k: {
            "query": "submit",
            "matches": [
                {"ref": f"ref:{index}", "name": "Submit application " * 40}
                for index in range(20)
            ],
            "returned": 20,
            "truncated": False,
        },
    )

    result = browser_tools.find_elements("submit", "reader", max_chars=3_000)

    assert result["budget_truncated"] is True
    assert result["returned"] == len(result["matches"]) < 20
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# 4. Nobody could see which agent was where
# ---------------------------------------------------------------------------


def test_a_session_can_carry_the_name_of_the_agent_that_opened_it(monkeypatch):
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Driver())

    session = browser_tools._create_session(
        "worker", 1440, 900, False, "current", agent_label="  applier-3  "
    )

    assert session.agent_label == "applier-3"


def test_no_label_is_not_an_error(monkeypatch):
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Driver())

    session = browser_tools._create_session("worker", 1440, 900, False, "current")

    assert session.agent_label is None


def test_a_second_open_may_introduce_an_agent_that_did_not_the_first_time(monkeypatch):
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Driver())
    browser_tools._create_session("worker", 1440, 900, False, "current")

    again = browser_tools._create_session(
        "worker", 1440, 900, False, "current", agent_label="applier-3"
    )

    assert again.agent_label == "applier-3"


def test_a_label_already_on_a_session_is_not_rewritten(monkeypatch):
    """An id in two agents' hands is the collision shared_session warns about;
    letting the second one rename the owner would hide it."""
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Driver())
    browser_tools._create_session(
        "worker", 1440, 900, False, "current", agent_label="applier-3"
    )

    again = browser_tools._create_session(
        "worker", 1440, 900, False, "current", agent_label="applier-9"
    )

    assert again.agent_label == "applier-3"


def test_the_overview_says_who_is_where(monkeypatch):
    monkeypatch.setattr(browser_tools, "effective_max_sessions", lambda: (8, "default"))
    session = _register("hh", agent_label="applier-3", current_tab_id=77, tab_group="agents")
    session.last_url = "https://hh.ru/vacancy/1"
    session.last_title = "Unity developer"

    overview = browser_tools.sessions_overview()

    row = overview["sessions"][0]
    assert row["session_id"] == "hh"
    assert row["agent_label"] == "applier-3"
    assert row["current_tab_id"] == 77
    assert row["tab_group"] == "agents"
    assert row["last_url"] == "https://hh.ru/vacancy/1"
    assert row["last_title"] == "Unity developer"
    assert row["created_at"] and row["last_used_at"]
    assert row["busy"] is False
    assert overview["sessions_open"] == 1
    assert overview["max_sessions"] == 8
    assert overview["sessions_free"] == 7
    assert overview["sessions_summary"] == "1 of 8 browser sessions open, 0 busy right now."


def test_the_overview_reports_a_session_another_thread_is_inside():
    """`busy` is about somebody else being inside; a session's own thread
    re-entering its lock is one caller, not two."""
    session = _register("busy-one", agent_label="applier-3")
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with session.lock:
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert holding.wait(10)
        overview = browser_tools.sessions_overview()
    finally:
        release.set()
        worker.join(10)

    assert overview["sessions_in_use"] == ["busy-one"]
    assert overview["sessions"][0]["busy"] is True


def test_the_overview_never_touches_another_agents_tab():
    """Asking each tab for its URL would mean waiting on the lock its own agent
    is holding, and status is what a stuck run reads first."""

    class _Explodes(_Driver):
        def execute_script(self, script: str, *_args: object) -> object:
            raise AssertionError("the overview must not drive a session")

    _register("theirs", _Explodes(), agent_label="applier-9")

    overview = browser_tools.sessions_overview()

    assert overview["sessions"][0]["last_url"] is None


def test_browser_status_carries_the_whole_roster():
    _register("mine", agent_label="applier-1")
    _register("theirs", agent_label="applier-2")

    status = browser_tools.get_status("mine")

    assert status["sessions_open"] == 2
    assert {row["agent_label"] for row in status["sessions"]} == {"applier-1", "applier-2"}
    assert status["max_sessions_source"] in {"environment", "companion", "default"}


def test_a_page_read_records_where_the_session_was_last_seen():
    driver = _Driver(_fat_elements(count=1), url="https://hh.ru/vacancy/42")
    session = _register("reader", driver)

    browser_tools.get_page_elements("reader")

    assert session.last_url == "https://hh.ru/vacancy/42"
    assert session.last_title == "Vacancy"


def test_capabilities_reports_occupancy_and_not_only_the_cap():
    """An agent that reads "8" and finds all eight taken has learned nothing."""
    _register("held", agent_label="applier-1")

    limits = main._capabilities()["limits"]

    assert limits["browser_sessions_open"] == 1
    assert limits["browser_sessions_free"] == limits["parallel_browser_sessions"] - 1
    assert limits["response_char_budget_default"] == browser_tools.DEFAULT_RESPONSE_CHAR_BUDGET
