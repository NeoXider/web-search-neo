"""``persist=true`` for temporary/isolated sessions, live across real MCP server processes.

Each test starts the real stdio MCP server (``main.py``) as a subprocess and talks
JSON-RPC to it directly - deliberately not through the MCP SDK's stdio client,
whose Windows job object ends every process the server started when the client
goes (that case is checked separately: the answer has to say so). The server is
stopped the way a client stops it, by closing its stdin, so its exit hook runs.

What must hold: the browser survives the server process, a new process continues
it by ``session_id`` and acts on it, and every way the browser ends - an explicit
close, the TTL (enforced by the record's watchdog, or by the next server when
the watchdog is gone), a server that died without parking - leaves no Chrome or
chromedriver process and no temporary profile behind.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from web_search_neo import extra_actions, owned_parking, persist_actions, process_probe

ROOT = Path(__file__).resolve().parents[1]


class Server:
    """One stdio MCP server process, spoken to in newline-delimited JSON-RPC."""

    def __init__(self, env: dict[str, str] | None = None):
        environment = {**os.environ, "PYTHONPATH": str(ROOT), **(env or {})}
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py")], cwd=str(ROOT), env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "persist-test", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self) -> None:
        for line in self.process.stdout:
            self.lines.put(line)

    def _send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            line = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
            message = json.loads(line)
            if message.get("id") == self.next_id:
                assert "error" not in message, message
                return message["result"]

    def act(self, *steps: dict) -> list[dict]:
        result = self.request("tools/call", {"name": "web_action", "arguments": {
            "actions": list(steps), "continue_on_error": True}})
        return json.loads(result["content"][0]["text"])["results"]

    def one(self, step: dict) -> dict:
        [result] = self.act(step)
        assert result["success"], result.get("error")
        return result["data"]

    def pids(self) -> set[int]:
        """The server's pid - and, behind a venv launcher, the interpreter it started."""
        return {self.process.pid, *(pid for pid, _name in process_probe.children(self.process.pid))}

    def stop(self, timeout: float = 60.0) -> None:
        """Exit as a client ends a server: stdin closes, the exit hooks run."""
        self.process.stdin.close()
        self.process.wait(timeout=timeout)


def _opener(local_site) -> str:
    return f"{local_site.base_url}/fixtures/tabs/opener.html"


def _open_parked(local_site, session_id: str, env: dict[str, str] | None = None) -> dict:
    """A persist session opened in one server that has exited since; its record."""
    server = Server(env)
    try:
        [opened] = server.act({"action": "open", "url": _opener(local_site), "session_id": session_id,
                               "profile_mode": "temporary", "headless": True, "persist": True})
        if not opened["success"]:
            pytest.skip(f"Chrome/Selenium is unavailable: {opened.get('error')}")
        assert opened["data"]["persist"] is True and opened["data"]["title"] == "Tabs opener"
        live = owned_parking.read(session_id)
        assert live["state"] == "live" and live["owner_pid"] in server.pids()
    finally:
        server.stop()
    record = owned_parking.read(session_id)
    assert record["state"] == "parked" and record["owner_pid"] is None
    return record


def _processes(record: dict) -> dict[str, int | None]:
    return {"chromedriver": record.get("driver_pid"), "chrome": record.get("chrome_pid"),
            "watchdog": record.get("watchdog_pid")}


def _wait_gone(record: dict, seconds: float = 20.0, *, watchdog: bool = True) -> list[str]:
    """What of the parked browser is still there after ``seconds``: processes, profile, record."""
    deadline = time.monotonic() + seconds
    while True:
        left = [name for name, pid in _processes(record).items()
                if pid and (watchdog or name != "watchdog") and process_probe.process_alive(pid)]
        if record.get("profile_dir") and Path(record["profile_dir"]).exists():
            left.append("profile_dir")
        if owned_parking.read(str(record["session_id"])) is not None:
            left.append("record")
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(0.25)


def _backdate(session_id: str, seconds: float) -> None:
    owned_parking.claim(session_id, lambda record: {"updated_at": time.time() - seconds})


@pytest.fixture(autouse=True)
def _no_parked_browser_survives_a_test():
    yield
    for record in owned_parking.records():
        owned_parking.retire_and_forget(str(record["session_id"]), force=True)


def test_a_parked_browser_survives_the_server_and_a_new_one_continues_it(local_site):
    record = _open_parked(local_site, "keep-tmp")
    assert all(process_probe.process_alive(pid) for pid in _processes(record).values() if pid)
    assert record["chrome_pid"] and record["profile_dir"] and Path(record["profile_dir"]).is_dir()
    assert process_probe.same_process(record["chrome_pid"], record["chrome_started"])

    second = Server()
    try:
        status = second.request("tools/call", {"name": "web_info", "arguments": {"topic": "browser_status"}})
        parked = json.loads(status["content"][0]["text"])["parked_sessions"]
        assert {"session_id": "keep-tmp", "profile_mode": "temporary"}.items() <= next(
            row for row in parked if row["session_id"] == "keep-tmp").items()
        resumed = second.one({"action": "reattach", "session_id": "keep-tmp"})
        assert resumed["reattached"] is True and resumed["profile_mode"] == "temporary"
        assert resumed["title"] == "Tabs opener"
        title = second.one({"action": "run_script", "script": "return document.title", "session_id": "keep-tmp"})
        assert title["value"] == "Tabs opener"
        # The continued session is the same browser: same chromedriver, same profile.
        assert owned_parking.read("keep-tmp")["driver_pid"] == record["driver_pid"]
        assert owned_parking.read("keep-tmp")["owner_pid"] in second.pids()
        closed = second.one({"action": "close", "session_id": "keep-tmp"})
        assert closed["closed"] is True
    finally:
        second.stop()
    assert _wait_gone(record) == []


def test_open_with_persist_continues_a_parked_browser_and_a_second_restart_parks_it_again(local_site):
    record = _open_parked(local_site, "keep-again")
    second = Server()
    try:
        # No profile_mode: a parked temporary browser keeps its own mode.
        reopened = second.one({"action": "open", "url": f"{local_site.base_url}/fixtures/tabs/child.html",
                               "session_id": "keep-again", "persist": True})
        assert reopened["reattached"] is True and reopened["profile_mode"] == "temporary"
        assert reopened["title"] == "Tabs child"
    finally:
        second.stop()
    again = owned_parking.read("keep-again")
    assert again["state"] == "parked" and again["driver_pid"] == record["driver_pid"]
    assert again["url"].endswith("/child.html")
    # This test process is a third one: an explicit close of a parked browser retires it.
    closed = persist_actions.close_session("keep-again")
    assert closed["closed"] is True and "note" not in closed
    assert closed["retired_parked"]["browser_stopped"] is True
    assert closed["retired_parked"]["profile_removed"] is True
    assert _wait_gone(record) == []


def test_the_watchdog_retires_a_browser_whose_ttl_ran_out(local_site):
    record = _open_parked(local_site, "keep-ttl", env={"WEB_SEARCH_NEO_PARKED_SESSION_TTL": "60"})
    assert process_probe.process_alive(record["watchdog_pid"])
    _backdate("keep-ttl", 61)
    assert _wait_gone(record, 45) == [], "the watchdog stops Chrome, removes the profile and exits"


def test_the_next_server_retires_an_expired_browser_whose_watchdog_is_gone(local_site, monkeypatch):
    record = _open_parked(local_site, "keep-orphan", env={"WEB_SEARCH_NEO_PARKED_SESSION_TTL": "60"})
    assert _kill(record["watchdog_pid"])
    _backdate("keep-orphan", 3600)
    monkeypatch.setenv("WEB_SEARCH_NEO_PARKED_SESSION_TTL", "60")
    reaped = owned_parking.reap_expired()
    assert [row["session_id"] for row in reaped] == ["keep-orphan"]
    assert reaped[0]["browser_stopped"] and reaped[0]["profile_removed"]
    assert _wait_gone(record, 10) == []


def test_a_server_that_died_without_parking_leaves_a_browser_the_next_one_can_continue(local_site):
    first = Server()
    [opened] = first.act({"action": "open", "url": _opener(local_site), "session_id": "keep-crash",
                          "profile_mode": "isolated", "headless": True, "persist": True})
    if not opened["success"]:
        first.process.kill()
        pytest.skip(f"Chrome/Selenium is unavailable: {opened.get('error')}")
    record = owned_parking.read("keep-crash")
    first.process.kill()  # no exit hook runs
    first.process.wait(timeout=30)
    # Another server does not take a record over while its owner lives; this one is dead.
    second = Server()
    try:
        resumed = second.one({"action": "reattach", "session_id": "keep-crash"})
        assert resumed["profile_mode"] == "isolated" and resumed["title"] == "Tabs opener"
        second.one({"action": "close", "session_id": "keep-crash"})
    finally:
        second.stop()
    assert _wait_gone(record) == []


def test_a_session_live_in_another_server_is_never_taken_over(local_site):
    first = Server()
    try:
        [opened] = first.act({"action": "open", "url": _opener(local_site), "session_id": "keep-busy",
                              "profile_mode": "temporary", "headless": True, "persist": True})
        if not opened["success"]:
            pytest.skip(f"Chrome/Selenium is unavailable: {opened.get('error')}")
        answer = persist_actions.reattach("keep-busy")
        assert answer["success"] is False and "live in another MCP server process" in answer["error"]
        closed = persist_actions.close_session("keep-busy")
        assert "left alone" in closed["parked_note"]
        with pytest.raises(ValueError, match="live in another MCP server process"):
            persist_actions.open_page(_opener(local_site), session_id="keep-busy",
                                      profile_mode="temporary", headless=True, persist=True)
        assert first.one({"action": "run_script", "script": "return 7", "session_id": "keep-busy"})["value"] == 7
        record = owned_parking.read("keep-busy")
        first.one({"action": "close", "session_id": "keep-busy"})
    finally:
        first.stop()
    assert _wait_gone(record) == []


def _kill(pid: int) -> bool:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
    else:
        os.kill(pid, 9)
    deadline = time.monotonic() + 10
    while process_probe.process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not process_probe.process_alive(pid)


def test_persist_options_and_refusals():
    # A new session with persist and no mode must name it; an explicit mode is kept.
    with pytest.raises(ValueError, match="needs profile_mode: 'current'"):
        extra_actions.inherited_open_options("fresh", None, None, None, None, True)
    assert extra_actions.inherited_open_options("fresh", "isolated", None, None, None, True)["profile_mode"] == "isolated"
    assert extra_actions.inherited_open_options("fresh", "current", None, None, None, True)["profile_mode"] == "current"
    assert extra_actions.inherited_open_options("fresh", None, None, None, 7, True)["profile_mode"] == "current"
    owned_parking.write("parked-mode", {"state": "parked", "profile_mode": "temporary", "token": "t"})
    try:  # open with persist=true continues a parked temporary browser in its own mode
        assert extra_actions.inherited_open_options(
            "parked-mode", None, None, None, None, True)["profile_mode"] == "temporary"
        assert extra_actions.inherited_open_options(
            "parked-mode", None, None, None, None, False)["profile_mode"] == "isolated"
    finally:
        owned_parking.forget("parked-mode")
    with pytest.raises(ValueError, match="not 'default'"):
        persist_actions.open_page("http://127.0.0.1:9/", profile_mode="temporary", persist=True)
    with pytest.raises(ValueError, match="never parked"):
        persist_actions.open_page("http://127.0.0.1:9/", session_id="p", profile_mode="persistent",
                                  profile_id="work", persist=True)


def test_a_record_whose_browser_is_gone_is_cleaned_up_without_killing_anything():
    profile = Path(tempfile.gettempdir()) / "scoped_dir_wsn_test_gone"
    profile.mkdir(exist_ok=True)
    owned_parking.write("gone", {
        "state": "parked", "profile_mode": "temporary", "token": "t", "profile_dir": str(profile),
        "executor_url": "http://127.0.0.1:9", "webdriver_session_id": "x",
        # A live pid with a stamp that does not match: never killed.
        "driver_pid": os.getpid(), "driver_started": "not-this-process",
        "chrome_pid": os.getpid(), "chrome_started": "not-this-process",
    })
    assert owned_parking.reapable(owned_parking.read("gone"))
    [answer] = owned_parking.reap_expired()
    assert answer["how"] == "already_gone" and answer["profile_removed"] is True
    assert not profile.exists() and owned_parking.read("gone") is None
    assert process_probe.process_alive(os.getpid())


def test_the_python_sdk_job_is_named_in_the_open_answer(monkeypatch):
    monkeypatch.setattr(process_probe, "job_breakaway", lambda: "forbidden")
    assert "job object" in owned_parking.outlives_client()
    monkeypatch.setattr(process_probe, "job_breakaway", lambda: "silent")
    assert owned_parking.outlives_client() is None
    with process_probe.detached_launch():
        flags = process_probe.driver_popen_kwargs()
    assert flags == ({"start_new_session": True} if os.name != "nt" else {"creation_flags": 0x08000000})
    monkeypatch.setattr(process_probe, "job_breakaway", lambda: "allowed")
    with process_probe.detached_launch():
        flags = process_probe.driver_popen_kwargs()
    if os.name == "nt":
        assert flags["creation_flags"] & process_probe.CREATE_BREAKAWAY_FROM_JOB
    assert process_probe.driver_popen_kwargs() == ({} if os.name != "nt" else {"creation_flags": 0x08000000})


@pytest.mark.skipif(sys.platform != "win32", reason="the MCP SDK's kill-on-close job object is Windows-only")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")  # the SDK's pipe transports
def test_under_the_python_sdk_job_the_answer_warns_and_nothing_is_left_behind(local_site):
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # The base interpreter, as the shim bypass runs a real server: a venv launcher would
    # start the interpreter before the SDK puts the launcher into its job, and so escape it.
    base = getattr(sys, "_base_executable", "") or sys.executable
    launcher = {"__PYVENV_LAUNCHER__": sys.executable} if base != sys.executable else {}

    async def open_and_leave() -> dict:
        parameters = StdioServerParameters(command=base, args=[str(ROOT / "main.py")], cwd=str(ROOT),
                                           env={**os.environ, "PYTHONPATH": str(ROOT), **launcher})
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("web_action", {"actions": [{
                    "action": "open", "url": _opener(local_site), "session_id": "keep-job",
                    "profile_mode": "temporary", "headless": True, "persist": True}]})
                return json.loads(result.content[0].text)["results"][0]

    opened = asyncio.run(open_and_leave())
    if not opened["success"]:
        pytest.skip(f"Chrome/Selenium is unavailable: {opened.get('error')}")
    record = owned_parking.read("keep-job")
    if "persist_warning" not in opened["data"]:
        pytest.skip("this MCP SDK does not put the server into a kill-on-close job")
    assert "job object" in opened["data"]["persist_warning"]
    import gc

    gc.collect()  # the SDK's job handle closes with its process object: the job ends its processes
    deadline = time.monotonic() + 20
    while (owned_parking.driver_alive(record) or owned_parking.chrome_alive(record)) and time.monotonic() < deadline:
        time.sleep(0.25)
    assert not owned_parking.chrome_alive(record), "the job ended the browser with the client"
    # The next server says so and cleans up what the job left (the profile, the record).
    refused = persist_actions.reattach("keep-job")
    assert refused["success"] is False and "no longer running" in refused["error"]
    assert _wait_gone(record) == []


def test_a_child_older_than_its_parent_is_a_stranger_behind_a_reused_pid():
    """The owner's chrome.exe, left with the dead parent id our chromedriver now reuses, is never ours."""
    from web_search_neo.sessions import process_tree
    table = {100: (1, "chromedriver.exe"), 200: (100, "chrome.exe"), 999: (100, "chrome.exe")}
    stamps = {100: "win:500", 200: "win:600", 999: "win:10"}
    assert process_tree.children(100, table=table, stamp_of=stamps.get) == [(200, "chrome.exe")]
    assert [pid for pid, _stamp in process_tree.family(100, table=table, stamp_of=stamps.get)] == [100, 200]
    # An unreadable stamp cannot prove anything either way: the listed child is kept.
    assert (999, "chrome.exe") in process_tree.children(100, table=table, stamp_of={100: "win:500"}.get)
