"""Server-side polling of JavaScript conditions."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from selenium.common.exceptions import TimeoutException

from .scripts import clip_result


def js_truthy(value: Any) -> bool:
    """Truthiness of a JSON value in JS; empty arrays and objects are true."""
    if isinstance(value, (dict, list)):
        return True
    if isinstance(value, float) and value != value:
        return False
    return bool(value)


def wait_for_condition(
    driver: Any, script: str, *, timeout_seconds: float = 10.0,
    poll_ms: int = 150, frame_selector: str | None = None,
    enter_frame: Callable[[Any, str | None, str], Any],
    release_frame: Callable[[Any, str | None, str], Any],
    page_summary: Callable[[], dict[str, Any]],
    describe_error: Callable[[Exception], str],
) -> dict[str, Any]:
    """Poll while the caller holds its session lock; always release the frame."""
    if not str(script or "").strip():
        raise ValueError("script must not be empty")
    timeout = max(0.1, float(timeout_seconds))
    poll = max(0.05, min(float(poll_ms) / 1000.0, 2.0))
    started = time.monotonic()
    deadline = started + timeout
    last_value: Any = None
    last_error: str | None = None
    enter_frame(driver, frame_selector, script)
    try:
        while True:
            try:
                last_value = driver.execute_script(f"return ({script});")
                last_error = None
                if js_truthy(last_value):
                    break
            except Exception as exc:
                last_error = describe_error(exc)
            now = time.monotonic()
            if now >= deadline:
                break
            time.sleep(min(poll, deadline - now))
        waited = round(time.monotonic() - started, 2)
        if js_truthy(last_value):
            return {**page_summary(), "success": True, "script": script,
                    "value": clip_result(last_value), "waited_seconds": waited,
                    "timeout_seconds": timeout, "frame_selector": frame_selector}
        raise TimeoutException(
            f"Condition was still falsy after {timeout:g}s"
            + (f" (last error: {last_error})" if last_error else "")
            + f" (waited {waited:g}s)"
        )
    finally:
        release_frame(driver, frame_selector, script)
