"""Closing tabs the agent never opened.

Until close_tabs existed, the only tab this server could close was one it had
opened itself, so an agent asked to tidy up a browser had nothing to do it with:
``close`` on an attached tab detaches and hands it back, which is right for a
borrowed tab and useless for a discarded one.

What is worth pinning down here is not that the call reaches ``tabs.remove`` -
it is the refusals. Closing a tab cannot be undone, and the two ways to get it
badly wrong are taking the pinned tabs a person keeps on purpose and yanking a
page out from under another agent mid-run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chrome_bridge


class FakeBridge:
    """Answers tabs.remove and records what it was asked to remove."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.removed: list[int] = []
        self.raise_on: set[int] = set()

    def status(self, wait_seconds: float = 1.0) -> dict:
        return {"connected": self.connected, "host": "127.0.0.1", "port": 8765}

    def request(self, method: str, params: dict | None = None, timeout: float = 5.0):
        if method != "tabs.remove":
            raise AssertionError("unexpected bridge call %r" % method)
        tab_id = int((params or {})["tabId"])
        if tab_id in self.raise_on:
            raise RuntimeError("chrome.tabs.remove failed")
        self.removed.append(tab_id)
        return {"removed": True, "id": tab_id}


@pytest.fixture
def wired(monkeypatch):
    """Wire a fake bridge plus a tab listing that shrinks as tabs are removed."""
    bridge = FakeBridge()
    tabs = [
        {"id": 1, "title": "plain", "url": "https://a.test/", "pinned": False},
        {"id": 2, "title": "pinned", "url": "https://b.test/", "pinned": True},
        {"id": 3, "title": "theirs", "url": "https://c.test/", "pinned": False,
         "driven_by": "other.py#99", "driven_by_me": False},
        {"id": 4, "title": "mine", "url": "https://d.test/", "pinned": False,
         "driven_by": "main.py#1", "driven_by_me": True},
    ]

    def listing(wait_seconds: float = 1.0) -> dict:
        alive = [t for t in tabs if t["id"] not in bridge.removed]
        return {"connected": bridge.connected, "tabs": alive}

    monkeypatch.setattr(chrome_bridge, "get_chrome_bridge", lambda: bridge)
    monkeypatch.setattr(chrome_bridge, "list_current_chrome_tabs", listing)
    return bridge


def test_closes_a_plain_tab(wired):
    result = chrome_bridge.close_current_chrome_tabs([1])
    assert wired.removed == [1]
    assert [entry["tab_id"] for entry in result["closed"]] == [1]
    assert result["failed"] == []


def test_pinned_tab_is_refused_by_default(wired):
    """A pinned tab is the set the user keeps on purpose, not clutter."""
    result = chrome_bridge.close_current_chrome_tabs([2])
    assert wired.removed == []
    assert result["closed"] == []
    reason = result["skipped"][0]["reason"]
    assert "pinned" in reason and "include_pinned" in reason


def test_pinned_tab_closes_when_asked_by_name(wired):
    result = chrome_bridge.close_current_chrome_tabs([2], include_pinned=True)
    assert wired.removed == [2]
    assert [entry["tab_id"] for entry in result["closed"]] == [2]


def test_another_agents_tab_is_refused(wired):
    """Closing it would pull the page out from under a running session."""
    result = chrome_bridge.close_current_chrome_tabs([3])
    assert wired.removed == []
    assert "other.py#99" in result["skipped"][0]["reason"]


def test_our_own_driven_tab_is_not_refused(wired):
    """The caller can see from the listing that this one is theirs."""
    result = chrome_bridge.close_current_chrome_tabs([4])
    assert wired.removed == [4]
    assert [entry["tab_id"] for entry in result["closed"]] == [4]


def test_a_tab_that_is_already_gone_is_not_a_failure(wired):
    """Already closed is the outcome the caller asked for."""
    result = chrome_bridge.close_current_chrome_tabs([99])
    assert result["closed"] == []
    assert result["failed"] == []
    assert result["skipped"][0]["already_gone"] is True


def test_outcome_follows_the_tab_list_not_the_acknowledgement(wired, monkeypatch):
    """tabs.remove throwing does not mean the tab survived.

    The extension reports failure whenever chrome.tabs.remove throws, and a tab
    the user closed by hand a moment earlier throws. Trusting that answer made
    the two most ordinary teardowns report a leak that was not there.
    """
    wired.raise_on = {1}
    reads: list[int] = []

    def listing(wait_seconds: float = 1.0) -> dict:
        # The tab is there when we look before removing and gone when we look
        # after, even though chrome.tabs.remove threw: the user had closed it
        # in between.
        reads.append(1)
        if len(reads) == 1:
            return {"connected": True,
                    "tabs": [{"id": 1, "pinned": False}, {"id": 2, "pinned": True}]}
        return {"connected": True, "tabs": [{"id": 2, "pinned": True}]}

    monkeypatch.setattr(chrome_bridge, "list_current_chrome_tabs", listing)
    result = chrome_bridge.close_current_chrome_tabs([1])
    assert [entry["tab_id"] for entry in result["closed"]] == [1]
    assert result["failed"] == []


def test_a_tab_that_survives_is_reported_as_failed(wired, monkeypatch):
    monkeypatch.setattr(
        chrome_bridge, "list_current_chrome_tabs",
        lambda wait_seconds=1.0: {"connected": True,
                                  "tabs": [{"id": 1, "pinned": False}]},
    )
    result = chrome_bridge.close_current_chrome_tabs([1])
    assert result["closed"] == []
    assert [entry["tab_id"] for entry in result["failed"]] == [1]


def test_ids_are_deduplicated_and_junk_is_reported(wired):
    result = chrome_bridge.close_current_chrome_tabs([1, 1, "nonsense"])
    assert wired.removed == [1]
    assert result["requested"] == [1]
    assert any(entry["reason"] == "not a tab id" for entry in result["skipped"])


def test_nothing_is_sent_when_chrome_is_not_connected(wired):
    wired.connected = False
    result = chrome_bridge.close_current_chrome_tabs([1])
    assert wired.removed == []
    assert result["closed"] == []


def test_closing_a_tab_forgets_the_session_sitting_on_it(monkeypatch):
    """A session on a dead tab would dispatch at whatever id Chrome reuses."""
    import browser_tools

    monkeypatch.setattr(
        browser_tools, "close_current_chrome_tabs",
        lambda tab_ids, **kw: {"connected": True, "requested": list(tab_ids),
                               "closed": [{"tab_id": 7}], "failed": [], "skipped": []},
    )
    released: list[int] = []
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", released.append)

    class FakeSession:
        current_tab_id = 7

    monkeypatch.setitem(browser_tools._sessions, "doomed", FakeSession())
    result = browser_tools.close_tabs([7])
    assert result["sessions_dropped"] == ["doomed"]
    assert "doomed" not in browser_tools._sessions
    assert released == [7]


class TestForbidCurrentProfile:
    """Keeping a run out of the browser its user is working in.

    profile_mode defaults to "current", so an agent that never mentions the
    mode drives the real Chrome someone is using. For a benchmark or any batch
    job that means their window fills with tabs. The prompt can ask for better
    behaviour and a weak model will ignore it, so the switch has to be here.
    """

    def test_current_is_downgraded_not_refused(self, monkeypatch):
        """Работа продолжается, просто в своём браузере."""
        import browser_tools
        monkeypatch.setenv('WSN_FORBID_CURRENT_PROFILE', '1')
        assert browser_tools.resolve_profile_mode('current', None) == 'temporary'

    def test_auto_does_not_fall_back_to_the_users_chrome(self, monkeypatch):
        import browser_tools
        monkeypatch.setenv('WSN_FORBID_CURRENT_PROFILE', '1')
        assert browser_tools.resolve_profile_mode('auto', None) == 'temporary'

    def test_extension_alias_is_covered_too(self, monkeypatch):
        """'extension' is just another spelling of 'current' and must not escape."""
        import browser_tools
        monkeypatch.setenv('WSN_FORBID_CURRENT_PROFILE', '1')
        assert browser_tools.resolve_profile_mode('extension', None) == 'temporary'

    def test_unset_leaves_behaviour_alone(self, monkeypatch):
        import browser_tools
        monkeypatch.delenv('WSN_FORBID_CURRENT_PROFILE', raising=False)
        assert browser_tools.resolve_profile_mode('current', None) == 'current'

    def test_only_truthy_values_switch_it_on(self, monkeypatch):
        import browser_tools
        monkeypatch.setenv('WSN_FORBID_CURRENT_PROFILE', '0')
        assert browser_tools.resolve_profile_mode('current', None) == 'current'
