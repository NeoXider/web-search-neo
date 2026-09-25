"""Which processes a process started - read so that a reused pid never adopts a stranger.

Windows records a parent pid for every process but never clears it: when the
parent exits, its pid can be handed to a new program, and every process the old
parent left behind now looks like a child of that newcomer. A chromedriver
started a minute ago can therefore "have" a chrome.exe that the owner started
this morning. A real child is always younger than its parent, so a listed child
that started before its parent is not one, and it is dropped here - before
anybody records it as the browser to retire, or kills it as part of a tree.
POSIX reparents orphans to init, but the same age test costs nothing there.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_FAMILY = 512


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("usage", ctypes.c_ulong), ("pid", ctypes.c_ulong),
                ("heap", ctypes.c_size_t), ("module", ctypes.c_ulong), ("threads", ctypes.c_ulong),
                ("parent", ctypes.c_ulong), ("priority", ctypes.c_long), ("flags", ctypes.c_ulong),
                ("exe", ctypes.c_wchar * 260)]


def _windows_started_at(pid: int) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.GetProcessTimes.argtypes = (ctypes.c_void_p,) + (ctypes.POINTER(_FileTime),) * 4
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [_FileTime() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return None
        return f"win:{(times[0].high << 32) | times[0].low}"
    finally:
        kernel32.CloseHandle(handle)


def started_at(pid: Any) -> str | None:
    """An opaque stamp of when ``pid`` started, or ``None`` when it cannot be read.

    Two reads of the same process give the same stamp; a reused pid gives another.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _windows_started_at(pid)
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            fields = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            return f"proc:{fields[19]}"  # starttime, field 22 of stat
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5)
        stamp = done.stdout.strip()
        return f"ps:{stamp}" if done.returncode == 0 and stamp else None
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        return None


def _windows_exited(pid: int) -> bool | None:
    """True when the process has exited (its object may live on while a handle is open); None if unknown."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value != 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def still_running(pid: int, stamp: str | None) -> bool:
    """``pid`` still runs as the very process stamped ``stamp``.

    A start stamp stays readable after exit for as long as somebody holds a
    handle (subprocess.Popen does), so the exit code is asked as well.
    """
    if not stamp or started_at(pid) != stamp:
        return False
    if sys.platform == "win32":
        return _windows_exited(pid) is not True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _order(stamp: str | None) -> int | None:
    """A stamp as a comparable number (Windows FILETIME, /proc clock ticks); None for other kinds."""
    kind, _, value = str(stamp or "").partition(":")
    return int(value) if kind in {"win", "proc"} and value.isdigit() else None


def younger_or_unknown(child_stamp: str | None, parent_stamp: str | None) -> bool:
    """False only when both stamps are known and the "child" started before its "parent"."""
    child, parent = _order(child_stamp), _order(parent_stamp)
    return child is None or parent is None or child >= parent


def windows_table() -> dict[int, tuple[int, str]] | None:
    """Every process as pid -> (parent pid, executable), from one Toolhelp snapshot; None if unavailable."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_ulong, ctypes.c_ulong)
    kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))
    kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = _ProcessEntry()
        entry.size = ctypes.sizeof(_ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[int(entry.pid)] = (int(entry.parent), str(entry.exe))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def _listed_children(pid: int, table: dict[int, tuple[int, str]] | None) -> list[tuple[int, str]]:
    if table is not None:
        return [(child, exe) for child, (parent, exe) in table.items() if parent == pid and child != pid]
    listed = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5)
    found = []
    for child in (int(item) for item in listed.stdout.split() if item.isdigit()):
        name = subprocess.run(["ps", "-o", "comm=", "-p", str(child)], capture_output=True,
                              text=True, timeout=5).stdout.strip()
        found.append((child, os.path.basename(name)))
    return found


def children(pid: Any, *, table: dict[int, tuple[int, str]] | None = None,
             stamp_of: Any = None) -> list[tuple[int, str]]:
    """Direct children of ``pid`` as ``(pid, executable)``, strangers behind a reused pid left out.

    ``table`` and ``stamp_of`` exist for tests (a prepared snapshot and stamps).
    """
    stamp_of = stamp_of or started_at
    try:
        pid = int(pid)
        if table is None and sys.platform == "win32":
            table = windows_table()
            if table is None:
                return []
        listed = _listed_children(pid, table)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    parent_stamp = stamp_of(pid)
    return [(child, exe) for child, exe in listed if younger_or_unknown(stamp_of(child), parent_stamp)]


def family(pid: int, *, table: dict[int, tuple[int, str]] | None = None,
           stamp_of: Any = None) -> list[tuple[int, str | None]] | None:
    """``pid`` and every real descendant, parents before children, each with its start stamp.

    None when the process table cannot be read at all (then nobody can tell a
    child from a stranger, and the caller must decide how careful to be).
    """
    stamp_of = stamp_of or started_at
    if table is None and sys.platform == "win32":
        table = windows_table()
        if table is None:
            return None
    out: list[tuple[int, str | None]] = []
    seen: set[int] = set()
    frontier = [int(pid)]
    while frontier and len(out) < MAX_FAMILY:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        out.append((current, stamp_of(current)))
        frontier.extend(child for child, _exe in children(current, table=table, stamp_of=stamp_of))
    return out
