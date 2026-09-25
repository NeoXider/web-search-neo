"""1.19.0 final audit (F1-F9): every close path stops a frozen browser, honest kills,
per-command teardown deadline, the CLI token file, console paging with dedupe,
replay windows, and SyntaxError only for a script that did not compile."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import types

import pytest

from web_search_neo import browser_tools, console_log
from web_search_neo.actions import repl
from web_search_neo.actions import scripts as script_guard

RUN = "run-final-audit"
ROOT = Path(__file__).resolve().parents[1]


class _Process:
    def __init__(self, pid=4242, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code
        self.killed = False

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed = True


class _HungDriver:
    """A Selenium driver of a server-launched browser whose chromedriver stopped answering."""

    def __init__(self, process, profile=""):
        self._wsn_hung = True
        self.service = types.SimpleNamespace(process=process, stop=self._never)
        self.capabilities = {"chrome": {"userDataDir": str(profile)}}

    def _never(self):
        raise AssertionError("the polite teardown must not run for a frozen browser")

    quit = _never


@pytest.fixture
def hung(monkeypatch):
    """Record kills and mock forgets; fail if the polite teardown is attempted."""
    killed: list[int] = []
    forgotten: list[str] = []
    monkeypatch.setattr(script_guard, "_kill_tree", lambda pid, process: killed.append(pid))
    monkeypatch.setattr(browser_tools.request_mocks, "forget", forgotten.append)
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", lambda tab_id: None)

    def polite(*_args, **_kwargs):
        raise AssertionError("_shutdown_session ran for a frozen browser")

    monkeypatch.setattr(browser_tools, "_shutdown_session", polite)
    monkeypatch.setattr(browser_tools, "_sessions", {})  # nobody else's sessions in the way

    def register(session_id, **fields):
        session = browser_tools.BrowserSession(
            driver=_HungDriver(_Process()), headless=True, profile_mode="temporary",
            agent_label="final-audit", **fields)
        browser_tools._sessions[session_id] = session
        return session

    return types.SimpleNamespace(killed=killed, forgotten=forgotten, register=register)


# ------------------------------------------------------------------ F1


def test_close_all_stops_a_frozen_browser_outright(hung):
    hung.register("frozen-a")
    answer = browser_tools.close_all_sessions(agent_label="final-audit")
    assert answer["closed_sessions"] == ["frozen-a"]
    assert "stopped outright" in answer["forced"]["frozen-a"]
    assert hung.killed == [4242] and hung.forgotten == ["frozen-a"]
    assert "frozen-a" not in browser_tools._sessions


def test_idle_release_stops_a_frozen_browser_outright(hung):
    session = hung.register("frozen-idle")
    session.last_used = time.monotonic() - 3600
    answer = browser_tools.close_all_sessions(agent_label="final-audit", idle_for_seconds=60)
    assert answer["closed_sessions"] == ["frozen-idle"] and "frozen-idle" in answer["forced"]
    assert hung.killed == [4242]


def test_the_idle_ttl_sweep_stops_a_frozen_browser_outright(hung, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_SESSION_IDLE_TTL", "1")
    session = hung.register("frozen-ttl")
    session.last_used = time.monotonic() - 3600
    assert browser_tools._drop_idle_sessions() == ["frozen-ttl"]  # quit() would raise here
    assert hung.killed == [4242]


def test_process_exit_stops_a_frozen_browser_outright(hung):
    hung.register("frozen-exit")
    answer = browser_tools._close_everything_at_exit()
    assert "frozen-exit" in answer["forced"] and 4242 in hung.killed


def test_close_session_still_stops_a_frozen_browser_outright(hung):
    hung.register("frozen-one")
    answer = browser_tools.close_session("frozen-one")
    assert answer["closed"] is True and "stopped outright" in answer["forced"]
    assert hung.killed == [4242] and hung.forgotten == ["frozen-one"]


# ------------------------------------------------------------------ F2, F3, F4


def _posix(monkeypatch, run):
    killed: list[object] = []
    monkeypatch.setattr(script_guard.os, "name", "posix")
    monkeypatch.setattr(script_guard.subprocess, "run", run)
    monkeypatch.setattr(script_guard.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(script_guard, "signal", types.SimpleNamespace(SIGKILL=9))
    return killed


def _no_pgrep(args, **_kwargs):
    raise FileNotFoundError(2, "No such file or directory", "pgrep")


def _fake_proc(root: Path, tree: dict[int, list[int]], children_files: bool) -> None:
    for pid, kids in tree.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} (chrome (x)) S {next((p for p, k in tree.items() if pid in k), 1)} 0")
        if children_files:
            task = root / str(pid) / "task" / str(pid)
            task.mkdir(parents=True)
            (task / "children").write_text(" ".join(str(kid) for kid in kids) + (" " if kids else ""))


@pytest.mark.parametrize("children_files", [True, False], ids=["task-children", "stat-ppid"])
def test_posix_without_pgrep_reads_the_tree_from_proc(monkeypatch, tmp_path, children_files):
    _fake_proc(tmp_path, {100: [200, 201], 200: [300], 201: [], 300: []}, children_files)
    monkeypatch.setattr(script_guard, "PROC_ROOT", str(tmp_path))
    killed = _posix(monkeypatch, _no_pgrep)
    process = _Process(100)
    assert script_guard._kill_tree(100, process) is None
    assert sorted(killed) == [100, 200, 201, 300] and process.killed


def test_posix_without_pgrep_or_proc_says_so(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(script_guard, "PROC_ROOT", str(tmp_path / "no-proc"))
    killed = _posix(monkeypatch, _no_pgrep)
    driver = _HungDriver(_Process(100))
    session = browser_tools.BrowserSession(driver=driver, headless=True, profile_mode="temporary")
    with caplog.at_level(logging.WARNING, logger=script_guard.__name__):
        outcome = script_guard.stop_hung_driver(session)
    assert killed == [100] and driver.service.process.killed
    assert "could not be listed" in outcome["problem"] and "Chrome may still be running" in outcome["problem"]
    assert "stopped outright" not in outcome["forced"]
    assert any("could not be listed" in record.getMessage() for record in caplog.records)


def test_windows_taskkill_failure_is_a_problem_and_cleanup_still_runs(monkeypatch):
    forgotten: list[str] = []
    monkeypatch.setattr(browser_tools, "_sessions", {})
    monkeypatch.setattr(browser_tools.request_mocks, "forget", forgotten.append)
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", lambda tab_id: None)
    monkeypatch.setattr(script_guard.os, "name", "nt")

    def missing(*_args, **_kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified", "taskkill")

    monkeypatch.setattr(script_guard.subprocess, "run", missing)
    process = _Process(777)
    browser_tools._sessions["taskkill-missing"] = browser_tools.BrowserSession(
        driver=_HungDriver(process), headless=True, profile_mode="temporary")
    answer = browser_tools.close_session("taskkill-missing")
    assert answer["forced"] and "could not be stopped" in answer["warning"]
    assert answer["released"] is False and forgotten == ["taskkill-missing"] and process.killed


def test_windows_taskkill_exit_code_is_reported(monkeypatch):
    from web_search_neo.sessions import process_tree
    monkeypatch.setattr(script_guard.os, "name", "nt")
    monkeypatch.setattr(process_tree, "family", lambda pid: [(777, "win:1")])
    monkeypatch.setattr(process_tree, "still_running", lambda pid, stamp: True)  # taskkill did not stop it
    monkeypatch.setattr(script_guard.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(
        args, 128, stdout="", stderr="ERROR: The process \"777\" not found."))
    assert "taskkill exited with 128" in script_guard._kill_tree(777, _Process(777))


def test_windows_kill_names_only_real_descendants(monkeypatch):
    """A process older than its listed parent sits behind a reused pid: it is not killed."""
    from web_search_neo.sessions import process_tree
    table = {100: (1, "chromedriver.exe"), 200: (100, "chrome.exe"), 300: (200, "chrome.exe"),
             999: (100, "chrome.exe")}  # 999: the owner's Chrome, whose dead parent's pid 100 was reused
    stamps = {100: "win:500", 200: "win:600", 300: "win:700", 999: "win:10"}
    monkeypatch.setattr(process_tree, "windows_table", lambda: table)
    monkeypatch.setattr(process_tree, "started_at", lambda pid: stamps.get(pid))
    monkeypatch.setattr(process_tree.sys, "platform", "win32")
    monkeypatch.setattr(script_guard.os, "name", "nt")
    sent = []
    monkeypatch.setattr(script_guard.subprocess, "run",
                        lambda args, **k: sent.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    assert script_guard._kill_tree(100, _Process(100)) is None
    assert sent == [["taskkill", "/F", "/PID", "300", "/PID", "200", "/PID", "100"]]
    assert "/T" not in sent[0] and "999" not in sent[0]


def test_an_exited_chromedriver_is_not_killed_only_its_profile_removed(monkeypatch, tmp_path):
    import tempfile

    killed: list[int] = []
    monkeypatch.setattr(script_guard, "_kill_tree", lambda pid, process: killed.append(pid))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    profile = tmp_path / "scoped_dir42_1"
    (profile / "Default").mkdir(parents=True)
    process = _Process(4242, exit_code=0)
    session = browser_tools.BrowserSession(driver=_HungDriver(process, profile), headless=True,
                                           profile_mode="temporary")
    outcome = script_guard.stop_hung_driver(session)
    assert killed == [] and not process.killed  # the pid may name another program now
    assert not profile.exists() and outcome["problem"] is None
    assert "already exited" in outcome["forced"]


# ------------------------------------------------------------------ F5


class _BorrowedTab:
    """The user's tab on the companion; every CDP command costs four seconds of a fake clock."""

    is_extension_bridge = True

    def __init__(self, clock):
        self.clock = clock
        self.cdp: list[tuple[str, dict]] = []
        self.calls: list[str] = []
        self.tab_id = 55
        self._wsn_slow = True

    def set_script_timeout(self, seconds):
        self.calls.append(f"timeout {seconds}")

    def execute_script(self, script, *args):
        return {"url": "https://example.test/", "title": "Page"}

    def execute_cdp_cmd(self, method, params=None, **_kwargs):
        self.cdp.append((method, params or {}))
        self.clock[0] += 4.0
        return {}

    def quit(self):
        self.calls.append("quit")
        return {"detached": True}

    def close_tab(self):
        self.calls.append("close_tab")
        return {"removed": True}


def test_teardown_deadline_holds_inside_the_injected_state_step(monkeypatch):
    offset = [0.0]
    monkeypatch.setattr(script_guard, "time", types.SimpleNamespace(
        monotonic=lambda: time.monotonic() + offset[0], sleep=time.sleep))
    released: list[int] = []
    monkeypatch.setattr(browser_tools, "_release_claimed_tab", released.append)
    monkeypatch.setattr(browser_tools, "_current_browser_run", lambda: RUN)
    tab = _BorrowedTab(offset)
    session = browser_tools.BrowserSession(
        driver=tab, headless=False, profile_mode="current", current_tab_id=55, owns_browser=True,
        owns_tab=False, browser_run=RUN, tab_group=browser_tools.DEFAULT_TAB_GROUP)
    session.injected_scripts.extend(f"id-{index}" for index in range(10))
    browser_tools._sessions["deadline-loop"] = session
    answer = browser_tools.close_session("deadline-loop")
    removals = [params["identifier"] for method, params in tab.cdp
                if method == "Page.removeScriptToEvaluateOnNewDocument"]
    assert removals == ["id-0", "id-1"]  # 4 s each against the 6 s deadline
    assert "removing 8 of 10 script(s)" in answer["warning"] and answer["released"] is False
    assert "removing the request stubs skipped" in answer["warning"]
    assert "quit" in tab.calls and released == [55]  # detach and the claim are never skipped
    assert session.injected_scripts == []


def test_page_steps_report_what_they_skip_and_what_failed():
    steps = script_guard.PageSteps(time.monotonic() - 1.0)
    sent: list[str] = []
    assert steps("removing the badge", lambda: sent.append("badge")) is False
    live = script_guard.PageSteps(None)

    def boom():
        raise RuntimeError("tab gone")

    live("restoring dialogs", boom)
    assert sent == [] and "skipped" in steps.problems[0]
    assert live.problems == ["restoring dialogs failed (RuntimeError: tab gone)"]


# ------------------------------------------------------------------ F6


def _cli():
    spec = importlib.util.spec_from_file_location("mcp_cli_under_test", ROOT / "scripts" / "mcp_cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch, tmp_path):
    module = _cli()
    monkeypatch.setattr(module, "_token_path", lambda port: tmp_path / f"cli-{port}.token")
    return module


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_daemon_rejects_a_bad_token_and_obeys_the_right_one(cli, monkeypatch, tmp_path):
    class _Client:
        def call(self, tool, args):
            return {"content": [{"type": "text", "text": "{}"}]}

        def close(self):
            pass

    monkeypatch.setattr(cli, "StdioClient", _Client)
    port = _free_port()
    worker = threading.Thread(target=cli.serve, args=(port, tmp_path / "out", 0), daemon=True)
    worker.start()
    token_file = tmp_path / f"cli-{port}.token"
    deadline = time.monotonic() + 10
    while not token_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert token_file.exists()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(json.dumps({"op": "stop", "token": "not-the-token"}).encode() + b"\n")
        assert json.loads(connection.makefile("rb").readline()) == {"error": "bad token"}
    assert worker.is_alive()  # a wrong token stops nothing
    assert cli.send(port, {"op": "stop"}, timeout=10)["stopped"] is True
    worker.join(10)
    assert not worker.is_alive() and not token_file.exists()


def test_remove_token_leaves_a_foreign_token_alone(cli, tmp_path):
    path = tmp_path / "cli-1.token"
    path.write_text("somebody-else", encoding="utf-8")
    cli._remove_token(1, "mine")
    assert path.read_text(encoding="utf-8") == "somebody-else"
    cli._remove_token(1, "somebody-else")
    assert not path.exists()


def test_a_failed_token_write_leaves_no_temporary_file(cli, monkeypatch, tmp_path):
    calls: list[int] = []

    def locked(*_args):
        calls.append(1)
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(cli.os, "replace", locked)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    with pytest.raises(PermissionError):
        cli._write_token(2)
    assert len(calls) == cli.TOKEN_REPLACE_ATTEMPTS and list(tmp_path.iterdir()) == []


def test_a_briefly_locked_token_file_is_retried(cli, monkeypatch, tmp_path):
    real_replace = os.replace
    failures = [PermissionError(13, "locked")]

    def flaky(source, target):
        if failures:
            raise failures.pop()
        real_replace(source, target)

    monkeypatch.setattr(cli.os, "replace", flaky)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    token = cli._write_token(3)
    assert (tmp_path / "cli-3.token").read_text(encoding="utf-8") == token
    assert [item.name for item in tmp_path.iterdir()] == ["cli-3.token"]


def test_a_private_token_file_cannot_fail_open(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_restrict_to_owner", lambda path: "icacls could not run")
    with pytest.raises(RuntimeError, match="private"):
        cli._write_token(4)
    assert list(tmp_path.iterdir()) == []


def test_the_token_file_is_readable_by_its_owner_only(cli, tmp_path):
    cli._write_token(5)
    path = tmp_path / "cli-5.token"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        return
    if shutil.which("icacls") is None:
        pytest.skip("icacls is not available")
    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    user = os.environ.get("USERNAME", "")
    assert user and f"{user}:(F)" in listing
    assert "(I)" not in listing  # nothing inherited from the folder
    for broad in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
        assert broad not in listing


# ------------------------------------------------------------------ F7


def _console_session(texts):
    session = types.SimpleNamespace(console_history=[], console_hseq=0, console_history_dropped=0)
    console_log.ingest(session, {"entries": [{"level": "log", "kind": "console", "text": text}
                                             for text in texts]})
    return session


def _read(session, since_seq, limit=1):
    return console_log.read(session, lambda: {"entries": []}, levels=None, contains=None, kinds=None,
                            limit=limit, since_seq=since_seq, since_ms=None, dedupe=True, order="asc")


def test_dedupe_paging_never_skips_an_entry():
    session = _console_session(["A", "B", "A", "C"])
    seen, since, pages = [], 0, 0
    while pages < 10:
        page = _read(session, since)
        seen.extend((entry["text"], entry["first_seq"], entry["count"]) for entry in page["entries"])
        since = page["next_seq"]
        pages += 1
        if not page["has_more"]:
            break
    assert seen == [("A", 1, 1), ("B", 2, 1), ("A", 3, 1), ("C", 4, 1)]
    assert sum(count for _, _, count in seen) == 4  # every entry counted exactly once


def test_dedupe_paging_still_folds_within_a_page():
    session = _console_session(["A", "B", "A", "C"])
    first = _read(session, 0, limit=2)
    assert [(entry["text"], entry["count"]) for entry in first["entries"]] == [("A", 2), ("B", 1)]
    assert first["next_seq"] == 3 and first["has_more"] is True
    second = _read(session, first["next_seq"], limit=2)
    assert [entry["text"] for entry in second["entries"]] == ["C"] and second["has_more"] is False


# ------------------------------------------------------------------ F8


def test_replay_names_a_way_to_the_whole_body(monkeypatch):
    from web_search_neo import script_results

    cut = {"ok": True, "status": 200, "headers": {}, "body": "x" * 20000, "truncated": True,
           "body_chars": 50000}
    monkeypatch.setattr(browser_tools, "execute_js", lambda *a, **k: script_results.shape(
        {"success": True, "value": cut, "value_json": json.dumps(cut)}, max_chars=10 ** 6))
    monkeypatch.setattr(browser_tools, "_get_session", lambda session_id: None)
    answer = browser_tools.replay_request(url="https://host/api", method="POST", body='{"a": 1}')
    note = answer["window_note"]
    assert "run_script" in note and "save_to=" in note and "20000 characters" in note
    script = json.loads(note.split("script=", 1)[1].rsplit(", save_to=", 1)[0])
    assert '"https://host/api"' in script and '"method": "POST"' in script and '"body": "{\\"a\\": 1}"' in script


def test_replay_without_a_cut_has_no_window_note(monkeypatch):
    from web_search_neo import script_results

    whole = {"ok": True, "status": 200, "headers": {}, "body": "short", "truncated": False, "body_chars": 5}
    monkeypatch.setattr(browser_tools, "execute_js", lambda *a, **k: script_results.shape(
        {"success": True, "value": whole, "value_json": json.dumps(whole)}, max_chars=10 ** 6))
    monkeypatch.setattr(browser_tools, "_get_session", lambda session_id: None)
    assert "window_note" not in browser_tools.replay_request(url="https://host/api")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_the_replay_note_script_is_valid_javascript(tmp_path):
    from web_search_neo.perception.action_scripts import _replay_window_note

    spec = {"url": "https://host/a?b='c'", "method": "POST", "headers": {"X-T": "1"},
            "body": "line\n\"quoted\"", "credentials": "include"}
    note = _replay_window_note(spec, {"response": {"truncated": True}})["window_note"]
    script = json.loads(note.split("script=", 1)[1].rsplit(", save_to=", 1)[0])
    source = tmp_path / "replay.js"
    source.write_text(f"(async function () {{ {script} }});", encoding="utf-8")
    assert subprocess.run(["node", "--check", str(source)], capture_output=True).returncode == 0


# ------------------------------------------------------------------ F9


class _AsyncDriver:
    def __init__(self, outcome=None, error=None):
        self.outcome, self.error = outcome, error

    def execute_async_script(self, script, *args):
        if self.error:
            raise self.error
        return self.outcome


def test_a_syntax_error_thrown_at_run_time_is_not_a_compile_error():
    thrown = _AsyncDriver({"__wsn_ok": False, "name": "SyntaxError",
                           "message": "Unexpected token 'x', \"x\" is not valid JSON"})
    with pytest.raises(repl.ScriptError) as failure:
        repl.run(thrown, 'return JSON.parse("x")')
    assert failure.value.syntax is False and "SyntaxError" in str(failure.value)
    answer = script_guard.execute(thrown, 'return JSON.parse("x")', page_summary=dict,
                                  wait_until_ready=lambda *a: None, describe_error=str)
    assert answer["success"] is False and "syntax_error" not in answer


def test_a_script_that_does_not_compile_is_still_a_syntax_error():
    from selenium.common.exceptions import JavascriptException

    broken = _AsyncDriver(error=JavascriptException("javascript error: SyntaxError: Unexpected end of input"))
    with pytest.raises(repl.ScriptError) as failure:
        repl.run(broken, "return (1 +")
    assert failure.value.syntax is True


def test_cdp_tells_a_rejected_promise_from_a_source_that_did_not_parse():
    compile_error = {"text": "Uncaught", "exception": {"className": "SyntaxError",
                                                       "description": "SyntaxError: Unexpected end of input"}}
    thrown = {"text": "Uncaught (in promise)", "exception": {
        "className": "SyntaxError", "description": "SyntaxError: Unexpected token 'x'"}}
    assert script_guard.did_not_compile(compile_error) is True
    assert script_guard.did_not_compile(thrown) is False
    assert script_guard.did_not_compile({"text": "Uncaught", "exception": {"className": "TypeError"}}) is False
