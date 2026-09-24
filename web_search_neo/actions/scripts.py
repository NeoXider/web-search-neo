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
            error = describe_error(exc)
            if any(marker in error.lower() for marker in UNSERIALISABLE_MARKERS):
                error += (" - the returned value cannot cross as JSON (cyclic, or a "
                          "window/DOM object); return a plain object of the fields you need.")
            return {**page_summary(), "success": False,
                    "error": error, "attempts": attempt}
        safe = clip_result(json_safe(value))
        answer = {**page_summary(), "success": True,
                  "value": safe, "value_json": value_json(safe),
                  "value_type": value_type(safe), "attempts": attempt}
        if safe is None and "return" not in script:
            answer["value_note"] = (
                "The script returned nothing: it is a function body, so end it with "
                "`return <value>;` (an expression alone yields null)."
            )
        return answer
    raise AssertionError("at least one script attempt must run")
