"""Waiting for a page to finish loading, within the time the caller gave.

`open` polls ``document.readyState`` until it reads "complete". Each poll is a
script the page's own thread has to answer, and a heavy page on a busy machine
can leave one unanswered for longer than the whole wait was meant to last. Two
things went wrong with that (2026-11-11, sixteen cores at 100 %): the first
unanswered poll ended `open` with a bridge timeout although most of its
``timeout_seconds`` were left, and once that was tolerated a wait of 120 s ran
on for minutes because every poll waited out the full command limit.

So a poll here is short and never longer than the time that is left, an
unanswered one means "not ready yet", and the error at the end says which of the
two it was: a page that kept answering "interactive" is still fetching
something, a page that never answered is starved of CPU.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from selenium.common.exceptions import TimeoutException

PROBE_SECONDS = 5.0
_READY_EXPRESSION = "document.readyState"


def _ready_state(driver: Any, limit: float) -> str | None:
    """``document.readyState``, or None when the page did not answer within ``limit``."""
    try:
        if getattr(driver, "is_extension_bridge", False):
            # The companion bridge takes a per-call limit; a limit the caller names is kept
            # to the second, so one poll cannot outlive the wait it belongs to.
            answer = driver.execute_cdp_cmd(
                "Runtime.evaluate", {"expression": _READY_EXPRESSION, "returnByValue": True}, timeout=limit
            )
            return str(((answer or {}).get("result") or {}).get("value") or "") or None
        return str(driver.execute_script(f"return {_READY_EXPRESSION}") or "") or None
    except TimeoutError:
        return None


def wait_until_ready(
    driver: Any,
    timeout_seconds: float,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return once the page reads "complete"; raise ``TimeoutException`` when it has not in time."""
    timeout = max(1.0, float(timeout_seconds))
    deadline = now() + timeout
    state: str | None = None
    while True:
        state = _ready_state(driver, max(0.5, min(PROBE_SECONDS, deadline - now())))
        if state == "complete":
            return
        if now() >= deadline:
            seen = f"document.readyState was still '{state}'" if state else "the page left the question unanswered"
            raise TimeoutException(
                f"the page did not finish loading within {timeout:.0f} s ({seen}); "
                "a heavy page or a busy machine needs a larger timeout_seconds"
            )
        sleep(0.5 if state else 0.1)
