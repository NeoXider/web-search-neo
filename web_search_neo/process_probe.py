"""Ask the OS whether a process id still names a running process."""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import os
import subprocess
import sys
from typing import Any, Iterator

# Start stamps and the child listing live in sessions/process_tree.py, where the
# action layer can reach them too; they are re-exported here under their old names.
from web_search_neo.sessions.process_tree import _ProcessEntry, children, started_at  # noqa: F401

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


# ---------------------------------------------------------------- identity
# A process id alone proves nothing once its process is gone: Windows and POSIX
# both hand the number to the next program. Before anything is killed by a
# recorded pid, the start stamp recorded with it has to match again.

def same_process(pid: Any, stamp: Any) -> bool:
    """True only when ``pid`` runs and is provably the process recorded with ``stamp``."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not stamp or not process_alive(pid):
        return False
    return started_at(pid) == stamp


# ---------------------------------------------------------------- outliving the server
# A browser parked with persist=true must outlive this process. On Windows the
# MCP client may have put the server into a job object that kills every process
# in it when the client goes; a child may leave the job only if the job allows it.

class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", ctypes.c_ulong), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active", ctypes.c_ulong), ("affinity", ctypes.c_size_t), ("priority", ctypes.c_ulong),
                ("scheduling", ctypes.c_ulong)]


_JOB_KILL_ON_CLOSE, _JOB_BREAKAWAY_OK, _JOB_SILENT_BREAKAWAY_OK = 0x2000, 0x800, 0x1000


def job_breakaway() -> str:
    """How a child of this process relates to its job (Windows; "none" elsewhere).

    ``none``: no job, or one that does not kill on close - children outlive us.
    ``silent``: the job lets children out by itself. ``allowed``: a child started
    with CREATE_BREAKAWAY_FROM_JOB leaves it. ``forbidden``: the job ends every
    child when the MCP client goes and lets none out.
    """
    if sys.platform != "win32":
        return "none"
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.IsProcessInJob.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
        kernel32.QueryInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                       ctypes.c_ulong, ctypes.c_void_p)
        inside = ctypes.c_int(0)
        if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(inside)) or not inside:
            return "none"
        limits = _BasicLimits()
        if not kernel32.QueryInformationJobObject(None, 2, ctypes.byref(limits), ctypes.sizeof(limits), None):
            return "forbidden"
    except (OSError, AttributeError):
        return "none"
    if limits.flags & _JOB_SILENT_BREAKAWAY_OK:
        return "silent"
    if limits.flags & _JOB_BREAKAWAY_OK:
        return "allowed"
    return "forbidden" if limits.flags & _JOB_KILL_ON_CLOSE else "none"


_DETACHED_LAUNCH: contextvars.ContextVar[bool] = contextvars.ContextVar("wsn_detached_launch", default=False)


@contextlib.contextmanager
def detached_launch() -> Iterator[None]:
    """Inside this block chromedriver (and so Chrome) is started to outlive the server."""
    token = _DETACHED_LAUNCH.set(True)
    try:
        yield
    finally:
        _DETACHED_LAUNCH.reset(token)


def driver_popen_kwargs() -> dict[str, Any]:
    """Selenium's ``popen_kw`` for chromedriver; Selenium owns the startupinfo argument.

    No console window on Windows. Inside :func:`detached_launch` the process also
    leaves the MCP client's job where that is allowed (Windows) or starts its own
    session (POSIX), so neither the client's job nor its process-group signal ends it.
    """
    detached = _DETACHED_LAUNCH.get()
    if os.name != "nt":
        return {"start_new_session": True} if detached else {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if detached and job_breakaway() == "allowed":
        flags |= _CREATE_BREAKAWAY_FROM_JOB
    return {"creation_flags": flags}
