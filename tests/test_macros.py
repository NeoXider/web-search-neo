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
    main._RECORDING.update({"active": False, "name": "", "steps": []})
    yield tmp_path
    main._RECORDING.update({"active": False, "name": "", "steps": []})


APPLY_STEPS = [
    {"action": "open", "url": "{{vacancy_url}}", "session_id": "hh"},
    {"action": "click", "selector": "[data-qa=vacancy-response-link-top]", "session_id": "hh"},
    {"action": "fill", "fields": {"textarea[name=text]": "{{cover_letter}}"}, "session_id": "hh"},
]


# --- storage ----------------------------------------------------------------


def test_save_declares_placeholders_and_round_trips():
    record = macros.save("hh-apply", APPLY_STEPS, description="Apply to one vacancy")
    assert record["step_count"] == 3
    assert sorted(record["variables"]) == ["cover_letter", "vacancy_url"]

    loaded = macros.load("hh-apply")
    assert loaded["steps"] == APPLY_STEPS
    assert loaded["description"] == "Apply to one vacancy"


def test_declared_default_survives_and_beats_auto_declaration():
    macros.save("greet", [{"action": "open", "url": "{{site}}"}], variables={"site": "https://hh.ru"})
    assert macros.load("greet")["variables"]["site"] == "https://hh.ru"


def test_list_summarises_and_delete_removes():
    macros.save("one", [{"action": "close_all"}])
    macros.save("two", APPLY_STEPS)
    listed = {item["name"]: item for item in macros.list_macros()}
    assert set(listed) == {"one", "two"}
    assert listed["two"]["step_count"] == 3
    assert listed["two"]["variables"] == ["cover_letter", "vacancy_url"]

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
        {"action": "open", "url": "https://hh.ru/vacancy/{{id}}"},
        {"action": "step", "frames": "{{frames}}", "session_id": "s"},
    ]
    filled = macros.resolve(steps, None, {"id": 42, "frames": 3})
    assert filled[0]["url"] == "https://hh.ru/vacancy/42"
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
        macros.resolve(APPLY_STEPS, {"cover_letter": None, "vacancy_url": None}, {})
    message = str(excinfo.value)
    assert "cover_letter" in message and "vacancy_url" in message


def test_declared_default_fills_an_unsupplied_variable():
    steps = [{"action": "open", "url": "{{site}}"}]
    assert macros.resolve(steps, {"site": "https://hh.ru"}, {})[0]["url"] == "https://hh.ru"


def test_supplied_value_overrides_the_default():
    steps = [{"action": "open", "url": "{{site}}"}]
    filled = macros.resolve(steps, {"site": "https://hh.ru"}, {"site": "https://linkedin.com"})
    assert filled[0]["url"] == "https://linkedin.com"


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
    macros.save("apply", APPLY_STEPS)
    outcome = _call(op="run", name="apply", variables={"vacancy_url": "https://hh.ru/v/1", "cover_letter": "Hi"})

    assert outcome["macro"] == "apply"
    assert outcome["step_count"] == 3
    assert seen[0]["actions"][0]["url"] == "https://hh.ru/v/1"
    assert seen[0]["actions"][2]["fields"]["textarea[name=text]"] == "Hi"
    # A replay must not be captured into an open recording as its own steps.
    assert seen[0]["record"] is False


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
    macros.save("apply", APPLY_STEPS)
    with pytest.raises(ValueError, match="cover_letter"):
        _call(op="run", name="apply", variables={"vacancy_url": "https://hh.ru/v/1"})


def test_show_returns_the_stored_record_and_list_is_empty_at_first():
    assert _call(op="list")["macros"] == []
    macros.save("apply", APPLY_STEPS, description="d")
    shown = _call(op="show", name="apply")
    assert shown["description"] == "d"
    assert len(shown["steps"]) == 3


def test_delete_reports_whether_anything_was_removed():
    macros.save("apply", APPLY_STEPS)
    assert _call(op="delete", name="apply")["deleted"] is True
    assert _call(op="delete", name="apply")["deleted"] is False


@pytest.mark.parametrize("op", ["run", "show", "delete", "record"])
def test_ops_that_need_a_name_say_so(op):
    with pytest.raises(ValueError, match="requires name"):
        _call(op=op)


def test_unknown_op_lists_the_real_ones():
    with pytest.raises(ValueError, match="record"):
        _call(op="bogus")


def test_macro_is_registered_as_an_action():
    assert "macro" in main._ACTIONS
    assert main._ACTIONS["macro"].group == "macro"
