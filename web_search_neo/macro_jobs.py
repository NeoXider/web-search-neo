"""Macro jobs: every script run is a job you can see, wait for, answer and stop - from anywhere.

A script run (``op='run'`` / ``op='run_steps'``) is a job, foreground or ``background=true``:
the background one answers at once with a ``job_id``; the foreground one is awaited, but is a
job all the same, so it shows up and can be stopped while it runs.

Where a job is visible:

* this server process: ``op='job'`` (progress, ``wait_seconds`` to wait), ``op='jobs'``,
  ``op='cancel'``, ``op='answer'`` (for an ``ask`` step);
* every other server process: each job writes its state to ``JOBS_DIR/<job_id>.json``, so an
  agent sees (and stops, through ``<job_id>.cancel``) a job another agent started, and a
  restarted server still lists what ran before - a job whose process is gone reads
  ``interrupted`` (a half-run script is never resumed: its steps may already have acted);
* the person: every couple of seconds the process reports its jobs to the Chrome companion
  (``companion.jobsReport``), whose popup lists them with Stop and the ``ask`` buttons; the
  report's answer carries the Stop presses and the answers back.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from web_search_neo import process_probe
from web_search_neo.sessions import file_lock

KEEP = 50
REPORT_SECONDS = 2.0
WATCH_SECONDS = 1.0
SHOW_FINISHED_SECONDS = 30 * 60
ACTIVE = ("starting", "running", "waiting")
JOBS_DIR_ENV = "WEB_SEARCH_NEO_JOBS_DIR"
_JOBS: dict[str, dict[str, Any]] = {}
_WATCHER: dict[str, Any] = {"task": None, "reported": 0.0}


def jobs_dir() -> Path:
    configured = os.environ.get(JOBS_DIR_ENV, "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".web-search-neo" / "macro-jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ------------------------------------------------------------------ lifecycle

def new(macro: str | None, *, session: str | None = None, background: bool = True) -> dict[str, Any]:
    """A job record for the runner to report into; not yet started."""
    job_id = f"job-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}"
    job: dict[str, Any] = {
        "job_id": job_id, "macro": macro, "state": "starting", "session": session, "background": background,
        "pid": os.getpid(), "started_at": time.time(), "finished_at": None, "cancel_requested": False,
        "progress": {"executed": 0, "step": None, "action": None, "last_ok": None},
        "log": [], "question": None, "sessions": [], "result": None, "error": None,
        "_task": None, "_saved": 0.0, "_answer": None,
    }
    _JOBS[job_id] = job
    _forget_old()
    return job


def launch(job: dict[str, Any], run: Callable[[], Awaitable[dict[str, Any]]]) -> asyncio.Task:
    """Start the run as a task on the server's loop; the watcher reports and listens for it."""
    job["state"] = "running"
    task = asyncio.get_running_loop().create_task(_run(job, run))
    job["_task"] = task
    persist(job, force=True)
    _ensure_watcher()
    return task


async def _run(job: dict[str, Any], run: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    outcome: dict[str, Any]
    try:
        outcome = await run()
        job["state"] = "cancelled" if outcome.get("cancelled") else ("done" if outcome.get("success") else "failed")
        job["error"] = outcome.get("error")
    except asyncio.CancelledError:
        outcome = {"success": False, "cancelled": True, "error": "cancelled"}
        job["state"], job["error"] = "cancelled", "cancelled"
    except Exception as exc:  # a job never takes the server down; it reports what broke
        outcome = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        job["state"], job["error"] = "failed", outcome["error"]
    job["result"] = outcome
    job["question"] = None
    job["finished_at"] = time.time()
    persist(job, force=True)
    return outcome


def _forget_old() -> None:
    finished = [job for job in _JOBS.values() if job["finished_at"] is not None]
    for job in sorted(finished, key=lambda item: item["finished_at"])[:-KEEP] if len(finished) > KEEP else []:
        _JOBS.pop(job["job_id"], None)


# ------------------------------------------------------------------ views

def view(job: dict[str, Any], *, include_result: bool = True) -> dict[str, Any]:
    """What a caller sees of a job: never the task object, the result only when asked."""
    ended = job["finished_at"] or time.time()
    shown = {key: value for key, value in job.items() if not key.startswith("_") and key != "result"}
    shown["running_seconds"] = round(ended - job["started_at"], 1)
    shown["log"] = job["log"][-20:]
    trace = job.get("_trace") or []
    result = job["result"]
    if job["state"] in ACTIVE:
        shown["recent"] = trace[-10:]
        shown["note"] = ("Waiting for an answer: op='answer' job_id=<id> answer=<option>, or the companion popup."
                         if job["state"] == "waiting" else
                         "Still running: op='job' with this job_id (wait_seconds to wait for it), op='cancel' to stop.")
    elif result is not None:
        # the verdict on top, where summary='min' keeps it: returned, the failing step, the trace's end
        for key in ("returned", "failed_step"):
            if key in result:
                shown[key] = result[key]
        shown["trace"] = trace[-40:]
    if include_result and result is not None:
        shown["result"] = result
    return shown


def _record(job: dict[str, Any]) -> dict[str, Any]:
    """The on-disk / reported form: small, no result body, enough to show and to judge."""
    shown = view(job, include_result=False)
    shown.pop("note", None)
    result = job.get("result") or {}
    for key in ("returned", "failed_step"):
        if key in result:
            shown[key] = result[key]
    shown["recent"] = (job.get("_trace") or [])[-10:]
    shown["updated_at"] = time.time()
    return json.loads(json.dumps(shown, ensure_ascii=False, default=str))


def persist(job: dict[str, Any], *, force: bool = False) -> None:
    """Write the job's record for other processes (throttled to once a second unless forced)."""
    now = time.monotonic()
    if not force and now - job.get("_saved", 0.0) < 1.0:
        return
    job["_saved"] = now
    try:
        file_lock.atomic_write_text(jobs_dir() / f"{job['job_id']}.json",
                                    json.dumps(_record(job), ensure_ascii=False, indent=1))
    except OSError:
        pass  # a full or read-only disk costs the cross-process view, never the run
    _prune_disk()


def _prune_disk() -> None:
    try:
        files = sorted(jobs_dir().glob("job-*.json"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return
    for path in files[:-KEEP * 2] if len(files) > KEEP * 2 else []:
        for stale in (path, path.with_suffix(".cancel"), path.with_name(path.stem + ".answer.json")):
            try:
                stale.unlink()
            except OSError:
                pass


def _disk(job_id: str) -> dict[str, Any] | None:
    try:
        record = json.loads((jobs_dir() / f"{job_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if record.get("state") in ACTIVE and not process_probe.process_alive(int(record.get("pid") or 0)):
        record["state"] = "interrupted"
        record["note"] = "its server process ended while it ran; the script is not resumed (its steps may have acted)"
    return record


def get(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(str(job_id or "").strip())
    if job is None:
        raise ValueError(f"no job '{job_id}' in this server process; op='jobs' lists every process's jobs")
    return job


def listing() -> list[dict[str, Any]]:
    """This process's jobs and every other process's (from JOBS_DIR), newest last."""
    shown = {job["job_id"]: view(job, include_result=False) for job in _JOBS.values()}
    try:
        for path in jobs_dir().glob("job-*.json"):
            if path.stem not in shown and not path.stem.endswith(".answer"):
                record = _disk(path.stem)
                if record is not None:
                    record["other_process"] = record.get("pid") != os.getpid()
                    shown[path.stem] = record
    except OSError:
        pass
    return sorted(shown.values(), key=lambda item: item.get("started_at") or 0)[-KEEP:]


async def wait(job_id: str, seconds: float) -> dict[str, Any]:
    """The job's view once it ends or *seconds* pass, whichever is first (any process's job)."""
    key = str(job_id or "").strip()
    if key in _JOBS:
        task = _JOBS[key].get("_task")
        if task is not None and not task.done() and seconds > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=min(float(seconds), 3600.0))
            except asyncio.TimeoutError:
                pass
        return view(_JOBS[key])
    end = time.monotonic() + max(0.0, min(float(seconds), 3600.0))
    while True:
        record = _disk(key)
        if record is None:
            raise ValueError(f"no job '{job_id}' here or in {jobs_dir()}; op='jobs' lists them")
        if record.get("state") not in ACTIVE or time.monotonic() >= end:
            return record
        await asyncio.sleep(1.0)


def cancel(job_id: str) -> dict[str, Any]:
    """Stop a job: before its next step, and a step waiting on the page is interrupted."""
    key = str(job_id or "").strip()
    if key in _JOBS:
        job = _JOBS[key]
        job["cancel_requested"] = True
        task = job.get("_task")
        if task is not None and not task.done():
            task.cancel()
        persist(job, force=True)
        return view(job, include_result=False)
    record = _disk(key)
    if record is None:
        raise ValueError(f"no job '{job_id}' here or in {jobs_dir()}; op='jobs' lists them")
    if record.get("state") in ACTIVE:
        (jobs_dir() / f"{key}.cancel").write_text(str(time.time()), encoding="utf-8")
        record["cancel_requested"] = True
        record["note"] = "it runs in another server process, which stops it within a couple of seconds"
    return record


# ------------------------------------------------------------------ ask

async def ask(job: dict[str, Any], text: str, options: list[str], timeout: float) -> Any:
    """Pause the job until someone answers (popup, op='answer', another process); TimeoutError otherwise."""
    future = asyncio.get_running_loop().create_future()
    job["_answer"] = future
    job["question"] = {"text": text, "options": list(options), "asked_at": time.time(),
                       "timeout_seconds": timeout}
    job["state"] = "waiting"
    persist(job, force=True)
    await _report_now()
    try:
        return await asyncio.wait_for(future, timeout=max(1.0, float(timeout)))
    finally:
        job["_answer"] = None
        job["question"] = None
        if job["state"] == "waiting":
            job["state"] = "running"
        persist(job, force=True)


def answer(job_id: str, value: Any) -> dict[str, Any]:
    """Answer a waiting ask step, here or in another process."""
    key = str(job_id or "").strip()
    if key in _JOBS:
        job = _JOBS[key]
        future = job.get("_answer")
        if job["state"] != "waiting" or future is None or future.done():
            raise ValueError(f"job '{key}' is not waiting for an answer (state {job['state']})")
        future.set_result(value)
        return view(job, include_result=False)
    record = _disk(key)
    if record is None or record.get("state") != "waiting":
        raise ValueError(f"job '{key}' is not waiting for an answer")
    (jobs_dir() / f"{key}.answer.json").write_text(json.dumps({"value": value}, ensure_ascii=False), encoding="utf-8")
    record["note"] = "answer handed to the job's own process"
    return record


# ------------------------------------------------------------------ watcher and companion report

def _active_local() -> list[dict[str, Any]]:
    return [job for job in _JOBS.values() if job["state"] in ACTIVE]


def _ensure_watcher() -> None:
    task = _WATCHER.get("task")
    if task is None or task.done():
        _WATCHER["task"] = asyncio.get_running_loop().create_task(_watch())


async def _watch() -> None:
    """While this process has a job running: apply cancel/answer files, report to the companion."""
    idle_rounds = 0
    while idle_rounds < 2:  # one more report after the last job ends, so the popup sees the end
        _apply_files()
        if time.monotonic() - _WATCHER["reported"] >= REPORT_SECONDS:
            await _report_now()
        for job in _active_local():
            persist(job)
        idle_rounds = 0 if _active_local() else idle_rounds + 1
        await asyncio.sleep(WATCH_SECONDS)


def _apply_files() -> None:
    root = jobs_dir()
    for job in _active_local():
        stop = root / f"{job['job_id']}.cancel"
        if stop.exists():
            try:
                stop.unlink()
            except OSError:
                pass
            cancel(job["job_id"])
        reply = root / f"{job['job_id']}.answer.json"
        if reply.exists() and job["state"] == "waiting":
            try:
                value = json.loads(reply.read_text(encoding="utf-8")).get("value")
                reply.unlink()
                answer(job["job_id"], value)
            except (OSError, ValueError):
                pass


def _report_payload() -> dict[str, Any]:
    now = time.time()
    jobs = [job for job in _JOBS.values()
            if job["state"] in ACTIVE or now - (job["finished_at"] or now) < SHOW_FINISHED_SECONDS]
    records = []
    for job in sorted(jobs, key=lambda item: item["started_at"])[-20:]:
        record = _record(job)
        record["tab_ids"] = _tab_ids(job.get("sessions") or [])
        records.append(record)
    return {"pid": os.getpid(), "jobs": records}


def _tab_ids(sessions: list[str]) -> list[int]:
    """The Chrome tabs a job drove (current-Chrome sessions), for the companion's macro badge."""
    try:
        from web_search_neo import browser_tools
        found = []
        for name in sessions:
            item = browser_tools._sessions.get(name)
            tab = getattr(item, "current_tab_id", None)
            if isinstance(tab, int):
                found.append(tab)
        return found
    except Exception:
        return []


def _send_report(payload: dict[str, Any]) -> Any:
    try:
        from web_search_neo import browser_tools
        bridge = browser_tools.get_chrome_bridge()
        if not bridge.wait_connected(0.0):
            return None
        return bridge.request("companion.jobsReport", payload, timeout=3.0)
    except Exception:
        return None  # no companion: the jobs stay visible through op='jobs'


REPORTER: Callable[[dict[str, Any]], Any] = _send_report


async def _report_now() -> None:
    _WATCHER["reported"] = time.monotonic()
    payload = _report_payload()
    if not payload["jobs"]:
        return
    try:
        reply = await asyncio.wait_for(asyncio.to_thread(REPORTER, payload), timeout=5.0)
    except Exception:
        return
    if not isinstance(reply, dict):
        return
    for job_id in reply.get("cancel") or []:
        if job_id in _JOBS and _JOBS[job_id]["state"] in ACTIVE:
            cancel(job_id)
    for job_id, value in (reply.get("answers") or {}).items():
        if job_id in _JOBS and _JOBS[job_id]["state"] == "waiting":
            try:
                answer(job_id, value)
            except ValueError:
                pass
