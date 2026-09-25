"""MCP wrappers for the 1.20 site checks: security_report, perf_report, har_export, test_run.

Like ``extra_actions``, these live outside the size-ratcheted facade and are
registered on the same legacy FastMCP instance main's action table resolves
schemas on. The analysis itself is pure and lives in ``web_search_neo/audit``;
this module only opens and closes sessions, reads the browser and dispatches.

``test_run`` replays ordinary actions, so it needs main's dispatcher: main hands
its own module over in ``register`` (``bind``), which also works when main runs
as ``__main__``. Every browser read happens off the event loop.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
import time
from typing import Any, Literal

from web_search_neo import __version__, browser_tools, macros, network_log
from web_search_neo.audit import har, page as page_checks, perf, report, scenario, site
from web_search_neo.fetch.safety import write_download
from web_search_neo.perception import min_summary

_FACADE: Any = None
INLINE_HAR_CHARS = 100_000
# Set while a test_run executes: a step (or a macro it runs) cannot start another one.
_IN_TEST_RUN: contextvars.ContextVar[bool] = contextvars.ContextVar("wsn_in_test_run", default=False)


def bind(facade: Any) -> None:
    """Remember the facade module whose dispatcher test_run replays steps through."""
    global _FACADE
    _FACADE = facade


def _facade() -> Any:
    if _FACADE is None:
        raise RuntimeError("test_run needs the MCP facade; import web_search_neo.main first")
    return _FACADE


def _session_exists(session_id: str) -> bool:
    with browser_tools._sessions_lock:
        return session_id in browser_tools._sessions


def _open_isolated(url: str, session_id: str, timeout: float) -> dict[str, Any]:
    return browser_tools.open_page(url, session_id=session_id, width=1366, height=900,
                                   timeout_seconds=timeout, headless=True, profile_mode="isolated",
                                   label_tab=False)


def _script_value(session_id: str, source: str, args: list[Any] | None, timeout: float) -> Any:
    answer = browser_tools.execute_js(source, args=args, session_id=session_id,
                                      timeout_seconds=timeout, max_chars=5_000_000)
    if not answer.get("success"):
        raise RuntimeError(str(answer.get("error") or "the page script failed"))
    return answer.get("value")


def _browser_view(url: str, session_id: str, timeout: float, keep_open: bool) -> dict[str, Any]:
    """Load the page once in an isolated browser and read DOM facts, cookies and requests.

    The session stays open only when keep_open was asked for and everything
    worked; after a failure the caller gets no session_id, so it is closed.
    """
    failed = True
    try:
        _open_isolated(url, session_id, timeout)
        snapshot = _script_value(session_id, page_checks.SNAPSHOT_SCRIPT, None, 15.0)
        jar = browser_tools.cookies(op="get", session_id=session_id, limit=1000).get("cookies") or []
        session = browser_tools._get_session(session_id)
        with session.lock:
            rows, _dropped, pending = network_log.drain(session)
        failed = False
        return {"snapshot": snapshot, "cookies": jar, "network": rows + pending}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if failed or not keep_open:
            browser_tools.close_session(session_id)


async def browser_security_report(
    url: str | None = None,
    scope: Literal["page", "site", "hosts"] = "page",
    hosts: list[str] | None = None,
    paths: list[str] | None = None,
    include_subdomains: bool = False,
    max_pages: int = 10,
    max_depth: int = 2,
    delay_ms: int = 500,
    respect_robots: bool = True,
    session_id: str | None = None,
    browser: bool = True,
    keep_open: bool = False,
    timeout_seconds: float = 20.0,
    save_to: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Grade your site's security configuration with Mozilla HTTP Observatory's tests, a fix per finding.

    scope='page' (default) checks url; 'site' crawls links and sitemap.xml of
    your hosts (max_pages <= 50, delay_ms, robots.txt); 'hosts' checks each named
    origin (<= 10). paths are your own routes to check too. Requests go only to
    hosts in scope and are all listed in requests_made. save_to writes the full
    report as JSON (download folder).
    """
    sid = session_id or f"security-report-{secrets.token_hex(3)}"
    if browser and scope == "page" and _session_exists(sid):
        raise ValueError(f"security_report opens its own isolated session; '{sid}' is already open - "
                         "close it or pass another session_id.")

    def page_report(target: str, http_view: dict[str, Any]) -> dict[str, Any]:
        view = _browser_view(target, sid, timeout, keep_open) if browser and not http_view.get("error") else None
        result = report.build(target, http_view, view)
        if view is not None and keep_open and not view.get("error"):
            result["session_id"] = sid
        return result

    timeout = max(1.0, min(float(timeout_seconds), 120.0))

    def run() -> dict[str, Any]:
        result = site.run(url, mode=scope, hosts=hosts, paths=paths, include_subdomains=include_subdomains,
                          max_pages=max_pages, max_depth=max_depth, delay_ms=delay_ms,
                          respect_robots=respect_robots, timeout=timeout,
                          page_report=page_report if scope == "page" else None)
        if save_to:
            text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
            path = write_download(save_to, text.encode("utf-8"), overwrite=bool(overwrite))
            result = {**result, "saved_to": str(path)}
        return result
    return await asyncio.to_thread(run)


async def browser_perf_report(
    url: str | None = None,
    session_id: str | None = None,
    wait_seconds: float = 5.0,
    keep_open: bool = False,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Load metrics from the Performance API: TTFB, FCP, LCP, CLS, resources, render-blocking.

    With url and no open session_id: a cold load in a fresh isolated browser,
    closed afterwards unless keep_open. With session_id alone: the page already
    open there. wait_seconds bounds the wait for the load event.
    """
    if url is None and not session_id:
        raise ValueError("perf_report needs url (a fresh isolated load) or the session_id of an open page")
    sid = session_id or f"perf-report-{secrets.token_hex(3)}"

    def run() -> dict[str, Any]:
        opened = False
        timeout = max(1.0, min(float(timeout_seconds), 120.0))
        if url is not None:
            with browser_tools._sessions_lock:
                live = browser_tools._sessions.get(sid)
            if live is not None:  # the open session's own browser and profile, warm cache
                browser_tools.open_page(url, session_id=sid, timeout_seconds=timeout,
                                        profile_mode=live.profile_mode, profile_id=live.profile_id,
                                        debugger_address=live.debugger_address)
            else:
                _open_isolated(url, sid, timeout)
                opened = True
        failed = True
        try:
            wait = max(0.0, min(float(wait_seconds), 30.0))
            raw = _script_value(sid, perf.PERF_SCRIPT, [int(wait * 1000)], wait + 15.0)
            shaped = perf.shape(raw if isinstance(raw, dict) else {})
            failed = False
            return {"success": True, "session_id": sid if not opened or keep_open else None,
                    "fresh_isolated_load": opened, **shaped}
        finally:
            if opened and (failed or not keep_open):  # a failed run returns no session_id to close later
                browser_tools.close_session(sid)
    return await asyncio.to_thread(run)


async def browser_har_export(
    session_id: str = "default",
    save_to: str | None = None,
    overwrite: bool = False,
    third_party_only: bool = False,
    include_pending: bool = True,
    inline: bool = False,
) -> dict[str, Any]:
    """Write the session's network journal as a HAR 1.2 file (download folder).

    inline=true also returns the document when it is under 100000 characters.
    The journal keeps no request headers and no bodies; the HAR comment says so.
    """
    def run() -> dict[str, Any]:
        session = browser_tools._get_session(session_id)
        with session.lock:
            rows, dropped, pending = network_log.drain(session)
            page_url = network_log.page_url(session)
            title = str(getattr(session, "last_title", "") or "")
        selected = rows + (pending if include_pending else [])
        party: dict[str, Any] = {}
        if third_party_only:
            selected, party = network_log.third_party_rows(selected, page_url)
        document = har.build(selected, page_url=page_url, title=title, version=__version__, dropped=dropped)
        text = json.dumps(document, ensure_ascii=False, indent=1)
        name = save_to or f"{session_id}-{time.strftime('%Y%m%d-%H%M%S')}.har"
        path = write_download(name, text.encode("utf-8"), overwrite=bool(overwrite))
        answer: dict[str, Any] = {
            "success": True, "session_id": session_id, "saved_to": str(path), "entries": len(selected),
            "in_flight": sum(1 for row in selected if row.get("done") is False), "dropped": dropped,
            "bytes": len(text.encode("utf-8")), "page_url": page_url, "note": har.LIMITATIONS, **party,
        }
        if inline and len(text) <= INLINE_HAR_CHARS:
            answer["har"] = document
        elif inline:
            answer["inline_refused"] = (f"The HAR is {len(text)} characters (inline limit {INLINE_HAR_CHARS}); "
                                        "read the file at saved_to.")
        return answer
    return await asyncio.to_thread(run)


def _console_seq(session_id: str) -> int:
    try:
        return int(browser_tools.get_console(session_id=session_id, limit=1, order="desc").get("history_seq") or 0)
    except Exception:
        return 0


def _new_console_errors(session_id: str, since_seq: int) -> list[str]:
    answer = browser_tools.get_console(session_id=session_id, levels=["error"], since_seq=since_seq, limit=20)
    return [str(entry.get("text") or entry.get("message") or entry)[:300] for entry in answer.get("entries") or []]


def _new_failed_requests(session_id: str, since_ms: float) -> list[str]:
    answer = browser_tools.get_network(session_id=session_id, only_errors=True, output="json", limit=500)
    return [f"{row.get('method', 'GET')} {row.get('status') or row.get('error') or 'failed'} {row.get('url', '')}"[:300]
            for row in answer.get("requests") or [] if float(row.get("ts") or 0) >= since_ms]


async def _dispatch(action: dict[str, Any]) -> dict[str, Any]:
    outcome = await _facade()._execute_actions([dict(action)], False)
    return outcome["results"][0]


async def _run_check(check: dict[str, Any], session_id: str, marks: dict[str, float]) -> dict[str, Any]:
    if "action" in check:
        result = await _dispatch(check["action"])
        return scenario.judged(check, result.get("success") is True, result.get("error"))
    if check["read"] == "console":
        errors = await asyncio.to_thread(_new_console_errors, session_id, int(marks["console_seq"]))
        return scenario.judged(check, not errors, errors)
    failures = await asyncio.to_thread(_new_failed_requests, session_id, marks["started_ms"])
    return scenario.judged(check, not failures, failures)


async def _run_step(step: dict[str, Any], session_id: str, include_data: bool) -> dict[str, Any]:
    started = time.monotonic()
    marks = {"started_ms": time.time() * 1000 - 50,
             "console_seq": await asyncio.to_thread(_console_seq, session_id) if step["needs_console"] else 0}
    record: dict[str, Any] = {"index": step["index"], "name": step["name"], "action": step["action_name"]}
    checks: list[dict[str, Any]] = []
    action_ok = True
    if step["action"] is not None:
        result = await _dispatch(step["action"])
        action_ok = result.get("success") is True
        checks.append(scenario.action_verdict(step, action_ok, result.get("error")))
        if include_data and "data" in result:
            record["data"] = min_summary.minimize(result["data"])
    if action_ok or step["expect_failure"]:
        for check in scenario.checks_for(step, session_id):
            checks.append(await _run_check(check, session_id, marks))
    record["checks"] = checks
    record["status"] = "passed" if all(c["passed"] for c in checks) else "failed"
    failed = next((c for c in checks if not c["passed"]), None)
    if failed:
        record["error"] = scenario.describe(failed)
    record["duration_ms"] = round((time.monotonic() - started) * 1000)
    return record


async def browser_test_run(
    steps: list[dict[str, Any]],
    session_id: str = "test",
    url: str | None = None,
    stop_on_failure: bool = True,
    screenshot_on_failure: bool = False,
    keep_open: bool = False,
    timeout_seconds: float = 5.0,
    include_data: bool = False,
) -> dict[str, Any]:
    """Run a scenario - actions plus expect checks per step - and report pass/fail per step.

    A step is an ordinary action object with optional "step_name" and "expect"
    ({selector, absent, text, no_text, url_contains, title_contains, script,
    no_console_errors, no_failed_requests, action_fails, timeout_seconds}), or an
    expect-only step; every step is validated before the first runs. url opens
    the session first (isolated when it is new).
    """
    facade = _facade()
    if _IN_TEST_RUN.get():
        raise ValueError("test_run cannot run inside another test_run (directly or through a macro)")
    plan = scenario.plan(steps, session_id, facade, default_timeout=timeout_seconds)
    await asyncio.to_thread(_refuse_nested_macros, plan)
    token = _IN_TEST_RUN.set(True)
    try:
        return await _run_plan(plan, session_id, url, stop_on_failure, screenshot_on_failure, keep_open, include_data)
    finally:
        _IN_TEST_RUN.reset(token)


def _refuse_nested_macros(plan: list[dict[str, Any]]) -> None:
    """A macro step whose saved steps start a test_run is refused before the first step runs.

    A macro that does not exist yet (an earlier step may save it) is left to the
    replay; the runtime guard (``_IN_TEST_RUN``) still refuses the nesting then.
    """
    for step in plan:
        action = step.get("action") or {}
        if step.get("action_name") != "macro" or str(action.get("op") or "").lower() != "run" or not action.get("name"):
            continue
        try:
            record = macros.load(str(action["name"]), action.get("project_root"))
        except (ValueError, OSError):
            continue
        for inner in record.get("steps") or []:
            if str(inner.get("action") or "").strip().lower() in scenario.FORBIDDEN_IN_STEPS:
                raise ValueError(f"step {step['index']}: macro '{record['name']}' runs a test_run, and a test_run "
                                 "cannot run inside another one")


async def _run_plan(plan: list[dict[str, Any]], session_id: str, url: str | None, stop_on_failure: bool,
                    screenshot_on_failure: bool, keep_open: bool, include_data: bool) -> dict[str, Any]:
    started = time.monotonic()
    opened_here = False
    records: list[dict[str, Any]] = []
    if url is not None:
        opened_here = not _session_exists(session_id)
        setup = {"action": "open", "url": url, "session_id": session_id,
                 **({"profile_mode": "isolated"} if opened_here else {})}
        result = await _dispatch(setup)
        if result.get("success") is not True:
            return {"success": False, "error": f"test_run could not open {url}: {result.get('error')}",
                    "session_id": session_id, "passed": 0, "failed": 0, "skipped": len(plan), "total": len(plan)}
    try:
        stopped = False
        for step in plan:
            if stopped:
                records.append({"index": step["index"], "name": step["name"], "action": step["action_name"],
                                "status": "skipped"})
                continue
            record = await _run_step(step, session_id, include_data)
            if record["status"] == "failed" and screenshot_on_failure:
                shot = await _dispatch({"action": "screenshot", "session_id": session_id, "overwrite": True,
                                        "path": f"test-run-{session_id}-step{step['index']}.png"})
                record["screenshot"] = (shot.get("data") or {}).get("path") or shot.get("error")
            records.append(record)
            stopped = record["status"] == "failed" and stop_on_failure
    finally:
        if opened_here and not keep_open:
            await asyncio.to_thread(browser_tools.close_session, session_id)
    return scenario.summarize(records, session_id, round((time.monotonic() - started) * 1000),
                              kept_open=bool(opened_here and keep_open) or not opened_here)


ACTION_SPECS = (
    ("security_report", browser_security_report, "audit", "Passive A-F security grade: page, site or hosts; fixes."),
    ("perf_report", browser_perf_report, "audit", "Load metrics: TTFB, FCP, LCP, CLS, resources, blocking."),
    ("har_export", browser_har_export, "audit", "Save the session's network journal as a HAR file."),
    ("test_run", browser_test_run, "audit", "Run steps with expect checks; pass/fail per step."),
)

__all__ = ["ACTION_SPECS", "bind", "browser_har_export", "browser_perf_report", "browser_security_report",
           "browser_test_run"]
