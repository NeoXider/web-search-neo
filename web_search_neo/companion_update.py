"""Bring this checkout up to date, and the Chrome companion with it.

Two halves live here, deliberately apart. The first is the repository: a fast-forward
pull of the branch this package is installed from, and a reinstall only when the pull
touched what a reinstall is for. It runs commands through an injected callable, so the
decisions - refuse a dirty tree, refuse a diverged branch, name what changed - are
testable without touching a machine's git.

The second half is Chrome. An unpacked extension cannot update itself: a service worker
has no filesystem and no process access, and ``runtime.reload`` only re-reads the folder
it was loaded from. So the update has to be done by the side that *can* run git - this
server - and the companion then reloads onto those files. That is why the button in the
popup and this action exist: Chrome's Reload is a human gesture, and the pull is not
something an extension is allowed to do.

What it refuses is the point of the module. A tree with uncommitted work is not pulled,
a branch that diverged from its upstream is not forced, and nothing is reloaded while an
agent is driving a tab. Every refusal names itself and returns the steps a person would
take instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

# A pull on a healthy checkout answers in under a second; 120 s is the ceiling for a
# machine whose CPU is busy with other work, not an invitation to hang.
COMMAND_TIMEOUT_SECONDS = 120.0
REINSTALL_ENV = "WEB_SEARCH_NEO_UPDATE_REINSTALL"

# Files whose change means the *installed* package is stale, not just the checkout.
INSTALL_SPEC_FILES = {"pyproject.toml", "setup.py", "setup.cfg"}
LISTED_PATH_LIMIT = 12

CommandRunner = Callable[[list[str], Path], tuple[int, str]]


def run_command(command: list[str], cwd: Path) -> tuple[int, str]:
    """Run one command in ``cwd`` and return ``(exit code, output)``."""
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(command[:2])} did not answer within {COMMAND_TIMEOUT_SECONDS:.0f}s"
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def package_checkout(package_file: str | Path) -> Path | None:
    """The git checkout this package's own code lives in, or ``None``.

    An editable install points at the checkout, which is the case this module exists
    for. A wheel installed into site-packages has no branch to pull and no folder
    Chrome loaded, and saying so is better than a pull that changes nothing.
    """
    own = Path(package_file).resolve()
    candidate = own.parent.parent if own.name == "__init__.py" else own.parent
    if not (candidate / "web_search_neo").is_dir():
        return None
    probe = run_command(["git", "rev-parse", "--show-toplevel"], candidate)
    if probe[0] != 0:
        return None
    top = (probe[1] or "").strip().splitlines()
    if not top:
        return None
    root = Path(top[0])
    if not (root / "chrome-extension").is_dir():
        return None
    return root


def _brief(output: str, limit: int = 400) -> str:
    text = " ".join(line.strip() for line in (output or "").splitlines() if line.strip())
    return text[:limit]


def _changed_paths(output: str) -> list[str]:
    return [line.strip() for line in (output or "").splitlines() if line.strip()]


def _install_spec_touched(paths: list[str]) -> bool:
    for path in paths:
        name = Path(path).name
        if name in INSTALL_SPEC_FILES or name.startswith("requirements"):
            return True
    return False


def update_checkout(
    root: Path, *, runner: CommandRunner = run_command, python_executable: str | None = None
) -> dict[str, Any]:
    """Fast-forward this checkout. Refuses rather than touching work in progress."""
    status_code, status_out = runner(["git", "status", "--porcelain"], root)
    if status_code != 0:
        return {"updated": False, "refused": "git_status_failed", "reason": _brief(status_out)}
    dirty = [line for line in status_out.splitlines() if line.strip() and not line.startswith("??")]
    if dirty:
        listed = [line[3:].strip() for line in dirty[:LISTED_PATH_LIMIT]]
        return {
            "updated": False,
            "refused": "uncommitted_work",
            "uncommitted_paths": listed,
            "uncommitted_count": len(dirty),
            "reason": (
                f"{len(dirty)} tracked file(s) here have uncommitted changes, so pulling would "
                "mix new code into work in progress. Commit or stash them first."
            ),
        }

    head_code, head_out = runner(["git", "rev-parse", "HEAD"], root)
    branch_code, branch_out = runner(["git", "rev-parse", "--abbrev-ref", "@{u}"], root)
    if branch_code != 0:
        return {
            "updated": False,
            "refused": "no_upstream",
            "reason": (
                "This branch has no upstream to pull from, so there is nothing this action "
                "can update; the checkout stays as it is."
            ),
        }
    branch = (branch_out or "").strip().splitlines()[0] if branch_out.strip() else ""

    fetch_code, fetch_out = runner(["git", "fetch", "--prune"], root)
    if fetch_code != 0:
        return {"updated": False, "refused": "fetch_failed", "reason": _brief(fetch_out)}

    behind_code, behind_out = runner(["git", "rev-list", "--count", "HEAD..@{u}"], root)
    behind = int((behind_out or "0").strip() or 0) if behind_code == 0 else 0
    if behind_code != 0:
        return {"updated": False, "refused": "ahead_of_upstream", "reason": _brief(behind_out)}
    if behind == 0:
        return {
            "updated": False,
            "already_current": True,
            "branch": branch,
            "commit": (head_out or "").strip()[:12],
        }

    before = (head_out or "").strip()[:12]
    pull_code, pull_out = runner(["git", "pull", "--ff-only"], root)
    if pull_code != 0:
        return {
            "updated": False,
            "refused": "not_fast_forward",
            "branch": branch,
            "behind": behind,
            "reason": (
                f"{behind} commit(s) behind, and the pull was not a fast-forward, so this "
                f"branch has diverged from {branch}. Nothing was merged or rebased: "
                f"{_brief(pull_out, 240)}"
            ),
        }

    after_code, after_out = runner(["git", "rev-parse", "HEAD"], root)
    after = (after_out or "").strip()[:12]
    changed_code, changed_out = runner(
        ["git", "diff", "--name-only", f"{before}..{after}"], root
    )
    paths = _changed_paths(changed_out) if changed_code == 0 else []

    reinstall_requested = str(sys.executable if python_executable is None else python_executable)
    reinstall = False
    reinstall_note = ""
    if _install_spec_touched(paths):
        if str(os.environ.get(REINSTALL_ENV, "1")).strip().lower() in {"0", "false", "off", "no"}:
            reinstall_note = (
                f"{REINSTALL_ENV}=0: the pull changed an install spec, so the installed package "
                "was left as it is and this server may need a manual reinstall."
            )
        else:
            code, output = runner([reinstall_requested, "-m", "pip", "install", "-e", "."], root)
            reinstall = code == 0
            if not reinstall:
                reinstall_note = (
                    f"The pull changed an install spec and the reinstall failed ({code}): "
                    f"{_brief(output, 240)}"
                )

    return {
        "updated": True,
        "branch": branch,
        "from_commit": before,
        "to_commit": after,
        "files_changed": len(paths),
        "changed_paths": paths[:LISTED_PATH_LIMIT],
        "reinstalled": reinstall,
        "reinstall_note": reinstall_note,
        "python_code_changed": any(path.startswith("web_search_neo/") for path in paths),
    }


def update_companion(
    *,
    runner: CommandRunner = run_command,
    package_file: str | Path | None = None,
    bridge_getter: Callable[[], Any] | None = None,
    local_sessions: int = 0,
) -> dict[str, Any]:
    """Pull this checkout, reinstall when that is what changed, reload the companion.

    Nothing is reloaded while an agent drives a tab: a companion reload detaches every
    session attached to it, which is worse for the caller than staying one version behind.
    """
    own = str(package_file or Path(__file__).resolve().with_name("__init__.py"))
    root = package_checkout(own)
    if root is None:
        return {
            "updated": False,
            "refused": "not_a_checkout",
            "reason": (
                "This server is not running from a git checkout with a chrome-extension folder, "
                "so there is nothing here to pull. Update the package the way it was installed."
            ),
        }

    bridge = None
    try:
        if bridge_getter is not None:
            bridge = bridge_getter()
    except Exception:  # noqa: BLE001 - an unreachable companion is reported, not fatal
        bridge = None

    claims: list[Any] = []
    if bridge is not None:
        try:
            status = bridge.status(1.0) or {}
            claims = list(((status.get("daemon") or {}).get("claims")) or [])
        except Exception:  # noqa: BLE001
            claims = []
    if claims or local_sessions:
        return {
            "updated": False,
            "refused": "agent_driving_tab",
            "checkout": str(root),
            "claimed_tabs": len(claims),
            "local_sessions": local_sessions,
            "reason": (
                "An agent is driving a tab on this companion right now. A reload would detach "
                "those sessions, so nothing was pulled or reloaded; run this once none is."
            ),
        }

    result = update_checkout(root, runner=runner)
    if not result.get("updated"):
        return {**result, "checkout": str(root)}

    from web_search_neo.chrome_bootstrap import (  # noqa: PLC0415 - only needed once files changed
        _reload_companion,
        expected_extension_version,
    )

    restarted = False
    if bridge is not None and result.get("python_code_changed"):
        try:
            bridge.stop_daemon("companion update")
            bridge.start()
            restarted = True
        except Exception as exc:  # noqa: BLE001
            result["daemon_restart_error"] = f"{type(exc).__name__}: {exc}"

    companion: dict[str, Any] = {"self_update": "not_attempted"}
    if bridge is not None:
        try:
            companion = _reload_companion(bridge, expected_extension_version())
        except Exception as exc:  # noqa: BLE001
            companion = {"self_update": "unsupported", "self_update_error": f"{type(exc).__name__}: {exc}"}

    result.update({
        "checkout": str(root),
        "daemon_restarted": restarted,
        "companion_reloaded": companion.get("self_update") == "done",
        "companion_reload": companion,
        "server_restart_note": (
            "This server process still runs the code it was started with. Restart it to pick up "
            "the pulled files; until then a new daemon and the companion are current, this server "
            "is not."
        ) if result.get("python_code_changed") else "",
    })
    return result


def update_log_path() -> Path:
    return Path.home() / ".web-search-neo" / "update.log"


def start_detached_update(
    *,
    claimed_tabs: int = 0,
    package_file: str | Path | None = None,
    spawn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """The popup's Update button: run ``main.py --update`` as a process of its own.

    The bridge daemon is what answers the popup, and ``--update`` stops that very daemon
    before starting a fresh one, so the update cannot run inside it: it runs detached,
    outlives the daemon, and writes what it did to ``update.log``. Its last step reloads
    the companion, which closes the popup. Refused while agents drive tabs here, the same
    refusal ``--update`` itself makes.
    """
    root = package_checkout(str(package_file or Path(__file__).resolve().with_name("__init__.py")))
    if root is None:
        return {"started": False, "refused": "not_a_checkout", "reason": (
            "This Web Search Neo is not a git checkout, so there is nothing to pull. "
            "Update the package the way it was installed.")}
    if claimed_tabs:
        return {"started": False, "refused": "agent_driving_tab", "claimed_tabs": claimed_tabs, "reason": (
            f"An agent is driving {claimed_tabs} tab(s) here; updating would cut it off. "
            "Press Update again when it is done.")}
    from web_search_neo.process_probe import popen_detached  # noqa: PLC0415 - only the popup path spawns

    log = update_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n--- update from the popup, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        handle.flush()
        (spawn or popen_detached)(
            [sys.executable, "-m", "web_search_neo.main", "--update"],
            windows=sys.platform == "win32", windows_flags=0x08000000,  # CREATE_NO_WINDOW
            cwd=str(root), stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            close_fds=True,
        )
    return {"started": True, "checkout": str(root), "log": str(log)}
