"""The macro script runner: variables, conditions, loops, page checks, sub-macros, background runs.

A plain macro is a list of actions replayed in one batch. A script macro is the same file with
logic in it, and runs here one step at a time through the same dispatcher (``web_action`` and
replays share ``dispatch.execute_actions``), so every action is validated against the schema it
publishes exactly as a hand-written batch would be. What this adds, in the JSON itself:

* step keys on any action: ``when`` (run only if), ``save_as`` (keep the result as a variable),
  ``assert`` (an expression the result must satisfy), ``expect`` (test_run's page checks),
  ``retry``/``retry_delay``, ``on_error: continue``, ``label``;
* control steps: ``if``, ``repeat``, ``for_each``, ``wait_until``, ``try``, ``set``, ``sleep``,
  ``log``, ``fail``, ``return``, ``break``, ``continue``, ``call`` (another macro) and ``read``
  (any web_info topic: network, page_text, find, console, ...);
* ``{{expression}}`` templates in any parameter (macro_expr), with ``last`` always holding the
  previous step's ``ok``/``value``/``error``.

Every loop is capped, a run is capped at MAX_EXECUTED steps, calls nest at most CALL_DEPTH deep,
and a background job stops between steps when cancelled.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

from web_search_neo import macro_check, macro_expr, macro_jobs, macros
from web_search_neo.audit import scenario

MAX_EXECUTED = 20_000
LOOP_DEFAULT = 1_000
LOOP_MAX = 100_000
CALL_DEPTH = 5
SLEEP_MAX = 86_400.0
_MISSING = object()


class _Failed(Exception):
    def __init__(self, message: str, path: str, action: str) -> None:
        super().__init__(message)
        self.path, self.action = path, action


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__("return")
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


def value_of(result: dict[str, Any]) -> Any:
    """What ``save_as`` keeps: a script's returned value (JSON text decoded), else the step's data."""
    data = result.get("data")
    if isinstance(data, dict) and "value" in data and result.get("action") in ("run_script", "execute_js"):
        value = data["value"]
        if isinstance(value, str) and value.strip()[:1] in ("{", "["):
            parsed = macro_expr.FUNCTIONS["json"](value)
            return value if parsed is None else parsed
        return value
    return data


def _preview(value: Any, limit: int = 140) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


class _Shared:
    """What every runner of one run shares, sub-macro calls included."""

    def __init__(self, job: dict[str, Any] | None, deadline: float | None) -> None:
        self.results: list[dict[str, Any]] = []
        self.trace: list[str] = []
        self.log: list[str] = []
        self.executed = 0
        self.failures = 0
        self.job = job
        self.deadline = deadline
        if job is not None:  # a job's view shows the newest trace lines while it runs
            job["_trace"] = self.trace


class Runner:
    def __init__(self, facade: Any, scope: dict[str, Any], *, summary: str = "full", continue_on_error: bool = False,
                 session_override: str | None = None, project_root: str | None = None,
                 call_stack: tuple[str, ...] = (), shared: _Shared | None = None, prefix: str = "") -> None:
        self.facade, self.scope, self.summary = facade, scope, summary
        self.continue_on_error, self.session_override = continue_on_error, session_override
        self.project_root, self.call_stack, self.prefix = project_root, call_stack, prefix
        self.shared = shared or _Shared(None, None)
        self.outputs: dict[str, Any] = {}
        self.scope.setdefault("last", {"ok": None, "value": None, "error": None, "action": None, "step": None})

    # --- the run -----------------------------------------------------------------------------
    async def run(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        started = time.monotonic()
        failed: _Failed | None = None
        returned: Any = _MISSING
        cancelled = False
        try:
            await self.block(steps, self.prefix)
        except _Return as exc:
            returned = exc.value
        except _Failed as exc:
            failed = exc
        except (_Break, _Continue):
            failed = _Failed("break/continue outside a loop", "?", "break")
        except asyncio.CancelledError:
            cancelled = True
            failed = _Failed("cancelled", "?", "cancel")
        shared = self.shared
        outcome: dict[str, Any] = {
            "success": failed is None, "flow": True,
            "requested_count": len(steps), "completed_count": shared.executed,
            "executed_count": shared.executed, "failure_count": shared.failures,
            "stopped_early": failed is not None, "duration_ms": round((time.monotonic() - started) * 1000),
            "trace": shared.trace, "results": shared.results,
        }
        if failed is not None:
            outcome.update(error=str(failed), failed_step=failed.path, failed_action=failed.action)
        if cancelled:
            outcome["cancelled"] = True
        if returned is not _MISSING:
            outcome["returned"] = returned
        if self.outputs:
            outcome["outputs"] = self.outputs
        if shared.log:
            outcome["log"] = shared.log
        return outcome

    async def block(self, steps: Any, prefix: str) -> None:
        for index, step in enumerate(steps or []):
            await self.step(step, f"{prefix}{index}")

    def _check_limits(self, path: str, action: str) -> None:
        shared = self.shared
        if shared.job is not None and shared.job.get("cancel_requested"):
            raise _Failed("cancelled", path, action)
        if shared.deadline is not None and time.monotonic() > shared.deadline:
            raise _Failed("the run's timeout_seconds ran out", path, action)
        if shared.executed >= MAX_EXECUTED:
            raise _Failed(f"over {MAX_EXECUTED} steps in one run; a loop probably never ends", path, action)

    async def step(self, step: dict[str, Any], path: str) -> None:
        action = macro_check.action_of(step)
        self._check_limits(path, action)
        if "when" in step and not self._truthy(step["when"], path, action, "when"):
            self.shared.trace.append(f"{path} {action} skipped (when false)")
            return
        if self.shared.job is not None:
            self.shared.job["progress"].update(step=path, action=action, executed=self.shared.executed)
        if action in macro_check.CONTROL_ACTIONS:
            await getattr(self, "_c_" + action)(step, path)
        else:
            await self._action(step, path, action)

    # --- helpers -----------------------------------------------------------------------------
    def _eval(self, expression: Any, path: str, action: str, key: str) -> Any:
        try:
            return macro_expr.evaluate(expression, self.scope)
        except macro_expr.ExpressionError as exc:
            raise _Failed(f"{key}: {exc}", path, action) from None

    def _truthy(self, expression: Any, path: str, action: str, key: str) -> bool:
        return bool(self._eval(expression, path, action, key))

    def _render(self, value: Any, path: str, action: str) -> Any:
        try:
            return macro_expr.render(value, self.scope)
        except macro_expr.ExpressionError as exc:
            raise _Failed(str(exc), path, action) from None

    def _items_or_number(self, value: Any, path: str, action: str, key: str) -> Any:
        if isinstance(value, str) and "{{" not in value:
            return self._eval(value, path, action, key)
        return self._render(value, path, action)

    async def _sleep(self, seconds: float, path: str, action: str) -> None:
        end = time.monotonic() + max(0.0, min(float(seconds), SLEEP_MAX))
        while (left := end - time.monotonic()) > 0:
            self._check_limits(path, action)
            await asyncio.sleep(min(left, 0.5))

    def _record(self, path: str, action: str, result: dict[str, Any], step: dict[str, Any], attempts: int,
                value: Any) -> None:
        shared = self.shared
        shared.executed += 1
        ok = result.get("success") is True
        entry: dict[str, Any] = {"index": shared.executed - 1, "step": path, "action": action, "success": ok}
        if step.get("label"):
            entry["label"] = step["label"]
        for key in ("data", "error", "duration_ms"):
            if key in result:
                entry[key] = result[key]
        if attempts > 1:
            entry["attempts"] = attempts
        shared.results.append(entry)
        if not ok:
            shared.failures += 1
        label = f" [{step['label']}]" if step.get("label") else ""
        tail = f" -> {_preview(value)}" if ok and value is not None and (step.get("save_as") or action == "run_script") \
            else ("" if ok else f": {_preview(result.get('error') or 'failed', 200)}")
        shared.trace.append(f"{path} {action}{label} {'ok' if ok else 'FAILED'}"
                            f"{' ' + str(result.get('duration_ms')) + 'ms' if 'duration_ms' in result else ''}{tail}")
        self.scope["last"] = {"ok": ok, "value": value, "error": None if ok else result.get("error"),
                              "action": action, "step": path}
        if ok and isinstance(step.get("save_as"), str):
            self.scope[step["save_as"]] = value
            self.outputs[step["save_as"]] = value
        if shared.job is not None:
            shared.job["progress"].update(executed=shared.executed, step=path, action=action, last_ok=ok)

    def _settle(self, step: dict[str, Any], path: str, action: str, result: dict[str, Any]) -> None:
        """After recording: stop the run on a failure unless the step (or the run) says continue."""
        if result.get("success") is True:
            return
        if (step.get("on_error") or ("continue" if self.continue_on_error else "stop")) == "continue":
            return
        raise _Failed(str(result.get("error") or "step failed"), path, action)

    # --- actions -----------------------------------------------------------------------------
    async def _dispatch(self, action: dict[str, Any]) -> dict[str, Any]:
        outcome = await self.facade._execute_actions([action], False, self.summary)
        results = outcome.get("results") or []
        return dict(results[0]) if results else {"success": False, "error": "the dispatcher returned no result"}

    async def _action(self, step: dict[str, Any], path: str, action: str) -> None:
        params = {key: value for key, value in step.items() if key not in macro_check.STEP_KEYS}
        rendered = self._render(params, path, action)
        spec = self.facade._ACTIONS.get(action)
        if self.session_override and spec is not None and "session_id" in self.facade._argument_model(
                spec.tool_name).model_fields:
            rendered["session_id"] = self.session_override
        attempts, retries = 0, int(step.get("retry") or 0)
        while True:
            attempts += 1
            result = await self._dispatch(rendered)
            value = value_of(result)
            if result.get("success") is True and step.get("expect"):
                result = await self._expect(step, path, action, rendered, spec, result)
            if result.get("success") is True and "assert" in step:
                checked = {**self.scope, "result": result, "value": value}
                try:
                    holds = macro_expr.truthy(step["assert"], checked)
                except macro_expr.ExpressionError as exc:
                    raise _Failed(f"assert: {exc}", path, action) from None
                if not holds:
                    result = {**result, "success": False, "error": f"assert failed: {step['assert']}"}
            if result.get("success") is True or attempts > retries:
                break
            await self._sleep(float(step.get("retry_delay") or 1.0), path, action)
        self._record(path, action, result, step, attempts, value)
        self._settle(step, path, action, result)

    async def _expect(self, step: dict[str, Any], path: str, action: str, rendered: dict[str, Any],
                      spec: Any, result: dict[str, Any]) -> dict[str, Any]:
        expect = self._render(step["expect"], path, action)
        try:
            checked = scenario._validate_expect(expect, 0)
        except ValueError as exc:
            raise _Failed(str(exc).replace("step 0: ", ""), path, action) from None
        session = (self.facade._step_session(spec.tool_name, rendered) if spec is not None else None) or "default"
        plan = {"expect": checked, "index": 0, "timeout": max(0.5, min(float(checked.get("timeout_seconds", 5)), 600.0))}
        for check in scenario.checks_for(plan, session):
            if "action" not in check:
                continue
            verdict = await self._dispatch(check["action"])
            if verdict.get("success") is not True:
                return {**result, "success": False,
                        "error": f"expect.{check['key']} not met: {check['expected']!r} ({verdict.get('error')})"}
        return result

    async def _c_read(self, step: dict[str, Any], path: str) -> None:
        topic = str(self._render(step.get("topic"), path, "read") or "").strip()
        handler = self.facade._TOPIC_HANDLERS.get(topic)
        started = time.monotonic()
        if handler is None:
            result: dict[str, Any] = {"success": False, "error": f"unknown topic '{topic}'; topics: "
                                                                  f"{sorted(self.facade._TOPIC_HANDLERS)}"}
        else:
            params = dict(self._render(step.get("params") or {}, path, "read"))
            if "output" in inspect.signature(handler).parameters and "output" not in params:
                params["output"] = "json"  # a script reads fields, not a text table
            try:
                validated = self.facade._validate_arguments(handler.__name__, f"topic '{topic}'", params)
                data = await handler(**validated)
                result = {"success": not (isinstance(data, dict) and data.get("success") is False), "data": data}
                if not result["success"]:
                    result["error"] = str(data.get("error") or "read reported success=false")
            except Exception as exc:
                result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        if result.get("success") and "assert" in step:
            if not macro_expr.truthy(step["assert"], {**self.scope, "result": result, "value": result.get("data")}):
                result = {**result, "success": False, "error": f"assert failed: {step['assert']}"}
        self._record(path, f"read:{topic}", result, step, 1, result.get("data"))
        self._settle(step, path, "read", result)

    # --- control steps -------------------------------------------------------------------------
    async def _c_if(self, step: dict[str, Any], path: str) -> None:
        branch = "then" if self._truthy(step.get("test"), path, "if", "test") else "else"
        self.shared.trace.append(f"{path} if -> {branch}")
        await self.block(step.get(branch), f"{path}.{branch}.")

    async def _loop_body(self, step: dict[str, Any], path: str) -> bool:
        """Run one pass of a loop body; False when a break ended the loop."""
        try:
            await self.block(step.get("steps"), f"{path}.steps.")
        except _Break:
            return False
        except _Continue:
            pass
        return True

    def _cap(self, step: dict[str, Any], key: str) -> int:
        return max(0, min(int(step.get(key) or LOOP_DEFAULT), LOOP_MAX))

    async def _c_repeat(self, step: dict[str, Any], path: str) -> None:
        times = step.get("times")
        count = None if times is None else int(self._items_or_number(times, path, "repeat", "times"))
        limit = min(count, LOOP_MAX) if count is not None else self._cap(step, "max_iterations")
        deadline = time.monotonic() + float(step["timeout_seconds"]) if step.get("timeout_seconds") else None
        done = 0
        for iteration in range(limit):
            if "while" in step and not self._truthy(step["while"], path, "repeat", "while"):
                break
            if step.get("as"):
                self.scope[str(step["as"])] = iteration
            done += 1
            if not await self._loop_body(step, path):
                break
            if "until" in step and self._truthy(step["until"], path, "repeat", "until"):
                break
            if deadline is not None and time.monotonic() > deadline:
                raise _Failed(f"repeat ran out of its timeout_seconds after {done} pass(es)", path, "repeat")
            if step.get("delay_seconds"):
                await self._sleep(float(step["delay_seconds"]), path, "repeat")
        self.shared.trace.append(f"{path} repeat done after {done} pass(es)")

    async def _c_for_each(self, step: dict[str, Any], path: str) -> None:
        items = self._items_or_number(step.get("items"), path, "for_each", "items")
        if isinstance(items, dict):
            items = [{"key": key, "value": value} for key, value in items.items()]
        if items is None:
            items = []
        if not isinstance(items, list):
            raise _Failed(f"items is a {type(items).__name__}, not a list", path, "for_each")
        cap = self._cap(step, "max_items")
        if len(items) > cap:
            raise _Failed(f"{len(items)} items is over max_items={cap}; raise it if you mean it", path, "for_each")
        name, index_name = str(step.get("as") or "item"), step.get("index_as")
        done = 0
        for index, item in enumerate(items):
            self.scope[name] = item
            if index_name:
                self.scope[str(index_name)] = index
            done += 1
            if not await self._loop_body(step, path):
                break
            if step.get("delay_seconds"):
                await self._sleep(float(step["delay_seconds"]), path, "for_each")
        self.shared.trace.append(f"{path} for_each done: {done} of {len(items)}")

    async def _c_wait_until(self, step: dict[str, Any], path: str) -> None:
        timeout = max(0.0, min(float(step.get("timeout_seconds", 30)), SLEEP_MAX))
        interval = max(0.2, float(step.get("interval_seconds", 1)))
        end, polls = time.monotonic() + timeout, 0
        while True:
            polls += 1
            await self.block(step.get("steps"), f"{path}.steps.")
            if self._truthy(step.get("test"), path, "wait_until", "test"):
                self.shared.trace.append(f"{path} wait_until met after {polls} poll(s)")
                return
            if time.monotonic() + interval > end:
                break
            await self._sleep(interval, path, "wait_until")
        message = f"wait_until timed out after {timeout:g}s ({polls} polls): {step.get('test')}"
        self.shared.trace.append(f"{path} {message}")
        self.scope["last"] = {"ok": False, "value": None, "error": message, "action": "wait_until", "step": path}
        if step.get("on_timeout") != "continue":
            raise _Failed(message, path, "wait_until")

    async def _c_try(self, step: dict[str, Any], path: str) -> None:
        try:
            await self.block(step.get("steps"), f"{path}.steps.")
        except _Failed as exc:
            if not step.get("catch") or str(exc) == "cancelled":
                raise
            self.scope["error"] = {"message": str(exc), "step": exc.path, "action": exc.action}
            self.shared.trace.append(f"{path} try caught: {_preview(str(exc), 160)}")
            await self.block(step.get("catch"), f"{path}.catch.")
        finally:
            if step.get("finally"):
                await self.block(step["finally"], f"{path}.finally.")

    async def _c_set(self, step: dict[str, Any], path: str) -> None:
        values = dict(step.get("values") or {})
        if "name" in step:
            values[str(step["name"])] = step.get("value")
        for name, value in values.items():
            self.scope[name] = self._render(value, path, "set")
            self.outputs[name] = self.scope[name]
        self.shared.trace.append(f"{path} set {', '.join(f'{name}={_preview(self.scope[name], 60)}' for name in values)}")

    async def _c_sleep(self, step: dict[str, Any], path: str) -> None:
        seconds = float(self._items_or_number(step.get("seconds", 1), path, "sleep", "seconds"))
        await self._sleep(seconds, path, "sleep")
        self.shared.trace.append(f"{path} sleep {seconds:g}s")

    async def _c_log(self, step: dict[str, Any], path: str) -> None:
        rendered = self._render(step.get("message", ""), path, "log")
        message = _preview("" if rendered is None else rendered, 1000)
        self.shared.log.append(message)
        self.shared.trace.append(f"{path} log: {_preview(message, 160)}")
        if self.shared.job is not None:
            self.shared.job["log"].append(message)

    async def _c_fail(self, step: dict[str, Any], path: str) -> None:
        raise _Failed(str(self._render(step.get("message", "the macro failed on purpose"), path, "fail")), path, "fail")

    async def _c_return(self, step: dict[str, Any], path: str) -> None:
        raise _Return(self._render(step.get("value"), path, "return"))

    async def _c_break(self, step: dict[str, Any], path: str) -> None:
        raise _Break()

    async def _c_continue(self, step: dict[str, Any], path: str) -> None:
        raise _Continue()

    async def _c_call(self, step: dict[str, Any], path: str) -> None:
        name = str(self._render(step.get("name"), path, "call"))
        if name in self.call_stack or len(self.call_stack) >= CALL_DEPTH:
            raise _Failed(f"call '{name}' would nest too deep or loop: {' > '.join(self.call_stack + (name,))}",
                          path, "call")
        root = step.get("project_root") or self.project_root
        try:
            record = await asyncio.to_thread(load_script, name, root)
        except ValueError as exc:
            raise _Failed(str(exc), path, "call") from None
        try:
            scope = _resolve_scope(record, self._render(step.get("variables") or {}, path, "call"))
        except ValueError as exc:
            raise _Failed(f"call '{name}': {exc}", path, "call") from None
        child = Runner(self.facade, scope, summary=self.summary, continue_on_error=self.continue_on_error,
                       session_override=self.session_override, project_root=root,
                       call_stack=self.call_stack + (name,), shared=self.shared, prefix=f"{path}>{name}:")
        self.shared.trace.append(f"{path} call {name}")
        returned: Any = _MISSING
        try:
            await child.block(record["steps"], child.prefix)
        except _Return as exc:
            returned = exc.value
        except _Failed as exc:
            raise _Failed(f"macro '{name}' failed at {exc.path}: {exc}", path, "call") from None
        value = child.outputs if returned is _MISSING else returned
        if isinstance(step.get("save_as"), str):
            self.scope[step["save_as"]] = value
            self.outputs[step["save_as"]] = value
        self.scope["last"] = {"ok": True, "value": value, "error": None, "action": "call", "step": path}


def load_script(name: str, project_root: str | None) -> dict[str, Any]:
    """A saved macro with only the variables its file declares.

    ``macros.load`` declares every placeholder it finds (as null) so op=show can list them; a
    script reads null as "no value", so an input the caller forgot would run as null instead of
    being reported. Here only what the author wrote in ``variables`` counts as declared.
    """
    record = macros.load(name, project_root)
    return {**record, "variables": macro_check._declared_variables(macros.raw_payload(name, project_root)) or {}}


def _resolve_scope(record: dict[str, Any], values: dict[str, Any] | None) -> dict[str, Any]:
    """Declared variables and supplied values; in a script a declared null is a real "no value".

    A plain replay treats null as missing (it would send "None" into the page). A script can ask
    about it instead - ``"when": "thumbnail"`` - so a variable the file declares, or the caller
    passes, exists even when it is null. Only a name nobody declared, passed or produces is missing.
    """
    scope = dict(record.get("variables") or {})
    scope.update(values or {})
    missing = sorted(macro_check.inputs_needed(record["steps"]) - set(scope) - set(macro_expr.CONSTANTS))
    if missing:
        raise ValueError(f"macro needs value(s) for {missing}. Pass them as variables, for example "
                         f"{{\"{missing[0]}\": \"...\"}}.")
    return scope


async def run_record(facade: Any, record: dict[str, Any], values: dict[str, Any] | None, *,
                     continue_on_error: bool = False, session_id: str | None = None, project_root: str | None = None,
                     background: bool = False, timeout_seconds: float | None = None,
                     summary: str = "full") -> dict[str, Any]:
    """Run a macro (file or inline steps) through the script runner, now or as a background job."""
    problems = macro_check.flow_problems(record["steps"])
    if problems:
        raise ValueError(f"macro '{record.get('name')}' has {len(problems)} script error(s), first: "
                         f"{problems[0]['index']}: {problems[0]['error']}. op='validate' lists them all.")
    scope = _resolve_scope(record, values)
    deadline = time.monotonic() + float(timeout_seconds) if timeout_seconds else None
    name = record.get("name")

    def runner(job: dict[str, Any] | None) -> Runner:
        return Runner(facade, scope, summary=summary, continue_on_error=continue_on_error,
                      session_override=session_id, project_root=project_root, call_stack=(name,) if name else (),
                      shared=_Shared(job, deadline))

    if background:
        job = macro_jobs.new(name, session=session_id)
        view = macro_jobs.launch(job, lambda: runner(job).run(record["steps"]))
        return {"success": True, "background": True, "macro": name, **view,
                "note": "Started in the background. op='job' job_id=<id> reads progress; wait_seconds waits for it."}
    outcome = await runner(None).run(record["steps"])
    return {**outcome, "macro": name}


def preview(record: dict[str, Any], values: dict[str, Any] | None) -> dict[str, Any]:
    """A script macro's preview: inputs checked, known templates filled, the outline to read."""
    scope = _resolve_scope(record, values)
    return {"flow": True, "steps": macro_check.partial_render(record["steps"], scope),
            "outline": macro_check.outline(record["steps"]),
            "inputs": sorted(macro_check.inputs_needed(record["steps"])),
            "produced": sorted(macro_check.produced_names(record["steps"]))}
