"""Named action scripts: record a task once, replay it with different values.

The point is the long tail of a real site. Applying to one hh.ru vacancy is a
dozen actions - open the page, dismiss a banner, click "respond", pick a resume,
type a cover letter, submit - and the next vacancy is the same dozen with two
strings changed. A macro is that list saved under a name with its changing parts
left as ``{{placeholders}}``, so the second application costs one call instead of
twelve and the model never re-derives the click path.

This module owns only what can be tested without a browser: where macros live,
what a well-formed one looks like, and how placeholders resolve. Dispatching the
steps belongs to the caller that already owns the action table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from typing import Any

# Deliberately the same shape as a session id: a macro name ends up in a file
# name, so anything that could walk out of the macro directory is refused here
# rather than sanitised into something the caller did not ask for.
_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# A recorded application form runs long. web_action caps a hand-written batch at
# 32 because a model writing JSON by hand rarely means more; a macro is machine
# recorded, so the cap only has to stop a runaway file.
MAX_STEPS = 10000


def macro_root() -> Path:
    """The directory holding saved macros, created on first use.

    Macros outlive the server process on purpose: a click path learned once is
    worth keeping, and a model that has to re-record it after every restart will
    just stop using them.
    """
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


def _macro_path(name: str) -> Path:
    return macro_root() / f"{validate_name(name)}.json"


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
    _macro_path(name).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def load(name: str) -> dict[str, Any]:
    """Read one macro, checking the shape a hand-edited file could have lost.

    These files are meant to be edited by hand - that is how a placeholder gets
    added to a recording - so a wrong shape is a normal mistake and has to read
    as one, rather than as a KeyError from somewhere deep in the replay.
    """
    path = _macro_path(name)
    if not path.exists():
        raise ValueError(
            f"macro '{name}' does not exist. Saved macros: {[item['name'] for item in list_macros()]}."
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


def delete(name: str) -> bool:
    path = _macro_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_macros() -> list[dict[str, Any]]:
    """Summarise every saved macro, cheaply enough to call before each run.

    A file that is not readable JSON is reported as broken rather than skipped or
    raised on: one hand-edited macro should not hide the others.
    """
    summaries: list[dict[str, Any]] = []
    for path in sorted(macro_root().glob("*.json")):
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
