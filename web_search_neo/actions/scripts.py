"""JavaScript evaluation, optional retries, and bounded result reporting."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

MAX_SCRIPT_RESULT_CHARS = 200_000
RETRYABLE_SCRIPT_ERRORS = (
    "uncaught", "cannot find context", "no such context", "detached",
    "target crashed", "session closed", "disconnected",
    "execution context was destroyed", "cannot access before initialization",
)


def clip_result(value: Any) -> Any:
    """Bound script strings while reporting their original size."""
    if isinstance(value, str) and len(value) > MAX_SCRIPT_RESULT_CHARS:
        return {"clipped": True, "length": len(value),
                "head": value[:MAX_SCRIPT_RESULT_CHARS]}
    return value


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
    """CDP evaluation with independent promise and user-gesture controls."""
    expression = f"(function() {{\n{script}\n}}).apply(null, {json.dumps(args or [])})"
    params = {
        "expression": expression, "returnByValue": True,
        "awaitPromise": bool(await_promise), "userGesture": bool(user_gesture),
    }
    if timeout_seconds is None:
        # No explicit override: let each backend apply its own default (plain
        # Selenium drivers do not accept a per-call CDP timeout at all).
        result = driver.execute_cdp_cmd("Runtime.evaluate", params)
    else:
        result = driver.execute_cdp_cmd("Runtime.evaluate", params, timeout=timeout_seconds)
    if result.get("exceptionDetails"):
        raise RuntimeError(exception_text(result["exceptionDetails"]))
    return (result.get("result") or {}).get("value")


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
            if user_gesture or await_promise:
                value = evaluate_with_gesture(
                    driver, script, args, await_promise, user_gesture=user_gesture,
                    timeout_seconds=timeout_seconds,
                )
            else:
                value = driver.execute_script(script, *(args or []))
        except ValueError:
            raise
        except Exception as exc:
            if retry_on_uncaught and attempt < max_attempts and error_is_retryable(exc):
                time.sleep(max(0.0, float(retry_delay_ms)) / 1000.0)
                continue
            return {**page_summary(), "success": False,
                    "error": describe_error(exc), "attempts": attempt}
        return {**page_summary(), "success": True,
                "value": clip_result(value), "attempts": attempt}
    raise AssertionError("at least one script attempt must run")
