"""The ordered-action loop behind ``web_action``, macro replays and ``test_run``.

Moved out of ``main.py`` (a size-ratcheted compatibility facade) unchanged in
behaviour. Every function takes the facade module and reads its registry,
validator and argument models at call time, so the one table main publishes is
the one that dispatches - and a test that patches main's helpers still steers
this loop.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from web_search_neo import browser_tools, obstacle_recovery, presence_live, tab_actions
from web_search_neo.action_calls import resolve_action_call
from web_search_neo.perception import min_summary

# Repeated once after the session followed a tab Chrome replaced, because they
# only observe: a wait and a screenshot change nothing. Nothing that writes -
# reload, cookies (set/clear), scripts, input - is ever repeated on its own.
RETRY_AFTER_TAB_FOLLOW = frozenset({"wait", "screenshot"})

# Off with WEB_SEARCH_NEO_RETRY=0 (no repeats at all). A blocked observation gets
# at most this many repeats, and only while the page itself asked to wait (see
# obstacle_recovery.retry_plan for what refuses to repeat).
RETRY_LIMIT_ENV = "WEB_SEARCH_NEO_RETRY"


def retry_limit() -> int:
    """Сколько всего попыток делает один шаг: первая плюс разрешённые повторы.

    ``WEB_SEARCH_NEO_RETRY=0`` оставляет только первую попытку (0 повторов);
    ``WEB_SEARCH_NEO_RETRY=N`` даёт N повторных попыток, не больше потолка.
    Само число — это obstacle_recovery.RETRY_LIMIT_MAX повторов.
    """
    raw = os.environ.get(RETRY_LIMIT_ENV, "").strip()
    if raw in {"0", "off", "no"}:
        return 1
    try:
        repeats = max(0, min(int(raw), obstacle_recovery.RETRY_LIMIT_MAX))
    except ValueError:
        repeats = obstacle_recovery.RETRY_LIMIT_DEFAULT
    return repeats + 1


def step_session(facade: Any, tool_name: str, arguments: dict[str, Any]) -> str | None:
    """The session an action actually acted on, its schema default included.

    ``exclude_unset`` keeps a default out of the recorded step, which is right -
    but the action still ran against that default tab, and attributing it to
    "no session" would hand it to whichever recording happened to be open.
    """
    fields = facade._argument_model(tool_name).model_fields
    if "session_id" not in fields:
        return None
    value = arguments.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    default = fields["session_id"].default
    return default if isinstance(default, str) and default else None


async def mark_agent_presence(facade: Any, tool_name: str, action_name: str, arguments: dict[str, Any],
                              *, ok: bool) -> None:
    """Let the human watching the tab see that this step happened.

    Hooked in the dispatcher rather than inside each handler: there are dozens
    of handlers and one dispatcher, and a signal only as complete as the last
    action someone remembered to instrument is worse than none. Never raises
    and never blocks the result; a step without a session is not marked.
    """
    session_id = facade._step_session(tool_name, arguments)
    if not session_id:
        return
    try:
        await asyncio.to_thread(browser_tools.note_agent_activity, session_id, action_name, arguments, ok)
    except Exception:
        pass


async def lead_agent_presence(facade: Any, tool_name: str, action_name: str, arguments: dict[str, Any]) -> None:
    """Say what a slow step is doing while it runs ("Waiting for #results").

    Only ``presence_live.LEAD_ACTIONS`` - waits, navigation, the captcha - are
    led: a lead is one page round trip, which the hot-path input actions never
    pay. Never raises; the action's own mark still follows it.
    """
    if action_name not in presence_live.LEAD_ACTIONS:
        return
    session_id = facade._step_session(tool_name, arguments)
    if not session_id:
        return
    try:
        await asyncio.to_thread(presence_live.lead_action, browser_tools._get_session, session_id,
                                action_name, arguments)
    except Exception:
        pass


async def mark_page_read(topic: str, session_id: str | None, arguments: dict[str, Any] | None = None) -> None:
    """Mark a ``web_info`` page read ("Reading the page") the way an action is marked.

    For the reader topics (page_text, page_outline, element_text, page_elements,
    screen_state, game_probe) a caller may await this after a successful read.
    Never raises.
    """
    if not session_id:
        return
    try:
        await asyncio.to_thread(browser_tools.note_agent_activity, session_id, topic, dict(arguments or {}), True)
    except Exception:
        pass


async def run_following_tab(facade: Any, spec: Any, action_name: str, validated: dict[str, Any]) -> Any:
    """Run one handler; after Chrome replaced the session's tab, repeat a safe step once."""
    session_id = facade._step_session(spec.tool_name, validated)
    try:
        return await spec.handler(**validated)
    except Exception as exc:
        # Off the event loop: the translation may ask the companion about the tab.
        followed = await asyncio.to_thread(browser_tools.translate_stale_tab_error, session_id, exc)
        if not (isinstance(followed, browser_tools.SessionTabFollowed) and action_name in RETRY_AFTER_TAB_FOLLOW):
            raise followed from exc
    try:
        return await spec.handler(**validated)
    except Exception as again:
        raise (await asyncio.to_thread(browser_tools.translate_stale_tab_error, session_id, again)) from again


def _unknown_action(facade: Any, index: int, action_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": index,
        "action": action_name or None,
        "success": False,
        "error": facade._unsupported_action_error(action_name, arguments),
        "example": {"actions": [{"action": "open", "url": "https://example.com", "session_id": "s"}]},
    }


async def _run_one(facade: Any, index: int, spec: Any, action_name: str, arguments: dict[str, Any],
                   summary: str) -> dict[str, Any]:
    attempts = 0
    while True:
        attempts += 1
        started = time.monotonic()  # duration_ms: every step says what it cost
        result = await _attempt(facade, index, spec, action_name, arguments, summary)
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        if result["success"]:
            if attempts > 1:
                result["attempts"] = attempts
            return result
        plan = obstacle_recovery.retry_plan(action_name, str(result.get("error") or ""),
                                            attempts, retry_limit())
        if not plan.get("retry"):
            result["retry_stopped"] = plan
            return result
        await asyncio.sleep(plan["wait_seconds"])


async def _attempt(facade: Any, index: int, spec: Any, action_name: str, arguments: dict[str, Any],
                   summary: str) -> dict[str, Any]:
    try:
        step_summary = min_summary.step_mode(arguments, facade._argument_model(spec.tool_name).model_fields, summary)
        validated = facade._validate_arguments(spec.tool_name, f"action '{action_name}'", arguments)
        await lead_agent_presence(facade, spec.tool_name, action_name, validated)
        # Keys, typing, waits: a page may open a tab on those too (new_tabs, tab_actions.py).
        data = await tab_actions.observe_step(action_name, facade._step_session(spec.tool_name, validated),
                                              run_following_tab(facade, spec, action_name, validated))
        reported_failure = isinstance(data, dict) and data.get("success") is False
        await mark_agent_presence(facade, spec.tool_name, action_name, validated, ok=not reported_failure)
        result: dict[str, Any] = {
            "index": index,
            "action": action_name,
            "success": not reported_failure,
            "data": min_summary.minimize(data) if step_summary == "min" else data,
        }
        if reported_failure:
            result["error"] = str(data.get("error") or "Action reported success=false")
        return result
    except Exception as exc:
        # A refused step shows too, in the failure colour (dead-tab errors are translated already).
        await mark_agent_presence(facade, spec.tool_name, action_name, arguments, ok=False)
        return {
            "index": index,
            "action": action_name,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def execute_actions(facade: Any, actions: list[dict[str, Any]], continue_on_error: bool = False,
                          summary: str = "full") -> dict[str, Any]:
    """Run an ordered action list, validating each against its published schema.

    ``web_action``, a macro replay and ``test_run`` share this loop rather than
    each having their own: a replay that ran its steps down a second, laxer
    path would drift from the calls its file was validated against.
    """
    results: list[dict[str, Any]] = []
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, dict):
            raise ValueError(f"Action {index} must be an object")
        action_name, arguments, error = resolve_action_call(facade, raw_action)
        spec = facade._ACTIONS.get(action_name)
        if error is not None:
            results.append({"index": index, "action": action_name or None, "success": False, "error": error})
        elif spec is None:
            results.append(_unknown_action(facade, index, action_name, arguments))
        else:
            results.append(await _run_one(facade, index, spec, action_name, arguments, summary))
        if not results[-1]["success"] and not continue_on_error:
            break
    failures = sum(not item["success"] for item in results)
    return {
        "success": failures == 0 and len(results) == len(actions),
        "requested_count": len(actions),
        "completed_count": len(results),
        "failure_count": failures,
        "stopped_early": len(results) < len(actions),
        "results": results,
    }
