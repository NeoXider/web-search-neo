"""The macro action's script-side ops, routed here so ``main.py`` (a size-ratcheted facade) only delegates.

run_steps (inline script), job / jobs / cancel / answer (macro_jobs), schedule / schedules /
unschedule (macro_schedules). Every macro call also makes sure this process's scheduler loop
runs, so a restarted server picks its schedules up on its first macro call.
"""
from __future__ import annotations

import asyncio
from typing import Any

from web_search_neo import macro_flow, macro_jobs, macro_schedules, macros

OPS = frozenset({"run_steps", "job", "jobs", "cancel", "answer", "schedule", "schedules", "unschedule"})


def start_scheduler(facade: Any) -> None:
    """Idempotent; called on every macro call."""
    async def launch(item: dict[str, Any]) -> dict[str, Any]:
        if item.get("name"):
            record = await asyncio.to_thread(macro_flow.load_script, item["name"], item.get("project_root"))
        else:
            record = {"name": f"schedule:{item['schedule_id']}", "steps": macros.validate_steps(item["steps"]),
                      "variables": {}}
        return await macro_flow.run_record(facade, record, item.get("variables"), session_id=item.get("session_id"),
                                           project_root=item.get("project_root"), background=True)

    try:
        macro_schedules.ensure_running(launch)
    except RuntimeError:
        pass  # no running loop (a synchronous caller): the next async call starts it


async def handle(facade: Any, op: str, *, name: str | None, steps: list[dict[str, Any]] | None,
                 variables: dict[str, Any] | None, continue_on_error: bool, session_id: str | None,
                 project_root: str | None, background: bool, job_id: str | None, wait_seconds: float,
                 timeout_seconds: float | None, answer: Any, every_seconds: float | None,
                 times: list[str] | None, max_runs: int | None, schedule_id: str | None) -> dict[str, Any]:
    if op == "jobs":
        return {"success": True, "jobs": macro_jobs.listing()}
    if op in ("job", "cancel", "answer"):
        if not job_id:
            raise ValueError(f"macro op '{op}' requires job_id (op='jobs' lists them)")
        if op == "cancel":
            return {"success": True, **macro_jobs.cancel(job_id)}
        if op == "answer":
            return {"success": True, **macro_jobs.answer(job_id, answer)}
        return {"success": True, **await macro_jobs.wait(job_id, wait_seconds)}
    if op == "run_steps":
        if not steps:
            raise ValueError("macro op 'run_steps' requires steps: a list of step objects")
        record = {"name": None, "steps": macros.validate_steps(steps), "variables": dict(variables or {})}
        return await macro_flow.run_record(facade, record, variables, continue_on_error=continue_on_error,
                                           session_id=session_id, project_root=project_root,
                                           background=background, timeout_seconds=timeout_seconds)
    if op == "schedule":
        if name:
            await asyncio.to_thread(macros.load, name, project_root)  # refuse a name that does not exist now
        checked = macros.validate_steps(steps) if steps else None
        created = await asyncio.to_thread(
            macro_schedules.create, name=name, steps=checked, variables=variables, every_seconds=every_seconds,
            times=times, max_runs=max_runs, session_id=session_id, project_root=project_root)
        return {"success": True, "schedule": created,
                "note": "Each run is a background job (op='jobs', the companion popup); op='unschedule' stops it."}
    if op == "schedules":
        return {"success": True, **await asyncio.to_thread(macro_schedules.listing)}
    if op == "unschedule":
        if not schedule_id:
            raise ValueError("macro op 'unschedule' requires schedule_id (op='schedules' lists them)")
        return {"success": True, **await asyncio.to_thread(macro_schedules.remove, schedule_id)}
    raise ValueError(f"unknown script op '{op}'")
