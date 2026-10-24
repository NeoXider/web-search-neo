"""Macro scripts: expressions, control steps, variables, sub-macros, page checks and background jobs.

The dispatcher is replaced by a fake that records every action, so a script's logic is tested
without a browser: what ran, in which order, with which rendered parameters.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from web_search_neo import macro_check, macro_expr, macro_jobs, macros
from web_search_neo import main


@pytest.fixture(autouse=True)
def macro_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_MACRO_ROOT", str(tmp_path / "macros"))
    monkeypatch.setenv("WEB_SEARCH_NEO_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(macro_jobs, "REPORTER", lambda payload: None)
    monkeypatch.delenv("WEB_SEARCH_NEO_PROJECT_ROOT", raising=False)
    yield tmp_path


def _call(**kwargs):
    return asyncio.run(main.browser_macro(**kwargs))


def _save(name, steps, variables=None):
    path = macros.macro_root() / f"{name}.json"
    path.write_text(json.dumps({"name": name, "steps": steps, "variables": variables or {}}), encoding="utf-8")


def _fake(monkeypatch, behaviour=None):
    """Replace the dispatcher; behaviour(action, n) -> {"success": ..., "data"/"error": ...}."""
    calls: list[dict] = []

    async def fake(actions, continue_on_error=False, summary="full"):
        results = []
        for index, action in enumerate(actions):
            calls.append(action)
            outcome = behaviour(action, len(calls)) if behaviour else {"success": True, "data": {}}
            results.append({"index": index, "action": action["action"], **outcome})
        return {"success": all(item["success"] for item in results), "results": results}

    monkeypatch.setattr(main, "_execute_actions", fake)
    return calls


def _script_value(value):
    return {"success": True, "data": {"success": True, "value": value}}


# --- expressions -----------------------------------------------------------------------------

NET = {"requests": [{"method": "GET", "url": "https://s/a", "status": 200},
                    {"method": "POST", "url": "https://s/api/upload", "status": 201, "body": '{"id": 7}'}]}


@pytest.mark.parametrize("expression, expected", [
    ("upload.ok and len(tags) > 0", True),
    ('find(net, method="POST", url="~/api/upload").status', 201),
    ('json(find(net.requests, method="POST").body).id', 7),
    ("count(net, status=200)", 1),
    ("!thumbnail", True),
    ("thumbnail === null || tags.length == 2", True),
    ('contains(title, "Hello!") && !empty(tags)', True),
    ('"x" if empty(thumbnail) else "y"', "x"),
    ("upload.missing.deeper", None),
    ('number("Осталось 5 минут") + 1', 6),
    ('search("Проверка: 12 %", "(\\\\d+) %")', "12"),
    ("pluck(net, 'status')", [200, 201]),
    ("tags[-1]", "b"),
])
def test_expressions_read_the_run_data(expression, expected):
    scope = {"upload": {"ok": True}, "tags": ["a", "b"], "net": NET, "thumbnail": None, "title": "Hello!"}
    assert macro_expr.evaluate(expression, scope) == expected


@pytest.mark.parametrize("expression", [
    "__import__('os')", "(lambda: 1)()", "[x for x in tags]", "open('f')", "tags.__class__.__base__()",
    "2 ** 10", "'x' * 10**9",
])
def test_expressions_refuse_anything_outside_the_whitelist(expression):
    with pytest.raises(macro_expr.ExpressionError):
        macro_expr.evaluate(expression, {"tags": ["a"]})


def test_an_unknown_name_is_named_with_the_known_ones():
    with pytest.raises(macro_expr.ExpressionError) as excinfo:
        macro_expr.evaluate("uplaod.ok", {"upload": {}})
    assert "uplaod" in str(excinfo.value) and "upload" in str(excinfo.value)


def test_templates_keep_types_fill_text_and_leave_scripts_and_handlebars_alone():
    scope = {"base": "https://s", "item": {"id": 5}, "files": [1, 2, 3]}
    rendered = macro_expr.render({"url": "{{base}}/item/{{item.id}}", "times": "{{len(files)}}",
                                  "items": "{{files}}", "script": "return '{{base}}'", "hb": "{{#each}}"}, scope)
    assert rendered == {"url": "https://s/item/5", "times": 3, "items": [1, 2, 3],
                        "script": "return '{{base}}'", "hb": "{{#each}}"}


# --- control flow ----------------------------------------------------------------------------

def test_if_else_save_as_and_templates_steer_the_run(monkeypatch):
    calls = _fake(monkeypatch, lambda action, n: _script_value({"link": "https://y/1", "count": 2})
                  if action["action"] == "run_script" else {"success": True, "data": {}})
    outcome = _call(op="run_steps", steps=[
        {"action": "run_script", "script": "return 1", "save_as": "info"},
        {"action": "if", "test": "info.count > 1",
         "then": [{"action": "open", "url": "{{info.link}}?n={{info.count}}"}],
         "else": [{"action": "open", "url": "https://never"}]},
        {"action": "return", "value": "{{info.link}}"},
    ])
    assert outcome["success"] is True
    assert [call["action"] for call in calls] == ["run_script", "open"]
    assert calls[1]["url"] == "https://y/1?n=2"
    assert outcome["returned"] == "https://y/1"
    assert outcome["outputs"]["info"]["count"] == 2
    assert any("if -> then" in line for line in outcome["trace"])


def test_when_skips_a_step_and_says_so(monkeypatch):
    calls = _fake(monkeypatch)
    outcome = _call(op="run_steps", variables={"thumbnail": None}, steps=[
        {"action": "open", "url": "https://a"},
        {"action": "upload", "selector": "#thumb", "file_paths": ["{{thumbnail}}"], "when": "thumbnail"},
    ])
    assert outcome["success"] is True
    assert [call["action"] for call in calls] == ["open"]
    assert any("skipped (when false)" in line for line in outcome["trace"])


def test_for_each_repeat_until_break_and_continue(monkeypatch):
    calls = _fake(monkeypatch)
    outcome = _call(op="run_steps", variables={"tags": ["a", "skip", "c", "stop", "e"]}, steps=[
        {"action": "set", "values": {"seen": 0}},
        {"action": "for_each", "items": "tags", "as": "tag", "steps": [
            {"action": "if", "test": "tag == 'skip'", "then": [{"action": "continue"}]},
            {"action": "if", "test": "tag == 'stop'", "then": [{"action": "break"}]},
            {"action": "open", "url": "https://t/{{tag}}"},
        ]},
        {"action": "repeat", "until": "seen >= 3", "as": "i", "steps": [
            {"action": "set", "values": {"seen": "{{seen + 1}}"}},
        ]},
        {"action": "return", "value": "{{seen}}"},
    ])
    assert [call["url"] for call in calls] == ["https://t/a", "https://t/c"]
    assert outcome["returned"] == 3


def test_retry_then_on_error_continue_and_last(monkeypatch):
    attempts = {"n": 0}

    def flaky(action, n):
        if action["action"] == "click":
            attempts["n"] += 1
            return {"success": attempts["n"] >= 3, "error": "not yet"} if attempts["n"] < 3 else {"success": True, "data": {}}
        if action["action"] == "wait":
            return {"success": False, "error": "TimeoutError"}
        return {"success": True, "data": {}}

    calls = _fake(monkeypatch, flaky)
    outcome = _call(op="run_steps", steps=[
        {"action": "click", "selector": "#b", "retry": 3, "retry_delay": 0.01},
        {"action": "wait", "selector": "#maybe", "on_error": "continue"},
        {"action": "if", "test": "!last.ok", "then": [{"action": "log", "message": "optional part missing: {{last.error}}"}]},
    ])
    assert outcome["success"] is True and outcome["failure_count"] == 1
    assert [call["action"] for call in calls] == ["click", "click", "click", "wait"]
    assert outcome["results"][0]["attempts"] == 3
    assert outcome["log"] == ["optional part missing: TimeoutError"]


def test_try_catch_finally_and_fail(monkeypatch):
    calls = _fake(monkeypatch, lambda action, n: {"success": False, "error": "boom"}
                  if action.get("url") == "https://bad" else {"success": True, "data": {}})
    outcome = _call(op="run_steps", steps=[
        {"action": "try",
         "steps": [{"action": "open", "url": "https://bad"}, {"action": "open", "url": "https://never"}],
         "catch": [{"action": "log", "message": "caught {{error.message}} at {{error.step}}"}],
         "finally": [{"action": "open", "url": "https://cleanup"}]},
        {"action": "fail", "message": "stop here"},
    ])
    assert [call["url"] for call in calls] == ["https://bad", "https://cleanup"]
    assert outcome["log"] == ["caught boom at 0.steps.0"]
    assert outcome["success"] is False and outcome["error"] == "stop here" and outcome["failed_step"] == "1"


def test_assert_fails_a_step_whose_result_is_wrong(monkeypatch):
    _fake(monkeypatch, lambda action, n: {"success": True, "data": {"status": 500, "body": "oops"}})
    outcome = _call(op="run_steps", steps=[
        {"action": "http_request", "url": "https://api/x", "method": "POST", "assert": "result.data.status == 200"},
    ])
    assert outcome["success"] is False
    assert "assert failed" in outcome["error"]


def test_expect_runs_test_run_page_checks_as_waits(monkeypatch):
    calls = _fake(monkeypatch, lambda action, n: {"success": False, "error": "TimeoutError"}
                  if action["action"] == "wait" else {"success": True, "data": {}})
    outcome = _call(op="run_steps", steps=[
        {"action": "click", "selector": "#save", "session_id": "s", "expect": {"text": "Saved", "timeout_seconds": 1}},
    ])
    assert calls[1]["action"] == "wait" and "Saved" in calls[1]["script"] and calls[1]["session_id"] == "s"
    assert outcome["success"] is False and "expect.text not met" in outcome["error"]


def test_wait_until_polls_a_network_read_until_the_post_answers(monkeypatch):
    reads = {"n": 0}

    async def network(session_id="default", output="text"):
        reads["n"] += 1
        rows = [{"method": "POST", "url": "https://s/api/save", "status": 200}] if reads["n"] >= 3 else []
        return {"success": True, "requests": rows, "output_seen": output}

    monkeypatch.setitem(main._TOPIC_HANDLERS, "network", network)
    monkeypatch.setattr(main, "_validate_arguments", lambda name, where, params: dict(params))
    calls = _fake(monkeypatch)
    outcome = _call(op="run_steps", steps=[
        {"action": "wait_until", "interval_seconds": 0.2, "timeout_seconds": 10,
         "test": 'find(net, method="POST", url="~/api/save").status == 200',
         "steps": [{"action": "read", "topic": "network", "params": {"session_id": "s"}, "save_as": "net"}]},
        {"action": "if", "test": "net.output_seen == 'json'", "then": [{"action": "open", "url": "https://done"}]},
    ])
    assert outcome["success"] is True and reads["n"] == 3
    assert [call["url"] for call in calls] == ["https://done"]


def test_wait_until_times_out_or_continues(monkeypatch):
    _fake(monkeypatch)
    failed = _call(op="run_steps", steps=[{"action": "wait_until", "test": "false", "timeout_seconds": 0.3,
                                           "interval_seconds": 0.2}])
    assert failed["success"] is False and "timed out" in failed["error"]
    kept = _call(op="run_steps", steps=[{"action": "wait_until", "test": "false", "timeout_seconds": 0.3,
                                         "interval_seconds": 0.2, "on_timeout": "continue"},
                                        {"action": "return", "value": "{{last.ok}}"}])
    assert kept["success"] is True and kept["returned"] is False


def test_call_runs_another_macro_and_returns_its_value(monkeypatch):
    calls = _fake(monkeypatch)
    _save("child", [{"action": "open", "url": "https://c/{{page}}"}, {"action": "return", "value": "{{page * 2}}"}],
          {"page": None})
    _save("parent", [{"action": "call", "name": "child", "variables": {"page": "{{start}}"}, "save_as": "doubled"},
                     {"action": "return", "value": "{{doubled}}"}], {"start": 4})
    outcome = _call(op="run", name="parent")
    assert outcome["success"] is True and outcome["returned"] == 8
    assert calls[0]["url"] == "https://c/4"
    assert any(result["step"].startswith("0>child:") for result in outcome["results"])


def test_call_refuses_a_loop_between_macros(monkeypatch):
    _fake(monkeypatch)
    _save("ping", [{"action": "call", "name": "pong"}, {"action": "log", "message": "x"}])
    _save("pong", [{"action": "call", "name": "ping"}, {"action": "log", "message": "y"}])
    outcome = _call(op="run", name="ping")
    assert outcome["success"] is False and "nest too deep or loop" in outcome["error"]


def test_a_runaway_loop_hits_its_cap(monkeypatch):
    _fake(monkeypatch)
    outcome = _call(op="run_steps", steps=[{"action": "repeat", "max_iterations": 5, "as": "i",
                                            "steps": [{"action": "set", "values": {"n": "{{i}}"}}]},
                                           {"action": "return", "value": "{{n}}"}])
    assert outcome["returned"] == 4


def test_missing_inputs_are_reported_before_anything_runs(monkeypatch):
    calls = _fake(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        _call(op="run_steps", steps=[{"action": "open", "url": "{{base}}/x"}])
    assert "base" in str(excinfo.value) and calls == []


def test_plain_macros_still_replay_as_one_batch(monkeypatch):
    seen = []

    async def fake(actions, continue_on_error=False):
        seen.append(actions)
        return {"success": True, "results": [], "failure_count": 0}

    monkeypatch.setattr(main, "_execute_actions", fake)
    _save("plain", [{"action": "open", "url": "{{u}}"}, {"action": "click", "selector": "#a"}], {"u": None})
    outcome = _call(op="run", name="plain", variables={"u": "https://p"})
    assert len(seen) == 1 and len(seen[0]) == 2 and "flow" not in outcome


# --- background jobs -------------------------------------------------------------------------

def test_background_job_reports_progress_and_result(monkeypatch):
    _fake(monkeypatch)

    async def scenario():
        started = await main.browser_macro(op="run_steps", background=True, steps=[
            {"action": "sleep", "seconds": 0.3}, {"action": "open", "url": "https://bg"},
            {"action": "return", "value": "done"}])
        assert started["background"] is True and started["state"] == "running"
        listed = await main.browser_macro(op="jobs")
        assert any(job["job_id"] == started["job_id"] for job in listed["jobs"])
        finished = await main.browser_macro(op="job", job_id=started["job_id"], wait_seconds=5)
        return finished

    finished = asyncio.run(scenario())
    assert finished["state"] == "done" and finished["result"]["returned"] == "done"
    assert finished["progress"]["executed"] >= 1
    # the verdict sits on top, where summary='min' keeps it
    assert finished["returned"] == "done" and any("open ok" in line for line in finished["trace"])


def test_background_job_can_be_cancelled(monkeypatch):
    _fake(monkeypatch)

    async def scenario():
        started = await main.browser_macro(op="run_steps", background=True,
                                           steps=[{"action": "sleep", "seconds": 30}, {"action": "open", "url": "https://x"}])
        await asyncio.sleep(0.1)
        await main.browser_macro(op="cancel", job_id=started["job_id"])
        return await main.browser_macro(op="job", job_id=started["job_id"], wait_seconds=5)

    finished = asyncio.run(scenario())
    assert finished["state"] == "cancelled"


def test_an_unknown_job_names_the_known_ones():
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(macro_jobs.wait("job-nope", 0))
    assert "no job 'job-nope'" in str(excinfo.value) and "op='jobs'" in str(excinfo.value)


# --- validation, preview, guarded ------------------------------------------------------------

def test_validate_finds_script_mistakes_without_running(monkeypatch):
    calls = _fake(monkeypatch)
    _save("broken", [
        {"action": "if", "test": "a ==", "then": [{"action": "open", "url": "https://a"}]},
        {"action": "repeat", "steps": [{"action": "log", "message": "x"}]},
        {"action": "break"},
        {"action": "open", "url": "https://b", "save_as": "last"},
        {"action": "wait_until", "test": "x.ok", "bogus": 1},
        {"action": "if", "test": "true", "then": [{"action": "macro", "op": "run", "name": "other"}]},
    ])
    checked = _call(op="validate", name="broken")
    errors = " | ".join(item["error"] for item in checked["errors"])
    assert checked["valid"] is False and calls == []
    for expected in ("test: cannot parse", "'repeat' has no way to stop", "'break' outside",
                     "save_as 'last'", "unknown key(s) ['bogus']", "runs a macro through the 'macro' action"):
        assert expected in errors, expected


def test_validate_knows_inputs_from_produced_names():
    _save("script", [
        {"action": "run_script", "script": "return 1", "save_as": "info"},
        {"action": "for_each", "items": "{{files}}", "steps": [{"action": "open", "url": "{{item}}/{{info.id}}"}]},
        {"action": "wait", "selector": "#done"},
    ], {"files": None})
    checked = _call(op="validate", name="script")
    assert checked["valid"] is True, checked["errors"]
    assert checked["flow"] is True and checked["inputs"] == ["files"]
    assert {"info", "item", "last"} <= set(checked["produced"])


def test_validate_inline_steps_and_preview_outline(monkeypatch):
    checked = _call(op="validate", steps=[{"action": "if", "test": "x", "then": [{"action": "nope"}]}])
    assert checked["valid"] is False and checked["errors"][0]["index"] == "0.then.0"
    _save("show", [{"action": "open", "url": "{{base}}/a"},
                   {"action": "if", "test": "len(tags) > 0", "then": [{"action": "click", "selector": "#{{tags[0]}}"}]}],
          {"base": None, "tags": None})
    shown = _call(op="preview", name="show", variables={"base": "https://p", "tags": ["x"]})
    assert shown["executed"] is False and shown["flow"] is True
    assert shown["steps"][0]["url"] == "https://p/a"
    assert any("1.then.0 click" in line for line in shown["outline"])


def test_a_guarded_macro_must_stay_plain(tmp_path):
    _save("guarded", [{"action": "open", "url": "https://g", "when": "true"}, {"action": "submit"}])
    with pytest.raises(ValueError) as excinfo:
        _call(op="guarded_stage", name="guarded", guard={})
    assert "plain step list" in str(excinfo.value)


def test_uses_flow_tells_scripts_from_plain_macros():
    assert macro_check.uses_flow([{"action": "open", "url": "{{u}}"}]) is False
    assert macro_check.uses_flow([{"action": "open", "url": "{{u.v}}"}]) is True
    assert macro_check.uses_flow([{"action": "open", "url": "x", "save_as": "a"}]) is True
    assert macro_check.uses_flow([{"action": "log", "message": "x"}]) is True


def test_a_saved_script_reports_an_input_its_file_never_declared(monkeypatch):
    calls = _fake(monkeypatch)
    _save("needs", [{"action": "open", "url": "{{target}}"}, {"action": "log", "message": "{{note}}"}], {"note": None})
    with pytest.raises(ValueError) as excinfo:
        _call(op="run", name="needs")
    assert "target" in str(excinfo.value) and calls == []
    outcome = _call(op="run", name="needs", variables={"target": "https://t"})
    assert outcome["success"] is True and outcome["log"] == [""]


def test_the_cli_builds_a_macro_call_without_hand_written_json(tmp_path):
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "mcp_cli.py"
    spec = importlib.util.spec_from_file_location("wsn_mcp_cli_test", path)
    cli = importlib.util.module_from_spec(spec)
    sys.modules["wsn_mcp_cli_test"] = cli
    spec.loader.exec_module(cli)
    note = tmp_path / "note.txt"
    note.write_text("line 1\nline 2", encoding="utf-8")
    parser_args = ["macro", "run", "youtube-upload", "--var", "count=3", "--var", "title=Hello world",
                   "--var", f"description=@{note}", "--var", "tags=[\"a\", \"b\"]", "--background", "--session", "yt"]
    sys.argv = ["mcp_cli.py", *parser_args]
    import argparse
    captured = {}
    real_parse = argparse.ArgumentParser.parse_args

    def grab(self, *a, **k):
        captured["ns"] = real_parse(self, *a, **k)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = grab
    try:
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        argparse.ArgumentParser.parse_args = real_parse
    request = cli.macro_request(captured["ns"])
    action = request["args"]["actions"][0]
    assert action == {"action": "macro", "op": "run", "name": "youtube-upload", "background": True, "session_id": "yt",
                      "variables": {"count": 3, "title": "Hello world", "description": "line 1\nline 2", "tags": ["a", "b"]}}


# --- jobs everywhere, ask, parallel, schedules ------------------------------------------------

def test_a_foreground_run_is_a_job_too_and_says_so(monkeypatch):
    _fake(monkeypatch)
    outcome = _call(op="run_steps", steps=[{"action": "open", "url": "https://f"}, {"action": "return", "value": 1}])
    assert outcome["success"] is True and outcome["job_id"].startswith("job-")
    listed = _call(op="jobs")["jobs"]
    assert any(job["job_id"] == outcome["job_id"] and job["state"] == "done" for job in listed)
    assert (macro_jobs.jobs_dir() / f"{outcome['job_id']}.json").exists()


def test_another_process_s_jobs_are_listed_stopped_and_interrupted():
    import os
    root = macro_jobs.jobs_dir()
    alive = {"job_id": "job-000001-aaaaaa", "macro": "watcher", "state": "running", "pid": os.getpid(),
             "started_at": 1.0, "background": True}
    dead = {"job_id": "job-000002-bbbbbb", "macro": "old", "state": "running", "pid": 999_999_999,
            "started_at": 2.0, "background": True}
    for record in (alive, dead):
        (root / f"{record['job_id']}.json").write_text(json.dumps(record), encoding="utf-8")
    listed = {job["job_id"]: job for job in _call(op="jobs")["jobs"]}
    assert listed["job-000001-aaaaaa"]["state"] == "running"
    # Its process is gone: the job reads interrupted, never resumed.
    assert listed["job-000002-bbbbbb"]["state"] == "interrupted"
    stopped = _call(op="cancel", job_id="job-000001-aaaaaa")
    assert stopped["cancel_requested"] is True and (root / "job-000001-aaaaaa.cancel").exists()


def test_ask_pauses_a_job_until_op_answer(monkeypatch):
    calls = _fake(monkeypatch)

    async def scenario():
        started = await main.browser_macro(op="run_steps", background=True, steps=[
            {"action": "ask", "message": "Publish {{what}}?", "options": ["yes", "no"], "save_as": "reply"},
            {"action": "if", "test": "reply == 'yes'", "then": [{"action": "open", "url": "https://publish"}]},
            {"action": "return", "value": "{{reply}}"}], variables={"what": "the Short"})
        seen = {}
        for _ in range(50):
            seen = await main.browser_macro(op="job", job_id=started["job_id"])
            if seen["state"] == "waiting":
                break
            await asyncio.sleep(0.05)
        assert seen["question"]["text"] == "Publish the Short?" and seen["question"]["options"] == ["yes", "no"]
        await main.browser_macro(op="answer", job_id=started["job_id"], answer="yes")
        return await main.browser_macro(op="job", job_id=started["job_id"], wait_seconds=5)

    finished = asyncio.run(scenario())
    assert finished["state"] == "done" and finished["returned"] == "yes"
    assert [call["url"] for call in calls] == ["https://publish"]


def test_ask_falls_back_to_its_default_on_timeout(monkeypatch):
    _fake(monkeypatch)
    outcome = _call(op="run_steps", steps=[{"action": "ask", "message": "Continue?", "timeout_seconds": 1,
                                            "default": "no", "save_as": "reply"},
                                           {"action": "return", "value": "{{reply}}"}])
    assert outcome["success"] is True and outcome["returned"] == "no"


def test_parallel_runs_branches_at_once_and_merges_their_saves(monkeypatch):
    calls = _fake(monkeypatch, lambda action, n: _script_value(action["session_id"])
                  if action["action"] == "run_script" else {"success": True, "data": {}})
    outcome = _call(op="run_steps", steps=[
        {"action": "parallel", "branches": [
            [{"action": "sleep", "seconds": 0.2},
             {"action": "run_script", "script": "return 1", "session_id": "a", "save_as": "left"}],
            [{"action": "run_script", "script": "return 2", "session_id": "b", "save_as": "right"}],
        ]},
        {"action": "return", "value": "{{left}}+{{right}}"}])
    assert outcome["success"] is True and outcome["returned"] == "a+b"
    assert [call["session_id"] for call in calls] == ["b", "a"]  # the second branch did not wait for the first
    assert any(result["step"].startswith("0.branches.1.") for result in outcome["results"])


def test_parallel_fail_fast_stops_the_other_branch(monkeypatch):
    calls = _fake(monkeypatch, lambda action, n: {"success": False, "error": "nope"}
                  if action.get("url") == "https://bad" else {"success": True, "data": {}})
    outcome = _call(op="run_steps", steps=[{"action": "parallel", "branches": [
        [{"action": "open", "url": "https://bad"}],
        [{"action": "sleep", "seconds": 2}, {"action": "open", "url": "https://never"}]]}])
    assert outcome["success"] is False and "nope" in outcome["error"]
    assert "https://never" not in [call.get("url") for call in calls]


def test_a_background_job_marks_its_tabs_as_the_macro_s(monkeypatch):
    from web_search_neo import presence_live
    seen = []

    async def fake(actions, continue_on_error=False, summary="full"):
        seen.append(presence_live.MACRO_RUN.get())
        return {"success": True, "results": [{"index": 0, "action": actions[0]["action"], "success": True, "data": {}}]}

    monkeypatch.setattr(main, "_execute_actions", fake)
    _save("watch", [{"action": "open", "url": "https://w", "session_id": "bg"}, {"action": "log", "message": "x"}])

    async def scenario():
        started = await main.browser_macro(op="run", name="watch", background=True)
        await main.browser_macro(op="job", job_id=started["job_id"], wait_seconds=5)
        await main.browser_macro(op="run", name="watch", timeout_seconds=30)

    asyncio.run(scenario())
    # The background job's actions carry the padlock; the run the caller waited for is the agent's own.
    assert seen == ["watch", None]


def test_the_report_answer_stops_a_job_from_the_popup(monkeypatch):
    _fake(monkeypatch)
    replies = []

    def reporter(payload):
        running = [job["job_id"] for job in payload["jobs"] if job["state"] == "running"]
        replies.append(running)
        return {"cancel": running, "answers": {}}

    monkeypatch.setattr(macro_jobs, "REPORTER", reporter)
    monkeypatch.setattr(macro_jobs, "REPORT_SECONDS", 0.1)
    monkeypatch.setattr(macro_jobs, "WATCH_SECONDS", 0.1)

    async def scenario():
        started = await main.browser_macro(op="run_steps", background=True, steps=[
            {"action": "sleep", "seconds": 30}, {"action": "open", "url": "https://x"}])
        return await main.browser_macro(op="job", job_id=started["job_id"], wait_seconds=5)

    finished = asyncio.run(scenario())
    assert finished["state"] == "cancelled" and any(replies)


def test_schedules_are_stored_listed_run_and_removed(monkeypatch, tmp_path):
    from web_search_neo import macro_schedules
    monkeypatch.setenv("WEB_SEARCH_NEO_SCHEDULES_FILE", str(tmp_path / "schedules.json"))
    _save("tick", [{"action": "log", "message": "tick"}])
    created = _call(op="schedule", name="tick", every_seconds=30, max_runs=2)["schedule"]
    assert created["name"] == "tick" and created["every_seconds"] == 30.0 and created["next_run"]
    with pytest.raises(ValueError):
        _call(op="schedule", name="tick", every_seconds=1)
    with pytest.raises(ValueError):
        _call(op="schedule", name="tick", times=["25:00"])
    daily = _call(op="schedule", steps=[{"action": "log", "message": "d"}], times=["09:00", "18:30"])["schedule"]
    assert daily["inline_steps"] == 1
    listed = _call(op="schedules")
    assert {item["schedule_id"] for item in listed["schedules"]} == {created["schedule_id"], daily["schedule_id"]}

    launched = []

    async def launch(item):
        launched.append(item["schedule_id"])
        return {"job_id": f"job-{item['schedule_id']}", "state": "running"}

    items = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))["schedules"]
    for item in items:
        item["next_run_at"] = 0  # due now
    (tmp_path / "schedules.json").write_text(json.dumps({"schedules": items}), encoding="utf-8")
    monkeypatch.setitem(macro_schedules._STATE, "launch", launch)
    asyncio.run(macro_schedules._tick())
    assert sorted(launched) == sorted([created["schedule_id"], daily["schedule_id"]])
    stored = {item["schedule_id"]: item for item in macro_schedules._load()}
    assert stored[created["schedule_id"]]["runs"] == 1
    assert stored[created["schedule_id"]]["last_job_id"] == f"job-{created['schedule_id']}"
    assert stored[created["schedule_id"]]["next_run_at"] > 0
    _call(op="unschedule", schedule_id=daily["schedule_id"])
    assert [item["schedule_id"] for item in _call(op="schedules")["schedules"]] == [created["schedule_id"]]


def test_validate_knows_parallel_and_ask():
    checked = _call(op="validate", steps=[
        {"action": "parallel", "branches": [[{"action": "open", "url": "https://a"}], []]},
        {"action": "ask"},
        {"action": "parallel", "branches": [[{"action": "nope"}]]}])
    errors = " | ".join(item["error"] for item in checked["errors"])
    assert "'parallel' needs branches" in errors and "'ask' needs a message" in errors
    assert any(item["index"] == "2.branches.0.0" for item in checked["errors"])
