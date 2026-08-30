"""Saved action scripts: storage, placeholder resolution, and replay through the dispatcher."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from web_search_neo import macros
from web_search_neo import main


@pytest.fixture(autouse=True)
def macro_root(tmp_path, monkeypatch):
    """Keep every test's macros in its own directory, never the real one."""
    monkeypatch.setenv("WEB_SEARCH_NEO_MACRO_ROOT", str(tmp_path / "macros"))
    # A developer machine may name a project for its own MCP client; a test run
    # must not then write its macros into that project.
    monkeypatch.delenv("WEB_SEARCH_NEO_PROJECT_ROOT", raising=False)
    yield tmp_path


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


def test_list_summarises_every_macro_in_the_store():
    macros.save("one", [{"action": "close_all"}])
    macros.save("two", FORM_STEPS)
    listed = {item["name"]: item for item in macros.list_macros()}
    assert set(listed) == {"one", "two"}
    assert listed["two"]["step_count"] == 3
    assert listed["two"]["variables"] == ["notes", "target_url"]


def test_broken_file_is_reported_without_hiding_the_others(macro_root):
    macros.save("good", [{"action": "close_all"}])
    (macros.macro_root() / "bad.json").write_text("{not json", encoding="utf-8")
    listed = {item["name"]: item for item in macros.list_macros()}
    assert "broken" in listed["bad"]
    assert listed["good"]["step_count"] == 1


def test_load_unknown_macro_names_the_store_and_the_saved_ones(tmp_path):
    macros.save("known", [{"action": "close_all"}])
    with pytest.raises(ValueError, match="known"):
        macros.load("missing")

    # The store is the diagnosis: a project macro looked for without its
    # project_root is missing from a store the caller never meant to read.
    project = tmp_path / "elsewhere"
    project.mkdir()
    macros.save("project-only", [{"action": "close_all"}], project_root=project)
    with pytest.raises(ValueError, match="user store"):
        macros.load("project-only")
    with pytest.raises(ValueError, match=r'project_root needs the same one'):
        macros.load("project-only")


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


def test_a_written_macro_replays_every_click_mode_and_new_action(monkeypatch):
    """Macros share the validated dispatcher, so every click target form and
    the newer actions replay through it instead of a side channel."""
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

    macros.save(
        "click-all",
        [
            {"action": "click", "selector": "#go", "session_id": "s"},
            {"action": "click", "text": "Submit", "role": "button", "session_id": "s"},
            {"action": "click", "x": 120, "y": 40, "session_id": "s"},
            {"action": "captcha", "session_id": "s"},
            {"action": "run_script", "script": "return 1", "session_id": "s"},
        ],
    )

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


def test_run_replays_every_step_through_the_dispatcher(monkeypatch):
    seen: list[dict] = []

    async def fake_execute(actions, continue_on_error=False):
        seen.append({"actions": actions})
        return {"success": True, "results": [], "failure_count": 0}

    monkeypatch.setattr(main, "_execute_actions", fake_execute)
    macros.save("request", FORM_STEPS)
    outcome = _call(op="run", name="request", variables={"target_url": "https://example.com/r/1", "notes": "Hi"})

    assert outcome["macro"] == "request"
    assert outcome["step_count"] == 3
    assert seen[0]["actions"][0]["url"] == "https://example.com/r/1"
    assert seen[0]["actions"][2]["fields"]["textarea[name=notes]"] == "Hi"


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


@pytest.mark.parametrize("op", ["run", "preview", "show", "validate"])
def test_ops_that_need_a_name_say_so(op):
    with pytest.raises(ValueError, match="requires name"):
        _call(op=op)


def test_unknown_op_lists_the_real_ones():
    with pytest.raises(ValueError, match="list, show, validate, preview, run"):
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
    resource_path = Path(resource_path)
    guard = {
        "target_url": "https://forms.example.com/requests/42",
        "canonical_url": "https://forms.example.com/requests/42/",
        "identity_key": "request-42",
        "allowed_hosts": ["example.com"],
        "denied_hosts": ["blocked.example"],
        "resource_path": str(resource_path),
        "resource_sha256": hashlib.sha256(resource_path.read_bytes()).hexdigest(),
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

    async def fake_execute(actions, continue_on_error=False):
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
    expected_sha256 = hashlib.sha256(resource.read_bytes()).hexdigest()
    assert staged["resource_sha256"] == expected_sha256
    assert staged["executed_submit"] is False
    assert all(step["action"] != "submit" for step in calls[0])
    committed = _call(op="guarded_commit", checkpoint=staged["checkpoint"])
    assert committed["executed_submit"] is True
    assert committed["resource_sha256"] == expected_sha256
    with pytest.raises(ValueError, match="already submit_attempted"):
        _call(op="guarded_commit", checkpoint=staged["checkpoint"])


@pytest.mark.parametrize(
    "terminal",
    [
        {"action": "click", "selector": "button[data-action='confirm']", "session_id": "guarded"},
        {
            "action": "click",
            "text": "Confirm request",
            "role": "button",
            "exact": True,
            "session_id": "guarded",
        },
    ],
)
def test_guarded_macro_stages_then_commits_safe_terminal_click_once(
    tmp_path, monkeypatch, terminal
):
    resource = tmp_path / "request-42.pdf"
    resource.write_bytes(b"pdf")
    macros.save("request-click", [*_guarded_steps()[:-1], terminal])
    calls = []

    async def fake_execute(actions, continue_on_error=False):
        calls.append(actions)
        results = [
            {"index": index, "action": step["action"], "success": True, "data": {}}
            for index, step in enumerate(actions)
        ]
        if len(actions) > 1:
            results[-1]["data"] = {"text": "Request 42 ready"}
        return {"success": True, "results": results, "failure_count": 0}

    monkeypatch.setattr(main, "_execute_actions", fake_execute)
    staged = _call(
        op="guarded_stage",
        name="request-click",
        variables={
            "target_url": "https://forms.example.com/requests/42",
            "resource_path": str(resource.resolve()),
        },
        guard=_guard(resource.resolve()),
    )
    assert staged["terminal_action"] == "click"
    assert staged["executed_terminal_action"] is False
    assert all(step != terminal for step in calls[0])
    committed = _call(op="guarded_commit", checkpoint=staged["checkpoint"])
    assert committed["executed_terminal_action"] is True
    assert committed["executed_click"] is True
    assert committed["executed_submit"] is False
    if "selector" in terminal and "text" not in terminal:
        assert calls[1][0]["selector_must_be_unique"] is True
    with pytest.raises(ValueError, match="already click_attempted"):
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


def test_canonical_target_preserves_query_identity_and_drops_fragment():
    senior = macros.canonical_target_url(
        "HTTPS://Example.COM/listing/?src=/roles/senior.md#application"
    )
    lead = macros.canonical_target_url(
        "https://example.com/listing?src=/roles/lead.md"
    )
    assert senior == "https://example.com/listing?src=/roles/senior.md"
    assert lead == "https://example.com/listing?src=/roles/lead.md"
    assert senior != lead


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


def test_guard_requires_matching_resource_sha256(tmp_path):
    resource = tmp_path / "resource.bin"
    resource.write_bytes(b"verified bytes")
    steps = macros.resolve(
        _guarded_steps(),
        None,
        {
            "target_url": "https://forms.example.com/requests/42",
            "resource_path": str(resource.resolve()),
        },
    )

    missing = _guard(resource)
    missing.pop("resource_sha256")
    with pytest.raises(ValueError, match="exactly 64 hexadecimal"):
        macros.validate_guard(missing, steps)

    with pytest.raises(ValueError, match="exactly 64 hexadecimal"):
        macros.validate_guard(_guard(resource, resource_sha256="not-a-sha256"), steps)

    with pytest.raises(ValueError, match="does not match"):
        macros.validate_guard(_guard(resource, resource_sha256="0" * 64), steps)

    expected = hashlib.sha256(resource.read_bytes()).hexdigest()
    checked = macros.validate_guard(_guard(resource, resource_sha256=expected.upper()), steps)
    assert checked["resource_sha256"] == expected


def test_guarded_checkpoint_persists_verified_resource_sha256(tmp_path):
    resource = tmp_path / "resource.bin"
    resource.write_bytes(b"immutable bytes")
    steps = macros.resolve(
        _guarded_steps(),
        None,
        {
            "target_url": "https://forms.example.com/requests/42",
            "resource_path": str(resource.resolve()),
        },
    )
    checked = macros.validate_guard(_guard(resource), steps)
    checkpoint = macros.reserve_checkpoint(checked, {"action": "submit"})
    reserved = macros.consume_checkpoint(checkpoint)

    assert reserved["resource_sha256"] == hashlib.sha256(resource.read_bytes()).hexdigest()


def test_failed_assertion_does_not_create_checkpoint(tmp_path, monkeypatch):
    resource = tmp_path / "resource.bin"
    resource.write_bytes(b"x")
    macros.save("request", _guarded_steps())

    async def fake_execute(actions, continue_on_error=False):
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


def test_every_guarded_refusal_says_which_file_is_refusing(tmp_path):
    """A one-time guard is permanent by design, so it has to be explainable.

    Each of these refusals is final: the token, the identity and the resource are
    burned for good, and a stage whose commit never ran - the browser died in
    between, the agent was killed - leaves a target that can never be staged
    again. The refusals used to end at "refusing replay", which named nothing a
    person could look at and nothing they could decide about. The ledger is one
    JSON file; saying which one turns a dead end into a deliberate choice.
    """
    resource = tmp_path / "resource.bin"
    resource.write_bytes(b"1")
    target = "https://forms.example.com/requests/42"
    steps = macros.resolve(
        _guarded_steps(), None, {"target_url": target, "resource_path": str(resource.resolve())}
    )
    guard = macros.validate_guard(
        _guard(
            resource.resolve(),
            target_url=target,
            canonical_url=target,
            idempotency_token="only-token-20260820",
        ),
        steps,
    )
    ledger = str(macros._guarded_ledger_path())
    checkpoint = macros.reserve_checkpoint(guard, {"action": "submit"})

    with pytest.raises(ValueError) as replay:
        macros.reserve_checkpoint(guard, {"action": "submit"})
    assert ledger in str(replay.value)

    assert macros.consume_checkpoint(checkpoint)["state"] == "submit_attempted"
    with pytest.raises(ValueError) as second_commit:
        macros.consume_checkpoint(checkpoint)
    assert ledger in str(second_commit.value)

    with pytest.raises(ValueError) as unknown:
        macros.consume_checkpoint("guard-never-staged")
    assert ledger in str(unknown.value)


def test_guarded_macro_accepts_one_terminal_submit_or_safe_click():
    _, submit = macros.split_terminal_action([{"action": "submit", "form_selector": "form"}])
    assert submit["action"] == "submit"
    _, click = macros.split_terminal_action([{"action": "click", "selector": "#confirm"}])
    assert click["selector_must_be_unique"] is True
    _, semantic = macros.split_terminal_action(
        [{"action": "click", "text": "Confirm", "role": "button", "exact": True}]
    )
    assert semantic["text"] == "Confirm"


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"action": "click", "text": "Confirm"}], "explicit role"),
        (
            [{"action": "click", "text": "Confirm", "role": "button", "exact": False}],
            "exact=true",
        ),
        ([{"action": "click", "x": 10, "y": 20}], "coordinate"),
        ([{"action": "click", "selector": "#confirm", "trusted": True}], "trusted=true"),
        ([{"action": "click", "selector": "ref:1:2"}], "plain CSS"),
        ([{"action": "click", "selector": "x-host >>> button"}], "plain CSS"),
        ([{"action": "submit"}, {"action": "click", "selector": "#confirm"}], "cannot contain"),
        ([{"action": "submit"}, {"action": "page_text"}], "terminal consequential"),
    ],
)
def test_guarded_macro_refuses_unsafe_or_nonterminal_consequential_actions(steps, message):
    with pytest.raises(ValueError, match=message):
        macros.split_terminal_action(steps)


def test_guarded_macro_requires_a_terminal_consequential_action():
    with pytest.raises(ValueError, match="exactly one"):
        macros.split_terminal_action([{"action": "page_text"}])


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


def test_a_project_macro_is_only_reachable_with_its_project_root(tmp_path):
    # A macro written into a project store and then asked for without that
    # project_root reads exactly like a macro that vanished, so the ops have to
    # find it with the path and miss it without one.
    project = tmp_path / "project"
    project.mkdir()
    macros.save("local", [{"action": "open", "url": "https://example.com"}], project_root=project)
    shown = _call(op="show", name="local", project_root=str(project))
    assert shown["steps"][0]["url"] == "https://example.com"
    assert shown["scope"] == "project"
    with pytest.raises(ValueError, match="does not exist"):
        _call(op="show", name="local")


def test_guard_checkpoint_is_available_only_in_its_project(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    guard = {
        "identity": "https://example.com/request/1",
        "resource_path": str((tmp_path / "resource.bin").resolve()),
        "resource_sha256": "0" * 64,
        "idempotency_token": "project-local-token-1",
    }
    checkpoint = macros.reserve_checkpoint(guard, {"action": "submit"}, project_a)
    with pytest.raises(ValueError, match="unknown guarded checkpoint"):
        macros.consume_checkpoint(checkpoint, project_b)
    assert macros.consume_checkpoint(checkpoint, project_a)["state"] == "submit_attempted"


def test_guarded_commit_accepts_a_legacy_staged_submit_checkpoint(monkeypatch):
    ledger = {
        "tokens": {
            "legacy-token-20260820": {
                "state": "staged",
                "checkpoint": "guard-legacy-token-20260820",
                "identity": "https://example.com/request/legacy",
                "resource_path": "C:/artifact.bin",
                "submit_step": {"action": "submit", "form_selector": "form"},
            }
        },
        "resources": {},
        "identities": {},
    }
    macros._write_guarded_ledger(ledger)

    async def fake_execute(actions, continue_on_error=False):
        assert actions == [{"action": "submit", "form_selector": "form"}]
        return {"success": True, "results": [], "failure_count": 0}

    monkeypatch.setattr(main, "_execute_actions", fake_execute)
    committed = _call(op="guarded_commit", checkpoint="guard-legacy-token-20260820")
    assert committed["terminal_action"] == "submit"
    assert committed["executed_submit"] is True


# --- project stores, hand-written files, and packs ---------------------------


def test_a_hand_written_step_list_is_a_macro():
    # The shortest honest thing to write by hand is the steps themselves, and a
    # model that has to wrap them in an object gets the wrapper wrong instead.
    (macros.macro_root() / "typed.json").write_text(
        '[{"action": "open", "url": "https://example.com"}]', encoding="utf-8"
    )
    record = macros.load("typed")
    assert record["name"] == "typed"
    assert record["step_count"] == 1
    assert [item["name"] for item in macros.list_macros()] == ["typed"]


def test_a_copied_file_is_the_macro_its_file_name_says():
    macros.save("original", [{"action": "close_all"}])
    copied = macros.macro_root() / "copy.json"
    copied.write_text((macros.macro_root() / "original.json").read_text(encoding="utf-8"), encoding="utf-8")
    # op=run reports what it ran, and reporting the name the file was copied
    # from would name a macro the caller never asked for.
    assert macros.load("copy")["name"] == "copy"


def test_the_store_explains_its_own_file_format(tmp_path):
    project = tmp_path / "documented"
    project.mkdir()
    notes = (macros.macro_root(project) / "README.md").read_text(encoding="utf-8")
    assert "op=run" in notes and "{{placeholder}}" in notes


def test_the_guarded_ledger_is_not_listed_as_a_macro(tmp_path):
    project = tmp_path / "ledgered"
    project.mkdir()
    macros.save("real", [{"action": "close_all"}], project_root=project)
    macros._write_guarded_ledger({"tokens": {}, "resources": {}, "identities": {}}, project)
    assert [item["name"] for item in macros.list_macros(project)] == ["real"]


def test_auto_project_root_takes_the_nearest_marker(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    package = repository / "packages" / "app"
    package.mkdir(parents=True)
    (package / ".web-search-neo").mkdir()
    monkeypatch.chdir(package)
    assert macros.discover_project_root() == package.resolve()
    assert macros.resolve_project_root("auto") == package.resolve()

    deeper = repository / "packages" / "other"
    deeper.mkdir()
    monkeypatch.chdir(deeper)
    assert macros.discover_project_root() == repository.resolve()


def test_the_environment_can_name_the_project_for_every_call(tmp_path, monkeypatch):
    project = tmp_path / "configured"
    project.mkdir()
    monkeypatch.setenv("WEB_SEARCH_NEO_PROJECT_ROOT", str(project))
    macros.save("from-env", [{"action": "close_all"}])
    assert (project / ".web-search-neo" / "macros" / "from-env.json").is_file()
    assert macros.store_info()["scope"] == "project"


def test_a_project_root_the_environment_cannot_find_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_NEO_PROJECT_ROOT", str(tmp_path / "gone"))
    with pytest.raises(ValueError, match="WEB_SEARCH_NEO_PROJECT_ROOT"):
        macros.macro_root()


def test_every_macro_answer_says_which_store_it_used(tmp_path):
    project = tmp_path / "reported"
    project.mkdir()
    macros.save("x", [{"action": "close_all"}], project_root=project)
    checked = _call(op="validate", name="x", project_root=str(project))
    assert checked["scope"] == "project"
    assert checked["project_root"] == str(project.resolve())
    assert checked["other_store"]["scope"] == "user"

    listed = _call(op="list")
    assert listed["scope"] == "user"
    assert listed["project_root"] is None
    assert listed["storage"] == str(macros.macro_root())
    assert listed["macros"] == []


def test_auto_reads_the_project_found_from_the_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "auto-project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.chdir(project)
    macros.save("local", [{"action": "close_all"}], project_root="auto")
    assert (project / ".web-search-neo" / "macros" / "local.json").is_file()
    listed = _call(op="list", project_root="auto")
    assert listed["scope"] == "project"
    assert [item["name"] for item in listed["macros"]] == ["local"]


def test_auto_that_finds_no_project_falls_back_to_the_user_store(monkeypatch):
    # "auto" is allowed to find nothing. What it must never do is derive a
    # project directory out of the user store's own path.
    monkeypatch.setattr(macros, "discover_project_root", lambda start=None: None)
    macros.save("loose", [{"action": "open", "url": "https://example.com"}], project_root="auto")
    listed = _call(op="list", project_root="auto")
    assert listed["scope"] == "user"
    assert listed["project_root"] is None
    assert macros.load("loose")["steps"][0]["url"] == "https://example.com"


def test_an_unknown_op_lists_the_ones_that_exist():
    with pytest.raises(ValueError) as excinfo:
        _call(op="teleport")
    message = str(excinfo.value)
    assert (
        "macro op must be list, show, validate, preview, run, guarded_stage or "
        "guarded_commit, not 'teleport'." in message
    )
    # The ops that went away were how a caller wrote a macro, so the refusal has
    # to say what replaced them rather than only listing what is left.
    assert "ordinary file operations" in message and "op='validate'" in message


def test_reading_a_project_that_has_no_macros_leaves_nothing_behind(tmp_path):
    # A model calling op=list to find out where it is must not thereby create a
    # directory in a repository that never asked for one.
    project = tmp_path / "untouched"
    project.mkdir()
    listed = _call(op="list", project_root=str(project))
    assert listed["macros"] == []
    assert listed["macro_count"] == 0
    assert not (project / ".web-search-neo").exists()

    macros.save("first", [{"action": "close_all"}], project_root=project)
    assert (project / ".web-search-neo" / "macros" / "first.json").is_file()
    assert _call(op="list", project_root=str(project))["macro_count"] == 1


def test_a_hand_written_file_declares_the_placeholders_it_uses():
    # save() declares what it recorded; a file written by hand has the
    # placeholders and no declaration, and a summary reporting "wants nothing"
    # is how a caller finds out what a macro needs from a failed run.
    (macros.macro_root() / "typed.json").write_text(
        '[{"action": "open", "url": "https://example.com/{{page}}"}]', encoding="utf-8"
    )
    assert sorted(macros.load("typed")["variables"]) == ["page"]
    assert [item["variables"] for item in macros.list_macros()] == [["page"]]

    shown = _call(op="show", name="typed")
    assert sorted(shown["variables"]) == ["page"]


def test_a_declared_default_still_wins_over_the_auto_declaration():
    macros.save("greet", [{"action": "open", "url": "{{site}}"}], variables={"site": "https://a.test"})
    assert macros.load("greet")["variables"]["site"] == "https://a.test"


def test_preview_checks_every_step_against_its_schema_without_dispatching():
    # The mistake in a hand-written macro is usually a wrong parameter name, and
    # without this it surfaces mid-replay, after earlier steps already ran.
    (macros.macro_root() / "typo.json").write_text(
        '[{"action": "wait", "selector": "#ready", "timeout_ms": 5000}]', encoding="utf-8"
    )
    previewed = _call(op="preview", name="typo")
    assert previewed["executed"] is False
    assert previewed["steps_valid"] is False
    assert previewed["problems"][0]["index"] == 0
    assert "timeout_ms" in previewed["problems"][0]["error"]
    assert "timeout_seconds" in previewed["problems"][0]["error"]

    (macros.macro_root() / "misnamed.json").write_text(
        '[{"action": "teleport", "url": "https://example.com"}]', encoding="utf-8"
    )
    assert "Unsupported action" in _call(op="preview", name="misnamed")["problems"][0]["error"]


def test_preview_of_a_sound_macro_says_so_and_carries_no_problems():
    macros.save("sound", [{"action": "open", "url": "{{url}}", "session_id": "s"}])
    previewed = _call(op="preview", name="sound", variables={"url": "https://example.com"})
    assert previewed["steps_valid"] is True
    assert "problems" not in previewed
    assert previewed["steps"][0]["url"] == "https://example.com"


# --- which session a step belongs to -----------------------------------------


def test_the_schema_default_is_attributed_rather_than_treated_as_sessionless():
    # exclude_unset keeps session_id out of a written step, but the action still
    # runs against the default tab, and calling that "no session" would let a
    # retarget or a validate reason about a tab that is not the one used.
    assert main._step_session(main._ACTIONS["open"].tool_name, {"url": "u"}) == "default"
    assert main._step_session(main._ACTIONS["search"].tool_name, {"query": "q"}) is None


# --- op='validate': checking a file without running it -----------------------


def _write_macro(name: str, payload) -> None:
    """Put an exact JSON body in the store, including bodies save() would not write."""
    (macros.macro_root() / f"{name}.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


def test_validate_refuses_a_placeholder_inside_a_run_script_script():
    """The expensive mistake this whole checker exists for.

    A `{{body}}` in a script is pasted in as raw text, so any value with a
    newline, a quote or a backslash produces broken JavaScript and the step dies
    with an opaque "Uncaught" from inside the page, halfway through a macro that
    has already clicked things. Passed through `args` the same value crosses as
    data, so that spelling must stay clean.
    """
    _write_macro(
        "scripted",
        {
            "steps": [
                {"action": "run_script", "script": "return '{{body}}'.length", "session_id": "s"}
            ],
            "variables": {"body": None},
        },
    )
    checked = _call(op="validate", name="scripted")
    assert checked["valid"] is False
    assert any("body" in item["error"] and "script" in item["error"] for item in checked["errors"])

    _write_macro(
        "argued",
        {
            "steps": [
                {
                    "action": "run_script",
                    "script": "return arguments[0].length",
                    "args": ["{{body}}"],
                    "session_id": "s",
                }
            ],
            "variables": {"body": None},
        },
    )
    passed = _call(op="validate", name="argued")
    assert passed["errors"] == []
    assert passed["valid"] is True


def test_validate_names_an_action_that_does_not_exist():
    """A misspelled action used to reach the dispatcher and fail on a live page,
    after the steps in front of it had already run."""
    _write_macro("misnamed", [{"action": "teleport", "url": "https://example.com"}])
    checked = main._validate_macro_file("misnamed")
    assert checked["valid"] is False
    assert "teleport" in checked["errors"][0]["error"]
    assert checked["errors"][0]["index"] == 0


def test_validate_reports_a_required_parameter_that_is_missing():
    """`open` without a url is refused at dispatch, so a macro carrying one is a
    file that cannot work; saying so costs nothing and saves a half-run replay."""
    _write_macro("bare-open", [{"action": "open", "session_id": "s"}])
    checked = main._validate_macro_file("bare-open")
    assert checked["valid"] is False
    assert any("url" in item["error"] and "missing" in item["error"] for item in checked["errors"])


def test_validate_reports_a_parameter_the_action_has_never_heard_of():
    """The usual hand-written mistake is a plausible wrong name - timeout_ms for
    timeout_seconds - which the schema refuses at dispatch and nowhere earlier."""
    _write_macro("typo", [{"action": "wait", "selector": "#ready", "timeout_ms": 5000}])
    checked = _call(op="validate", name="typo")
    assert checked["valid"] is False
    assert any("timeout_ms" in item["error"] for item in checked["errors"])


def test_validate_treats_an_undeclared_placeholder_as_an_error_only_when_variables_exist():
    """A file that declares `variables` is claiming to list what it wants, so a
    placeholder missing from that list is a broken claim. A file with no
    `variables` key never made the claim, and gets told to make it instead."""
    _write_macro(
        "half-declared",
        {
            "steps": [{"action": "open", "url": "{{url}}", "session_id": "s"}, {"action": "wait", "selector": "{{marker}}", "session_id": "s"}],
            "variables": {"url": None},
        },
    )
    declared = _call(op="validate", name="half-declared")
    assert declared["valid"] is False
    assert any("marker" in item["error"] for item in declared["errors"])

    _write_macro(
        "undeclared",
        [
            {"action": "open", "url": "{{url}}", "session_id": "s"},
            {"action": "wait", "selector": "#done", "session_id": "s"},
        ],
    )
    silent = _call(op="validate", name="undeclared")
    assert silent["valid"] is True
    assert silent["errors"] == []
    assert any("url" in item["error"] for item in silent["warnings"])
    assert silent["declared_variables"] == []
    assert silent["used_placeholders"] == ["url"]


def test_validate_warns_about_a_declared_variable_no_step_uses():
    """Usually the placeholder is spelled differently in the steps, which reads
    at run time as a variable the caller passed and the macro ignored."""
    _write_macro(
        "orphan",
        {
            "steps": [{"action": "wait", "selector": "#done", "session_id": "s"}],
            "variables": {"unused": None},
        },
    )
    checked = _call(op="validate", name="orphan")
    assert checked["valid"] is True
    assert any("unused" in item["error"] for item in checked["warnings"])


def test_validate_warns_when_the_steps_drive_two_different_sessions():
    """Two session ids in one macro is almost always a typo in one of them, and
    at run time it silently opens a second tab that nothing else touches."""
    _write_macro(
        "drifted",
        [
            {"action": "open", "url": "https://example.com", "session_id": "form"},
            {"action": "wait", "selector": "#done", "session_id": "from"},
        ],
    )
    checked = _call(op="validate", name="drifted")
    assert checked["valid"] is True
    assert any(
        "form" in item["error"] and "from" in item["error"] for item in checked["warnings"]
    )


def test_validate_warns_about_a_macro_that_never_reads_its_result_back():
    """A macro whose last act is a click or a submit reports whatever the
    dispatcher returned, so a submit the site rejected still comes back a
    success. Trailing housekeeping steps are not the macro's point and must not
    hide the check that is missing in front of them."""
    _write_macro(
        "blind",
        [
            {"action": "open", "url": "https://example.com", "session_id": "s"},
            {"action": "click", "selector": "#send", "session_id": "s"},
            {"action": "close_all"},
        ],
    )
    blind = _call(op="validate", name="blind")
    assert blind["valid"] is True
    assert any("click" in item["error"] for item in blind["warnings"])

    _write_macro(
        "checked",
        [
            {"action": "open", "url": "https://example.com", "session_id": "s"},
            {"action": "click", "selector": "#send", "session_id": "s"},
            {"action": "wait", "selector": "#sent", "session_id": "s"},
            {"action": "close_all"},
        ],
    )
    verified = _call(op="validate", name="checked")
    assert verified["warnings"] == []
    assert verified["valid"] is True


def test_validate_of_an_unreadable_file_names_the_path_it_read():
    """Two stores means the answer "your macro is wrong" is useless without the
    file it is about: the wrong store is the commonest reason for all three."""
    missing = _call(op="validate", name="absent")
    assert (missing["success"], missing["valid"]) == (False, False)
    assert "step_count" not in missing
    assert len(missing["errors"]) == 1
    assert str(macros.macro_file("absent")) in missing["errors"][0]["error"]

    _write_macro("broken", "{not json")
    unparsable = _call(op="validate", name="broken")
    assert (unparsable["success"], unparsable["valid"]) == (False, False)
    assert str(macros.macro_file("broken")) in unparsable["errors"][0]["error"]

    _write_macro("wrong-shape", {"name": "wrong-shape", "description": "no steps"})
    shapeless = _call(op="validate", name="wrong-shape")
    assert (shapeless["success"], shapeless["valid"]) == (False, False)
    assert str(macros.macro_file("wrong-shape")) in shapeless["errors"][0]["error"]


def test_validate_dispatches_nothing_even_for_a_macro_it_approves(monkeypatch):
    """The point of checking a macro is to learn what is wrong with it before a
    single step touches a page, so this op must never reach the dispatcher."""

    async def forbidden_execute(*args, **kwargs):
        raise AssertionError("validate must not dispatch browser actions")

    monkeypatch.setattr(main, "_execute_actions", forbidden_execute)
    macros.save("sound", [{"action": "wait", "selector": "#done", "session_id": "s"}])
    checked = _call(op="validate", name="sound")
    assert checked["executed"] is False
    assert checked["valid"] is True
    assert checked["step_count"] == 1
    assert checked["path"] == str(macros.macro_file("sound"))
    assert "Nothing was dispatched" in checked["note"]


# --- replaying a macro in another tab ---------------------------------------


def test_run_can_point_a_recorded_macro_at_another_session(monkeypatch):
    seen: list[dict] = []

    async def fake_execute(actions, continue_on_error=False):
        seen.append(actions)
        return {"success": True, "results": [], "failure_count": 0}

    monkeypatch.setattr(main, "_execute_actions", fake_execute)
    macros.save(
        "login",
        [
            {"action": "open", "url": "https://example.com", "session_id": "recorded"},
            {"action": "click", "selector": "#go", "session_id": "recorded"},
            {"action": "search", "query": "unchanged"},
        ],
    )
    outcome = _call(op="run", name="login", session_id="second-agent")
    assert outcome["session_override"] == "second-agent"
    assert [step.get("session_id") for step in seen[0]] == [
        "second-agent",
        "second-agent",
        None,
    ]


def test_retargeting_a_two_session_macro_is_refused_instead_of_collapsed():
    # Two sessions means two tabs on purpose; running them in one would quietly
    # change what the macro does.
    macros.save(
        "compare",
        [
            {"action": "open", "url": "https://a.example", "session_id": "left"},
            {"action": "open", "url": "https://b.example", "session_id": "right"},
        ],
    )
    with pytest.raises(ValueError, match="already use"):
        _call(op="run", name="compare", session_id="one-tab")


def test_preview_shows_the_retargeted_steps_before_they_run():
    macros.save("login", [{"action": "open", "url": "https://example.com", "session_id": "recorded"}])
    previewed = _call(op="preview", name="login", session_id="other")
    assert previewed["executed"] is False
    assert previewed["session_override"] == "other"
    assert previewed["steps"][0]["session_id"] == "other"
    assert previewed["steps_valid"] is True
