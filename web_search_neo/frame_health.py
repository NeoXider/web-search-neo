"""Is the page actually rendering? Throttling, measured and named.

Chrome slows a tab it thinks nobody looks at: a background tab or a minimised
window gets no animation frames at all, a covered or unfocused window may drop
to a frame a second. A game then loses key presses (its focus and input are
handled on frames that do not come) and screenshots show old frames - with no
warning anywhere. ``assess`` turns the measured requestAnimationFrame rate and
the document's visibility into ``throttled`` with a reason and a hint, and
``unthrottle`` asks Chrome to treat the tab as focused and active, then measures
again and says honestly whether that helped.
"""
from __future__ import annotations

import time
from typing import Any

# Below this a page that asked for frames is being held back by the browser:
# every display runs rAF at 30 Hz or more when a tab is visible and focused.
THROTTLED_BELOW_FPS = 20.0

STATE_SCRIPT = (
    "return {visibility: document.visibilityState, focused: document.hasFocus(),"
    " was_discarded: !!document.wasDiscarded};"
)

SAMPLE_SCRIPT = r"""
const duration = arguments[0], done = arguments[arguments.length - 1];
const start = performance.now();
let frames = 0, finished = false;
const finish = () => { if (finished) return; finished = true;
  const elapsed = Math.max(1, performance.now() - start);
  done({frames, elapsed_ms: Math.round(elapsed), fps: Math.round(frames * 10000 / elapsed) / 10}); };
setTimeout(finish, duration + 50);
const tick = now => { frames += 1; if (now - start < duration) requestAnimationFrame(tick); };
requestAnimationFrame(tick);
"""


def page_state(driver: Any) -> dict[str, Any]:
    try:
        state = driver.execute_script(STATE_SCRIPT)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def sample_fps(driver: Any, seconds: float = 0.5) -> float | None:
    try:
        answer = driver.execute_async_script(SAMPLE_SCRIPT, max(100, int(seconds * 1000)))
    except Exception:
        return None
    return float(answer.get("fps")) if isinstance(answer, dict) and answer.get("fps") is not None else None


def assess(fps: float | None, state: dict[str, Any], render_mode: str = "normal") -> dict[str, Any]:
    """``{raf_fps, throttled, throttle_reason, visibility, focused, hint}``."""
    health: dict[str, Any] = {"raf_fps": fps, "visibility": state.get("visibility"),
                              "focused": state.get("focused")}
    if render_mode != "normal":
        return {**health, "throttled": None,
                "throttle_reason": f"render mode '{render_mode}' holds the frames itself"}
    if state.get("visibility") == "hidden":
        reason = "hidden"  # a background tab or a minimised window: no frames at all
    elif fps is not None and fps < THROTTLED_BELOW_FPS:
        reason = "occluded_or_background_window"
    else:
        return {**health, "throttled": False if fps is not None else None, "throttle_reason": None}
    return {**health, "throttled": True, "throttle_reason": reason, "hint": (
        "Chrome is holding this page's frames back, so a game drops input and screenshots "
        "lag. Try the unthrottle action; if raf_fps stays low the window is covered or "
        "minimised - only bringing it up helps (the show action, when the user agrees).")}


def health(driver: Any, fps: float | None, render_mode: str) -> dict[str, Any]:
    return assess(fps, page_state(driver), render_mode)


def unthrottle(driver: Any, render_mode: str = "normal") -> dict[str, Any]:
    """Ask Chrome to treat the tab as focused and active, then measure again."""
    before = assess(sample_fps(driver, 0.4), page_state(driver), render_mode)
    applied, refused = [], {}
    for method, params in (("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                           ("Page.setWebLifecycleState", {"state": "active"})):
        try:
            driver.execute_cdp_cmd(method, params)
            applied.append(method)
        except Exception as exc:
            refused[method] = f"{type(exc).__name__}: {str(exc)[:160]}"
    time.sleep(0.2)
    after = assess(sample_fps(driver, 0.6), page_state(driver), render_mode)
    answer = {"success": True, "before": before, "after": after, "applied": applied,
              "still_throttled": after.get("throttled")}
    if refused:
        answer["refused"] = refused
    if after.get("throttled"):
        answer["note"] = ("Focus and lifecycle emulation did not bring the frames back: the "
                          "window itself is covered or minimised, which only the user (or the "
                          "show action, with their consent) can change.")
    return answer
