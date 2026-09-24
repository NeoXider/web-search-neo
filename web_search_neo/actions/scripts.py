"""JavaScript evaluation, optional retries, and bounded result reporting."""
from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any

from web_search_neo.actions import repl

logger = logging.getLogger(__name__)
MAX_SCRIPT_RESULT_CHARS = 200_000
RETRYABLE_SCRIPT_ERRORS = (
    "uncaught", "cannot find context", "no such context", "detached",
    "target crashed", "session closed", "disconnected",
    "execution context was destroyed", "cannot access before initialization",
)

UNSERIALISABLE_MARKERS = (
    "reference chain is too long", "couldn't be returned by value",
    "could not be serialized", "circular", "cyclic",
)


def clip_result(value: Any) -> Any:
    """Bound script strings while reporting their original size."""
    if isinstance(value, str) and len(value) > MAX_SCRIPT_RESULT_CHARS:
        return {"clipped": True, "length": len(value),
                "head": value[:MAX_SCRIPT_RESULT_CHARS]}
    return value


def json_safe(value: Any, _depth: int = 0) -> Any:
    """Replace live handles with descriptors so the result always serialises.

    A script that returns a DOM node hands Selenium a WebElement, and the MCP
    layer then had to guess how to carry it - the answer came back as an
    object one call and as content parts the next, and a naive String() of an
    object value read "[object Object]". Sanitising here fixes one contract:
    ``value`` is always plain JSON, ``value_json`` always its string form.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _depth > 20:
        return "[max depth]"
    if isinstance(value, dict):
        return {str(key): json_safe(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item, _depth + 1) for item in value]
    tag = getattr(value, "tag_name", None)
    if tag is not None:
        return {
            "element": str(tag).lower(),
            "note": "DOM elements are not serialisable; query their properties instead",
        }
    return {"unserialisable": f"{type(value).__name__}: {value!r}"[:2000]}


def value_json(value: Any) -> str:
    """The sanitised value as one JSON string, for callers that want text."""
    return json.dumps(json_safe(value), ensure_ascii=False, default=str)


def value_type(value: Any) -> str:
    """JSON type name of a sanitised value: null|boolean|number|string|array|object."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def error_is_retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in RETRYABLE_SCRIPT_ERRORS)


def exception_text(details: dict[str, Any]) -> str:
    """Prefer CDP's specific exception message over its generic 'Uncaught'."""
    exception = details.get("exception") or {}
    message = exception.get("description") or details.get("text") or "evaluation failed"
    line = details.get("lineNumber")
    return f"{message} (line {line})" if line is not None else str(message)


def evaluate_with_gesture(
    driver: Any, script: str, args: list[Any] | None, await_promise: bool,
    user_gesture: bool = True, timeout_seconds: float | None = None,
) -> Any:
    """CDP evaluation with a user gesture; same REPL semantics as every other path."""
    expression = f"(async function() {{\n{repl.body_for(script)}\n}}).apply(null, {json.dumps(args or [])})"
    params = {
        "expression": expression, "returnByValue": True,
        "awaitPromise": True, "userGesture": bool(user_gesture),
    }
    if timeout_seconds is None:
        # No explicit override: let each backend apply its own default (plain
        # Selenium drivers do not accept a per-call CDP timeout at all).
        result = driver.execute_cdp_cmd("Runtime.evaluate", params)
    else:
        client_config = getattr(getattr(driver, "command_executor", None), "client_config", None)
        if client_config is not None:
            # Selenium's CDP method has no per-call timeout keyword. Its HTTP
            # client config is per driver; the caller holds the session lock,
            # so temporarily extending it cannot affect another session.
            previous_timeout = client_config.timeout
            try:
                client_config.timeout = timeout_seconds
                result = driver.execute_cdp_cmd("Runtime.evaluate", params)
            finally:
                client_config.timeout = previous_timeout
        else:
            # The companion bridge implements a native per-call deadline.
            result = driver.execute_cdp_cmd("Runtime.evaluate", params, timeout=timeout_seconds)
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        raise repl.ScriptError(exception_text(details), syntax=did_not_compile(details))
    return (result.get("result") or {}).get("value")


def did_not_compile(details: dict[str, Any]) -> bool:
    """A SyntaxError the script's source raised, not one its code threw while running.

    The wrapper is an async function, so an error thrown while the script runs
    (``JSON.parse("x")`` is a SyntaxError too) comes back as a rejected promise -
    CDP says "Uncaught (in promise)". Only a source that did not parse fails
    before the promise exists, and only that one is not worth running again.
    """
    if "in promise" in str(details.get("text") or "").lower():
        return False
    exception = details.get("exception") or {}
    return exception.get("className") == "SyntaxError" or "SyntaxError" in exception_text(details)


DEFAULT_SCRIPT_SECONDS = 15.0


def _run_repl(driver: Any, script: str, args: list[Any] | None,
              timeout_seconds: float | None) -> tuple[Any, bool]:
    """The asynchronous-script path under one hard deadline.

    The script timeout is set for this call, and on Selenium the HTTP read
    timeout to chromedriver is bounded just above it: a script stuck in an
    endless loop used to hold the call - and the session lock that status and
    close wait on - for chromedriver's 120 s transport timeout.
    """
    limit = min(max(float(timeout_seconds or DEFAULT_SCRIPT_SECONDS), 1.0), 600.0)
    client_config = getattr(getattr(driver, "command_executor", None), "client_config", None)
    previous_http = getattr(client_config, "timeout", None)
    hung = False
    try:
        if hasattr(driver, "set_script_timeout"):
            driver.set_script_timeout(limit)
        if client_config is not None:
            client_config.timeout = limit + 5.0
        answer = repl.run(driver, script, args)
        _set_hung(driver, False)  # a driver that answered is not frozen (any more)
        _set_flag(driver, "_wsn_slow", False)
        return answer
    except repl.ScriptError:
        raise
    except Exception as exc:
        if transport_timed_out(exc):
            # chromedriver itself did not answer within limit + 5 s: the renderer is
            # blocked by the script, every further command would wait too, so the
            # driver is marked and close() stops that browser hard.
            hung = True
            _set_hung(driver, True)
            raise repl.ScriptError(timed_out=True, message=(
                f"The script ran past its {limit:g} s limit and the page stopped answering "
                "(an endless loop). The tab stays frozen: close the session (its browser is "
                "stopped outright) and open the page again.")) from exc
        if _script_timed_out(exc):
            # A slow promise or a companion call that ran out of time: the browser is
            # alive, nothing is killed; teardown only keeps its page-side steps short.
            _set_flag(driver, "_wsn_slow", True)
            raise repl.ScriptError(timed_out=True, message=(
                f"The script did not settle within its {limit:g} s limit (timeout_seconds). "
                "The page is still responsive; raise timeout_seconds (max 600) for a slow "
                "await.")) from exc
        raise
    finally:
        if client_config is not None and previous_http is not None:
            client_config.timeout = previous_http
        if hasattr(driver, "set_script_timeout") and not hung:
            try:
                driver.set_script_timeout(DEFAULT_SCRIPT_SECONDS)
            except Exception:
                pass


def transport_timed_out(exc: BaseException) -> bool:
    """chromedriver's HTTP answer never came (urllib3 ReadTimeoutError), not a script timeout."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names = {klass.__name__ for klass in type(current).__mro__}
        if names & {"ReadTimeoutError", "ReadTimeout"} or "read timed out" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _script_timed_out(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "timed out" in text or "timeout" in text


def _set_hung(driver: Any, value: bool) -> None:
    _set_flag(driver, "_wsn_hung", value)


def _set_flag(driver: Any, name: str, value: bool) -> None:
    try:
        setattr(driver, name, value)
    except Exception:
        pass


TEARDOWN_SECONDS = 6.0
TEARDOWN_COMMAND_SECONDS = 2.0


def teardown_deadline(driver: Any) -> float | None:
    """A shared deadline for page-side teardown after a script timed out; None otherwise.

    The per-command limit drops to ``TEARDOWN_COMMAND_SECONDS`` too, so the steps
    that do run (key release, removing injected state) cannot each wait out the
    full script limit on a page that stopped answering. Detaching the debugger,
    closing the tab and releasing the claim are never skipped.
    """
    if not (getattr(driver, "_wsn_slow", False) or getattr(driver, "_wsn_hung", False)):
        return None
    if getattr(driver, "is_extension_bridge", False):  # local on the companion driver; a
        try:  # Selenium call here would itself wait on the frozen chromedriver
            driver.set_script_timeout(TEARDOWN_COMMAND_SECONDS)
        except Exception:
            pass
    return time.monotonic() + TEARDOWN_SECONDS


class PageSteps:
    """Page-side teardown commands under one shared deadline, none skipped in silence.

    The deadline used to be checked only between the steps of a teardown, so a
    step that had started - removing a dozen new-document scripts, say - went on
    asking a page that had stopped answering long after time ran out. Each
    command is now its own step: past the deadline it is not sent and is named
    in ``problems``; a command that fails is named there too.
    """

    def __init__(self, deadline: float | None) -> None:
        self.deadline = deadline
        self.problems: list[str] = []

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def __call__(self, step: str, action: Callable[[], Any]) -> bool:
        if self.expired():
            self.problems.append(f"{step} skipped: the page stopped answering and teardown ran out of time")
            return False
        try:
            action()
        except Exception as exc:
            self.problems.append(f"{step} failed ({type(exc).__name__}: {exc})")
            logger.warning("Browser session cleanup: %s failed: %s: %s", step, type(exc).__name__, exc)
        return True


def clear_target_state(session: Any, step: PageSteps) -> None:
    """Drop the extra headers and every new-document script from a tab that outlives us.

    They live on the Chrome target, not on the session. One removal per step, so
    the shared deadline is honoured inside the loop; what the deadline left on
    the tab is counted in ``step.problems``. The session forgets them either way.
    """
    driver = session.driver
    if session.extra_headers:
        step("clearing the extra request headers",
             lambda: driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {}}))
        session.extra_headers = {}
    pending = list(session.injected_scripts)
    for index, identifier in enumerate(pending):
        if step.expired():
            step.problems.append(
                f"removing {len(pending) - index} of {len(pending)} script(s) that run on every new "
                "document skipped: the page stopped answering and teardown ran out of time")
            break
        step(f"removing new-document script {identifier}", lambda identifier=identifier: driver.execute_cdp_cmd(
            "Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier}))
    session.injected_scripts.clear()
    session.stealth_identifier = None


def execute(
    driver: Any, script: str, args: list[Any] | None = None, *,
    await_promise: bool = False, user_gesture: bool = False,
    retry_on_uncaught: bool = False, retries: int = 2,
    retry_delay_ms: int = 300, wait_ready: bool = False,
    timeout_seconds: float | None = None,
    page_summary: Callable[[], dict[str, Any]],
    wait_until_ready: Callable[[Any, float], Any],
    describe_error: Callable[[Exception], str],
) -> dict[str, Any]:
    """Execute under the caller's session lock; never replay by default.

    Readiness, page reporting, and error formatting are injected explicitly.
    Retrying is opt-in because a thrown error may follow a completed mutation.
    """
    max_attempts = 1 + max(0, int(retries)) if retry_on_uncaught else 1
    for attempt in range(1, max_attempts + 1):
        try:
            if wait_ready and attempt == 1:
                try:
                    if driver.execute_script("return document.readyState") not in (
                        None, "complete", "interactive",
                    ):
                        wait_until_ready(driver, 2.0)
                except Exception:
                    pass
            if (await_promise or user_gesture) and not hasattr(driver, "execute_cdp_cmd"):
                raise ValueError("await_promise/user_gesture requires a CDP-capable browser backend")
            returned_undefined = False
            if user_gesture or await_promise:
                # CDP evaluation: a user gesture, or a long await whose deadline has to
                # reach the transport too. Same REPL semantics as the path below.
                value = evaluate_with_gesture(
                    driver, script, args, True, user_gesture=user_gesture,
                    timeout_seconds=timeout_seconds,
                )
            elif hasattr(driver, "execute_async_script"):
                # await works at top level with or without await_promise now.
                value, returned_undefined = _run_repl(driver, script, args, timeout_seconds)
            else:  # a minimal stand-in driver: plain synchronous body
                value = driver.execute_script(script, *(args or []))
        except repl.ScriptError as exc:
            if exc.timed_out:  # the tab may still be frozen: do not ask it for a summary
                return {"success": False, "error": str(exc), "attempts": attempt, "timed_out": True}
            if exc.syntax or not (retry_on_uncaught and attempt < max_attempts):
                return {**page_summary(), "success": False, "error": str(exc),
                        "attempts": attempt, **({"syntax_error": True} if exc.syntax else {})}
            time.sleep(max(0.0, float(retry_delay_ms)) / 1000.0)
            continue
        except ValueError:
            raise
        except Exception as exc:
            if retry_on_uncaught and attempt < max_attempts and error_is_retryable(exc):
                time.sleep(max(0.0, float(retry_delay_ms)) / 1000.0)
                continue
            error = describe_error(exc)
            if any(marker in error.lower() for marker in UNSERIALISABLE_MARKERS):
                error += (" - the returned value cannot cross as JSON (cyclic, or a "
                          "window/DOM object); return a plain object of the fields you need.")
            return {**page_summary(), "success": False,
                    "error": error, "attempts": attempt}
        safe = json_safe(value)  # the public tools window it (script_results), with a flag
        answer = {**page_summary(), "success": True,
                  "value": safe, "value_json": value_json(safe),
                  "value_type": value_type(safe), "attempts": attempt}
        if safe is None and (returned_undefined or "return" not in script):
            answer["value_note"] = (
                "The script returned nothing. It is the body of an async function: end it "
                "with `return <value>;`, or send a one-line expression without `return`."
            )
        return answer
    raise AssertionError("at least one script attempt must run")


def stop_hung_driver(session: Any) -> dict[str, Any] | None:
    """Tear down a browser frozen by an endless page script, fast; None otherwise.

    Only a browser this server launched, whose chromedriver stopped answering (the
    transport timed out - see ``transport_timed_out``), is killed: every polite
    teardown step would wait on chromedriver, which waits on the frozen page. The
    user's own Chrome (``current``), an attached browser, or a driver without a
    process is never killed - the ordinary teardown runs for those, so the
    debugger, headers, mocks and the tab claim are all released.

    A chromedriver that has already exited is not killed either: its pid may
    belong to another program by now. Only its temporary profile is removed.
    What could not be done comes back in ``problem``, never as a clean stop.
    """
    driver = session.driver
    if not getattr(driver, "_wsn_hung", False):
        return None
    if session.profile_mode in {"current", "attach"} or not getattr(session, "owns_browser", True):
        return None
    process = getattr(getattr(driver, "service", None), "process", None)
    pid = getattr(process, "pid", None)
    if not pid:
        return None
    try:
        profile_dir = str(((driver.capabilities or {}).get("chrome") or {}).get("userDataDir") or "")
    except Exception:
        profile_dir = ""
    exited = _has_exited(process)
    kill_problem = None if exited else _kill_tree(pid, process)
    left = _remove_scoped_profile(profile_dir, session.profile_mode)
    problems = [kill_problem, f"its temporary profile could not be removed: {left}" if left else None]
    if exited:
        forced = ("the page was frozen by a script and its chromedriver had already exited, so "
                  "nothing was killed (its process id may belong to another program now)")
    elif kill_problem:
        forced = "the page was frozen by a script; its browser could not be stopped completely"
    else:
        forced = "the page was frozen by a script, so its browser was stopped outright"
    return {"tab_closed": True, "browser_gone": False,
            "problem": "; ".join(item for item in problems if item) or None, "forced": forced}


def close_one(
    session: Any, shutdown: Callable[[], dict[str, Any]], *,
    release_claim: Callable[[], Any], forget: Callable[[], Any], lock_timeout: float | None = None,
) -> dict[str, Any] | None:
    """Every close path: a frozen browser is stopped outright, anything else shut down politely.

    ``close_session`` used to be the only path that asked ``stop_hung_driver``;
    ``close_all``, the idle release and process exit went straight to the polite
    teardown and waited out chromedriver's 120 s on the frozen page, leaving its
    Chrome and ``scoped_dir`` behind. The forced stop runs without the session
    lock - a hung call may hold it - and the claim and mock registries still let
    go. None when ``lock_timeout`` ran out before the lock was free.
    """
    forced = stop_hung_driver(session)
    if forced is not None:
        try:
            release_claim()
        finally:
            forget()
        return forced
    if lock_timeout is None:
        with session.lock:
            return shutdown()
    if not session.lock.acquire(timeout=lock_timeout):
        return None
    try:
        return shutdown()
    finally:
        session.lock.release()


def _has_exited(process: Any) -> bool:
    try:
        return callable(getattr(process, "poll", None)) and process.poll() is not None
    except Exception:
        return False


def _kill_tree(pid: int, process: Any) -> str | None:
    """Kill chromedriver and everything it started (Chrome, its renderers, the frozen tab).

    Windows: ``taskkill /T``. POSIX: the descendants are collected first (a killed
    parent orphans them, and they could no longer be found) - with ``pgrep -P``,
    or from ``/proc`` where there is no pgrep - then every one is killed; a
    process group is not used, because chromedriver shares the server's own group.
    Returns what could not be done, so a partial kill is never reported as a stop.
    """
    problem: str | None = None
    try:
        if os.name == "nt":
            done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
                                  timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode != 0:
                problem = (f"taskkill exited with {done.returncode} for chromedriver {pid}: "
                           f"{(done.stderr or done.stdout or '').strip()[:300]}")
        else:
            problem = _kill_posix_tree(int(pid))
    except (OSError, subprocess.SubprocessError) as exc:
        problem = (f"the browser started by chromedriver {pid} could not be stopped "
                   f"({type(exc).__name__}: {exc})")
    try:
        process.kill()
    except Exception:
        pass
    if problem:
        logger.warning("Forced stop of a frozen browser: %s", problem)
    return problem


def _kill_posix_tree(pid: int) -> str | None:
    family: list[int] = []
    frontier, unlisted = [pid], []
    while frontier and len(family) < 512:
        parent = frontier.pop()
        family.append(parent)
        children = _child_pids(parent)
        if children is None:
            unlisted.append(parent)
        else:
            frontier.extend(children)
    for member in reversed(family):
        try:
            os.kill(member, signal.SIGKILL)
        except OSError:
            pass
    if not unlisted:
        return None
    return (f"the child processes of {unlisted} could not be listed (no pgrep and no /proc), so only "
            f"{len(family)} process(es) were killed; Chrome may still be running")


PROC_ROOT = "/proc"


def _child_pids(parent: int) -> list[int] | None:
    """The direct children of ``parent``; None when neither pgrep nor /proc can tell."""
    try:
        listed = subprocess.run(["pgrep", "-P", str(parent)], capture_output=True, text=True, timeout=5)
        if listed.returncode in (0, 1):  # 1 = no children, which is an answer too
            return [int(line) for line in listed.stdout.split() if line.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return _proc_child_pids(parent)


def _proc_child_pids(parent: int) -> list[int] | None:
    """Children from ``/proc/<pid>/task/*/children``, else from the ppid in every ``/proc/*/stat``."""
    own = os.path.join(PROC_ROOT, str(parent))
    listings = glob.glob(os.path.join(own, "task", "*", "children"))
    if listings:
        try:
            found = []
            for listing in listings:
                with open(listing, encoding="ascii") as handle:
                    found.extend(int(item) for item in handle.read().split() if item.isdigit())
            return found
        except (OSError, ValueError):
            pass
    if not os.path.exists(os.path.join(own, "stat")):
        return None
    found = []
    for stat in glob.glob(os.path.join(PROC_ROOT, "[0-9]*", "stat")):
        try:
            with open(stat, encoding="utf-8", errors="replace") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()  # the name may hold spaces and ')'
        except (OSError, IndexError):
            continue
        if len(fields) > 1 and fields[1] == str(parent):
            found.append(int(os.path.basename(os.path.dirname(stat))))
    return found


def _remove_scoped_profile(profile_dir: str, profile_mode: str = "temporary") -> str | None:
    """Delete chromedriver's own temporary profile of a killed browser.

    chromedriver removes it on a normal quit; a killed chromedriver cannot. The
    name alone proves nothing - a persistent ``profile_id`` may be spelled
    ``scoped_dir_work`` - so the profile is removed only for a temporary or
    isolated session, only when it is called ``scoped_dir*``, and only when it lies
    inside the system temp directory (compared after resolving links). Returns
    the path when it is still there after a few tries (files may stay locked).
    """
    if profile_mode not in {"temporary", "isolated"} or not profile_dir:
        return None
    try:
        resolved = Path(profile_dir).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return None
    if not resolved.name.startswith("scoped_dir") or temp_root not in resolved.parents:
        return None
    profile_dir = str(resolved)
    for _ in range(10):
        shutil.rmtree(profile_dir, ignore_errors=True)
        if not os.path.exists(profile_dir):
            return None
        time.sleep(0.3)
    return profile_dir
