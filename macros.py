"""Named action scripts: record a task once, replay it with different values.

The point is the long tail of a real site. A repeated form can be opened, filled,
reviewed, and advanced with the changing parts left as ``{{placeholders}}``, so
the next run costs one call instead of a dozen and the model never re-derives the
click path. Consequential submits use the separate domain-neutral fail-closed
guard below; the generic replay remains useful for reversible browser work.

This module owns only what can be tested without a browser: where macros live,
what a well-formed one looks like, and how placeholders resolve. Dispatching the
steps belongs to the caller that already owns the action table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Deliberately the same shape as a session id: a macro name ends up in a file
# name, so anything that could walk out of the macro directory is refused here
# rather than sanitised into something the caller did not ask for.
_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# A recorded multi-step form can run long. web_action caps a hand-written batch at
# 32 because a model writing JSON by hand rarely means more; a macro is machine
# recorded, so the cap only has to stop a runaway file.
MAX_STEPS = 10000

_GUARDED_LEDGER_NAME = ".guarded-macro-ledger.json"
_GUARDED_LEDGER_LOCK = threading.Lock()


def macro_root(project_root: str | os.PathLike[str] | None = None) -> Path:
    """The directory holding saved macros, created on first use.

    Macros outlive the server process on purpose: a click path learned once is
    worth keeping, and a model that has to re-record it after every restart will
    just stop using them.
    """
    if project_root is not None:
        project = Path(project_root).expanduser()
        if not project.is_absolute() or not project.is_dir():
            raise ValueError("project_root must be an existing absolute directory")
        project = project.resolve()
        container = project / ".web-search-neo"
        if container.exists():
            try:
                container.resolve().relative_to(project)
            except ValueError:
                raise ValueError("project macro storage escapes project_root; refusing it") from None
        else:
            container.mkdir()
        root = container / "macros"
        if root.exists():
            try:
                root.resolve().relative_to(project)
            except ValueError:
                raise ValueError("project macro storage escapes project_root; refusing it") from None
        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve()
        try:
            resolved_root.relative_to(project)
        except ValueError:
            raise ValueError("project macro storage escapes project_root; refusing it") from None
        return resolved_root
    configured = os.getenv("WEB_SEARCH_NEO_MACRO_ROOT")
    if configured:
        root = Path(configured).expanduser()
    elif os.getenv("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "WebSearchNeo" / "macros"
    else:
        root = Path.home() / ".web-search-neo" / "macros"
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_name(name: str) -> str:
    if not name or not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "macro name must be 1-64 characters using letters, digits, '.', '_' or '-'"
        )
    return name


def _macro_path(name: str, project_root: str | os.PathLike[str] | None = None) -> Path:
    return macro_root(project_root) / f"{validate_name(name)}.json"


def _guarded_ledger_path(project_root: str | os.PathLike[str] | None = None) -> Path:
    return macro_root(project_root) / _GUARDED_LEDGER_NAME


def canonical_target_url(value: str) -> str:
    """Return a stable HTTP(S) target identity, without query noise or fragments."""
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("guard requires an absolute http(s) canonical target URL")
    host = parsed.hostname.lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host + port, path, "", ""))


def _host_is(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def validate_guard(guard: Any, resolved_steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the immutable target identity and resource for a guarded run.

    This deliberately validates data only. Live semantic assertions are checked
    against the staged browser results by ``evaluate_assertions``.
    """
    if not isinstance(guard, dict):
        raise ValueError("guarded_stage requires a guard object")
    canonical_url = canonical_target_url(str(guard.get("canonical_url") or ""))
    target_url = canonical_target_url(str(guard.get("target_url") or ""))
    if target_url != canonical_url:
        raise ValueError("guard target_url must equal canonical_url after normalization")
    host = urlsplit(canonical_url).hostname or ""
    allowed_hosts = {
        str(item).strip().lower().rstrip(".")
        for item in (guard.get("allowed_hosts") or [])
        if str(item).strip()
    }
    denied_hosts = {
        str(item).strip().lower().rstrip(".")
        for item in (guard.get("denied_hosts") or [])
        if str(item).strip()
    }
    denied = next((item for item in denied_hosts if _host_is(host, item)), None)
    if denied:
        raise ValueError(f"guard policy denies target host '{host}' via '{denied}'")
    if not allowed_hosts or not any(_host_is(host, item) for item in allowed_hosts):
        raise ValueError(
            f"guard.allowed_hosts must explicitly allow canonical host '{host}'"
        )
    opened = [
        canonical_target_url(str(step.get("url") or ""))
        for step in resolved_steps
        if step.get("action") == "open" and step.get("url")
    ]
    if canonical_url not in opened:
        raise ValueError("the resolved macro must open the exact canonical target URL")
    identity_key = str(guard.get("identity_key") or "").strip()
    identity = canonical_url + (f"#id={identity_key}" if identity_key else "")
    resource_path = Path(str(guard.get("resource_path") or "")).expanduser()
    if not resource_path.is_absolute() or not resource_path.is_file():
        raise ValueError("guard.resource_path must be an existing absolute file")
    resource_resolved = str(resource_path.resolve())
    uploaded_paths: list[str] = []
    for step in resolved_steps:
        if step.get("action") == "upload":
            uploaded_paths.extend(str(item) for item in (step.get("file_paths") or []))
        elif step.get("action") == "fill" and isinstance(step.get("files"), dict):
            uploaded_paths.extend(str(item) for item in step["files"].values())
    normalized_uploads = {
        os.path.normcase(str(Path(item).expanduser().resolve())) for item in uploaded_paths
    }
    if os.path.normcase(resource_resolved) not in normalized_uploads:
        raise ValueError("the resolved macro must upload the exact guard.resource_path")
    idempotency_token = str(guard.get("idempotency_token") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", idempotency_token):
        raise ValueError("guard.idempotency_token must be 16-128 stable safe characters")
    assertions = guard.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("guard.assertions must contain at least one live semantic assertion")
    return {
        "canonical_url": canonical_url,
        "target_url": target_url,
        "identity_key": identity_key,
        "identity": identity,
        "resource_path": resource_resolved,
        "idempotency_token": idempotency_token,
        "assertions": assertions,
        "allowed_hosts": sorted(allowed_hosts),
        "denied_hosts": sorted(denied_hosts),
    }


def split_terminal_submit(steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Require one explicit terminal submit, never a heuristic click-to-submit."""
    submit_indexes = [index for index, step in enumerate(steps) if step.get("action") == "submit"]
    if submit_indexes != [len(steps) - 1]:
        raise ValueError(
            "guarded macro requires exactly one explicit terminal action='submit'; "
            "submit-like click actions are not accepted"
        )
    return steps[:-1], dict(steps[-1])


def _result_value(result: Any, path: str) -> Any:
    current = result
    for part in path.split("."):
        if not part or not isinstance(current, dict) or part not in current:
            raise ValueError(f"assertion result path '{path}' does not exist")
        current = current[part]
    return current


def evaluate_assertions(outcome: dict[str, Any], assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate declarative assertions against fresh results from staged actions."""
    results = outcome.get("results") or []
    checked: list[dict[str, Any]] = []
    for number, assertion in enumerate(assertions):
        if not isinstance(assertion, dict):
            raise ValueError(f"guard assertion {number} must be an object")
        index = assertion.get("result_index")
        path = str(assertion.get("path") or "").strip()
        if not isinstance(index, int) or index < 0 or index >= len(results) or not path:
            raise ValueError(f"guard assertion {number} needs a valid result_index and path")
        actual = _result_value(results[index], path)
        if "equals" in assertion:
            passed = actual == assertion["equals"]
            rule = "equals"
            expected = assertion["equals"]
        elif "contains" in assertion:
            expected = str(assertion["contains"])
            passed = expected in str(actual)
            rule = "contains"
        else:
            raise ValueError(f"guard assertion {number} needs equals or contains")
        checked.append({"index": number, "passed": passed, "rule": rule, "expected": expected})
        if not passed:
            raise ValueError(
                f"guard assertion {number} failed: {path} {rule} {expected!r}, got {actual!r}"
            )
    return checked


def _load_guarded_ledger(project_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = _guarded_ledger_path(project_root)
    if not path.exists():
        return {"tokens": {}, "resources": {}, "identities": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"guarded macro ledger is unreadable; refusing submit: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("guarded macro ledger is invalid; refusing submit")
    data.setdefault("tokens", {})
    data.setdefault("resources", {})
    data.setdefault("identities", {})
    return data


def _write_guarded_ledger(
    data: dict[str, Any], project_root: str | os.PathLike[str] | None = None
) -> None:
    path = _guarded_ledger_path(project_root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def reserve_checkpoint(
    guard: dict[str, Any],
    submit_step: dict[str, Any],
    project_root: str | os.PathLike[str] | None = None,
) -> str:
    """Reserve a unique target/resource/token tuple after all assertions pass."""
    with _GUARDED_LEDGER_LOCK:
        ledger = _load_guarded_ledger(project_root)
        token = guard["idempotency_token"]
        existing = ledger["tokens"].get(token)
        if existing:
            raise ValueError(
                f"idempotency token was already {existing.get('state', 'used')}; refusing replay"
            )
        prior_token = ledger["identities"].get(guard["identity"])
        if prior_token:
            raise ValueError("canonical target identity was already staged; refusing duplicate submit")
        resource_key = os.path.normcase(guard["resource_path"])
        prior_identity = ledger["resources"].get(resource_key)
        if prior_identity and prior_identity != guard["identity"]:
            raise ValueError("resource path was already reserved for another target identity")
        checkpoint = f"guard-{token}"
        ledger["resources"][resource_key] = guard["identity"]
        ledger["identities"][guard["identity"]] = token
        ledger["tokens"][token] = {
            "state": "staged",
            "checkpoint": checkpoint,
            "identity": guard["identity"],
            "resource_path": guard["resource_path"],
            "submit_step": submit_step,
            "staged_at": time.time(),
        }
        _write_guarded_ledger(ledger, project_root)
    return checkpoint


def consume_checkpoint(
    checkpoint: str, project_root: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Mark a terminal submit attempted before dispatch, making retries fail closed."""
    with _GUARDED_LEDGER_LOCK:
        ledger = _load_guarded_ledger(project_root)
        match = next(
            (item for item in ledger["tokens"].values() if item.get("checkpoint") == checkpoint),
            None,
        )
        if not match:
            raise ValueError("unknown guarded checkpoint; run guarded_stage first")
        if match.get("state") != "staged":
            raise ValueError(f"guarded checkpoint is already {match.get('state')}; refusing replay submit")
        match["state"] = "submit_attempted"
        match["attempted_at"] = time.time()
        _write_guarded_ledger(ledger, project_root)
        return dict(match)


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    """Check the shape of a step list before it reaches the dispatcher.

    A macro that names a nested ``macro`` step is refused outright rather than
    depth-limited: the useful cases (a login prologue shared by two macros) are
    better served by running two macros in order, and the useless case is an
    accidental self-reference that would recurse until something else broke.
    """
    if not isinstance(steps, list) or not steps:
        raise ValueError("macro steps must be a non-empty list of action objects")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"macro steps must number at most {MAX_STEPS}, got {len(steps)}")
    checked: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"macro step {index} must be an object")
        action = str(step.get("action") or "").strip().lower()
        if not action:
            raise ValueError(f'macro step {index} needs an "action" key')
        if action == "macro":
            raise ValueError(
                f"macro step {index} runs a macro from inside a macro, which is "
                "refused. Run the macros one after another instead."
            )
        checked.append(dict(step))
    return checked


def placeholders_in(value: Any) -> set[str]:
    """Every ``{{name}}`` appearing anywhere in a nested structure."""
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        # Keys count too: a fill step's keys are its CSS selectors, which is
        # exactly the part a caller wants to vary between runs.
        if not value:
            return set()
        return set().union(
            *(placeholders_in(key) | placeholders_in(item) for key, item in value.items())
        )
    if isinstance(value, list):
        return set().union(*(placeholders_in(item) for item in value)) if value else set()
    return set()


def _substitute_value(value: Any, values: dict[str, Any]) -> Any:
    """Replace placeholders, keeping the native type when one fills a whole string.

    ``"{{count}}"`` becomes the number 3 rather than the string "3", so a recorded
    step whose parameter was an int stays valid when replayed; a placeholder
    embedded in a longer string is text either way and is formatted as such.
    """
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole is not None:
            return values[whole.group(1)]
        return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), value)
    if isinstance(value, dict):
        return {
            _substitute_value(key, values): _substitute_value(item, values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_substitute_value(item, values) for item in value]
    return value


def resolve(
    steps: list[dict[str, Any]],
    variables: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fill a saved macro's placeholders from its defaults and the caller's values.

    Missing names are reported together, with the ones that have defaults left
    out: a caller that forgot two of five variables should learn both in one
    answer instead of discovering them one failed replay at a time.
    """
    declared = dict(variables or {})
    supplied = dict(values or {})
    resolved = {name: default for name, default in declared.items() if default is not None}
    # A null passed for a variable is a value the caller does not have, not a
    # value of None: substituting it would send "None" into the page and fail
    # far from here, so it counts as missing and is reported with the rest.
    resolved.update({name: item for name, item in supplied.items() if item is not None})
    needed = placeholders_in(steps)
    missing = sorted(name for name in needed if name not in resolved)
    if missing:
        raise ValueError(
            f"macro needs value(s) for {missing}. "
            f"Pass them as variables, for example {{\"{missing[0]}\": \"...\"}}."
        )
    return [_substitute_value(step, resolved) for step in steps]


def save(
    name: str,
    steps: list[dict[str, Any]],
    description: str = "",
    variables: dict[str, Any] | None = None,
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Write one macro to disk, declaring every placeholder it turned out to use.

    Placeholders found in the steps are declared automatically so a recorded
    macro is self-describing: whoever runs it next is told what it wants without
    reading the steps. An explicit ``variables`` entry supplies a default and
    wins over the auto-declaration.
    """
    checked = validate_steps(steps)
    declared: dict[str, Any] = {found: None for found in sorted(placeholders_in(checked))}
    declared.update(variables or {})
    record = {
        "name": validate_name(name),
        "description": str(description or ""),
        "steps": checked,
        "variables": declared,
        "step_count": len(checked),
        "saved_at": time.time(),
    }
    _macro_path(name, project_root).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def load(name: str, project_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read one macro, checking the shape a hand-edited file could have lost.

    These files are meant to be edited by hand - that is how a placeholder gets
    added to a recording - so a wrong shape is a normal mistake and has to read
    as one, rather than as a KeyError from somewhere deep in the replay.
    """
    path = _macro_path(name, project_root)
    if not path.exists():
        raise ValueError(
            f"macro '{name}' does not exist. Saved macros: "
            f"{[item['name'] for item in list_macros(project_root)]}."
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"macro '{name}' is not readable JSON: {exc}") from None
    if not isinstance(record, dict) or "steps" not in record:
        raise ValueError(f"macro '{name}' has no 'steps' key; the file is {path}.")
    record["steps"] = validate_steps(record["steps"])
    variables = record.get("variables")
    record["variables"] = variables if isinstance(variables, dict) else {}
    # Recount rather than trust: these files are hand-edited, and a step_count
    # left behind by an edit misreports the macro everywhere it is shown.
    record["step_count"] = len(record["steps"])
    return record


def delete(name: str, project_root: str | os.PathLike[str] | None = None) -> bool:
    path = _macro_path(name, project_root)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_macros(project_root: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Summarise every saved macro, cheaply enough to call before each run.

    A file that is not readable JSON is reported as broken rather than skipped or
    raised on: one hand-edited macro should not hide the others.
    """
    summaries: list[dict[str, Any]] = []
    for path in sorted(macro_root(project_root).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            summaries.append({"name": path.stem, "broken": f"{type(exc).__name__}: {exc}"})
            continue
        # Valid JSON that is not an object is as broken as unreadable JSON here,
        # and reporting it as broken is what keeps one bad file from hiding the rest.
        if not isinstance(record, dict):
            summaries.append(
                {"name": path.stem, "broken": f"file holds {type(record).__name__}, not an object"}
            )
            continue
        steps = record.get("steps")
        summaries.append(
            {
                "name": record.get("name") or path.stem,
                "description": record.get("description") or "",
                # Counted from the steps themselves: a hand-edited file whose
                # step_count was not updated would otherwise report the old number.
                "step_count": len(steps) if isinstance(steps, list) else 0,
                "variables": sorted(record.get("variables") or {}),
                "saved_at": record.get("saved_at"),
            }
        )
    return summaries
