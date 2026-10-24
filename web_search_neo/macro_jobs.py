"""Background macro runs: start one now, read its progress and result later.

A long macro - wait for a picture to be generated, watch a page until a server answers,
poll an upload - used to hold the whole call open, and through a single-call daemon every
other call queued behind it. ``op='run', background=true`` hands the run to an asyncio task
inside the server and answers at once with a job id; ``op='job'`` reads progress (and waits,
with ``wait_seconds``), ``op='jobs'`` lists them, ``op='cancel'`` stops one between steps.

Jobs live as long as the server process (the CLI daemon or the MCP client's session); the
newest ``KEEP`` finished ones are remembered. A job drives tabs like any caller, so a
background job and the foreground should use different session ids.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Awaitable, Callable

KEEP = 50
_JOBS: dict[str, dict[str, Any]] = {}


def new(macro: str | None, *, session: str | None = None) -> dict[str, Any]:
    """A job record for the runner to report into; not yet started."""
    job_id = f"job-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}"
    job: dict[str, Any] = {
        "job_id": job_id, "macro": macro, "state": "starting", "session": session,
        "started_at": time.time(), "finished_at": None, "cancel_requested": False,
        "progress": {"executed": 0, "step": None, "action": None, "last_ok": None},
        "log": [], "result": None, "error": None, "_task": None,
    }
    _JOBS[job_id] = job
    _forget_old()
    return job


def launch(job: dict[str, Any], run: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Start the run as a task on the server's loop and answer with the job's view at once."""
    job["state"] = "running"
    job["_task"] = asyncio.get_running_loop().create_task(_run(job, run))
    return view(job)


async def _run(job: dict[str, Any], run: Callable[[], Awaitable[dict[str, Any]]]) -> None:
    try:
        outcome = await run()
        job["result"] = outcome
        job["state"] = "cancelled" if outcome.get("cancelled") else ("done" if outcome.get("success") else "failed")
        job["error"] = outcome.get("error")
    except asyncio.CancelledError:
        job["state"] = "cancelled"
        job["error"] = "cancelled"
    except Exception as exc:  # a job never takes the server down; it reports what broke
        job["state"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["finished_at"] = time.time()


def _forget_old() -> None:
    finished = [job for job in _JOBS.values() if job["finished_at"] is not None]
    for job in sorted(finished, key=lambda item: item["finished_at"])[:-KEEP] if len(finished) > KEEP else []:
        _JOBS.pop(job["job_id"], None)


def get(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(str(job_id or "").strip())
    if job is None:
        known = [item["job_id"] for item in _JOBS.values()]
        raise ValueError(f"no background job '{job_id}' in this server process; known: {known}. Jobs live as "
                         "long as the server; a restarted server starts with none.")
    return job


def view(job: dict[str, Any], *, include_result: bool = True) -> dict[str, Any]:
    """What a caller sees of a job: never the task object, the result only when asked."""
    ended = job["finished_at"] or time.time()
    shown = {key: value for key, value in job.items() if not key.startswith("_") and key != "result"}
    shown["running_seconds"] = round(ended - job["started_at"], 1)
    shown["log"] = job["log"][-20:]
    trace = job.get("_trace") or []
    result = job["result"]
    if job["state"] in ("starting", "running"):
        shown["recent"] = trace[-10:]
        shown["note"] = "Still running: op='job' with this job_id (wait_seconds to wait for it), op='cancel' to stop."
    elif result is not None:
        # the verdict on top, where summary='min' keeps it: returned, the failing step, the trace's end
        for key in ("returned", "failed_step"):
            if key in result:
                shown[key] = result[key]
        shown["trace"] = trace[-40:]
    if include_result and result is not None:
        shown["result"] = result
    return shown


async def wait(job_id: str, seconds: float) -> dict[str, Any]:
    """The job's view once it ends or *seconds* pass, whichever is first."""
    job = get(job_id)
    task = job.get("_task")
    if task is not None and not task.done() and seconds > 0:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=min(float(seconds), 3600.0))
        except asyncio.TimeoutError:
            pass
    return view(job)


def cancel(job_id: str) -> dict[str, Any]:
    """Ask the job to stop: it stops before its next step, and a step waiting on the page is interrupted."""
    job = get(job_id)
    job["cancel_requested"] = True
    task = job.get("_task")
    if task is not None and not task.done():
        task.cancel()
    return view(job, include_result=False)


def listing() -> list[dict[str, Any]]:
    return [view(job, include_result=False) for job in sorted(_JOBS.values(), key=lambda item: item["started_at"])]
