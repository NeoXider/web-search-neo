"""What a session leaves behind when it ends badly.

Teardown runs when something has already gone wrong, so it is the least
exercised code here and the easiest place to report a clean result over a tab
that is still open, a debugger that is still attached, or a session that was
never usable in the first place.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from chrome_bridge import (
    ChromeBridge,
    ChromeBridgeDriver,
    ChromeBridgeError,
    ChromeBridgeUnavailable,
)
import browser_tools


class _SwitchTo:
    def default_content(self) -> None:
        return None


class _Bridge:
    """The companion, answering the one question eviction asks it.

    ``reachable=False`` raises what the real client raises when there is nobody
    to ask - ``ChromeBridgeUnavailable`` - and not ``ConnectionError``. The
    difference is the whole test: every transport failure in
    :meth:`ChromeBridge.request` is a ``ChromeBridgeError`` of some kind, so a
    fake that raised something else let a caller that condemns tabs on
    ``ChromeBridgeError`` pass a test written to prove it does not.
    """

    def __init__(self, gone: bool = False, reachable: bool = True) -> None:
        self.gone = gone
        self.reachable = reachable
        self.asked = 0

    def request(self, method: str, params: dict | None = None, **_kwargs: object) -> dict:
        self.asked += 1
        if not self.reachable:
            raise ChromeBridgeUnavailable(
                "Chrome companion extension is not connected, so profile_mode "
                "'current' cannot be used."
            )
        if self.gone:
            raise ChromeBridgeError(f"No tab with id {(params or {}).get('tabId')}")
        return {"id": (params or {}).get("tabId"), "url": "https://example.test/"}


class _Tab:
    """The extension-backed driver, in the respects teardown depends on.

    ``quit`` returns rather than raises, because :meth:`ChromeBridgeDriver.quit`
    structurally cannot raise - teardown may not take its caller down with it -
    and a fake that raised let "a debugger left attached is never reported" pass
    as covered.
    """

    def __init__(self, tab_id: int = 42, bridge: _Bridge | None = None) -> None:
        self.tab_id = tab_id
        self.bridge = bridge or _Bridge()
        self.actual_tab_group = browser_tools.DEFAULT_TAB_GROUP
        self.is_extension_bridge = True
        self.switch_to = _SwitchTo()
        self.calls: list[str] = []
        self.closes = True
        self.detaches = True

    def get(self, url: str) -> None:
        self.calls.append(f"get {url}")

    def quit(self) -> dict[str, object]:
        self.calls.append("quit")
        if not self.detaches:
            return {
                "detached": False,
                "id": self.tab_id,
                "error": "ChromeBridgeError: the companion never answered debugger.detach",
            }
        return {"detached": True, "id": self.tab_id}

    def close_tab(self) -> dict[str, bool]:
        self.calls.append("close_tab")
        return {"removed": self.closes}

    def execute_script(self, script: str, *_args: object) -> object:
        if script.strip() == "return document.readyState":
            return "complete"
        return {"url": "https://example.test/", "title": "Page"}

    def execute_cdp_cmd(self, *_args: object, **_kwargs: object) -> dict:
        return {}


def _register(session_id: str, driver: _Tab, **overrides) -> browser_tools.BrowserSession:
    fields = {
        "driver": driver,
        "headless": False,
        "profile_mode": "current",
        "current_tab_id": driver.tab_id,
        "owns_browser": True,
        "owns_tab": True,
    }
    fields.update(overrides)
    session = browser_tools.BrowserSession(**fields)
    browser_tools._sessions[session_id] = session
    return session


def test_a_claim_that_fails_halfway_leaves_no_session_behind(monkeypatch):
    driver = _Tab()
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: driver)
    monkeypatch.setattr(
        browser_tools,
        "_register_render_bootstrap",
        lambda _session: (_ for _ in ()).throw(ChromeBridgeError("debugger.attach failed")),
    )

    with pytest.raises(ChromeBridgeError):
        browser_tools.attach_current_tab(42, session_id="reader")

    # Otherwise it holds the tab against every other session while answering none.
    assert "reader" not in browser_tools._sessions
    assert "quit" in driver.calls


def test_a_claimed_tab_is_not_closed_when_the_claim_fails(monkeypatch):
    driver = _Tab()
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: driver)
    monkeypatch.setattr(
        browser_tools,
        "_register_render_bootstrap",
        lambda _session: (_ for _ in ()).throw(ChromeBridgeError("debugger.attach failed")),
    )

    with pytest.raises(ChromeBridgeError):
        browser_tools.attach_current_tab(42, session_id="reader")

    assert "close_tab" not in driver.calls


def test_close_says_so_when_the_tab_would_not_close():
    driver = _Tab()
    driver.closes = False
    _register("stuck", driver)

    result = browser_tools.close_session("stuck")

    assert result["closed"] is True
    assert result["tab_closed"] is False
    assert result["released"] is False
    assert "still open" in result["warning"]


def test_close_says_so_when_the_debugger_stays_attached():
    """The one thing only the user can undo: the banner stays on their tab."""
    driver = _Tab()
    driver.detaches = False
    _register("attached", driver)

    result = browser_tools.close_session("attached")

    assert result["released"] is False
    assert "debugger may still be attached to tab 42" in result["warning"]


def test_a_clean_detach_is_not_reported_as_a_problem():
    _register("tidy", _Tab())

    result = browser_tools.close_session("tidy")

    assert "warning" not in result


def test_nothing_to_detach_is_not_a_failed_detach():
    """The extension answers `detached: false` for a tab it never held, which is
    a clean outcome and not a banner left on anybody's tab."""
    driver = _Tab()
    driver.quit = lambda: {"detached": False, "id": driver.tab_id}
    _register("loose", driver)

    result = browser_tools.close_session("loose")

    assert "warning" not in result


def test_a_tab_the_user_already_closed_is_not_reported_as_a_leak():
    """`removed: false` is also what the extension says when `tabs.remove`
    throws, and a tab closed from the tab strip is the commonest reason."""
    driver = _Tab(bridge=_Bridge(gone=True))
    driver.closes = False
    _register("byhand", driver)

    result = browser_tools.close_session("byhand")

    assert result["closed"] is True
    assert result["tab_closed"] is False
    assert "warning" not in result
    assert "released" not in result


def test_close_does_not_claim_a_leak_it_could_not_check():
    """A companion that cannot be asked has not said the tab is still there,
    and has not said it is gone either. Say that, rather than either."""
    driver = _Tab(bridge=_Bridge(reachable=False))
    driver.closes = False
    _register("unsure", driver)

    result = browser_tools.close_session("unsure")

    assert result["tab_closed"] is False
    assert "could not be asked" in result["warning"]
    assert "the tab is still open" not in result["warning"]


def test_one_failed_step_does_not_skip_the_others(monkeypatch):
    """Releasing input used to be in the same try as closing the tab, so a
    hiccup there leaked the tab as well."""
    driver = _Tab()
    monkeypatch.setattr(
        browser_tools,
        "_reset_session_runtime_state",
        lambda _session: (_ for _ in ()).throw(RuntimeError("the page is gone")),
    )
    _register("clumsy", driver)

    result = browser_tools.close_session("clumsy")

    assert driver.calls == ["close_tab", "quit"]
    assert result["tab_closed"] is True


def test_closing_a_session_that_was_never_open_says_so():
    result = browser_tools.close_session("typo")

    assert result["closed"] is False
    assert "typo" in result["note"]


def test_close_all_reports_what_it_could_not_release():
    good = _Tab(1)
    bad = _Tab(2)
    bad.closes = False
    _register("good", good)
    _register("bad", bad)

    result = browser_tools.close_all_sessions()

    assert result["closed_all"] is False
    assert result["closed_sessions"] == ["bad", "good"]
    assert result["tabs_closed"] == 1
    assert "still open" in result["warnings"]["bad"]
    assert "good" not in result["warnings"]


def test_close_all_on_a_clean_run_reports_no_warnings():
    _register("one", _Tab(1))

    result = browser_tools.close_all_sessions()

    assert result["closed_all"] is True
    assert "warnings" not in result
    assert result["tabs_closed"] == 1


def test_a_tab_the_user_closed_frees_its_session_slot(monkeypatch):
    """Four tabs closed from the tab strip used to leave a server that refused
    to open a fifth."""
    for index in range(browser_tools.MAX_SESSIONS):
        _register(f"dead{index}", _Tab(index, _Bridge(gone=True)))
    replacement = _Tab(99)
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: replacement)

    session = browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")

    assert session.driver is replacement
    assert sorted(browser_tools._sessions) == ["fresh"]


def test_a_companion_that_cannot_be_asked_condemns_nothing(monkeypatch):
    """A transport failure says the daemon is down, not that the tabs are."""
    for index in range(browser_tools.MAX_SESSIONS):
        _register(f"live{index}", _Tab(index, _Bridge(reachable=False)))
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(99))

    with pytest.raises(RuntimeError, match="Maximum of"):
        browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")

    assert len(browser_tools._sessions) == browser_tools.MAX_SESSIONS


def test_the_cap_names_the_session_to_close(monkeypatch):
    for index in range(browser_tools.MAX_SESSIONS):
        session = _register(f"live{index}", _Tab(index, _Bridge()))
        session.last_used = 100.0 + index
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(99))

    with pytest.raises(RuntimeError) as failure:
        browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")

    message = str(failure.value)
    assert "Least recently used: 'live0'" in message
    assert "live3" in message


def test_selenium_sessions_are_never_asked_about_their_tab(monkeypatch):
    """They have no companion to ask, and no tab id that means anything to it."""
    bridge = _Bridge(gone=True)
    driver = _Tab(1, bridge)
    driver.is_extension_bridge = False
    _register("selenium", driver, profile_mode="temporary")

    browser_tools._drop_sessions_whose_tab_is_gone()

    assert bridge.asked == 0
    assert "selenium" in browser_tools._sessions


@pytest.mark.parametrize(
    ("bridge", "exists"),
    [
        (_Bridge(), True),
        (_Bridge(gone=True), False),
        (_Bridge(reachable=False), None),
    ],
    ids=["answered-it-is-there", "answered-it-is-not", "never-answered"],
)
def test_only_an_answer_says_anything_about_a_tab(bridge, exists):
    session = browser_tools.BrowserSession(
        driver=_Tab(42, bridge),
        headless=False,
        profile_mode="current",
        current_tab_id=42,
    )

    assert browser_tools._tab_still_exists(session) is exists
    assert browser_tools._tab_is_gone(session) is (exists is False)


def test_the_cap_sweep_asks_chrome_with_the_session_registry_unlocked(monkeypatch):
    """Every browser tool call goes through `_get_session`, which wants
    `_sessions_lock`; the sweep used to hold it for a round trip per dead
    session, so one wedged companion stalled the whole server."""
    probing = threading.Event()
    may_answer = threading.Event()

    class _Wedged(_Bridge):
        def request(self, method: str, params: dict | None = None, **kwargs: object) -> dict:
            probing.set()
            assert may_answer.wait(20), "the sweep was never let go"
            return super().request(method, params, **kwargs)

    for index in range(browser_tools.MAX_SESSIONS):
        _register(f"dead{index}", _Tab(index, _Wedged(gone=True)))
    monkeypatch.setattr(browser_tools, "create_driver", lambda *a, **k: _Tab(99))

    opened: list[object] = []
    caller = threading.Thread(
        target=lambda: opened.append(
            browser_tools._create_session("fresh", 1280, 800, None, profile_mode="current")
        )
    )
    caller.start()
    try:
        assert probing.wait(20), "the sweep never got as far as asking"
        took_it = browser_tools._sessions_lock.acquire(timeout=5)
        if took_it:
            browser_tools._sessions_lock.release()
        assert took_it, "the sweep is holding _sessions_lock across a bridge round trip"
    finally:
        may_answer.set()
        caller.join(timeout=30)

    assert opened, "the fifth session never opened"
    assert sorted(browser_tools._sessions) == ["fresh"]


def test_the_sweep_leaves_alone_a_session_another_thread_is_using():
    """Its driver is not idle: quitting it would pull the debugger out from
    under whatever that thread is in the middle of."""
    busy = _Tab(1, _Bridge(gone=True))
    idle = _Tab(2, _Bridge(gone=True))
    _register("busy", busy)
    _register("idle", idle)
    sweep = threading.Thread(target=browser_tools._drop_sessions_whose_tab_is_gone)

    with browser_tools._sessions["busy"].lock:
        sweep.start()
        sweep.join(timeout=20)

    assert not sweep.is_alive(), "the sweep waited for a session in use"
    assert "busy" in browser_tools._sessions
    assert busy.calls == []
    # The one nobody was using is still swept.
    assert "idle" not in browser_tools._sessions
    assert "quit" in idle.calls


def test_the_session_lock_can_tell_a_second_caller_from_a_reentrant_one():
    """Counting depth would report one careful tool as two competing agents."""
    lock = browser_tools.SessionLock()
    assert lock.busy is False and lock.concurrent_callers == 0

    holding = threading.Event()
    inside = threading.Event()
    may_finish = threading.Event()

    def other_agent() -> None:
        holding.set()
        with lock:
            inside.set()
            may_finish.wait(5)

    caller = threading.Thread(target=other_agent)
    try:
        with lock:
            # The same thread taking its own session's lock twice is one caller.
            with lock:
                assert lock.busy is False and lock.concurrent_callers == 0

            caller.start()
            assert holding.wait(5)
            # It is queued behind us, which is exactly the overlap worth naming.
            deadline = time.monotonic() + 5
            while lock.waiting != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert lock.waiting == 1
            assert lock.concurrent_callers == 1

        # Now that it holds the lock, the same overlap reads from the other side.
        assert inside.wait(5)
        assert lock.busy is True
        assert lock.concurrent_callers == 1
    finally:
        may_finish.set()
        caller.join(timeout=10)
    assert caller.is_alive() is False

    assert lock.waiting == 0 and lock.busy is False
    # And the non-blocking form the cap sweep uses still works, and still counts
    # nothing: a caller that refuses to wait is not waiting.
    assert lock.acquire(blocking=False) is True
    try:
        assert lock.waiting == 0
    finally:
        lock.release()


def test_browser_status_says_when_another_caller_is_inside_the_same_session():
    """Two subagents of one run share this process, so they share 'default'.

    Nothing here can tell them apart - an MCP call carries no caller identity -
    so the tab is genuinely shared and each one's navigation lands in the
    other's page. What was wrong was that it happened in silence: the second
    caller waited for the lock, took it, and read a page somebody else had
    opened as its own.
    """
    _register("default", _Tab(1))
    _register("other", _Tab(2))
    session = browser_tools._sessions["default"]
    released = threading.Event()
    holding = threading.Event()

    def other_agent() -> None:
        with session.lock:
            holding.set()
            # Long enough for the status call below to be started while we are
            # inside, and short enough that the test never hangs on us.
            released.wait(3)

    caller = threading.Thread(target=other_agent)
    caller.start()
    try:
        assert holding.wait(5)
        status = browser_tools.get_status("default")
    finally:
        released.set()
        caller.join(timeout=10)

    assert status["shared_session"] is True
    assert status["concurrent_callers"] >= 1
    assert "session_id" in status["shared_session_warning"]
    # Named so an agent can act on it, rather than left as a number to interpret.
    assert "default" in status["shared_session_warning"]
    assert status["sessions_in_use"] == ["default"], "the idle session read as busy"


def test_a_session_nobody_else_is_touching_is_reported_without_a_warning():
    """The warning has to stay invisible in the ordinary case or it is noise."""
    _register("solo", _Tab(3))
    status = browser_tools.get_status("solo")
    assert "shared_session" not in status
    assert "shared_session_warning" not in status
    assert status["sessions_in_use"] == []


def test_the_session_cap_is_a_setting_because_agents_outnumber_it(monkeypatch):
    """The cap counts every session in the process and parallel agents share it,
    so five subagents hit a wall that has nothing to do with any of them."""
    monkeypatch.setenv("WEB_SEARCH_NEO_MAX_SESSIONS", "9")
    assert browser_tools._max_sessions() == 9
    # A floor, because zero would refuse every open and tell the caller to close
    # something that cannot exist.
    monkeypatch.setenv("WEB_SEARCH_NEO_MAX_SESSIONS", "0")
    assert browser_tools._max_sessions() == 1
    monkeypatch.setenv("WEB_SEARCH_NEO_MAX_SESSIONS", "not a number")
    assert browser_tools._max_sessions() == 8
    monkeypatch.delenv("WEB_SEARCH_NEO_MAX_SESSIONS")
    # Eight, not four: a session in the mode agents actually use is one tab of a
    # Chrome that is already running, and four of them turned five parallel
    # agents into four working agents and one that could not open a page at all.
    assert browser_tools._max_sessions() == 8
    assert browser_tools.DEFAULT_MAX_SESSIONS == 8


def test_the_full_session_cap_names_the_setting_that_lifts_it(monkeypatch):
    """A wall with no door written on it sends the reader to the source."""
    for index in range(browser_tools.MAX_SESSIONS):
        _register(f"agent{index}", _Tab(index + 1))
    with pytest.raises(RuntimeError) as refusal:
        browser_tools._create_session(
            "one-too-many", 1440, 900, False, "current", None, None, None,
            browser_tools.DEFAULT_TAB_GROUP,
        )
    assert "WEB_SEARCH_NEO_MAX_SESSIONS" in str(refusal.value)
    assert "parallel agents" in str(refusal.value)


# What the real client and the real driver do, which is the only thing that
# makes the fakes above worth anything: two of the tests here were green over
# live defects because the fakes were kinder than the code they stood in for.


def test_a_client_with_nobody_to_ask_raises_the_unavailable_kind():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    client = ChromeBridge(
        port=port, token="a1" * 32, spawn=False, connect_timeout=0.2, start_timeout=0.2
    )
    try:
        with pytest.raises(ChromeBridgeUnavailable):
            client.request("tabs.get", {"tabId": 42}, timeout=0.5)
    finally:
        client.shutdown()
    # And it is still a ChromeBridgeError, so nothing that catches those changes.
    assert issubclass(ChromeBridgeUnavailable, ChromeBridgeError)


def test_the_real_driver_reports_a_failed_detach_instead_of_raising():
    """`quit` cannot raise - teardown may not take its caller down - so the
    outcome has to be in the return value or it is nowhere."""

    class _Deaf:
        def request(self, method: str, params: dict | None = None, **_kwargs: object):
            raise ChromeBridgeError("the companion never answered debugger.detach")

    driver = ChromeBridgeDriver.__new__(ChromeBridgeDriver)
    driver.bridge = _Deaf()
    driver.tab_id = 42

    answer = driver.quit()

    assert answer["detached"] is False
    assert "never answered" in answer["error"]


def test_the_real_driver_passes_on_a_detach_that_worked():
    class _Companion:
        def request(self, method: str, params: dict | None = None, **_kwargs: object):
            return {"detached": True}

    driver = ChromeBridgeDriver.__new__(ChromeBridgeDriver)
    driver.bridge = _Companion()
    driver.tab_id = 42

    assert driver.quit() == {"detached": True, "id": 42}
