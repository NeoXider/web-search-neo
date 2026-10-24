"""Macro schedules: run a saved macro (or inline steps) every N seconds or at set times of day.

Each run is an ordinary background job (macro_jobs): visible in op='jobs' and the companion
popup, stoppable there. Schedules live in a file (SCHEDULES_FILE), so they survive a restart;
whichever server process holds the scheduler (OWNER_FILE, a heartbeat every few seconds) runs
them, and another process takes over when that one goes quiet - several agents' servers never
start the same run twice. A run that is still going when the next one is due is skipped, not
stacked.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from web_search_neo import macro_jobs, process_probe
from web_search_neo.sessions import file_lock

TICK_SECONDS = 5.0
OWNER_STALE_SECONDS = 20.0
MIN_EVERY_SECONDS = 10.0
MAX_SCHEDULES = 100
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_STATE: dict[str, Any] = {"task": None, "launch": None}


def _root() -> Path:
    return macro_jobs.jobs_dir().parent


def schedules_file() -> Path:
    configured = os.environ.get("WEB_SEARCH_NEO_SCHEDULES_FILE", "").strip()
    return Path(configured).expanduser() if configured else _root() / "macro-schedules.json"


def _owner_file() -> Path:
    return schedules_file().with_name("macro-scheduler.owner.json")


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(schedules_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("schedules", []) if isinstance(data, dict) else []


def _save(items: list[dict[str, Any]]) -> None:
    file_lock.atomic_write_text(schedules_file(), json.dumps({"schedules": items}, ensure_ascii=False, indent=1))


def next_time(item: dict[str, Any], now: float) -> float:
    """When a schedule is next due after *now*: every N seconds, or the soonest listed time of day."""
    if item.get("every_seconds"):
        last = item.get("next_run_at") or now
        every = float(item["every_seconds"])
        return last + every if last + every > now else now + every
    today = _dt.datetime.fromtimestamp(now)
    candidates = []
    for text in item.get("times") or []:
        hour, minute = (int(part) for part in text.split(":"))
        moment = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment.timestamp() <= now:
            moment += _dt.timedelta(days=1)
        candidates.append(moment.timestamp())
    return min(candidates)


def create(*, name: str | None, steps: list[dict[str, Any]] | None, variables: dict[str, Any] | None,
           every_seconds: float | None, times: list[str] | None, max_runs: int | None,
           session_id: str | None, project_root: str | None) -> dict[str, Any]:
    if bool(name) == bool(steps):
        raise ValueError("op='schedule' takes name (a saved macro) or steps (inline), not both and not neither")
    if bool(every_seconds) == bool(times):
        raise ValueError("op='schedule' takes every_seconds or times (['09:00', '18:30']), one of the two")
    if every_seconds and float(every_seconds) < MIN_EVERY_SECONDS:
        raise ValueError(f"every_seconds must be at least {MIN_EVERY_SECONDS:g}")
    for text in times or []:
        if not _TIME.match(str(text)):
            raise ValueError(f"time {text!r} is not HH:MM (24-hour, local time)")
    now = time.time()
    item = {"schedule_id": f"sch-{secrets.token_hex(3)}", "name": name, "steps": steps,
            "variables": dict(variables or {}), "every_seconds": float(every_seconds) if every_seconds else None,
            "times": [str(text) for text in times or []] or None, "max_runs": int(max_runs) if max_runs else None,
            "session_id": session_id, "project_root": project_root, "runs": 0, "created_at": now,
            "last_run_at": None, "last_job_id": None, "last_state": None, "skipped": 0}
    item["next_run_at"] = now + float(every_seconds) if every_seconds else next_time(item, now)
    with file_lock.exclusive(schedules_file()):
        items = _load()
        if len(items) >= MAX_SCHEDULES:
            raise ValueError(f"{MAX_SCHEDULES} schedules already; op='unschedule' one first")
        items.append(item)
        _save(items)
    return describe(item)


def remove(schedule_id: str) -> dict[str, Any]:
    with file_lock.exclusive(schedules_file()):
        items = _load()
        kept = [item for item in items if item.get("schedule_id") != schedule_id]
        if len(kept) == len(items):
            raise ValueError(f"no schedule '{schedule_id}'; op='schedules' lists them")
        _save(kept)
    return {"removed": schedule_id}


def describe(item: dict[str, Any]) -> dict[str, Any]:
    shown = {key: value for key, value in item.items() if key != "steps"}
    shown["inline_steps"] = len(item.get("steps") or []) or None
    if item.get("next_run_at"):
        shown["next_run"] = _dt.datetime.fromtimestamp(item["next_run_at"]).strftime("%Y-%m-%d %H:%M:%S")
    return shown


def listing() -> dict[str, Any]:
    owner = _read_owner()
    return {"schedules": [describe(item) for item in _load()], "scheduler": owner,
            "file": str(schedules_file())}


# ------------------------------------------------------------------ the scheduler

def _read_owner() -> dict[str, Any] | None:
    try:
        return json.loads(_owner_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _claim() -> bool:
    """Hold the scheduler: ours already, or nobody's heartbeat for OWNER_STALE_SECONDS."""
    with file_lock.exclusive(_owner_file()):
        owner = _read_owner()
        now = time.time()
        mine = owner is not None and owner.get("pid") == os.getpid()
        free = (owner is None or now - float(owner.get("beat") or 0) > OWNER_STALE_SECONDS
                or not process_probe.process_alive(int(owner.get("pid") or 0)))
        if mine or free:
            file_lock.atomic_write_text(_owner_file(), json.dumps({"pid": os.getpid(), "beat": now}))
            return True
        return False


def ensure_running(launch: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> None:
    """Start this process's scheduler loop (once); it idles unless it holds the scheduler."""
    _STATE["launch"] = launch
    task = _STATE.get("task")
    if task is None or task.done():
        _STATE["task"] = asyncio.get_running_loop().create_task(_loop())


async def _loop() -> None:
    while True:
        try:
            if _load() and await asyncio.to_thread(_claim):
                await _tick()
        except Exception:
            pass  # a broken schedule file must not kill the loop; op='schedules' shows it
        await asyncio.sleep(TICK_SECONDS)


async def _tick() -> None:
    now = time.time()
    due = []
    with file_lock.exclusive(schedules_file()):
        items = _load()
        for item in items:
            if (item.get("next_run_at") or 0) > now:
                continue
            last = item.get("last_job_id")
            running = last in macro_jobs._JOBS and macro_jobs._JOBS[last]["state"] in macro_jobs.ACTIVE
            item["next_run_at"] = next_time(item, now)
            if running:
                item["skipped"] = int(item.get("skipped") or 0) + 1
                continue
            item["runs"] = int(item.get("runs") or 0) + 1
            item["last_run_at"] = now
            due.append(item)
        finished = [item for item in items if item.get("max_runs") and item["runs"] >= item["max_runs"]
                    and item not in due]
        _save([item for item in items if item not in finished])
    for item in due:
        try:
            started = await _STATE["launch"](item)
            item["last_job_id"], item["last_state"] = started.get("job_id"), started.get("state")
        except Exception as exc:
            item["last_state"] = f"refused: {type(exc).__name__}: {exc}"
        with file_lock.exclusive(schedules_file()):
            items = _load()
            for stored in items:
                if stored.get("schedule_id") == item["schedule_id"]:
                    stored.update(last_job_id=item.get("last_job_id"), last_state=item.get("last_state"))
            _save(items)
