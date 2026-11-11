"""1.30: the bridge stays usable when Chrome is slow and when another client is stuck.

Measured on a machine pegged by other work (2026-11-11): a fresh tab took 10 s to
be called "complete" and 12.7 s to attach, so `open` failed with "cdp.send timed
out" although nothing was broken. These tests hold the three fixes in place: one
debugger attach per tab however many commands ask at once, an explicit attach
with its own patience, and an outbox per daemon client so one that stopped
reading cannot stall the others.
"""

from __future__ import annotations

import threading
import time

import pytest

from test_chrome_bridge import (
    _TAB_WORLD,
    _VERIFIED_SOCKET,
    _WORKER_READY,
    _node_worker_eval,
    answer_error,
    requires_node,
)
from web_search_neo import bridge_attach, bridge_patience
from web_search_neo.bridge_daemon import _Client
from web_search_neo.bridge_outbox import Outbox

# A debugger that takes a while to attach and records what it was asked.
_SLOW_DEBUGGER = """
const seen = {attaches: 0, methods: [], inFlight: 0, peak: 0};
chrome.debugger.attach = async () => { seen.attaches += 1; await globalThis.__sleep(80); };
chrome.debugger.sendCommand = async (target, method) => {
  seen.methods.push(method);
  if (method !== "Runtime.enable" && method !== "Runtime.evaluate") {
    seen.inFlight += 1;
    seen.peak = Math.max(seen.peak, seen.inFlight);
    await globalThis.__sleep(30);
    seen.inFlight -= 1;
  }
  return method === "Runtime.evaluate" ? {result: {type: "number", value: 2}} : {};
};
const count = name => seen.methods.filter(method => method === name).length;
"""


@requires_node
def test_commands_arriving_together_share_one_debugger_attach() -> None:
    """Two commands on a fresh tab used to attach and set it up twice, each pass behind the last."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + _SLOW_DEBUGGER
        + """
        const socket = await openSocket();
        const before = socket.sent.length;
        const send = (id, method, params) =>
          socket.onmessage({data: JSON.stringify({type: "command", id, method, params})});
        send("a", "cdp.send", {tabId: 7, method: "Runtime.evaluate", params: {expression: "1+1"}});
        send("b", "events.subscribe", {tabId: 7, domains: ["console"]});
        send("c", "cdp.send", {tabId: 7, method: "Runtime.evaluate", params: {expression: "2"}});
        await globalThis.__waitFor(() => socket.sent.length >= before + 3, "all three answers");
        const answers = socket.sent.slice(before).map(item => JSON.parse(item));
        return {attaches: seen.attaches, autoAttach: count("Target.setAutoAttach"),
                focus: count("Emulation.setFocusEmulationEnabled"), answers};
        """
    )
    assert [answer_error(answer) for answer in outcome["answers"]] == [None, None, None]
    assert outcome["attaches"] == 1
    assert outcome["autoAttach"] == 1 and outcome["focus"] == 1


@requires_node
def test_the_setup_after_attach_goes_out_together() -> None:
    """None of the four setup commands depends on another, so they do not wait for each other."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + _SLOW_DEBUGGER
        + """
        const socket = await openSocket();
        const answer = await ask(socket, "debugger.attach", {tabId: 7});
        return {answer, peak: seen.peak, order: seen.methods};
        """
    )
    assert answer_error(outcome["answer"]) is None
    assert outcome["peak"] == 4
    assert outcome["order"][0] == "Runtime.enable"  # the one command the rest do wait for


@requires_node
def test_debugger_attach_reports_its_timing_and_reuses_the_attachment() -> None:
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + _SLOW_DEBUGGER
        + """
        const socket = await openSocket();
        const first = await ask(socket, "debugger.attach", {tabId: 7});
        const second = await ask(socket, "debugger.attach", {tabId: 7});
        return {first, second, attaches: seen.attaches};
        """
    )
    first, second = outcome["first"]["result"], outcome["second"]["result"]
    assert first["attached"] is True and first["reused"] is False and first["tab_id"] == 7
    assert first["attach_ms"] >= 70 and first["total_ms"] >= first["attach_ms"]
    assert first["setup_ms"] >= 0
    assert second["attached"] is True and second["reused"] is True
    assert outcome["attaches"] == 1


@requires_node
def test_a_failed_attach_is_not_remembered_and_the_next_call_tries_again() -> None:
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        let attempts = 0;
        chrome.debugger.attach = async () => {
          attempts += 1;
          if (attempts === 1) throw new Error("No tab with given id 7.");
        };
        chrome.debugger.sendCommand = async () => ({});
        const socket = await openSocket();
        const first = await ask(socket, "debugger.attach", {tabId: 7});
        const second = await ask(socket, "debugger.attach", {tabId: 7});
        return {first, second, attempts};
        """
    )
    assert "No tab with given id" in answer_error(outcome["first"])
    assert answer_error(outcome["second"]) is None
    assert outcome["attempts"] == 2


@requires_node
def test_a_blank_tab_is_returned_without_waiting_for_chrome_to_call_it_complete() -> None:
    """A busy Chrome left a new about:blank on "loading" for the whole ten seconds."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        chrome.tabs.get = async tabId => ({
          id: tabId, windowId: 1, active: false, status: "loading", url: "about:blank", title: ""});
        const socket = await openSocket();
        const started = Date.now();
        const answer = await ask(socket, "tabs.create", {url: "about:blank", group: ""});
        return {answer, ms: Date.now() - started};
        """
    )
    assert answer_error(outcome["answer"]) is None
    assert outcome["answer"]["result"]["id"] == 7
    assert 1200 <= outcome["ms"] < 2900


# ---------------------------------------------------------------- the server side: attach with its own patience
class _Bridge:
    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, float]] = []
        self.answer, self.error = answer, error

    def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
        self.calls.append((method, params or {}, timeout))
        if method == "debugger.attach" and self.error is not None:
            raise self.error
        return self.answer if method == "debugger.attach" else {"removed": True}


def test_attach_waits_as_long_as_a_busy_machine_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WSN_ATTACH_TIMEOUT", raising=False)
    bridge = _Bridge(answer={"tab_id": 9, "attached": True, "reused": False, "total_ms": 12700})
    timing = bridge_attach.attach_debugger(bridge, 9, created=True)
    assert timing["total_ms"] == 12700
    assert bridge.calls == [("debugger.attach", {"tabId": 9}, 60.0)]

    monkeypatch.setenv("WSN_ATTACH_TIMEOUT", "120")
    bridge_attach.attach_debugger(bridge, 9, created=True)
    assert bridge.calls[-1][2] == 120.0
    monkeypatch.setenv("WSN_ATTACH_TIMEOUT", "not a number")
    assert bridge_attach.attach_timeout() == 60.0
    monkeypatch.setenv("WSN_ATTACH_TIMEOUT", "1")
    assert bridge_attach.attach_timeout() == 5.0


def test_an_older_companion_is_warmed_up_with_a_trivial_script_instead() -> None:
    """Before 1.30 the companion attached inside its first command: give that command the patience."""
    bridge = _Bridge(error=RuntimeError("Error: Unknown bridge method: debugger.attach"))
    assert bridge_attach.attach_debugger(bridge, 9, created=True) is None
    assert [call[0] for call in bridge.calls] == ["debugger.attach", "cdp.send"]
    method, params, timeout = bridge.calls[1]
    assert params == {"tabId": 9, "method": "Runtime.evaluate",
                      "params": {"expression": "1", "returnByValue": True}}
    assert timeout == bridge.calls[0][2] == bridge_attach.attach_timeout()


def test_a_warm_up_that_never_answers_is_reported_like_a_slow_attach() -> None:
    class Old(_Bridge):
        def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
            self.calls.append((method, params or {}, timeout))
            if method == "debugger.attach":
                raise RuntimeError("Error: Unknown bridge method: debugger.attach")
            if method == "cdp.send":
                raise TimeoutError("Chrome bridge command 'cdp.send' timed out")
            return {"removed": True}

    bridge = Old()
    with pytest.raises(TimeoutError, match="did not attach its debugger"):
        bridge_attach.attach_debugger(bridge, 9, created=True)
    assert [call[0] for call in bridge.calls] == ["debugger.attach", "cdp.send", "tabs.remove"]


def test_an_attach_that_never_answers_closes_the_tab_it_was_opened_for() -> None:
    bridge = _Bridge(error=TimeoutError("Chrome bridge command 'debugger.attach' timed out"))
    with pytest.raises(TimeoutError) as raised:
        bridge_attach.attach_debugger(bridge, 9, created=True)
    assert "busy with other work" in str(raised.value) and "WSN_ATTACH_TIMEOUT" in str(raised.value)
    assert [call[0] for call in bridge.calls] == ["debugger.attach", "tabs.remove"]

    # A tab the user pointed at is theirs: it is never closed for being slow.
    claimed = _Bridge(error=TimeoutError("timed out"))
    with pytest.raises(TimeoutError):
        bridge_attach.attach_debugger(claimed, 9, created=False)
    assert [call[0] for call in claimed.calls] == ["debugger.attach"]


def test_other_attach_failures_reach_the_caller_unchanged() -> None:
    bridge = _Bridge(error=RuntimeError("Chrome would not let the companion drive tab 9"))
    with pytest.raises(RuntimeError, match="would not let the companion drive"):
        bridge_attach.attach_debugger(bridge, 9, created=True)
    assert [call[0] for call in bridge.calls] == ["debugger.attach"]


def test_a_timeout_says_how_long_it_waited_and_that_chrome_may_only_be_busy() -> None:
    message = bridge_patience.timeout_message("cdp.send", 15.0)
    assert message.startswith("Chrome bridge command 'cdp.send' timed out after 15 s")
    assert "retry" in message


# ---------------------------------------------------------------- built-in timeouts stretch while Chrome is slow
class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_built_in_timeouts_stretch_only_while_chrome_answers_slowly() -> None:
    clock = _Clock()
    patience = bridge_patience.Patience(now=clock)
    assert patience.stretch(15.0) == 15.0  # nothing measured yet
    patience.note("tabs.get", 0.2)
    assert patience.stretch(15.0) == 15.0  # a quick Chrome changes nothing
    patience.note("debugger.attach", 12.7)
    assert patience.stretch(15.0) == pytest.approx(50.8)  # four times the slowest answer
    assert patience.stretch(90.0) == 90.0  # a longer wait is never shortened
    patience.note("events.subscribe", 60.0)
    assert patience.stretch(15.0) == bridge_patience.Patience.MAX_SECONDS
    clock.now += bridge_patience.Patience.WINDOW_SECONDS + 1
    assert patience.stretch(15.0) == 15.0  # old news once the load has passed


def test_page_work_is_not_taken_for_a_slow_chrome() -> None:
    """A heavy page, a long navigation or the old companion's blank-tab wait say nothing about load."""
    patience = bridge_patience.Patience()
    for method in ("cdp.send", "tabs.navigate", "tabs.create", "frames.resolve"):
        patience.note(method, 40.0)
    assert patience.floor() == 0.0
    assert bridge_patience.patient(_Bridge(), 15.0) == 15.0  # a bridge that measures nothing


def test_the_bridge_times_its_probe_commands_and_counts_one_that_never_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from web_search_neo import chrome_bridge

    bridge = chrome_bridge.ChromeBridge(token="t", spawn=False)
    monkeypatch.setattr(bridge, "start", lambda: None)
    bridge._connected.set()
    clock = _Clock()
    bridge.patience = bridge_patience.Patience(now=clock, load=_Load(0.1))
    answered: list[str] = []

    class Daemon:
        silent = False

        def send(self, payload: str) -> None:
            message = json.loads(payload)
            answered.append(message["method"])
            if self.silent:
                return
            pending = bridge._pending[message["id"]]
            pending.result = {"id": 7}
            pending.event.set()

    bridge._daemon = Daemon()
    original_send = Daemon.send

    def slow_send(self, payload: str) -> None:
        clock.now += 6.0  # Chrome took six seconds to answer
        original_send(self, payload)

    monkeypatch.setattr(Daemon, "send", slow_send)
    assert bridge.request("tabs.get", {"tabId": 7}, timeout=10.0) == {"id": 7}
    assert bridge.patience.slowest() == pytest.approx(6.0)
    assert bridge_patience.patient(bridge, 15.0) == pytest.approx(24.0)

    Daemon.silent = True
    with pytest.raises(TimeoutError, match="timed out after 0 s"):
        bridge.request("events.subscribe", {"tabId": 7}, timeout=0.2)
    assert answered == ["tabs.get", "events.subscribe"]
    bridge.patience.note("events.subscribe", 10.0)  # what a real ten-second timeout leaves behind
    assert bridge_patience.patient(bridge, 15.0) == pytest.approx(40.0)


class _Load:
    """A machine whose CPU is as busy as the test says."""

    def __init__(self, busy: float | None) -> None:
        self.busy, self.looks = busy, 0

    def look(self) -> float | None:
        self.looks += 1
        return self.busy


def test_cpu_load_is_the_share_of_cpu_time_used_between_two_looks() -> None:
    clock = _Clock()
    times = [(100.0, 1000.0)]
    load = bridge_patience.CpuLoad(read=lambda: times[-1], now=clock)
    assert load.look() is None  # one look is a mark, not a measurement
    clock.now += 15.0
    times.append((100.0 + 15 * 16 * 0.95, 1000.0 + 15 * 16))  # sixteen cores, 95 % busy for 15 s
    assert load.look() == pytest.approx(0.95)
    clock.now += 0.1
    times.append((9999.0, 99999.0))
    assert load.look() == pytest.approx(0.95)  # too soon after the last look to measure again
    assert bridge_patience.CpuLoad(read=lambda: None).look() in (None, pytest.approx(0.5, abs=0.5))
    real = bridge_patience.CpuLoad()
    real.look()
    time.sleep(0.6)
    assert 0.0 <= real.look() <= 1.0  # this machine can be read


def _answer_after(seconds: float) -> threading.Event:
    answered = threading.Event()
    threading.Timer(seconds, answered.set).start()
    return answered


def test_a_built_in_limit_goes_on_waiting_while_the_machine_is_busy() -> None:
    """The answer came at 0.25 s; the limit was 0.1 s and the CPU was pegged the whole time."""
    busy = bridge_patience.Patience(load=_Load(0.97))
    busy.wait(_answer_after(0.25), "cdp.send", bridge_patience.BuiltIn(0.1))

    # The same late answer on an idle machine is a hung page: fail at the limit, as before.
    idle = bridge_patience.Patience(load=_Load(0.12))
    with pytest.raises(TimeoutError, match="the companion did not answer"):
        idle.wait(_answer_after(0.25), "cdp.send", bridge_patience.BuiltIn(0.1))

    # A limit somebody chose is theirs, however busy the machine is; the CPU is not even read.
    chosen_load = _Load(0.97)
    chosen = bridge_patience.Patience(load=chosen_load)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        chosen.wait(_answer_after(0.25), "cdp.send", 0.1)
    assert time.monotonic() - started < 0.24 and chosen_load.looks == 0

    # Where the load cannot be read the limit stays what it was.
    with pytest.raises(TimeoutError):
        bridge_patience.Patience(load=_Load(None)).wait(threading.Event(), "cdp.send", bridge_patience.BuiltIn(0.1))


def test_an_extended_wait_that_still_gets_no_answer_says_so() -> None:
    patience = bridge_patience.Patience(load=_Load(0.97))
    started = time.monotonic()
    with pytest.raises(TimeoutError) as raised:
        patience.wait(threading.Event(), "cdp.send", bridge_patience.BuiltIn(0.1))
    took = time.monotonic() - started
    assert 0.38 <= took < 1.5  # four limits, not one and not forever
    assert "CPU was at 97%" in str(raised.value) and "slow rather than broken" in str(raised.value)
    message = bridge_patience.timeout_message("cdp.send", 60.0, 0.97)
    assert message.startswith("Chrome bridge command 'cdp.send' timed out after 60 s: the wait was extended")


class _SlowBridge(_Bridge):
    """A bridge that has just watched Chrome take ten seconds over a cheap command."""

    def __init__(self) -> None:
        super().__init__(answer={"attached": True})
        self.patience = bridge_patience.Patience()
        self.patience.note("tabs.get", 10.0)

    def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
        self.calls.append((method, params or {}, timeout))
        if method == "tabs.create":
            return {"id": 9, "group": "AI"}
        return {"result": {"value": 1}} if method == "cdp.send" else {"attached": True}


def test_the_driver_gives_its_default_commands_the_patience_and_keeps_a_callers_limit() -> None:
    from web_search_neo.chrome_bridge import ChromeBridgeDriver

    bridge = _SlowBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI")
    subscribe = next(call for call in bridge.calls if call[0] == "events.subscribe")
    assert subscribe[2] == pytest.approx(40.0)  # was a flat ten seconds

    driver.set_script_timeout(15)  # what a session sets: the built-in default
    driver.execute_script("return 1")
    assert bridge.calls[-1][2] == pytest.approx(40.0)
    driver.execute_cdp_cmd("Page.enable", {})
    assert bridge.calls[-1][2] == pytest.approx(40.0)

    driver.set_script_timeout(20)  # a limit somebody chose is kept to the second
    driver.execute_script("return 1")
    assert bridge.calls[-1][2] == 20.0
    driver.execute_cdp_cmd("Runtime.evaluate", {"expression": "1"}, timeout=3)
    assert bridge.calls[-1][2] == 3.0


class _LoadingPage:
    """A companion-driven page that leaves its first ``unanswered`` readiness polls without an answer."""

    is_extension_bridge = True

    def __init__(self, unanswered: int = 0, state: str = "complete", clock: _Clock | None = None) -> None:
        self.unanswered, self.state, self.clock = unanswered, state, clock
        self.limits: list[float] = []

    def execute_cdp_cmd(self, command: str, params: dict, timeout: float | None = None):
        assert command == "Runtime.evaluate" and params["expression"] == "document.readyState"
        self.limits.append(timeout)
        if len(self.limits) <= self.unanswered:
            if self.clock is not None:
                self.clock.now += timeout  # the poll waited out its limit
            raise TimeoutError("Chrome bridge command 'cdp.send' timed out after 5 s")
        return {"result": {"type": "string", "value": self.state}}


def test_a_readiness_poll_the_busy_page_left_unanswered_is_not_a_failed_open() -> None:
    """`open` died on the first unanswered `document.readyState` although it had 120 s to wait."""
    from web_search_neo import browser_tools

    page = _LoadingPage(unanswered=2)
    browser_tools._wait_until_ready(page, 30.0)
    assert len(page.limits) == 3
    assert all(not isinstance(limit, bridge_patience.BuiltIn) for limit in page.limits)  # never extended


def test_the_wait_for_a_page_ends_when_the_caller_said_however_slow_each_poll_is() -> None:
    """With 120 s to wait and every poll unanswered, `open` took 287 s. Now it takes 120."""
    from selenium.common.exceptions import TimeoutException

    from web_search_neo import page_ready

    clock = _Clock()
    started = clock.now
    starved = _LoadingPage(unanswered=10_000, clock=clock)
    with pytest.raises(TimeoutException) as raised:
        page_ready.wait_until_ready(starved, 120.0, now=clock, sleep=lambda seconds: None)
    assert clock.now - started == pytest.approx(120.0, abs=1.0)
    assert max(starved.limits) == page_ready.PROBE_SECONDS  # a poll is short
    assert starved.limits[-1] <= page_ready.PROBE_SECONDS and min(starved.limits) >= 0.5
    assert "within 120 s" in str(raised.value) and "left the question unanswered" in str(raised.value)

    # A page that answers but is still fetching something is named as that.
    def tick(seconds: float) -> None:
        clock.now += seconds

    streaming = _LoadingPage(state="interactive")
    with pytest.raises(TimeoutException, match="was still 'interactive'"):
        page_ready.wait_until_ready(streaming, 3.0, now=clock, sleep=tick)


def test_a_selenium_page_is_asked_the_way_it_always_was() -> None:
    from web_search_neo import page_ready

    class Selenium:
        asked: list[str] = []

        def execute_script(self, script: str):
            self.asked.append(script)
            return "loading" if len(self.asked) < 3 else "complete"

    page = Selenium()
    page_ready.wait_until_ready(page, 5.0, sleep=lambda seconds: None)
    assert page.asked == ["return document.readyState"] * 3


# ---------------------------------------------------------------- the daemon: one stuck client stalls nobody else
class _Wire:
    """A connection whose send blocks until released, like a peer that stopped reading."""

    def __init__(self, blocked: bool = False) -> None:
        self.sent: list[str] = []
        self.release = threading.Event()
        self.closed: tuple | None = None
        if not blocked:
            self.release.set()

    def send(self, text: str) -> None:
        assert self.release.wait(10.0), "the test never released the wire"
        self.sent.append(text)

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def test_a_client_that_stopped_reading_does_not_block_the_sender() -> None:
    stuck, healthy = _Wire(blocked=True), _Wire()
    slow = _Client(connection=stuck, version="", label="stuck.py#1")
    fine = _Client(connection=healthy, version="", label="fine.py#2")

    started = time.monotonic()
    slow.send({"type": "result", "id": "a"})   # the reader thread used to hang here
    fine.send({"type": "result", "id": "b"})
    assert time.monotonic() - started < 1.0
    assert fine.outbox.flush(5.0)
    assert healthy.sent == ['{"type": "result", "id": "b"}']
    assert stuck.sent == []

    stuck.release.set()
    assert slow.outbox.flush(5.0)
    assert stuck.sent == ['{"type": "result", "id": "a"}']


def test_frames_reach_a_client_in_the_order_they_were_queued() -> None:
    wire = _Wire()
    client = _Client(connection=wire, version="", label="main.py#3")
    for number in range(200):
        client.send({"n": number})
    assert client.outbox.flush(5.0)
    assert wire.sent == [f'{{"n": {number}}}' for number in range(200)]


def test_a_client_too_far_behind_is_disconnected_rather_than_buffered_forever() -> None:
    wire = _Wire(blocked=True)
    outbox = Outbox(max_pending_bytes=100)
    outbox.put(wire, "x" * 60, "lagging.py#4")
    with pytest.raises(ConnectionError, match="stopped reading"):
        outbox.put(wire, "y" * 60, "lagging.py#4")
    deadline = time.monotonic() + 5.0
    while wire.closed is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert wire.closed == (1011, "Bridge client stopped reading")
    with pytest.raises(ConnectionError, match="is gone"):
        outbox.put(wire, "z", "lagging.py#4")
    wire.release.set()


def test_a_closed_outbox_writes_what_was_queued_and_then_refuses() -> None:
    wire = _Wire()
    outbox = Outbox()
    outbox.put(wire, "one", "main.py#5")
    outbox.close()
    assert outbox.flush(5.0)
    assert wire.sent == ["one"]
    with pytest.raises(ConnectionError):
        outbox.put(wire, "two", "main.py#5")


def test_a_write_error_marks_the_client_gone() -> None:
    class Broken:
        def send(self, text: str) -> None:
            raise OSError("the pipe is broken")

    outbox = Outbox()
    outbox.put(Broken(), "one", "broken.py#6")
    assert outbox.flush(5.0)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            outbox.put(Broken(), "two", "broken.py#6")
        except ConnectionError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("the outbox kept accepting frames for a broken connection")
