"""Server-side polling of JavaScript conditions."""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from selenium.common.exceptions import TimeoutException

from . import repl
from .scripts import clip_result

_ZERO_WAIT = re.compile(r"\b(after|waited) 0s\b")


def js_truthy(value: Any) -> bool:
    """Truthiness of a JSON value in JS; empty arrays and objects are true."""
    if isinstance(value, (dict, list)):
        return True
    if isinstance(value, float) and value != value:
        return False
    return bool(value)


def wait_for_condition(
    driver: Any, script: str, *, lock: Any, timeout_seconds: float = 10.0,
    poll_ms: int = 150, frame_selector: str | None = None,
    enter_frame: Callable[[Any, str | None, str], Any],
    release_frame: Callable[[Any, str | None, str], Any],
    page_summary: Callable[[], dict[str, Any]],
    describe_error: Callable[[Exception], str],
) -> dict[str, Any]:
    """Poll a JS condition, taking ``lock`` (and the frame) only for each probe.

    The wait is capped at ``MAX_WAIT_SECONDS``; ``timeout_note`` says when.
    """
    if not str(script or "").strip():
        raise ValueError("script must not be empty")
    timeout, note = clamp_wait(timeout_seconds)
    poll = max(0.05, min(float(poll_ms) / 1000.0, 2.0))
    started = time.monotonic()
    deadline = started + timeout
    last_error: str | None = None
    while True:
        with lock:
            enter_frame(driver, frame_selector, script)
            try:
                try:
                    if hasattr(driver, "execute_async_script"):
                        # The same semantics as run_script: an expression or a body
                        # with return, top-level await allowed.
                        last_value, _ = repl.run(driver, script)
                    else:
                        last_value = driver.execute_script(f"return ({script});")
                    last_error = None
                except repl.ScriptError as exc:
                    if exc.syntax:  # it will never compile: fail now, not at the timeout
                        raise ValueError(f"wait script does not compile: {exc}") from exc
                    last_value, last_error = None, str(exc)
                except Exception as exc:
                    last_value, last_error = None, describe_error(exc)
            finally:
                release_frame(driver, frame_selector, script)
            if js_truthy(last_value):
                return {**page_summary(), "success": True, "script": script,
                        "value": clip_result(last_value),
                        "waited_seconds": round(time.monotonic() - started, 2),
                        "timeout_seconds": timeout, "frame_selector": frame_selector,
                        **({"timeout_note": note} if note else {})}
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(poll, deadline - now))
    waited = round(time.monotonic() - started, 2)
    raise TimeoutException(
        f"Condition was still falsy after {timeout:g}s"
        + (f" (last error: {last_error})" if last_error else "")
        + f" (waited {waited:g}s)"
        + (f" ({note})" if note else "")
    )


def poll_under_lock(
    lock: Any, attempt: Callable[[], Any], *, timeout: float, poll: float = 0.15,
) -> Any:
    """Retry ``attempt`` under ``lock`` until it returns, releasing between tries.

    ``attempt`` makes one immediate check and raises ``TimeoutException`` when
    the wait is not over yet; the last such failure is re-raised with the real
    ``timeout`` in place of the single check's zero.
    """
    deadline = time.monotonic() + timeout
    poll = max(0.05, min(float(poll), 2.0))
    while True:
        with lock:
            try:
                return attempt()
            except TimeoutException as exc:
                if time.monotonic() >= deadline:
                    message = _ZERO_WAIT.sub(lambda m: f"{m.group(1)} {timeout:g}s", exc.msg or "")
                    raise TimeoutException(message) from None
        time.sleep(max(0.0, min(poll, deadline - time.monotonic())))


# A wait parks an agent; one that asks for an hour is a typo or a leak, and the
# caller has to be told the number was cut rather than find out from the clock.
MAX_WAIT_SECONDS = 300.0


def clamp_wait(timeout_seconds: float, floor: float = 0.1) -> tuple[float, str | None]:
    """Bound a requested wait to [floor, MAX_WAIT_SECONDS]; say so when it was cut."""
    requested = float(timeout_seconds)
    timeout = min(max(floor, requested), MAX_WAIT_SECONDS)
    if requested > MAX_WAIT_SECONDS:
        return timeout, (
            f"timeout_seconds={requested:g} exceeds the {MAX_WAIT_SECONDS:g}s maximum "
            f"and was clamped to {timeout:g}s."
        )
    return timeout, None


def wait_out_challenge(
    lock: Any, *, timeout_seconds: float, poll_interval_seconds: float,
    probe: Callable[[], dict[str, Any]],
    page_summary: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Poll a challenge until it clears, taking ``lock`` only for each probe.

    The session stays usable between polls - a human clearing the box in the
    same tab, or another read of it, is not queued behind a three-minute wait.
    """
    timeout, note = clamp_wait(timeout_seconds)
    poll_interval = max(0.05, min(float(poll_interval_seconds), 2.0))
    started = time.monotonic()
    challenge_seen = False
    while True:
        with lock:
            challenge = probe()
            blocked = bool(challenge["challenge_detected"]) or bool(
                challenge.get("invisible_challenge_pending")
            )
            challenge_seen = challenge_seen or blocked
            elapsed = time.monotonic() - started
            done = not blocked or elapsed >= timeout
            summary = page_summary() if done else None
        if summary is not None:
            result = {
                **summary,
                "success": not blocked,
                "resolved": not blocked,
                "timed_out": blocked,
                "challenge_seen": challenge_seen,
                "waited_seconds": round(elapsed if blocked else time.monotonic() - started, 2),
                "session_open": True,
            }
            if note:
                result["timeout_note"] = note
            return result
        time.sleep(min(poll_interval, timeout - elapsed))
