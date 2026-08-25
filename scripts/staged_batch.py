"""Run a small browser batch through explicit, verified stage barriers.

The runner is intentionally conservative.  It handles at most four items/tabs,
pauses an item when an unexpected form or questionnaire appears, and requires a
named CLI approval plus fresh pre/post verification around every terminal
action.  A terminal attempt is journalled *before* it is sent, so a timeout or
crash cannot silently cause a second submit on the next run.

See ``docs/staged-batch.md`` for the JSON format and examples.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MAX_ITEMS = 4
MAX_CONCURRENCY = 4
MAX_ACTIONS_PER_STAGE = 32
STATE_VERSION = 1

InfoCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
ActionCall = Callable[[list[dict[str, Any]], bool], Awaitable[dict[str, Any]]]

_SESSION_ACTIONS = {
    "open",
    "attach_tab",
    "show",
    "wait",
    "wait_challenge",
    "fill",
    "upload",
    "click",
    "click_text",
    "input",
    "press_keys",
    "pointer",
    "scroll",
    "touch",
    "touch_emulation",
    "pointer_lock",
    "render",
    "step",
    "release_inputs",
    "submit",
    "close",
}
_FORBIDDEN_BATCH_ACTIONS = {"open_many", "close_all"}
_OPENING_ACTIONS = {"open", "attach_tab"}
_AMBIGUOUS_INPUT_ACTIONS = {"click", "click_text", "input", "pointer", "press_keys", "touch"}
_IGNORED_FIELD_TYPES = {"hidden", "submit", "button", "reset", "image"}
_QUESTION_MARKERS = (
    "answer the questions",
    "additional questions",
    "application questions",
    "questionnaire",
    "ответьте на вопросы",
    "вопросы работодателя",
    "анкета работодателя",
    "опрос работодателя",
)
_TERMINAL_HINT = re.compile(
    r"submit|apply|send|confirm|purchase|checkout|delete|publish|approve|release|"
    r"отклик|отправ|подтверд|купить|удал|оплат",
    re.IGNORECASE,
)


class ConfigError(ValueError):
    """The workflow is unsafe or structurally invalid."""


class _StrictFormat(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise ConfigError(f"Unknown template variable {{{key}}}")


def render_template(value: Any, variables: Mapping[str, Any]) -> Any:
    """Recursively render ``{name}`` placeholders in JSON-compatible values."""
    if isinstance(value, str):
        try:
            return value.format_map(_StrictFormat(variables))
        except (ValueError, KeyError) as exc:
            raise ConfigError(f"Invalid template {value!r}: {exc}") from None
    if isinstance(value, list):
        return [render_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: render_template(item, variables) for key, item in value.items()}
    return value


def _gate_has_assertion(gate: Any) -> bool:
    if not isinstance(gate, dict):
        return False
    if "equals" in gate or "truthy" in gate:
        return True
    return any(bool(gate.get(key)) for key in ("contains_all", "contains_any", "not_contains"))


def _validate_string_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{label} must be a list of non-empty strings")


def _looks_terminal(action: Mapping[str, Any]) -> bool:
    name = str(action.get("action", "")).strip().lower()
    if action.get("terminal") is True or name == "submit":
        return True
    if name == "click_text":
        return bool(_TERMINAL_HINT.search(str(action.get("text", ""))))
    if name == "click":
        return bool(_TERMINAL_HINT.search(str(action.get("selector", ""))))
    return False


def validate_config(raw: Any) -> dict[str, Any]:
    """Validate and copy a workflow without touching browser state."""
    if not isinstance(raw, dict):
        raise ConfigError("Workflow root must be an object")
    config = deepcopy(raw)
    if config.get("version") != 1:
        raise ConfigError("Workflow version must be 1")
    if not isinstance(config.get("vars", {}), dict):
        raise ConfigError("vars must be an object")
    _validate_string_list(config.get("allowed_form_fields", []), "allowed_form_fields")
    _validate_string_list(config.get("question_markers", []), "question_markers")

    items = config.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ConfigError(f"items must contain 1-{MAX_ITEMS} entries")
    ids: set[str] = set()
    sessions: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"items[{index}] must be an object")
        item_id = str(item.get("id", "")).strip()
        session_id = str(item.get("session_id", "")).strip()
        if not item_id or not session_id:
            raise ConfigError(f"items[{index}] needs non-empty id and session_id")
        if item_id in ids:
            raise ConfigError(f"Duplicate item id: {item_id}")
        if session_id in sessions:
            raise ConfigError(f"Duplicate session_id: {session_id}")
        if not isinstance(item.get("vars", {}), dict):
            raise ConfigError(f"items[{index}].vars must be an object")
        ids.add(item_id)
        sessions.add(session_id)

    concurrency = config.get("max_concurrency", min(len(items), MAX_CONCURRENCY))
    if not isinstance(concurrency, int) or not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ConfigError(f"max_concurrency must be 1-{MAX_CONCURRENCY}")
    config["max_concurrency"] = concurrency

    stages = config.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ConfigError("stages must be a non-empty list")
    stage_names: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ConfigError(f"stages[{index}] must be an object")
        name = str(stage.get("name", "")).strip()
        if not name or name in stage_names:
            raise ConfigError(f"Stage names must be non-empty and unique: {name!r}")
        stage_names.add(name)
        actions = stage.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS_PER_STAGE:
            raise ConfigError(
                f"Stage {name!r} actions must contain 1-{MAX_ACTIONS_PER_STAGE} objects"
            )
        for action_index, action in enumerate(actions):
            if not isinstance(action, dict) or not str(action.get("action", "")).strip():
                raise ConfigError(f"Stage {name!r} action {action_index} needs action")
            if str(action["action"]).strip().lower() in _FORBIDDEN_BATCH_ACTIONS:
                raise ConfigError(
                    f"Stage {name!r} cannot use {action['action']!r}; it can escape the four-tab limit"
                )
            action_name = str(action["action"]).strip().lower()
            if "terminal" in action and not isinstance(action["terminal"], bool):
                raise ConfigError(
                    f"Stage {name!r} action {action_index} terminal must be true or false"
                )
            if (
                action_name in _AMBIGUOUS_INPUT_ACTIONS
                and "terminal" not in action
                and not stage.get("terminal")
            ):
                raise ConfigError(
                    f"Stage {name!r} action {action_index} ({action_name}) must explicitly set "
                    "terminal to true or false"
                )
        _validate_string_list(
            stage.get("allowed_form_fields", []),
            f"Stage {name!r} allowed_form_fields",
        )
        for gate_name in ("precheck", "verify"):
            gate = stage.get(gate_name)
            if gate is not None and not isinstance(gate, dict):
                raise ConfigError(f"Stage {name!r} {gate_name} must be an object")
        terminal = bool(stage.get("terminal")) or any(_looks_terminal(a) for a in actions)
        stage["terminal"] = terminal
        if terminal:
            if len(actions) != 1:
                raise ConfigError(f"Terminal stage {name!r} must contain exactly one action")
            if not _gate_has_assertion(stage.get("precheck")):
                raise ConfigError(f"Terminal stage {name!r} needs an asserted precheck gate")
            if not _gate_has_assertion(stage.get("verify")):
                raise ConfigError(f"Terminal stage {name!r} needs an asserted verify gate")
        settle = stage.get("settle_seconds", config.get("settle_seconds", 0.5))
        if not isinstance(settle, (int, float)) or not 0 <= float(settle) <= 10:
            raise ConfigError(f"Stage {name!r} settle_seconds must be between 0 and 10")
        stage["settle_seconds"] = float(settle)
    return config


def _variables(config: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    global_vars = config.get("vars", {})
    if not isinstance(global_vars, dict):
        raise ConfigError("vars must be an object")
    values.update(global_vars)
    values.update(item.get("vars", {}))
    values["item_id"] = item["id"]
    values["session_id"] = item["session_id"]
    return values


def _read_path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


async def evaluate_gate(
    gate: Mapping[str, Any],
    variables: Mapping[str, Any],
    info_call: InfoCall,
) -> tuple[bool, str]:
    rendered = render_template(dict(gate), variables)
    topic = str(rendered.get("topic", "")).strip()
    if not topic:
        raise ConfigError("Every gate needs topic")
    params = rendered.get("params", {})
    if not isinstance(params, dict):
        raise ConfigError("Gate params must be an object")
    params.setdefault("session_id", variables["session_id"])
    observed = await info_call(topic, params)
    try:
        value = _read_path(observed, str(rendered.get("path", "")))
    except KeyError:
        return False, f"missing path {rendered.get('path')!r}"

    case_sensitive = bool(rendered.get("case_sensitive", False))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    comparable = text if case_sensitive else text.casefold()

    if "equals" in rendered and value != rendered["equals"]:
        return False, f"expected equals {rendered['equals']!r}"
    if "truthy" in rendered and bool(value) is not bool(rendered["truthy"]):
        return False, f"expected truthy={bool(rendered['truthy'])}"

    def normalise(needle: Any) -> str:
        result = str(needle)
        return result if case_sensitive else result.casefold()

    contains_all = rendered.get("contains_all", [])
    contains_any = rendered.get("contains_any", [])
    not_contains = rendered.get("not_contains", [])
    if not all(isinstance(group, list) for group in (contains_all, contains_any, not_contains)):
        raise ConfigError("Gate contains_all/contains_any/not_contains must be lists")
    missing = [needle for needle in contains_all if normalise(needle) not in comparable]
    if missing:
        return False, f"missing required text: {missing}"
    if contains_any and not any(normalise(needle) in comparable for needle in contains_any):
        return False, f"none of required alternatives found: {contains_any}"
    forbidden = [needle for needle in not_contains if normalise(needle) in comparable]
    if forbidden:
        return False, f"forbidden text found: {forbidden}"
    return True, "matched"


async def safety_scan(
    config: Mapping[str, Any],
    stage: Mapping[str, Any],
    variables: Mapping[str, Any],
    info_call: InfoCall,
) -> tuple[bool, str]:
    """Refuse unexpected editable controls and common questionnaire markers."""
    elements = await info_call(
        "page_elements",
        {
            "session_id": variables["session_id"],
            "include_links": False,
            "include_forms": True,
            "include_buttons": False,
            "limit": 500,
        },
    )
    if isinstance(elements, dict):
        if elements.get("challenge_detected"):
            return False, "browser challenge detected"
        field_range = elements.get("range", {}).get("fields", {})
        collector = elements.get("collector_truncated", {})
        if field_range.get("next_offset") is not None or collector.get("fields"):
            return False, "form scan was truncated; cannot prove that every field is known"
    allowed_raw = list(config.get("allowed_form_fields", [])) + list(
        stage.get("allowed_form_fields", [])
    )
    allowed = set(render_template(allowed_raw, variables))
    unexpected = []
    live_control_count = 0
    for field in elements.get("fields", []) if isinstance(elements, dict) else []:
        if not isinstance(field, dict):
            continue
        if field.get("disabled"):
            continue
        field_type = str(field.get("type", "")).lower()
        if field_type in _IGNORED_FIELD_TYPES:
            continue
        # Custom radio/checkbox widgets commonly hide the native input under a
        # styled label. They are still live questions, not ignorable hidden DOM.
        if not field.get("visible", True) and field_type not in {"radio", "checkbox"}:
            continue
        live_control_count += 1
        selector = str(field.get("selector", ""))
        if selector not in allowed:
            unexpected.append(selector or field.get("label") or field.get("name") or "<field>")
    if unexpected:
        return False, f"unexpected visible form fields: {unexpected[:8]}"

    page = await info_call(
        "page_text",
        {"session_id": variables["session_id"], "mode": "main", "max_chars": 12000},
    )
    text = str(page.get("text", "") if isinstance(page, dict) else page).casefold()
    markers = list(_QUESTION_MARKERS) + list(config.get("question_markers", []))
    found = [marker for marker in markers if str(marker).casefold() in text]
    if found and live_control_count:
        return False, f"questionnaire marker found: {found[0]!r}"
    return True, "clear"


def _prepare_actions(
    stage: Mapping[str, Any], variables: Mapping[str, Any]
) -> list[dict[str, Any]]:
    actions = render_template(stage["actions"], variables)
    prepared: list[dict[str, Any]] = []
    for raw in actions:
        action = dict(raw)
        action.pop("terminal", None)
        name = str(action.get("action", "")).strip().lower()
        if name in _SESSION_ACTIONS:
            given = action.get("session_id")
            if given is not None and str(given) != str(variables["session_id"]):
                raise ConfigError(
                    f"Action {name!r} targets session {given!r}, not item session "
                    f"{variables['session_id']!r}"
                )
            action["session_id"] = variables["session_id"]
        prepared.append(action)
    return prepared


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "terminal_attempts": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read state journal {path}: {exc}") from None
    if state.get("version") != STATE_VERSION or not isinstance(
        state.get("terminal_attempts"), dict
    ):
        raise ConfigError(f"Unsupported state journal: {path}")
    return state


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _claim_path(state_path: Path, attempt_key: str) -> Path:
    digest = hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()
    return state_path.with_name(state_path.name + ".claims") / f"{digest}.json"


def _create_terminal_claim(state_path: Path, attempt_key: str) -> tuple[Path | None, str]:
    """Atomically claim an attempt across processes, not only asyncio tasks."""
    claim = _claim_path(state_path, attempt_key)
    claim.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": attempt_key,
        "status": "attempting",
        "at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    try:
        descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        try:
            existing = json.loads(claim.read_text(encoding="utf-8"))
            return None, str(existing.get("status", "attempted"))
        except (OSError, ValueError):
            return None, "attempted"
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return claim, "attempting"


def _update_terminal_claim(claim: Path, status: str) -> None:
    try:
        payload = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {"key": claim.stem}
    payload["status"] = status
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = claim.with_name(f"{claim.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(claim)


def _action_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        key: result.get(key)
        for key in ("success", "requested_count", "completed_count", "failure_count", "stopped_early")
        if key in result
    }
    errors = [
        entry.get("error")
        for entry in result.get("results", [])
        if isinstance(entry, dict) and entry.get("success") is False
    ]
    if errors:
        summary["errors"] = errors
    return summary


async def _run_item_stage(
    config: Mapping[str, Any],
    stage: Mapping[str, Any],
    item: Mapping[str, Any],
    approve_terminal: set[str],
    info_call: InfoCall,
    action_call: ActionCall,
    state: dict[str, Any],
    state_path: Path,
    state_lock: asyncio.Lock,
    is_first_stage: bool,
) -> dict[str, Any]:
    variables = _variables(config, item)
    name = str(stage["name"])
    actions = _prepare_actions(stage, variables)
    opens_page = any(str(action.get("action", "")).lower() in _OPENING_ACTIONS for action in actions)

    if not is_first_stage or not opens_page:
        clear, reason = await safety_scan(config, stage, variables, info_call)
        if not clear:
            return {"stage": name, "status": "paused_safety", "reason": reason}

    precheck = stage.get("precheck")
    precheck_report = None
    if precheck:
        matched, reason = await evaluate_gate(precheck, variables, info_call)
        if not matched:
            return {"stage": name, "status": "paused_precheck", "reason": reason}
        precheck_report = "matched"

    terminal = bool(stage.get("terminal"))
    attempt_key = f"{item['id']}:{name}"
    claim: Path | None = None
    if terminal:
        if name not in approve_terminal:
            return {
                "stage": name,
                "status": "paused_terminal_approval",
                "reason": f"rerun with --approve-terminal {name}",
                "precheck": precheck_report,
            }
        async with state_lock:
            previous = state["terminal_attempts"].get(attempt_key)
            if previous:
                return {
                    "stage": name,
                    "status": "paused_terminal_already_attempted",
                    "reason": f"journal says {previous.get('status', 'attempted')}; never retry automatically",
                }
            claim, claim_status = _create_terminal_claim(state_path, attempt_key)
            if claim is None:
                return {
                    "stage": name,
                    "status": "paused_terminal_already_attempted",
                    "reason": f"atomic claim says {claim_status}; never retry automatically",
                }
            state["terminal_attempts"][attempt_key] = {
                "status": "attempting",
                "at": datetime.now(timezone.utc).isoformat(),
            }
            _write_state(state_path, state)

    result = await action_call(actions, False)
    summary = _action_summary(result)
    if result.get("success") is not True:
        if terminal:
            async with state_lock:
                state["terminal_attempts"][attempt_key]["status"] = "action_failed"
                _write_state(state_path, state)
                _update_terminal_claim(claim, "action_failed")  # type: ignore[arg-type]
        return {"stage": name, "status": "failed_action", "action": summary}

    verify = stage.get("verify")
    if verify:
        matched, reason = await evaluate_gate(verify, variables, info_call)
        if not matched:
            if terminal:
                async with state_lock:
                    state["terminal_attempts"][attempt_key]["status"] = "unverified"
                    _write_state(state_path, state)
                    _update_terminal_claim(claim, "unverified")  # type: ignore[arg-type]
            return {
                "stage": name,
                "status": "paused_verification",
                "reason": reason,
                "action": summary,
            }

    closed = any(str(action.get("action", "")).lower() == "close" for action in actions)
    if not closed:
        clear, reason = await safety_scan(config, stage, variables, info_call)
        if not clear and not terminal:
            return {
                "stage": name,
                "status": "paused_safety",
                "reason": reason,
                "action": summary,
            }

    if terminal:
        async with state_lock:
            state["terminal_attempts"][attempt_key]["status"] = "verified"
            _write_state(state_path, state)
            _update_terminal_claim(claim, "verified")  # type: ignore[arg-type]
    return {"stage": name, "status": "success", "action": summary}


async def run_workflow(
    raw_config: Mapping[str, Any],
    state_path: Path,
    approve_terminal: set[str] | None = None,
    *,
    info_call: InfoCall | None = None,
    action_call: ActionCall | None = None,
) -> dict[str, Any]:
    """Run stage-by-stage, with a barrier between stages and up to four items."""
    config = validate_config(raw_config)
    approvals = set(approve_terminal or set())
    known_stages = {stage["name"] for stage in config["stages"]}
    unknown_approvals = approvals - known_stages
    if unknown_approvals:
        raise ConfigError(f"Unknown terminal stage approvals: {sorted(unknown_approvals)}")

    if info_call is None or action_call is None:
        import main as neo

        info_call = info_call or (lambda topic, params: neo.web_info(topic, params))
        action_call = action_call or (
            lambda actions, continue_on_error: neo.web_action(actions, continue_on_error)
        )

    state = _load_state(state_path)
    state_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(config["max_concurrency"])
    item_reports = {
        item["id"]: {"id": item["id"], "session_id": item["session_id"], "status": "active", "stages": []}
        for item in config["items"]
    }

    async def limited(stage: Mapping[str, Any], item: Mapping[str, Any], first: bool) -> dict[str, Any]:
        async with semaphore:
            return await _run_item_stage(
                config,
                stage,
                item,
                approvals,
                info_call,  # type: ignore[arg-type]
                action_call,  # type: ignore[arg-type]
                state,
                state_path,
                state_lock,
                first,
            )

    for stage_index, stage in enumerate(config["stages"]):
        active = [item for item in config["items"] if item_reports[item["id"]]["status"] == "active"]
        if not active:
            break
        results = await asyncio.gather(
            *(limited(stage, item, stage_index == 0) for item in active),
            return_exceptions=True,
        )
        for item, result in zip(active, results):
            report = item_reports[item["id"]]
            if isinstance(result, Exception):
                report["stages"].append(
                    {"stage": stage["name"], "status": "failed_exception", "reason": f"{type(result).__name__}: {result}"}
                )
                report["status"] = "failed"
                continue
            report["stages"].append(result)
            if result["status"].startswith("paused_"):
                report["status"] = "paused"
            elif result["status"].startswith("failed_"):
                report["status"] = "failed"
        await asyncio.sleep(stage["settle_seconds"])

    for report in item_reports.values():
        if report["status"] == "active":
            report["status"] = "completed"
    completed = sum(report["status"] == "completed" for report in item_reports.values())
    paused = sum(report["status"] == "paused" for report in item_reports.values())
    failed = sum(report["status"] == "failed" for report in item_reports.values())
    return {
        "success": failed == 0 and paused == 0,
        "completed": completed,
        "paused": paused,
        "failed": failed,
        "state_path": str(state_path),
        "items": list(item_reports.values()),
    }


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path, help="Path to a staged workflow JSON file")
    parser.add_argument(
        "--state",
        type=Path,
        help="Terminal-attempt journal (default: <workflow>.state.json)",
    )
    parser.add_argument(
        "--approve-terminal",
        action="append",
        default=[],
        metavar="STAGE",
        help="Approve one named terminal stage after reviewing its precheck (repeatable)",
    )
    parser.add_argument("--check", action="store_true", help="Validate JSON without browser actions")
    return parser.parse_args()


def cli() -> int:
    arguments = _parse_arguments()
    try:
        raw = json.loads(arguments.workflow.read_text(encoding="utf-8"))
        validated = validate_config(raw)
        if arguments.check:
            print(json.dumps({"valid": True, "items": len(validated["items"]), "stages": len(validated["stages"])}, indent=2))
            return 0
        state_path = arguments.state or arguments.workflow.with_name(arguments.workflow.name + ".state.json")
        report = asyncio.run(
            run_workflow(raw, state_path, set(arguments.approve_terminal))
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["failed"]:
            return 1
        if report["paused"]:
            return 3
        return 0
    except (OSError, ValueError, ConfigError) as exc:
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
