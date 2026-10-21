"""Start the bridge when you log in to Windows: the popup's "Start with Windows" switch.

The daemon behind the companion port normally starts with the first agent that needs
it. After a reboot that leaves this computer out of reach of the others until someone
starts an agent here. With this on, logging in starts the daemon (with no idle exit)
and, a few seconds later, this computer's LAN node while sharing is on - so a paired
computer can drive it as soon as Chrome is up.

It is one value under ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``: no
service, no scheduled task, no admin rights. It runs this checkout's ``main.py`` with
``pythonw.exe`` from the interpreter the daemon runs on, so no console window opens.

On by default. The choice is stored with the node settings; without one, the daemon
writes the entry each time the companion connects - so installing or updating the
extension switches it on - and rewrites it when the checkout or the interpreter moved.
Switching it off in the popup removes the entry and is remembered.
``WEB_SEARCH_NEO_LOGON_START=0`` makes off the default instead.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any

from web_search_neo.nodes import config

DEFAULT_ENV = "WEB_SEARCH_NEO_LOGON_START"
CHOICE = "logon_start"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WebSearchNeoBridge"
AT_LOGON_FLAG = "--at-logon"


def supported() -> bool:
    return sys.platform == "win32"


def _interpreter() -> str:
    """pythonw.exe beside this interpreter: the same packages, and no console window."""
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else executable)


def command() -> str:
    """The command line Windows runs at logon."""
    entry = Path(__file__).resolve().parent.parent / "main.py"
    target = [str(entry)] if entry.exists() else ["-m", "web_search_neo.main"]
    return subprocess.list2cmdline([_interpreter(), *target, "--bridge", AT_LOGON_FLAG])


# The registry is reached only through these three, so tests never touch it.
def _read() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return None
    return str(value)


def _write(value: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)


def _delete() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


def wanted() -> bool:
    return config.choice(CHOICE, DEFAULT_ENV)


def state() -> dict[str, Any]:
    """What the popup shows: whether it is wanted, and whether Windows will do it."""
    if not supported():
        return {"supported": False, "enabled": False}
    present = _read()
    return {"supported": True, "enabled": wanted(), "installed": present is not None,
            "current": present == command(), "command": present or command()}


def apply() -> dict[str, Any]:
    """Make the Run entry match the choice; a failure is reported, not raised."""
    if not supported():
        return state()
    try:
        if wanted():
            if _read() != command():
                _write(command())
        elif _read() is not None:
            _delete()
    except OSError as exc:
        return {**state(), "error": f"{type(exc).__name__}: {exc}"}
    return state()


def set_wanted(enabled: bool) -> dict[str, Any]:
    config.set_choice(CHOICE, enabled)
    return apply()
