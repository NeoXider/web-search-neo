"""What a macro is before it runs: its structure, its inputs, and everything wrong with it.

Moved out of ``main.py`` (a size-ratcheted facade) with the validator, and grown the
script layer: control steps (``if``, ``repeat``, ``for_each``, ``wait_until``, ``try``,
``set``, ``sleep``, ``log``, ``fail``, ``return``, ``break``, ``continue``, ``call``,
``read``) and per-step keys (``when``, ``save_as``, ``on_error``, ``retry``,
``retry_delay``, ``label``, ``assert``, ``expect``). Every function takes the facade
module, so the action table checked here is the one that dispatches.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator

from web_search_neo import macro_expr, macro_files, macros
from web_search_neo.audit import scenario

CONTROL_FIELDS: dict[str, frozenset[str]] = {
    "if": frozenset({"test", "then", "else"}),
    "repeat": frozenset({"times", "while", "until", "as", "steps", "max_iterations", "delay_seconds",
                         "timeout_seconds"}),
    "for_each": frozenset({"items", "as", "index_as", "steps", "max_items", "delay_seconds"}),
    "wait_until": frozenset({"test", "steps", "timeout_seconds", "interval_seconds", "on_timeout"}),
    "try": frozenset({"steps", "catch", "finally"}),
    "set": frozenset({"values", "name", "value"}),
    "sleep": frozenset({"seconds"}),
    "log": frozenset({"message"}),
    "fail": frozenset({"message"}),
    "return": frozenset({"value"}),
    "break": frozenset(),
    "continue": frozenset(),
    "call": frozenset({"name", "variables", "project_root"}),
    "read": frozenset({"topic", "params"}),
}
CONTROL_ACTIONS = frozenset(CONTROL_FIELDS)
BODIES = {"if": ("then", "else"), "repeat": ("steps",), "for_each": ("steps",), "wait_until": ("steps",),
          "try": ("steps", "catch", "finally")}
STEP_KEYS = frozenset({"when", "save_as", "on_error", "retry", "retry_delay", "label", "assert", "expect"})
EXPRESSION_KEYS = {"if": ("test",), "repeat": ("while", "until"), "wait_until": ("test",)}
# test_run's page checks, minus the ones that read the console/network journal since the step began.
EXPECT_KEYS = frozenset(scenario.EXPECT_KEYS) - {"no_console_errors", "no_failed_requests", "action_fails"}
RESERVED_NAMES = frozenset({"last", "result", "value", "error"}) | frozenset(macro_expr.CONSTANTS)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

# A placeholder inside a script is the expensive mistake. A `{{body}}` would be pasted as raw text
# into JavaScript, so a value with a newline, a quote or a backslash breaks the program and the step
# fails with an opaque "Uncaught" from inside the page. Values belong in the script's `args`.
SCRIPT_FIELDS = ("script", "source")
# The steps that read something back; a macro whose last act is to send reports whatever the
# dispatcher returned and calls it done. Control steps decide on what they read, so they count.
VERIFYING_ACTIONS = frozenset({"wait", "wait_challenge", "run_script", "fetch_text", "fetch_links", "search",
                               "screenshot", "http_request"}) | CONTROL_ACTIONS
EPILOGUE_ACTIONS = frozenset({"close", "close_all", "release_inputs", "render"})


def action_of(step: Any) -> str:
    return str(step.get("action") or "").strip().lower() if isinstance(step, dict) else ""


def walk(steps: Any, prefix: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every step, nested bodies included, with its path: ``3``, ``3.then.0``, ``5.steps.2``."""
    if not isinstance(steps, list):
        return
    for index, step in enumerate(steps):
        path = f"{prefix}{index}"
        if not isinstance(step, dict):
            continue
        yield path, step
        for body in BODIES.get(action_of(step), ()):
            yield from walk(step.get(body), f"{path}.{body}.")


def uses_flow(steps: Any) -> bool:
    """True when a macro needs the script runner: a control step, a step key, or an expression template."""
    for _, step in walk(steps):
        if action_of(step) in CONTROL_ACTIONS or STEP_KEYS & set(step):
            return True
        if macro_expr.has_expression_template({key: value for key, value in step.items() if key != "action"}):
            return True
    return False


def produced_names(steps: Any) -> set[str]:
    """Names a run gives values to itself: save_as, set, loop variables, a catch's error, last."""
    names = {"last"}
    for _, step in walk(steps):
        action = action_of(step)
        if isinstance(step.get("save_as"), str):
            names.add(step["save_as"])
        if action == "set":
            names |= set((step.get("values") or {}) if isinstance(step.get("values"), dict) else {})
            if isinstance(step.get("name"), str):
                names.add(step["name"])
        if action == "for_each":
            names.add(str(step.get("as") or "item"))
            if step.get("index_as"):
                names.add(str(step["index_as"]))
        if action == "repeat" and step.get("as"):
            names.add(str(step["as"]))
        if action == "try" and step.get("catch"):
            names.add("error")
    return names


def read_names(steps: Any) -> set[str]:
    """Every variable a macro's templates and expressions read."""
    names: set[str] = set()
    for _, step in walk(steps):
        action = action_of(step)
        names |= macro_expr.names_in(step.get("when")) | macro_expr.names_in(step.get("assert"))
        names |= macro_expr.template_names(step.get("expect"))
        for key in EXPRESSION_KEYS.get(action, ()):
            names |= macro_expr.names_in(step.get(key))
        if action in ("repeat", "for_each"):
            for key in ("times", "items"):
                value = step.get(key)
                names |= macro_expr.names_in(value) if isinstance(value, str) and "{{" not in value else set()
        bodies = set(BODIES.get(action, ()))
        params = {key: value for key, value in step.items()
                  if key not in STEP_KEYS and key not in bodies and key not in EXPRESSION_KEYS.get(action, ())}
        names |= macro_expr.template_names(params)
    return names - {"result", "value"}


def inputs_needed(steps: Any) -> set[str]:
    """What the caller must supply (or a default must cover): read but never produced by the run."""
    return read_names(steps) - produced_names(steps)


def _index(path: str) -> int | str:
    """A top-level step keeps the plain number callers have always read; a nested one its path."""
    return int(path) if path.isdigit() else path


def _problem(path: str, action: str, error: str, fix: str) -> dict[str, Any]:
    return {"index": _index(path), "action": action or None, "error": error, "fix": fix}


def flow_problems(steps: Any) -> list[dict[str, Any]]:
    """Structural mistakes in the script layer, found without running anything."""
    found: list[dict[str, Any]] = []
    loop_depth: dict[str, int] = {}
    for path, step in walk(steps):
        action = action_of(step)
        inside_loop = any(path.startswith(prefix) for prefix in loop_depth)
        for key in ("when", "assert"):
            reason = macro_expr.check(step.get(key)) if key in step else None
            if reason:
                found.append(_problem(path, action, f"{key}: {reason}", "Write a Python-like expression, e.g. "
                                      "'upload.ok and len(tags) > 0'; see macro docs for the functions."))
        if "save_as" in step and (not isinstance(step["save_as"], str) or not _NAME.fullmatch(step["save_as"])
                                  or step["save_as"] in RESERVED_NAMES):
            found.append(_problem(path, action, f"save_as {step['save_as']!r} is not a usable variable name",
                                  f"Use letters, digits and _ (not {sorted(RESERVED_NAMES)})."))
        if step.get("on_error", "stop") not in ("stop", "continue"):
            found.append(_problem(path, action, "on_error must be 'stop' or 'continue'", "Pick one of the two."))
        retry = step.get("retry", 0)
        if isinstance(retry, bool) or not isinstance(retry, int) or not 0 <= retry <= 20:
            found.append(_problem(path, action, "retry must be a whole number 0-20", "e.g. \"retry\": 2"))
        if "expect" in step:
            expect = step["expect"]
            unknown = sorted(set(expect) - EXPECT_KEYS) if isinstance(expect, dict) else ["(not an object)"]
            if unknown:
                found.append(_problem(path, action, f"expect key(s) {unknown} are not available in macros",
                                      f"Use {sorted(EXPECT_KEYS)}; for anything else use assert."))
        if action not in CONTROL_ACTIONS:
            if action == "macro":
                found.append(_problem(path, action, "a step runs a macro through the 'macro' action",
                                      'Use {"action": "call", "name": "<macro>", "variables": {...}}.'))
            continue
        extra = sorted(set(step) - CONTROL_FIELDS[action] - STEP_KEYS - {"action"})
        if extra:
            found.append(_problem(path, action, f"unknown key(s) {extra} for '{action}'",
                                  f"'{action}' takes {sorted(CONTROL_FIELDS[action])} plus the step keys."))
        for key in EXPRESSION_KEYS.get(action, ()):
            reason = macro_expr.check(step.get(key)) if key in step else None
            if reason:
                found.append(_problem(path, action, f"{key}: {reason}", "Fix the expression."))
        for body in BODIES.get(action, ()):
            value = step.get(body)
            required = body in ("then", "steps") and action != "wait_until"
            if value is None and not required:
                continue
            if not isinstance(value, list) or (required and not value):
                found.append(_problem(path, action, f"'{body}' must be a {'non-empty ' if required else ''}list of steps",
                                      f'e.g. "{body}": [{{"action": "log", "message": "..."}}]'))
        if action == "if" and "test" not in step:
            found.append(_problem(path, action, "'if' needs a test expression", '"test": "upload.ok"'))
        if action == "wait_until" and "test" not in step:
            found.append(_problem(path, action, "'wait_until' needs a test expression", '"test": "net.status == 200"'))
        if action == "repeat" and not any(key in step for key in ("times", "while", "until", "max_iterations")):
            found.append(_problem(path, action, "'repeat' has no way to stop",
                                  "Give times, while, until or max_iterations (break also works with a cap)."))
        if action == "for_each" and "items" not in step:
            found.append(_problem(path, action, "'for_each' needs items", '"items": "{{files}}" or a list'))
        if action == "set" and not isinstance(step.get("values"), dict) and "name" not in step:
            found.append(_problem(path, action, "'set' needs values {name: value} or name + value",
                                  '"values": {"count": "{{count + 1}}"}'))
        if action == "set":
            for name in [*(step.get("values") or {}), *([step["name"]] if "name" in step else [])]:
                if not isinstance(name, str) or not _NAME.fullmatch(name) or name in RESERVED_NAMES:
                    found.append(_problem(path, action, f"'{name}' is not a usable variable name",
                                          "Use letters, digits and _."))
        if action == "call" and not isinstance(step.get("name"), str):
            found.append(_problem(path, action, "'call' needs the macro name", '"name": "youtube-publish"'))
        if action == "read" and not isinstance(step.get("topic"), str):
            found.append(_problem(path, action, "'read' needs a web_info topic", '"topic": "network"'))
        if action in ("break", "continue") and not inside_loop:
            found.append(_problem(path, action, f"'{action}' outside a repeat or for_each", "Move it into a loop."))
        if action in ("repeat", "for_each"):
            loop_depth[f"{path}.steps."] = 1
    return found


def _declared_variables(payload: Any) -> dict[str, Any] | None:
    """What the file itself declares, or None when it declares nothing at all."""
    if isinstance(payload, dict) and isinstance(payload.get("variables"), dict):
        return dict(payload["variables"])
    return None


def _action_problems(facade: Any, path: str, step: dict[str, Any]) -> list[dict[str, Any]]:
    """Name, parameters and script placeholders of one action step (its step keys left out)."""
    arguments = {key: value for key, value in step.items() if key not in STEP_KEYS}
    action_name = str(arguments.pop("action", "")).strip().lower()
    spec = facade._ACTIONS.get(action_name)
    if spec is None:
        return [{"index": _index(path), "action": action_name or None,
                 "error": facade._unsupported_action_error(action_name, arguments),
                 "fix": "Use a published action name, or a control step; web_info(topic='actions') lists the actions."}]
    problems: list[dict[str, Any]] = []
    required, optional = facade._parameter_names(spec.tool_name)
    known = set(required) | set(optional)
    schema_hint = "web_info(topic='action_schema', params={'action': '" + action_name + "'})"
    missing = [item for item in required if item not in arguments]
    if missing:
        problems.append({"index": _index(path), "action": action_name, "error": f"required parameter(s) {missing} are missing",
                         "fix": f"Add them to step {path}; {schema_hint} has the types."})
    unknown = sorted(key for key in arguments if key not in known)
    if unknown:
        problems.append({"index": _index(path), "action": action_name,
                         "error": f"parameter(s) {unknown} are not part of this action and would be refused at dispatch",
                         "fix": f"Remove them, or read {schema_hint} for the right names."})
    for field in SCRIPT_FIELDS:
        value = arguments.get(field)
        inside = (macros.placeholders_in(value) | macro_expr.template_names(value)) if isinstance(value, str) else set()
        if inside:
            problems.append({"index": _index(path), "action": action_name,
                             "error": (f"{sorted(inside)} are substituted as raw text into '{field}'. A value "
                                       "containing a newline, a quote or a backslash breaks the JavaScript, and "
                                       "the step fails with an opaque error from inside the page."),
                             "fix": "Pass the value through args instead, and read it inside the script from arguments[0]."})
    return problems


def validate_file(facade: Any, name: str, project_root: str | None = None) -> dict[str, Any]:
    """Check a macro file without running a single step of it.

    Errors are things that cannot work: a misspelled action, a required parameter left out, a
    placeholder nobody declared, a control step with no way to stop. Warnings usually mean a typo
    but might be deliberate, so they never make a macro invalid.
    """
    validated_name = macros.validate_name(name)
    path = macros.macro_file(validated_name, project_root)
    if not path.exists():
        return macro_files.unreadable(validated_name, path, f"No macro file at {path}.",
                                      "Check op='list' for the store in use, and the file name.")
    try:
        payload = macros.raw_payload(validated_name, project_root)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return macro_files.unreadable(validated_name, path, f"{path} is not readable JSON: {exc}",
                                      "Fix the JSON syntax: a macro file is a JSON object or a JSON array of steps.")
    except OSError as exc:
        return macro_files.unreadable(validated_name, path, f"{path} could not be read: {exc}",
                                      "Check that the file exists and can be read.")
    declared = _declared_variables(payload)
    try:
        record = macros.record_from_payload(payload, validated_name, path)
    except ValueError as exc:
        return macro_files.unreadable(validated_name, path, str(exc),
                                      'A macro file is {"name": ..., "steps": [...]} or the bare list of steps.')
    report = check_steps(facade, record["steps"], declared)
    return {"success": True, "macro": validated_name, "valid": not report["errors"], "path": str(path),
            **report, "executed": False,
            "note": "Nothing was dispatched. " + (
                "No errors; run op='preview' with the variables to see the final steps." if not report["errors"]
                else f"{len(report['errors'])} error(s) would fail this macro as written.")}


def check_steps(facade: Any, steps: list[dict[str, Any]], declared: dict[str, Any] | None) -> dict[str, Any]:
    """Errors and warnings of a step list (a file's, or op='run_steps' inline steps)."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    sessions: list[str] = []
    flow = uses_flow(steps)
    for path, step in walk(steps):
        if action_of(step) in CONTROL_ACTIONS:
            continue
        errors.extend(_action_problems(facade, path, step))
        session = step.get("session_id")
        if isinstance(session, str) and session.strip() and "{{" not in session:
            sessions.append(session.strip())
    errors.extend(flow_problems(steps))
    used = macros.placeholders_in(steps) | (inputs_needed(steps) if flow else set())
    produced = produced_names(steps) if flow else set()
    inputs = used - produced
    if declared is None:
        if inputs:
            warnings.append({"error": f"the file declares no 'variables', but its steps use {sorted(inputs)}",
                             "fix": 'Add "variables": {"<name>": null} for each, so op=show says what the macro '
                                    "wants before anyone runs it."})
    else:
        undeclared = sorted(inputs - set(declared))
        unused = sorted(set(declared) - used - produced)
        if undeclared:
            errors.append({"error": f"placeholder(s) {undeclared} are used but not declared in 'variables'",
                           "fix": "Declare each one, with a default value or null."})
        if unused:
            warnings.append({"error": f"variable(s) {unused} are declared but no step uses them",
                             "fix": "Remove them, or check how the placeholder is spelled in the steps."})
    distinct = sorted(set(sessions))
    if len(distinct) > 1:
        warnings.append({"error": f"the steps drive {len(distinct)} different sessions: {distinct}",
                         "fix": "Almost always a typo. Use one session_id throughout, or pass session_id to "
                                "op='run' to point the whole macro at one tab."})
    meaningful = [(index, step) for index, step in enumerate(steps) if action_of(step) not in EPILOGUE_ACTIONS]
    if meaningful:
        last_index, last_step = meaningful[-1]
        last = action_of(last_step)
        if last not in VERIFYING_ACTIONS and not {"assert", "expect", "save_as"} & set(last_step):
            warnings.append({"index": last_index, "error": f"the macro ends on '{last}' and never reads the result back",
                             "fix": "End with a wait on a success marker, a run_script that reads the page state, "
                                    "or give the step an expect/assert. A macro that sends something without "
                                    "checking reports success for a submit that failed."})
    return {"step_count": len(steps), "flow": flow,
            "declared_variables": sorted(declared) if declared is not None else [],
            "used_placeholders": sorted(used), "inputs": sorted(inputs), "produced": sorted(produced),
            "errors": errors, "warnings": warnings}


def outline(steps: Any) -> list[str]:
    """One line per step, indented by nesting: how a script reads at a glance."""
    lines: list[str] = []
    for path, step in walk(steps):
        depth = path.count(".") // 2
        action = action_of(step)
        detail = {key: value for key, value in step.items()
                  if key != "action" and key not in BODIES.get(action, ()) and key not in SCRIPT_FIELDS}
        text = json.dumps(detail, ensure_ascii=False, default=str)
        lines.append(f"{'  ' * depth}{path} {action} {text[:160] + ('...' if len(text) > 160 else '')}")
    return lines


def partial_render(value: Any, scope: dict[str, Any]) -> Any:
    """Fill the templates whose names are all known now; leave run-time ones (save_as, loops) as written."""
    if isinstance(value, str):
        if "{{" not in value:
            return value
        whole = re.fullmatch(r"\s*\{\{(.+?)\}\}\s*", value, re.S)
        if whole is not None and macro_expr.names_in(whole.group(1)) <= set(scope):
            try:
                return macro_expr.render(value, scope)  # one whole template keeps its type, as at run time
            except macro_expr.ExpressionError:
                return value

        def one(match: re.Match[str]) -> str:
            names = macro_expr.names_in(match.group(1))
            if names and names <= set(scope):
                try:
                    rendered = macro_expr.render(match.group(0), scope)
                except macro_expr.ExpressionError:
                    return match.group(0)
                return rendered if isinstance(rendered, str) else json.dumps(rendered, ensure_ascii=False, default=str)
            return match.group(0)

        return re.sub(r"\{\{(.+?)\}\}", one, value)
    if isinstance(value, dict):
        return {key: value_ if key in SCRIPT_FIELDS else partial_render(value_, scope) for key, value_ in value.items()}
    if isinstance(value, list):
        return [partial_render(item, scope) for item in value]
    return value
