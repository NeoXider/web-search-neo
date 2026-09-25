"""test_run: a scenario of steps (an action plus expectations) and its pass/fail report.

A step is an ordinary ``web_action`` action object with two optional keys of
its own - ``step_name`` and ``expect`` - or an expect-only step. Every other key
belongs to the action: ``cookies`` and ``macro`` take a ``name`` of their own,
and a step must never swallow it (``cookies clear`` without its name clears the
whole domain). Expectations become
ordinary actions too (a ``wait`` with a selector or a condition), so a check
means exactly what the same wait would mean in a hand-written batch; the two
journal checks read the console and the network since the step began.

The whole plan is validated before anything runs - every action's arguments
against its published schema, every expectation, no nested test_run - and a
problem fails at once with the step's index, never halfway through a flow that
already submitted something.
"""
from __future__ import annotations

import json
from typing import Any

EXPECT_KEYS = {
    "selector": "CSS that must be visible (selector_state: present | visible | clickable)",
    "selector_state": "the state selector waits for (default visible)",
    "absent": "CSS that must not be in the page (waits until it is gone)",
    "text": "text (or list of texts) the page must show",
    "no_text": "text (or list of texts) the page must not show",
    "url_contains": "substring of the page URL",
    "title_contains": "substring of the document title",
    "script": "JS condition (run_script semantics) that must become truthy",
    "no_console_errors": "true: no console error/uncaught exception since the step began",
    "no_failed_requests": "true: no failed or 4xx/5xx request since the step began",
    "action_fails": "true: the step's action must fail (negative test)",
    "timeout_seconds": "how long each check may wait (default: the run's timeout_seconds)",
}
STEP_KEYS = ("step_name", "expect")
FORBIDDEN_IN_STEPS = frozenset({"test_run"})


def _texts(value: Any, key: str, index: int) -> list[str]:
    values = value if isinstance(value, list) else [value]
    if not values or not all(isinstance(item, str) and item for item in values):
        raise ValueError(f"step {index}: expect.{key} must be a non-empty string or a list of them")
    return values


def _validate_expect(expect: Any, index: int) -> dict[str, Any]:
    if expect is None:
        return {}
    if not isinstance(expect, dict):
        raise ValueError(f"step {index}: expect must be an object, e.g. {{\"selector\": \"#done\"}}")
    unknown = sorted(set(expect) - set(EXPECT_KEYS))
    if unknown:
        raise ValueError(f"step {index}: unknown expect key(s) {unknown}. Allowed: {sorted(EXPECT_KEYS)}")
    for key in ("text", "no_text"):
        if key in expect:
            _texts(expect[key], key, index)
    for key in ("selector", "absent", "url_contains", "title_contains", "script"):
        if key in expect and not (isinstance(expect[key], str) and expect[key].strip()):
            raise ValueError(f"step {index}: expect.{key} must be a non-empty string")
    if expect.get("selector_state", "visible") not in {"present", "visible", "clickable"}:
        raise ValueError(f"step {index}: expect.selector_state must be present, visible or clickable")
    for key in ("no_console_errors", "no_failed_requests", "action_fails"):
        if key in expect and not isinstance(expect[key], bool):
            raise ValueError(f"step {index}: expect.{key} must be true or false")
    timeout = expect.get("timeout_seconds", 1)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout != timeout:
        raise ValueError(f"step {index}: expect.timeout_seconds must be a number of seconds, e.g. 5")
    return dict(expect)


def plan(steps: Any, session_id: str, facade: Any, *, default_timeout: float = 5.0) -> list[dict[str, Any]]:
    """Validate every step up front and return the runnable plan."""
    if not isinstance(steps, list) or not steps or len(steps) > 100:
        raise ValueError("test_run needs steps: a list of 1-100 step objects")
    planned = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise ValueError(f"step {index}: must be an object (an action with optional name/expect)")
        body = {key: value for key, value in raw.items() if key not in STEP_KEYS}
        expect = _validate_expect(raw.get("expect"), index)
        if raw.get("step_name") is not None and not isinstance(raw["step_name"], str):
            raise ValueError(f"step {index}: step_name must be a string")
        action = None
        action_name = None
        if body:
            action_name = str(body.get("action", "")).strip().lower()
            action_name = facade._ACTION_NAME_ALIASES.get(action_name, action_name)
            if action_name in FORBIDDEN_IN_STEPS:
                raise ValueError(f"step {index}: test_run cannot contain another test_run")
            spec = facade._ACTIONS.get(action_name)
            if spec is None:
                raise ValueError(f"step {index}: " + facade._unsupported_action_error(action_name, body))
            fields = facade._argument_model(spec.tool_name).model_fields
            clash = [key for key in STEP_KEYS if key in fields]
            if clash:
                raise ValueError(f"step {index}: action '{action_name}' has its own {clash} parameter(s), "
                                 "which test_run reserves; wrap it in a macro")
            action = {**body, "action": action_name}
            if "session_id" not in action and "session_id" in fields:
                action["session_id"] = session_id
            arguments = {key: value for key, value in action.items() if key != "action"}
            if "summary" not in fields:
                arguments.pop("summary", None)  # the dispatcher's own per-step key
            facade._validate_arguments(spec.tool_name, f"step {index}: action '{action_name}'", arguments)
        elif not expect:
            raise ValueError(f"step {index}: a step needs an action, an expect, or both")
        if expect.get("action_fails") and action is None:
            raise ValueError(f"step {index}: expect.action_fails needs an action in the same step")
        timeout = float(expect.get("timeout_seconds", default_timeout))
        planned.append({
            "index": index, "name": str(raw.get("step_name") or action_name or "expect"),
            "action": action, "action_name": action_name, "expect": expect,
            "timeout": max(0.5, min(timeout, 120.0)),
            "expect_failure": bool(expect.get("action_fails")),
            "needs_console": bool(expect.get("no_console_errors")),
        })
    return planned


def _wait_script(session_id: str, key: str, script: str, timeout: float, shown: Any) -> dict[str, Any]:
    return {"key": key, "expected": shown,
            "action": {"action": "wait", "session_id": session_id, "script": script, "timeout_seconds": timeout}}


def checks_for(step: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    """The checks one step's expectations turn into, in a fixed order."""
    expect, timeout = step["expect"], step["timeout"]
    out: list[dict[str, Any]] = []
    if "selector" in expect:
        out.append({"key": "selector", "expected": expect["selector"], "action": {
            "action": "wait", "session_id": session_id, "selector": expect["selector"],
            "state": expect.get("selector_state", "visible"), "timeout_seconds": timeout}})
    if "absent" in expect:
        out.append(_wait_script(session_id, "absent", f"return !document.querySelector({json.dumps(expect['absent'])})",
                                timeout, expect["absent"]))
    body_text = "(document.body ? document.body.innerText : '')"
    for text in _texts(expect["text"], "text", step["index"]) if "text" in expect else []:
        out.append(_wait_script(session_id, "text", f"return {body_text}.includes({json.dumps(text)})", timeout, text))
    for text in _texts(expect["no_text"], "no_text", step["index"]) if "no_text" in expect else []:
        out.append(_wait_script(session_id, "no_text", f"return !{body_text}.includes({json.dumps(text)})",
                                timeout, text))
    if "url_contains" in expect:
        out.append(_wait_script(session_id, "url_contains",
                                f"return location.href.includes({json.dumps(expect['url_contains'])})",
                                timeout, expect["url_contains"]))
    if "title_contains" in expect:
        out.append(_wait_script(session_id, "title_contains",
                                f"return document.title.includes({json.dumps(expect['title_contains'])})",
                                timeout, expect["title_contains"]))
    if "script" in expect:
        out.append({"key": "script", "expected": expect["script"], "action": {
            "action": "wait", "session_id": session_id, "script": expect["script"], "timeout_seconds": timeout}})
    if expect.get("no_console_errors"):
        out.append({"key": "no_console_errors", "expected": True, "read": "console"})
    if expect.get("no_failed_requests"):
        out.append({"key": "no_failed_requests", "expected": True, "read": "network"})
    return out


def judged(check: dict[str, Any], passed: bool, detail: Any) -> dict[str, Any]:
    """One check's verdict, with the evidence when it failed."""
    item: dict[str, Any] = {"expect": check["key"], "expected": check.get("expected"), "passed": bool(passed)}
    if not passed and detail:
        item["detail"] = detail if isinstance(detail, list) else str(detail)[:400]
    return item


def action_verdict(step: dict[str, Any], succeeded: bool, error: Any) -> dict[str, Any]:
    """The action itself as a check: it must succeed, or fail when action_fails is expected."""
    if step["expect_failure"]:
        return {"expect": "action_fails", "expected": True, "passed": not succeeded,
                **({"detail": "the action succeeded"} if succeeded else {"observed_error": str(error or "")[:300]})}
    item: dict[str, Any] = {"expect": "action", "expected": step["action_name"], "passed": succeeded}
    if not succeeded:
        item["detail"] = str(error or "the action reported success=false")[:400]
    return item


def describe(check: dict[str, Any]) -> str:
    """A failed check in one sentence."""
    detail = check.get("detail")
    if isinstance(detail, list):
        detail = "; ".join(str(item) for item in detail[:3]) + (f" (+{len(detail) - 3} more)" if len(detail) > 3 else "")
    return f"expect {check['expect']}={check.get('expected')!r} failed" + (f": {detail}" if detail else "")


def summarize(records: list[dict[str, Any]], session_id: str, duration_ms: int, *, kept_open: bool) -> dict[str, Any]:
    """The run's verdict: counts, the failures up front, then every step."""
    passed = sum(1 for r in records if r["status"] == "passed")
    failed = [r for r in records if r["status"] == "failed"]
    skipped = sum(1 for r in records if r["status"] == "skipped")
    line = f"{passed}/{len(records)} steps passed"
    if failed:
        first = failed[0]
        line += f"; step {first['index']} '{first['name']}' failed: {first.get('error', '')}"[:400]
    if skipped:
        line += f"; {skipped} skipped after the failure"
    return {
        "success": not failed and skipped == 0, "passed": passed, "failed": len(failed), "skipped": skipped,
        "total": len(records), "duration_ms": duration_ms, "summary_line": line,
        "failed_steps": [{"index": r["index"], "name": r["name"], "error": r.get("error", "")} for r in failed],
        "steps": records, "session_id": session_id if kept_open else None,
    }
