"""Re-execute long-running entry points through the base interpreter.

Some venv implementations (uv's included) ship ``pythonw.exe`` as a small
launcher that starts the real interpreter as a *child* process. Under a
console-less parent that child - always the console flavor - owns a visible
console window for the whole session, which is exactly the window this project
exists to avoid. Pointing the command at the base ``pythonw.exe`` directly,
with ``__PYVENV_LAUNCHER__`` naming the venv, starts one windowless process
instead (the same hand-off the bridge daemon and the MCP config generator
already use).

This module performs that hand-off from the inside, so even a stale config
that still points at the shim heals itself: ``main()`` calls
:func:`ensure_direct` first, and a shim-started daemon or MCP server replaces
itself with the hidden base interpreter before binding anything. Short-lived
commands never reach it - only the modes that stay up are re-executed - and a
process that already has a console keeps it, so terminal runs behave exactly
as before, Ctrl+C included.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_LAUNCHER_VAR = "__PYVENV_LAUNCHER__"
_BYPASSED_VAR = "WEB_SEARCH_NEO_SHIM_BYPASSED"


def _console_window() -> int:
    """This process's console window handle, 0 when there is none."""
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except Exception:
        return 0


def _venv_base(executable: str) -> tuple[Path, Path] | None:
    """The ``(venv launcher, base pythonw)`` pair, or None when not a shim run.

    Only the standard Windows venv layout qualifies: the running executable
    must sit two levels below a directory holding ``pyvenv.cfg`` whose
    ``home`` names a directory with its own ``pythonw.exe``, different from
    the running file. Anything else - system interpreters, single-file builds,
    POSIX layouts - is returned untouched.
    """
    exe = Path(executable)
    try:
        resolved_exe = exe.resolve()
    except OSError:
        return None
    venv_root = resolved_exe.parent.parent
    try:
        text = (venv_root / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    home: Path | None = None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "home":
            candidate = Path(value.strip())
            if candidate.is_dir():
                home = candidate
            break
    if home is None:
        return None
    base = home / "pythonw.exe"
    try:
        same = base.resolve() == resolved_exe
    except OSError:
        return None
    if not base.is_file() or same:
        return None
    return resolved_exe, base


def _long_running(argv: list[str]) -> bool:
    """Whether these arguments start something that stays up.

    The bridge daemon (``--bridge`` without ``--stop``) and the stdio MCP
    server (no arguments at all) are the two modes that own a console for
    hours. One-shot commands keep their process, exit code path included.
    """
    if "--bridge" in argv:
        return "--stop" not in argv
    return len(argv) == 0


def ensure_direct(argv: list[str] | None = None) -> None:
    """Replace a shim-launched daemon/server with the hidden base interpreter.

    Exits the current process via ``SystemExit`` carrying the child's code, so
    callers just call this first and continue: either it returns (nothing to
    do) or the process is already the replacement.
    """
    if os.name != "nt":
        return
    if os.environ.get(_BYPASSED_VAR):
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not _long_running(arguments):
        return
    if _console_window():
        return
    pair = _venv_base(sys.executable)
    if pair is None:
        return
    venv_exe, base = pair
    environment = dict(os.environ)
    environment[_LAUNCHER_VAR] = str(venv_exe)
    environment[_BYPASSED_VAR] = "1"
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    child = subprocess.Popen(
        # sys.argv[1:] contains application options, not the entry point. A
        # bare interpreter exits on MCP input (or treats --bridge as its own
        # option). The module target also works for installed `wsn` launchers.
        [str(base), "-m", "web_search_neo.main", *arguments],
        env=environment,
        # Explicit redirection preserves the MCP pipes under pythonw even
        # when Windows standard handles are not inheritable by default.
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        startupinfo=startupinfo,
    )
    raise SystemExit(child.wait())
