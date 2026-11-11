"""Attaching the debugger to a tab, as its own step with its own patience.

The companion attaches Chrome's debugger the first time a tab is asked to do
anything. On an idle machine that takes about a second; on one whose CPU and GPU
are busy with other work (a build, a render, a model) it was measured at over
twelve. It used to happen inside whatever command came first, so that command -
the capture subscription, then the first script - spent its ordinary ten or
fifteen second timeout on a step that has nothing to do with it, and the agent
was told the bridge had timed out when Chrome was only slow.

So a fresh session attaches explicitly and waits for it as long as a busy
machine needs. Every command after that runs with its usual timeout against a
tab that is already attached.
"""

from __future__ import annotations

import os
import time
from typing import Any

from web_search_neo.bridge_patience import Patience

DEFAULT_ATTACH_TIMEOUT = 60.0
_MIN_TIMEOUT, _MAX_TIMEOUT = 5.0, 600.0


def attach_timeout() -> float:
    """Seconds to wait for the debugger to attach (``WSN_ATTACH_TIMEOUT``, default 60)."""
    try:
        seconds = float(os.environ.get("WSN_ATTACH_TIMEOUT", DEFAULT_ATTACH_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_ATTACH_TIMEOUT
    return min(max(seconds, _MIN_TIMEOUT), _MAX_TIMEOUT)


def attach_debugger(bridge: Any, tab_id: int, *, created: bool) -> dict[str, Any] | None:
    """Attach the debugger to ``tab_id`` now and return the companion's timing.

    ``created`` says the tab was opened for this session: if the attach never
    answers, nobody will use that tab, so it is closed rather than left behind
    as a blank orphan. A companion older than 1.30 does not know the command and
    attaches inside the first command it runs, so it is sent a trivial script
    with the same patience and ``None`` comes back instead of a timing.
    """
    timeout = attach_timeout()
    target = {"tabId": int(tab_id)}
    try:
        try:
            answer = bridge.request("debugger.attach", target, timeout=timeout)
        except Exception as exc:
            if isinstance(exc, TimeoutError) or "Unknown bridge method" not in str(exc):
                raise
            warm_up = {"expression": "1", "returnByValue": True}
            started = time.monotonic()
            bridge.request(
                "cdp.send", {**target, "method": "Runtime.evaluate", "params": warm_up}, timeout=timeout
            )
            # The warm-up is this companion's attach, so it is the same evidence of a slow Chrome.
            patience = getattr(bridge, "patience", None)
            if isinstance(patience, Patience):
                patience.note("debugger.attach", time.monotonic() - started)
            return None
    except TimeoutError as exc:
        if created:
            try:
                bridge.request("tabs.remove", target, timeout=5.0)
            except Exception:
                pass
        raise TimeoutError(
            f"Chrome did not attach its debugger to tab {tab_id} within {timeout:.0f} s. Chrome is "
            "answering very slowly, which usually means this machine is busy with other work. "
            "Wait and retry, or raise WSN_ATTACH_TIMEOUT."
        ) from exc
    return dict(answer) if isinstance(answer, dict) else None
