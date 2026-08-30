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

import hashlib
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

# Where a project keeps its own macros. A project is recognised by a store it
# already has, and failing that by a repository root: both are directories the
# project owns for its own reasons, so discovery never invents a location.
PROJECT_STORE_DIR = ".web-search-neo"
_PROJECT_MARKERS = (PROJECT_STORE_DIR, ".git")
AUTO_PROJECT_ROOT = "auto"

_STORE_NOTES_NAME = "README.md"
_STORE_NOTES = """# Web Search Neo macros

Every `*.json` file here is one macro: a named list of `web_action` steps that
replays with `macro op=run name=<file name without .json>`.

The shortest useful file, `open-docs.json`, is the step list itself:

```json
[
  {"action": "open", "url": "{{url}}", "session_id": "docs"}
]
```

The full form:

```json
{
  "name": "open-docs",
  "description": "Open one documentation page",
  "steps": [{"action": "open", "url": "{{url}}", "session_id": "docs"}],
  "variables": {"url": "https://example.com"}
}
```

- `{{placeholder}}` marks what changes between runs. Pass `variables` on each
  run; an entry in `variables` is that placeholder's default. A placeholder that
  fills a whole string keeps the type of the value it is given, so a number in
  the file replays as a number. Never put a placeholder inside a `run_script`
  script: it is pasted in as raw text, and a value with a newline or a quote
  breaks the JavaScript. Pass it through `args` instead.
- Every step is exactly one `web_action` action object. Ask the server what each
  action accepts with
  `web_info(topic="action_schema", params={"action": "<name>"})`.
- A macro cannot run another macro. Run them one after another instead.
- These files are the whole API for writing a macro. Create, edit, rename, copy
  and delete them like any other file; there is no MCP action that writes one.
  `macro op=list` reports a file it cannot read as `broken` rather than hiding
  the ones beside it, and `macro op=validate name=<name>` checks one without
  running a single step.

`.guarded-macro-ledger.json` is not a macro. It records the one-time guarded
operations of this store; deleting it un-reserves every token it holds.
"""


def _sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 for *path* without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_project_root(start: str | os.PathLike[str] | None = None) -> Path | None:
    """Find the project a macro call belongs to without being told its path.

    A model that has to spell an absolute path on every macro call will
    eventually leave it out, and the project's macros then land silently in the
    per-user store, where the next call cannot find them. Discovery removes the
    chance to forget: ``WEB_SEARCH_NEO_PROJECT_ROOT`` is the deliberate answer
    when the client can set it, an existing macro store is the next best
    evidence, and a repository root covers a project that has not saved one yet.
    """
    configured = os.getenv("WEB_SEARCH_NEO_PROJECT_ROOT")
    if configured and configured.strip():
        candidate = Path(configured.strip()).expanduser()
        if not candidate.is_dir():
            raise ValueError(
                f"WEB_SEARCH_NEO_PROJECT_ROOT is set to '{configured}', which is not an "
                "existing directory. Correct it or unset it."
            )
        return candidate.resolve()
    try:
        here = Path(start).expanduser().resolve() if start is not None else Path.cwd().resolve()
    except OSError:
        return None
    # Nearest first: a package inside a repository that keeps its own macros is
    # its own project, and a marker further up must not reach past it. At one
    # level an existing store outranks a repository root, which is the order
    # _PROJECT_MARKERS is written in.
    for directory in [here, *here.parents]:
        for marker in _PROJECT_MARKERS:
            if (directory / marker).exists():
                return directory
    return None


def resolve_project_root(
    project_root: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Turn what a caller passed into the project directory to use, or ``None``.

    ``None`` means the per-user store unless the environment names a project, so
    a client configured once per project keeps working without every call
    repeating the path. ``"auto"`` asks for discovery explicitly.
    """
    if project_root is None:
        return discover_project_root() if os.getenv("WEB_SEARCH_NEO_PROJECT_ROOT") else None
    if isinstance(project_root, str):
        text = project_root.strip()
        if not text:
            return None
        if text.lower() == AUTO_PROJECT_ROOT:
            return discover_project_root()
        project_root = text
    project = Path(project_root).expanduser()
    if not project.is_absolute() or not project.is_dir():
        raise ValueError(
            "project_root must be an existing absolute directory, or the word "
            '"auto" to use the project found from the working directory.'
        )
    return project.resolve()


def _write_store_notes(root: Path) -> None:
    """Leave the file format beside the files, once, for whoever opens them next.

    These directories are meant to be edited by hand and committed with the
    project, so the reader is often a person or a model that has never seen the
    macro schema and no reason to go looking for it.
    """
    notes = root / _STORE_NOTES_NAME
    try:
        if not notes.exists():
            notes.write_text(_STORE_NOTES, encoding="utf-8")
    except OSError:
        # A read-only or otherwise unwritable store still replays macros; the
        # explanation is a convenience and must never fail the call.
        pass


def macro_root(
    project_root: str | os.PathLike[str] | None = None, create: bool = True
) -> Path:
    """The directory holding saved macros, created on first use.

    ``create=False`` resolves the same path without touching the filesystem, for
    the reads: listing a project's macros should not leave a directory behind in
    a repository that has never saved one.

    Macros outlive the server process on purpose: a click path learned once is
    worth keeping, and a model that has to re-record it after every restart will
    just stop using them.
    """
    project = resolve_project_root(project_root)
    if project is not None:
        container = project / PROJECT_STORE_DIR
        if container.exists():
            try:
                container.resolve().relative_to(project)
            except ValueError:
                raise ValueError("project macro storage escapes project_root; refusing it") from None
        elif create:
            container.mkdir()
        root = container / "macros"
        if root.exists():
            try:
                root.resolve().relative_to(project)
            except ValueError:
                raise ValueError("project macro storage escapes project_root; refusing it") from None
        elif create:
            root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve()
        try:
            resolved_root.relative_to(project)
        except ValueError:
            raise ValueError("project macro storage escapes project_root; refusing it") from None
        if create:
            _write_store_notes(resolved_root)
        return resolved_root
    configured = os.getenv("WEB_SEARCH_NEO_MACRO_ROOT")
    if configured:
        root = Path(configured).expanduser()
    elif os.getenv("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "WebSearchNeo" / "macros"
    else:
        root = Path.home() / PROJECT_STORE_DIR / "macros"
    if create:
        root.mkdir(parents=True, exist_ok=True)
        _write_store_notes(root)
    return root


def _macro_files(root: Path) -> list[Path]:
    """Every file in a store that is meant to be a macro.

    ``glob`` in pathlib matches leading dots, so the guarded-operation ledger
    sitting beside the macros would otherwise be listed as a macro of its own,
    with no steps and a name nobody saved.
    """
    return sorted(
        path
        for path in root.glob("*.json")
        if path.name != _GUARDED_LEDGER_NAME and path.is_file()
    )


def store_info(project_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Say which store a call is using, and where the other one is.

    The commonest way to lose a macro is not a bad file: it is saving into one
    store and looking for it in the other. Every macro answer carries this, so
    the question is answered before it has to be asked.
    """
    project = resolve_project_root(project_root)
    root = macro_root(project, create=False)
    info: dict[str, Any] = {
        "scope": "project" if project is not None else "user",
        "project_root": str(project) if project is not None else None,
        "storage": str(root),
        "macro_count": len(_macro_files(root)),
    }
    if project is not None and not os.getenv("WEB_SEARCH_NEO_PROJECT_ROOT"):
        user_root = macro_root(None, create=False)
        info["other_store"] = {
            "scope": "user",
            "storage": str(user_root),
            "macro_count": len(_macro_files(user_root)),
        }
    return info


def validate_name(name: str) -> str:
    if not name or not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "macro name must be 1-64 characters using letters, digits, '.', '_' or '-'"
        )
    return name


def _macro_path(
    name: str, project_root: str | os.PathLike[str] | None = None, create: bool = True
) -> Path:
    return macro_root(project_root, create) / f"{validate_name(name)}.json"


def macro_file(name: str, project_root: str | os.PathLike[str] | None = None) -> Path:
    """Where one macro lives, whether or not it is there yet.

    Public because a macro is a file the caller edits: a checker that could not
    name the path would be telling somebody their macro is wrong without saying
    which of two stores it read.
    """
    return _macro_path(name, project_root, create=False)


def raw_payload(name: str, project_root: str | os.PathLike[str] | None = None) -> Any:
    """The file's JSON exactly as written, with no defaults filled in.

    ``load`` normalises - it declares every placeholder it finds, so a variable
    the author forgot to declare comes back looking declared. A checker has to
    see what the author actually wrote, so it reads through this instead.
    """
    path = _macro_path(name, project_root, create=False)
    return json.loads(path.read_text(encoding="utf-8"))


def _guarded_ledger_path(project_root: str | os.PathLike[str] | None = None) -> Path:
    return macro_root(project_root) / _GUARDED_LEDGER_NAME


def canonical_target_url(value: str) -> str:
    """Return a stable HTTP(S) target identity while preserving meaningful query data."""
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("guard requires an absolute http(s) canonical target URL")
    host = parsed.hostname.lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    # Query parameters can be the requisition identity (for example one shared
    # resource path with identity carried by ``?src=...``). Core cannot know
    # which parameters are "tracking noise", so discarding any of them would
    # merge distinct targets. Callers must supply their already-canonical URL;
    # only the client-side fragment is excluded from the server target identity.
    return urlunsplit((parsed.scheme.lower(), host + port, path, parsed.query, ""))


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
    expected_sha256 = str(guard.get("resource_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("guard.resource_sha256 must be exactly 64 hexadecimal characters")
    actual_sha256 = _sha256_file(Path(resource_resolved))
    if actual_sha256 != expected_sha256:
        raise ValueError(
            "guard.resource_sha256 does not match the current guard.resource_path bytes"
        )
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
        "resource_sha256": actual_sha256,
        "idempotency_token": idempotency_token,
        "assertions": assertions,
        "allowed_hosts": sorted(allowed_hosts),
        "denied_hosts": sorted(denied_hosts),
    }


def _validate_terminal_click(step: dict[str, Any]) -> dict[str, Any]:
    """Return a fail-closed terminal click accepted by guarded dispatch."""
    checked = dict(step)
    if checked.get("trusted") is True:
        raise ValueError("guarded terminal click refuses trusted=true")
    if checked.get("x") is not None or checked.get("y") is not None:
        raise ValueError("guarded terminal click refuses coordinate targets")

    text = checked.get("text")
    selector = checked.get("selector")
    if text is not None:
        if not str(text).strip():
            raise ValueError("guarded terminal click text must not be empty")
        if checked.get("exact", True) is not True:
            raise ValueError("guarded terminal text click requires exact=true")
        if not str(checked.get("role") or "").strip():
            raise ValueError("guarded terminal text click requires an explicit role")
        # A selector is allowed here only as the semantic dispatcher's candidate
        # filter. Its exact text+role match still has to be unique at commit.
        checked.pop("selector_must_be_unique", None)
        return checked

    if not isinstance(selector, str) or not selector.strip():
        raise ValueError(
            "guarded terminal click requires one plain CSS selector or exact text plus role"
        )
    normalized_selector = selector.strip()
    if normalized_selector.startswith("ref:") or ">>>" in normalized_selector:
        raise ValueError(
            "guarded terminal selector click requires plain CSS; ref handles and "
            "piercing paths are not stable one-time targets"
        )
    checked["selector_must_be_unique"] = True
    return checked


def split_terminal_action(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Hold back exactly one terminal consequential ``submit`` or safe ``click``."""
    if not steps:
        raise ValueError("guarded macro requires a terminal submit or click action")
    terminal = dict(steps[-1])
    terminal_name = str(terminal.get("action") or "").strip().lower()
    submit_indexes = [index for index, step in enumerate(steps) if step.get("action") == "submit"]
    if terminal_name == "submit":
        if submit_indexes != [len(steps) - 1]:
            raise ValueError(
                "guarded macro requires exactly one terminal consequential action; "
                "submit must be the last step"
            )
    elif terminal_name == "click":
        if submit_indexes:
            raise ValueError(
                "guarded macro with terminal click cannot contain a submit action"
            )
        terminal = _validate_terminal_click(terminal)
    else:
        raise ValueError(
            "guarded macro requires exactly one terminal consequential action='submit' "
            "or action='click'"
        )
    return steps[:-1], terminal


def split_terminal_submit(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Backward-compatible name for :func:`split_terminal_action`."""
    return split_terminal_action(steps)


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
    terminal_step: dict[str, Any],
    project_root: str | os.PathLike[str] | None = None,
) -> str:
    """Reserve a unique target/resource/token tuple after all assertions pass."""
    with _GUARDED_LEDGER_LOCK:
        ledger = _load_guarded_ledger(project_root)
        # Every refusal below is permanent by design - that is what a one-time
        # guard is - and each one used to end the sentence there. It left a
        # caller whose commit never ran (the browser died between stage and
        # commit, say) with a target that could not be staged again, no way to
        # see why, and no way out that was written down anywhere. The ledger is
        # a file; saying which file turns a dead end into a decision somebody can
        # make deliberately.
        ledger_path = _guarded_ledger_path(project_root)
        token = guard["idempotency_token"]
        existing = ledger["tokens"].get(token)
        if existing:
            raise ValueError(
                f"idempotency token was already {existing.get('state', 'used')}; refusing "
                f"replay. This is recorded in {ledger_path}; a genuinely new attempt needs "
                "a new idempotency_token."
            )
        prior_token = ledger["identities"].get(guard["identity"])
        if prior_token:
            raise ValueError(
                "canonical target identity was already staged; refusing duplicate submit. "
                f"The earlier staging is recorded in {ledger_path}."
            )
        resource_key = os.path.normcase(guard["resource_path"])
        prior_identity = ledger["resources"].get(resource_key)
        if prior_identity and prior_identity != guard["identity"]:
            raise ValueError(
                "resource path was already reserved for another target identity "
                f"({prior_identity}); the reservation is recorded in {ledger_path}."
            )
        checkpoint = f"guard-{token}"
        ledger["resources"][resource_key] = guard["identity"]
        ledger["identities"][guard["identity"]] = token
        ledger["tokens"][token] = {
            "state": "staged",
            "checkpoint": checkpoint,
            "identity": guard["identity"],
            "resource_path": guard["resource_path"],
            "resource_sha256": guard["resource_sha256"],
            "terminal_action": terminal_step["action"],
            "terminal_step": terminal_step,
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
            raise ValueError(
                "unknown guarded checkpoint; run guarded_stage first. Staged "
                f"checkpoints are recorded in {_guarded_ledger_path(project_root)}."
            )
        if match.get("state") != "staged":
            raise ValueError(
                f"guarded checkpoint is already {match.get('state')}; "
                "refusing replay of the terminal action. Whether it took effect is a "
                "question for the site, not for this server; the record is in "
                f"{_guarded_ledger_path(project_root)}."
            )
        terminal_action = str(
            match.get("terminal_action")
            or (match.get("terminal_step") or match.get("submit_step") or {}).get("action")
            or "submit"
        )
        match["state"] = f"{terminal_action}_attempted"
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


def record_from_payload(payload: Any, name: str, source: Path) -> dict[str, Any]:
    """Return a checked macro record from whatever the file actually held.

    A macro file is meant to be written by hand as often as by the recorder, and
    the shortest honest thing to write is the step list itself. A bare list is
    therefore read as the steps of a macro named after its file, so the smallest
    useful macro is three lines of JSON instead of a wrapper object somebody has
    to remember the shape of.
    """
    if isinstance(payload, list):
        payload = {"steps": payload}
    if not isinstance(payload, dict) or "steps" not in payload:
        raise ValueError(
            f"macro '{name}' has no 'steps' key; the file is {source}. A macro file is "
            'either {"name": ..., "steps": [...]} or the bare step list.'
        )
    record = dict(payload)
    record["steps"] = validate_steps(record["steps"])
    variables = record.get("variables")
    # Declared from the steps, not only from the file. ``save`` writes the
    # declaration for what it recorded, but a file written by hand has the
    # placeholders and no declaration - and a summary that then reports "wants
    # nothing" is how a caller learns what a macro needs from a failed run.
    declared: dict[str, Any] = {found: None for found in sorted(placeholders_in(record["steps"]))}
    if isinstance(variables, dict):
        declared.update(variables)
    record["variables"] = declared
    # Recount rather than trust: these files are hand-edited, and a step_count
    # left behind by an edit misreports the macro everywhere it is shown.
    record["step_count"] = len(record["steps"])
    # The file name is the identity every caller uses. A "name" carried in from a
    # copied file would otherwise make op=run report a macro nobody asked for.
    record["name"] = name
    return record


def load(name: str, project_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read one macro, checking the shape a hand-edited file could have lost.

    These files are meant to be edited by hand - that is how a placeholder gets
    added to a recording - so a wrong shape is a normal mistake and has to read
    as one, rather than as a KeyError from somewhere deep in the replay.
    """
    path = _macro_path(name, project_root, create=False)
    if not path.exists():
        # Naming the store is the whole diagnosis. "Does not exist" plus an empty
        # list, said about a store the caller did not know they were reading, is
        # how a macro that is safely on disk gets recorded a second time.
        root = macro_root(project_root, create=False)
        scope = "project" if resolve_project_root(project_root) is not None else "user"
        raise ValueError(
            f"macro '{name}' does not exist in the {scope} store {root}. Saved there: "
            f"{[item['name'] for item in list_macros(project_root)]}. A macro saved with a "
            'project_root needs the same one to run it: pass that path, or "auto".'
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"macro '{name}' is not readable JSON: {exc}") from None
    return record_from_payload(payload, validate_name(name), path)


def list_macros(project_root: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Summarise every saved macro, cheaply enough to call before each run.

    A file that does not read as a macro is reported as broken rather than
    skipped or raised on: one hand-edited file should not hide the others, and a
    macro that quietly disappears from the list is worse than one that says why.
    """
    summaries: list[dict[str, Any]] = []
    for path in _macro_files(macro_root(project_root, create=False)):
        try:
            record = record_from_payload(
                json.loads(path.read_text(encoding="utf-8")), path.stem, path
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            summaries.append({"name": path.stem, "broken": f"{type(exc).__name__}: {exc}"})
            continue
        summaries.append(
            {
                "name": record["name"],
                "description": record.get("description") or "",
                "step_count": record["step_count"],
                "variables": sorted(record["variables"]),
                "saved_at": record.get("saved_at"),
            }
        )
    return summaries
