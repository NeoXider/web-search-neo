"""Ask the OS whether a process id still names a running process."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import Any

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


def _windows_process_alive(pid: int) -> bool:
    # use_last_error makes ctypes capture GetLastError right after each call;
    # calling kernel32.GetLastError() later can read a value some other call in
    # between (the interpreter's own included) has already overwritten.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # "Invalid parameter" is how Windows says "no such process". Anything
        # else (access denied, for one) means it is alive and merely out of
        # reach, and a live tab must not be stolen.
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    """``True`` if ``pid`` exists (or cannot be ruled out), ``False`` if it is gone.

    On POSIX this is ``os.kill(pid, 0)``: signal 0 delivers nothing and
    succeeds only if the process exists. Windows needs a different call, and the
    difference is not cosmetic: there ``signal.CTRL_C_EVENT`` **is** 0, so
    ``os.kill(pid, 0)`` tries to deliver a Ctrl+C to a process group and, for a
    PID that is gone, raises a bare ``OSError`` (WinError 87). That once killed
    the claim reaper on its first dead claim and left tabs held for hours, so on
    Windows the kernel is asked directly instead of signalling anyone.
    """
    if sys.platform == "win32":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours
    except OSError:
        # Never guess "dead" from an error we did not plan for: releasing a
        # claim out from under a live agent is worse than holding it.
        return True


# A child created inside a job object dies with the job when the job was set up
# with KILL_ON_JOB_CLOSE, which is what the MCP Python SDK does on Windows for
# every server it starts. Breaking away keeps a detached helper alive past the
# server that started it; a job that forbids breakaway refuses the flag, and
# the plain start is the fallback.
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


CREATE_BREAKAWAY_FROM_JOB = _CREATE_BREAKAWAY_FROM_JOB


def popen_detached(
    command: list[str], *, windows: bool, windows_flags: int = 0, **kwargs: Any
) -> subprocess.Popen:
    """Start ``command`` so it outlives this process, on Windows and elsewhere."""
    if not windows:
        return subprocess.Popen(command, start_new_session=True, **kwargs)  # noqa: S603
    try:
        return subprocess.Popen(  # noqa: S603
            command, creationflags=windows_flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs
        )
    except OSError:
        return subprocess.Popen(command, creationflags=windows_flags, **kwargs)  # noqa: S603
