"""Saved action scripts: storage, placeholder resolution, and replay through the dispatcher."""

from __future__ import annotations

import asyncio
import json

import pytest

import macros
import main


@pytest.fixture(autouse=True)
def macro_root(tmp_path, monkeypatch):
    """Keep every test's macros in its own directory, never the real one."""
    monkeypatch.setenv("WEB_SEARCH_NEO_MACRO_ROOT", str(tmp_path / "macros"))
    main._RECORDING.update({"active": False, "name": "", "steps": [], "project_root": None})
    yield tmp_path
    main._RECORDING.update({"active": False, "name": "", "steps": [], "project_root": None})


FORM_STEPS = [
    {"action": "open", "url": "{{target_url}}", "session_id": "form"},
    {"action": "click", "selector": "#continue", "session_id": "form"},
    {"action": "fill", "fields": {"textarea[name=notes]": "{{notes}}"}, "session_id": "form"},
]


# --- storage ----------------------------------------------------------------


def test_save_declares_placeholders_and_round_trips():
    record = macros.save("form-flow", FORM_STEPS, description="Complete one request form")
    assert record["step_count"] == 3
    assert sorted(record["variables"]) == ["notes", "target_url"]

    loaded = macros.load("form-flow")
    assert loaded["steps"] == FORM_STEPS
    assert loaded["description"] == "Complete one request form"


def test_declared_default_survives_and_beats_auto_declaration():
    macros.save("greet", [{"action": "open", "url": "{{site}}"}], variables={"site": "https://example.com"})
    assert macros.load("greet")["variables"]["site"] == "https://example.com"


def test_list_summarises_and_delete_removes():
    macros.save("one", [{"action": "close_all"}])
    macros.save("two", FORM_STEPS)
    listed = {item["name"]: item for item in macros.list_macros()}
    assert set(listed) == {"one", "two"}
    assert listed["two"]["step_count"] == 3
    assert listed["two"]["variables"] == ["notes", "target_url"]

    assert macros.delete("one") is True
    assert macros.delete("one") is False
    assert [item["name"] for item in macros.list_macros()] == ["two"]


def test_broken_file_is_reported_without_hiding_the_others(macro_root):
    macros.save("good", [{"action": "close_all"}])
    (macros.macro_root() / "bad.json").write_text("{not json", encoding="utf-8")
    listed = {item["name"]: item for item in macros.list_macros()}
    assert "broken" in listed["bad"]
    assert listed["good"]["step_count"] == 1


def test_load_unknown_macro_names_the_saved_ones():
    macros.save("known", [{"action": "close_all"}])
    with pytest.raises(ValueError, match="known"):
        macros.load("missing")


@pytest.mark.parametrize("name", ["", "../escape", "has space", "a" * 65, "semi;colon"])
def test_bad_names_are_refused(name):
    with pytest.raises(ValueError):
        macros.validate_name(name)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"name": "x"}, "no 'steps' key"),
        ("{oops", "not readable JSON"),
        ({"name": "x", "steps": "open"}, "non-empty list"),
        ({"name": "x", "steps": []}, "non-empty list"),
        ({"name": "x", "steps": [{"url": "u"}]}, "action"),
    ],
)
def test_a_hand_edited_file_fails_with_a_readable_message(body, expected):
    # These files are meant to be edited by hand, so a wrong shape has to read
    # as a mistake rather than as a KeyError from inside the replay.
    path = macros.macro_root() / "edited.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        macros.load("edited")


def test_a_value_that_looks_like_a_placeholder_is_left_alone():
    macros.save("x", [{"action": "open", "url": "{{a}}"}])
    filled = macros.resolve(macros.load("x")["steps"], None, {"a": "{{b}}"})
    # No second pass: the value is data, not a template, or a macro could be
    # made to expand something the caller never wrote.
    assert filled[0]["url"] == "{{b}}"


def test_a_non_utf8_file_is_reported_broken_without_hiding_the_others():
    macros.save("good", [{"action": "close_all"}])
    (macros.macro_root() / "latin.json").write_bytes(
        b'{"name":"latin","steps":[{"action":"open","url":"\xe9"}]}'
    )
    listed = {item["name"]: item for item in macros.list_macros()}
    assert "broken" in listed["latin"]
    assert listed["good"]["step_count"] == 1
    with pytest.raises(ValueError, match="not readable JSON"):
        macros.load("latin")


def test_valid_json_that_is_not_an_object_is_reported_broken():
    macros.save("good", [{"action": "close_all"}])
    (macros.macro_root() / "array.json").write_text("[1,2,3]", encoding="utf-8")
    listed = {item["name"]: item for item in macros.list_macros()}
    assert "broken" in listed["array"]
    assert listed["good"]["step_count"] == 1


def test_a_stale_hand_edited_step_count_is_recounted():
    macros.save("edited", [{"action": "close_all"}])
    path = macros.macro_root() / "edited.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["step_count"] = 99
    record["steps"].append({"action": "close_all"})
    path.write_text(json.dumps(record), encoding="utf-8")

    assert macros.load("edited")["step_count"] == 2
    assert [item for item in macros.list_macros() if item["name"] == "edited"][0]["step_count"] == 2


# --- step validation --------------------------------------------------------


def test_steps_must_be_a_non_empty_list_of_actions():
    with pytest.raises(ValueError):
        macros.validate_steps([])
    with pytest.raises(ValueError):
        macros.validate_steps(["open"])
    with pytest.raises(ValueError, match="action"):
        macros.validate_steps([{"url": "https://example.com"}])


def test_a_macro_cannot_run_a_macro():
    with pytest.raises(ValueError, match="one after another"):
        macros.validate_steps([{"action": "macro", "name": "other"}])


def test_step_count_is_capped():
    with pytest.raises(ValueError, match="at most"):
        macros.validate_steps([{"action": "close_all"}] * (macros.MAX_STEPS + 1))


# --- placeholders -----------------------------------------------------------


def test_resolve_fills_nested_strings_and_keeps_whole_value_types():
    steps = [
        {"action": "open", "url": "https://example.com/items/{{id}}"},
        {"action": "step", "frames": "{{frames}}", "session_id": "s"},
    ]
    filled = macros.resolve(steps, None, {"id": 42, "frames": 3})
    assert filled[0]["url"] == "https://example.com/items/42"
    # A placeholder that is the whole string keeps its native type, so a
    # recorded int parameter still validates on replay.
    assert filled[1]["frames"] == 3


def test_resolve_reaches_into_dicts_and_lists():
    steps = [{"action": "fill", "fields": {"#a": "{{name}}"}, "tags": ["{{name}}", "x"]}]
    filled = macros.resolve(steps, None, {"name": "Viktor"})
    assert filled[0]["fields"]["#a"] == "Viktor"
    assert filled[0]["tags"] == ["Viktor", "x"]


def test_resolve_reports_every_missing_name_at_once():
    with pytest.raises(ValueError) as excinfo:
        macros.resolve(FORM_STEPS, {"notes": None, "target_url": None}, {})
    message = str(excinfo.value)
    assert "notes" in message and "target_url" in message


def test_declared_default_fills_an_unsupplied_variable():
    steps = [{"action": "open", "url": "{{site}}"}]
    assert macros.resolve(steps, {"site": "https://example.com"}, {})[0]["url"] == "https://example.com"


def test_supplied_value_overrides_the_default():
    steps = [{"action": "open", "url": "{{site}}"}]
    filled = macros.resolve(steps, {"site": "https://example.com"}, {"site": "https://example.org"})
    assert filled[0]["url"] == "https://example.org"


def test_a_placeholder_in_a_dict_key_is_declared_and_filled():
    # A fill step's keys are its CSS selectors, which is exactly the part worth
    # varying between runs; leaving keys out sent a literal {{sel}} to the page.
    steps = [{"action": "fill", "fields": {"{{sel}}": "value"}}]
    assert macros.placeholders_in(steps) == {"sel"}
    filled = macros.resolve(steps, None, {"sel": "#name"})
    assert filled[0]["fields"] == {"#name": "value"}

    record = macros.save("keyed", steps)
    assert sorted(record["variables"]) == ["sel"]


def test_an_explicit_null_counts_as_missing_not_as_a_value():
    steps = [{"action": "open", "url": "{{id}}"}]
    with pytest.raises(ValueError, match="id"):
        macros.resolve(steps, None, {"id": None})


def test_resolve_leaves_a_macro_without_placeholders_alone():
    steps = [{"action": "close_all"}]
    assert macros.resolve(steps, None, None) == steps


# --- the macro action itself ------------------------------------------------


def _call(**kwargs):
    return asyncio.run(main.browser_macro(**kwargs))


def test_record_then_save_captures_dispatched_actions(monkeypatch):
    _call(op="record", name="recorded")
    assert main._RECORDING["active"] is True

    main._record_step("open", {"url": "https://example.com", "session_id": "s"})
    main._record_step("click", {"selector": "#go", "session_id": "s"})

    saved = _call(op="save")
    assert saved["step_count"] == 2
    assert saved["recorded"] is True
    # Saving closes the recording, so the next drive does not append to it.
    assert main._RECORDING["active"] is False
    assert macros.load("recorded")["steps"][0]["action"] == "open"


def test_macro_records_and_replays_every_click_mode_and_new_actions(monkeypatch):
    """Macros share the validated dispatcher, so every click target form and
    the newer actions record and replay through it instead of a side channel."""
    seen: list[dict] = []

    async def fake_click(**kwargs):
        seen.append({"action": "click", **kwargs})
        return {"success": True, "target": "text" if kwargs.get("text") else kwargs.get("selector")}

    async def fake_captcha(**kwargs):
        seen.append({"action": "captcha", **kwargs})
        return {"success": True}

    async def fake_script(**kwargs):
        seen.append({"action": "run_script", **kwargs})
        return {"success": True}

    click_spec = main._ACTIONS["click"]
    monkeypatch.setitem(
        main._ACTIONS, "click",
        main.ActionSpec("click", fake_click, click_spec.tool_name, "page", "s"),
    )
    captcha_spec = main._ACTIONS["captcha"]
    monkeypatch.setitem(
        main._ACTIONS, "captcha",
        main.ActionSpec("captcha", fake_captcha, captcha_spec.tool_name, "page", "s"),
    )
    script_spec = main._ACTIONS["run_script"]
    monkeypatch.setitem(
        main._ACTIONS, "run_script",
        main.ActionSpec("run_script", fake_script, script_spec.tool_name, "page", "s"),
    )

    async def drive():
        await main.browser_macro(op="record", name="click-all")
        await main._execute_actions(
            [
                {"action": "click", "selector": "#go", "session_id": "s"},
                {"action": "click", "text": "Submit", "role": "button", "session_id": "s"},
                {"action": "click", "x": 120, "y": 40, "session_id": "s"},
                {"action": "captcha", "session_id": "s"},
                {"action": "run_script", "script": "return 1", "session_id": "s"},
            ]
        )

    asyncio.run(drive())
    saved = _call(op="save")
    assert saved["step_count"] == 5

    seen.clear()
    outcome = _call(op="run", name="click-all")
    assert outcome["macro"] == "click-all"
    assert outcome["step_count"] == 5
    assert outcome["failure_count"] == 0

    clicks = [item for item in seen if item["action"] == "click"]
    assert len(clicks) == 3
    # Selector mode: the element target survives.
    assert clicks[0]["selector"] == "#go" and clicks[0].get("text") is None
    # Text mode: the strict text/role target survives.
    assert clicks[1]["text"] == "Submit" and clicks[1]["role"] == "button"
    # Coordinate mode: the viewport point survives.
    assert clicks[2]["x"] == 120 and clicks[2]["y"] == 40
    # The newer actions replay through the same validated dispatch.
    assert any(item["action"] == "captcha" for item in seen)
    assert any(item["action"] == "run_script" for item in seen)


def test_recording_over_captured_steps_is_refused():
    _call(op="record", name="first")
    main._record_step("open", {"url": "https://example.com"})
    with pytest.raises(ValueError, match="already open"):
        _call(op="record", name="second")
    # The first recording is intact, not silently replaced.
    assert main._RECORDING["name"] == "first"
    assert len(main._RECORDING["steps"]) == 1


def test_recording_over_an_empty_one_is_allowed():
    _call(op="record", name="first")
    _call(op="record", name="second")
    assert main._RECORDING["name"] == "second"


def test_cancel_throws_the_recording_away():
    _call(op="record", name="doomed")
    main._record_step("open", {"url": "https://example.com"})
    assert _call(op="cancel")["discarded"] == "doomed"
    assert main._RECORDING["steps"] == []
    with pytest.raises(ValueError):
        macros.load("doomed")


def test_save_without_steps_or_recording_is_refused():
    with pytest.raises(ValueError, match="open recording"):
        _call(op="save", name="empty")


def test_explicit_save_does_not_disturb_an_open_recording():
    _call(op="record", name="live")
    main._record_step("open", {"url": "https://example.com"})
    _call(op="save", name="other", steps=[{"action": "close_all"}])
    # The explicit save is a different macro; the recording is still collecting.
    assert main._RECORDING["active"] is True
    assert main._RECORDING["name"] == "live"


def test_explicit_save_will_not_borrow_the_open_recordings_name():
    # Otherwise one unnamed save overwrites the macro the recording is going to
    # be saved as, destroying a task already driven by hand.
    macros.save("live", [{"action": "open", "url": "https://real.example"}])
    _call(op="record", name="live")
    main._record_step("click", {"selector": "#x"})
    with pytest.raises(ValueError, match="requires name"):
        _call(op="save", steps=[{"action": "close_all"}])
    assert macros.load("live")["steps"][0]["url"] == "https://real.example"


def test_a_macro_call_is_never_captured_into_a_recording():
    # Recording one would build a script the saver refuses outright, leaving the
    # recording unsaveable with only cancel - which loses everything - as a way out.
    _call(op="record", name="outer")
    main._record_step("open", {"url": "https://example.com"})
    main._record_step("macro", {"op": "run", "name": "login"})
    saved = _call(op="save")
    assert saved["step_count"] == 1


def test_run_replays_every_step_through_the_dispatcher(monkeypatch):
    seen: list[dict] = []

    async def fake_execute(actions, continue_on_error=False, record=True):
        seen.append({"actions": actions, "record": record})
        return {"success": True, "results": [], "failure_count": 0}

    monkeypatch.setattr(main, "_execute_actions", fake_execute)
    macros.save("request", FORM_STEPS)
    outcome = _call(op="run", name="request", variables={"target_url": "https://example.com/r/1", "notes": "Hi"})

    assert outcome["macro"] == "request"
    assert outcome["step_count"] == 3
    assert seen[0]["actions"][0]["url"] == "https://example.com/r/1"
    assert seen[0]["actions"][2]["fields"]["textarea[name=notes]"] == "Hi"
    # A replay must not be captured into an open recording as its own steps.
    assert seen[0]["record"] is False


def test_preview_resolves_every_step_without_dispatching(monkeypatch):
    async def forbidden_execute(*args, **kwargs):
        raise AssertionError("preview must not dispatch browser actions")

    monkeypatch.setattr(main, "_execute_actions", forbidden_execute)
    macros.save("request", FORM_STEPS, description="Review before sending")

    outcome = _call(
        op="preview",
        name="request",
        variables={"target_url": "https://example.com/r/42", "notes": "Hello"},
    )

    assert outcome["success"] is True
    assert outcome["executed"] is False
    assert outcome["description"] == "Review before sending"
    assert outcome["step_count"] == 3
    assert outcome["steps"][0]["url"] == "https://example.com/r/42"
    assert outcome["steps"][2]["fields"]["textarea[name=notes]"] == "Hello"
    assert "no browser state changed" in outcome["note"]


def test_preview_requires_every_variable_before_returning_steps():
    macros.save("request", FORM_STEPS)
    with pytest.raises(ValueError) as excinfo:
        _call(op="preview", name="request", variables={"target_url": "https://example.com/r/42"})
    assert "notes" in str(excinfo.value)


def test_concurrent_batches_do_not_interleave_into_one_recording(monkeypatch):
    """Two batches sent at once must record as two runs, not as one shuffled script."""
    started: list[str] = []

    async def slow_handler(**kwargs):
        # Yield in the middle, which is what a real browser round trip does.
        started.append(kwargs.get("url") or kwargs.get("selector") or "?")
        await asyncio.sleep(0.01)
        return {"success": True}

    spec = main._ACTIONS["close_all"]
    monkeypatch.setitem(
        main._ACTIONS, "open", main.ActionSpec("open", slow_handler, spec.tool_name, "page", "s")
    )
    monkeypatch.setattr(main, "_validate_arguments", lambda tool, label, arguments: arguments)

    async def drive():
        await main.browser_macro(op="record", name="ordered")
        await asyncio.gather(
            main._execute_actions([{"action": "open", "url": "A1"}, {"action": "open", "url": "A2"}]),
            main._execute_actions([{"action": "open", "url": "B1"}, {"action": "open", "url": "B2"}]),
        )

    asyncio.run(drive())
    recorded = [step["url"] for step in main._RECORDING["steps"]]
    # Each batch stays contiguous; which batch went first does not matter.
    assert recorded in (["A1", "A2", "B1", "B2"], ["B1", "B2", "A1", "A2"])


def test_run_without_required_variables_says_what_is_missing():
    macros.save("request", FORM_STEPS)
    with pytest.raises(ValueError, match="notes"):
        _call(op="run", name="request", variables={"target_url": "https://example.com/r/1"})


def test_show_returns_the_stored_record_and_list_is_empty_at_first():
    assert _call(op="list")["macros"] == []
    macros.save("request", FORM_STEPS, description="d")
    shown = _call(op="show", name="request")
    assert shown["description"] == "d"
    assert len(shown["steps"]) == 3


def test_delete_reports_whether_anything_was_removed():
    macros.save("request", FORM_STEPS)
    assert _call(op="delete", name="request")["deleted"] is True
    assert _call(op="delete", name="request")["deleted"] is False


@pytest.mark.parametrize("op", ["run", "preview", "show", "delete", "record"])
def test_ops_that_need_a_name_say_so(op):
    with pytest.raises(ValueError, match="requires name"):
        _call(op=op)


def test_unknown_op_lists_the_real_ones():
    with pytest.raises(ValueError, match="record"):
        _call(op="bogus")


def test_macro_is_registered_as_an_action():
    assert "macro" in main._ACTIONS
    assert main._ACTIONS["macro"].group == "macro"


# --- generic guarded consequential macros ----------------------------------


def _guarded_steps():
    return [
        {"action": "open", "url": "{{target_url}}", "session_id": "guarded"},
        {
            "action": "upload",
            "selector": "input[type=file]",
            "file_paths": ["{{resource_path}}"],
            "session_id": "guarded",
        },
        {"action": "page_text", "session_id": "guarded"},
        {"action": "submit", "form_selector": "form", "session_id": "guarded"},
    ]


def _guard(resource_path, **overrides):
    guard = {
        "target_url": "https://forms.example.com/requests/42",
        "canonical_url": "https://forms.example.com/requests/42/",
        "identity_key": "request-42",
        "allowed_hosts": ["example.com"],
        "denied_hosts": ["blocked.example"],
        "resource_path": str(resource_path),
        "idempotency_token": "request-42-20260820",
        "assertions": [{"result_index": 2, "path": "data.text", "contains": "Request 42"}],
    }
    guard.update(overrides)
    return guard


def test_guarded_macro_stages_asserts_then_commits_submit_once(tmp_path, monkeypatch):
    resource = tmp_path / "request-42.pdf"
    resource.write_bytes(b"pdf")
    macros.save("request", _guarded_steps())
    calls = []

    async def fake_execute(actions, continue_on_error=False, record=True):
        calls.append(actions)
        result_data = {} if actions[0]["action"] == "submit" else {"text": "Request 42 ready"}
        results = [
            {"index": index, "action": step["action"], "success": True, "data": {}}
            for index, step in enumerate(actions)
        ]
        results[-1]["data"] = result_data
        return {"success": True, "results": results, "failure_count": 0}

    monkeypatch.setattr(main, "_execute_actions", fake_execute)
    variables = {
        "target_url": "https://forms.example.com/requests/42",
        "resource_path": str(resource.resolve()),
    }
    staged = _call(
        op="guarded_stage",
        name="request",
        variables=variables,
        guard=_guard(resource.resolve()),
    )
    assert staged["checkpoint"] == "guard-request-42-20260820"
    assert staged["executed_submit"] is False
    assert all(step["action"] != "submit" for step in calls[0])
    assert _call(op="guarded_commit", checkpoint=staged["checkpoint"])["executed_submit"] is True
    with pytest.raises(ValueError, match="already submit_attempted"):
        _call(op="guarded_commit", checkpoint=staged["checkpoint"])


def test_host_policy_is_caller_configuration_not_built_in(tmp_path):
    resource = tmp_path / "resource.bin"
    resource.write_bytes(b"x")
    target = "https://blocked.example/requests/42"
    steps = macros.resolve(
        _guarded_steps(), None, {"target_url": target, "resource_path": str(resource.resolve())}
    )
    with pytest.raises(ValueError, match="policy denies"):
        macros.validate_guard(
            _guard(resource, target_url=target, canonical_url=target, allowed_hosts=["blocked.example"]),
            steps,
        )
    allowed = macros.validate_guard(
        _guard(
            resource,
            target_url=target,
            canonical_url=target,
            allowed_hosts=["blocked.example"],
            denied_hosts=[],
        ),
        steps,
    )
    assert allowed["canonical_url"] == target


def test_guard_requires_exact_target_host_and_uploaded_resource(tmp_path):
    resource = tmp_path / "resource.bin"
    other = tmp_path / "other.bin"
    resource.write_bytes(b"x")
    other.write_bytes(b"y")
    steps = macros.resolve(
        _guarded_steps(),
        None,
        {
            "target_url": "https://forms.example.com/requests/42",
            "resource_path": str(other.resolve()),
        },
    )
    with pytest.raises(ValueError, match="exact guard.resource_path"):
        macros.validate_guard(_guard(resource.resolve()), steps)
    with pytest.raises(ValueError, match="explicitly allow"):
        macros.validate_guard(_guard(other, allowed_hosts=["other.example"]), steps)
    with pytest.raises(ValueError, match="must equal"):
        macros.validate_guard(
            _guard(other, canonical_url="https://forms.example.com/requests/99"), steps
        )


def test_failed_assertion_does_not_create_checkpoint(tmp_path, monkeypatch):
    resource = tmp_path / "resource.bin"
    resource.write_bytes(b"x")
    macros.save("request", _guarded_steps())

    async def fake_execute(actions, continue_on_error=False, record=True):
        return {"success": True, "results": [{"data": {}}, {"data": {}}, {"data": {"text": "No"}}]}

    monkeypatch.setattr(main, "_execute_actions", fake_execute)
    with pytest.raises(ValueError, match="assertion 0 failed"):
        _call(
            op="guarded_stage",
            name="request",
            variables={
                "target_url": "https://forms.example.com/requests/42",
                "resource_path": str(resource.resolve()),
            },
            guard=_guard(resource.resolve()),
        )
    assert not macros._guarded_ledger_path().exists()


def test_resource_and_identity_reservations_prevent_reuse(tmp_path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"1")
    second.write_bytes(b"2")

    def checked(resource, token, target="https://forms.example.com/requests/42"):
        steps = macros.resolve(
            _guarded_steps(), None, {"target_url": target, "resource_path": str(resource.resolve())}
        )
        return macros.validate_guard(
            _guard(
                resource.resolve(),
                target_url=target,
                canonical_url=target,
                identity_key=target.rsplit("/", 1)[-1],
                idempotency_token=token,
            ),
            steps,
        )

    macros.reserve_checkpoint(checked(first, "first-token-20260820"), {"action": "submit"})
    with pytest.raises(ValueError, match="identity was already staged"):
        macros.reserve_checkpoint(checked(second, "second-token-20260820"), {"action": "submit"})
    with pytest.raises(ValueError, match="another target"):
        macros.reserve_checkpoint(
            checked(first, "third-token-20260820", "https://forms.example.com/requests/43"),
            {"action": "submit"},
        )


def test_guarded_macro_requires_one_terminal_explicit_submit():
    with pytest.raises(ValueError, match="exactly one"):
        macros.split_terminal_submit([{"action": "click", "text": "Confirm"}])
    with pytest.raises(ValueError, match="exactly one"):
        macros.split_terminal_submit([{"action": "submit"}, {"action": "page_text"}])


def test_project_roots_keep_macro_sets_and_ledgers_separate(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    macros.save("same-name", [{"action": "open", "url": "https://a.example"}], project_root=project_a)
    macros.save("same-name", [{"action": "open", "url": "https://b.example"}], project_root=project_b)
    assert macros.load("same-name", project_a)["steps"][0]["url"] == "https://a.example"
    assert macros.load("same-name", project_b)["steps"][0]["url"] == "https://b.example"
    assert macros.macro_root(project_a) == project_a / ".web-search-neo" / "macros"
    assert _call(op="list", project_root=str(project_a))["storage"] == str(macros.macro_root(project_a))
    with pytest.raises(ValueError, match="existing absolute"):
        macros.macro_root("relative/project")


def test_recording_remembers_its_project_store_until_save(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _call(op="record", name="recorded-local", project_root=str(project))
    main._record_step("open", {"url": "https://example.com"})
    saved = _call(op="save")
    assert saved["name"] == "recorded-local"
    assert macros.load("recorded-local", project)["steps"][0]["url"] == "https://example.com"
    with pytest.raises(ValueError, match="does not exist"):
        macros.load("recorded-local")


def test_guard_checkpoint_is_available_only_in_its_project(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    guard = {
        "identity": "https://example.com/request/1",
        "resource_path": str((tmp_path / "resource.bin").resolve()),
        "idempotency_token": "project-local-token-1",
    }
    checkpoint = macros.reserve_checkpoint(guard, {"action": "submit"}, project_a)
    with pytest.raises(ValueError, match="unknown guarded checkpoint"):
        macros.consume_checkpoint(checkpoint, project_b)
    assert macros.consume_checkpoint(checkpoint, project_a)["state"] == "submit_attempted"
