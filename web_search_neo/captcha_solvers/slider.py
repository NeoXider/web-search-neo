"""What a slider captcha looks like in the DOM, and the loop that gets past it.

TCaptcha renders its widget into the page's own document (``TJCaptcha.js`` builds
``#tcaptcha_transform_dy`` and the ``tencent-captcha-dy__*`` parts around it) and
the older variant lives in a frame from ``turing.captcha.qcloud.com``. Tencent
renames these classes between releases, so every selector here is a substring
match on the family rather than an exact name: ``[class*='slider-handler']`` still
finds the handler when ``handler`` becomes ``knob``, and an exact name would not.

The loop - locate, capture, measure, drag, check - takes its browser primitives as
callables. That is what keeps this package a leaf: it never imports Selenium or the
orchestrators, and a test can hand it four lambdas and watch the whole solve.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from web_search_neo.captcha_solvers.images import crop_png, detect_gap, image_size
from web_search_neo.captcha_solvers.trajectory import human_trajectory

# Opting a machine out of the free local solve: it costs nothing, so it is on by
# default, but an operator who wants every captcha left for a person can turn it
# off without touching the paid-service settings.
LOCAL_SOLVE_ENV = "WEB_SEARCH_NEO_CAPTCHA_LOCAL_SOLVE"

# A notch search that is barely sure of itself drags the piece to the wrong place,
# spends the attempt, and hands back a refreshed puzzle. Below this the honest
# answer is "wait for a person", not "guess".
MIN_CONFIDENCE = 0.12

# Handler candidates are ordered from the most specific to the least, and the
# smallest matching box wins anyway: the handler is a knob, the groove it sits in
# spans the whole widget, and pressing the middle of the groove is not a drag.
SLIDER_VENDORS = {
    "tcaptcha": {
        "root": [
            "#tcaptcha_transform_dy", "#TCaptcha", "#captcha_popup",
            "[id^='tCaptcha']", "[class*='tencent-captcha-dy__']",
            "[class*='tencent-captcha']",
        ],
        "frame": [
            "iframe[src*='turing.captcha.qcloud.com']",
            "iframe[src*='captcha.gtimg.cn']",
        ],
        "bg": [
            "[class*='bg-placeholder--slider']", "[class*='image-area']",
            "[class*='bg-placeholder']", "#TCAreaContainer", "#TCaptchaBgOut",
            "[class*='captcha-bg']",
        ],
        "handle": [
            "[class*='slider-handler']", "[class*='slide-btn']", "[class*='drag_button']",
            "#tcaptcha_drag_button", "[class*='slider-block']", "[class*='slider-groove']",
            "[class*='slider-bar']",
        ],
        "success": ["[class*='verify-status-img--success']"],
    },
    "geetest4": {
        "root": [".geetest_widget", "[class*='geetest_widget']", "[class*='geetest_panel']"],
        "frame": ["iframe[src*='geetest.com']"],
        "bg": [".geetest_canvas_bg", "[class*='geetest_bg']", "canvas[class*='bg']"],
        "handle": [".geetest_slider_button", "[class*='slider_button']", "[class*='btn_slide']"],
        "success": ["[class*='geetest_result']", "[class*='tip_success']"],
    },
}

# One round trip for the whole picture of the widget: which vendor, whether it is a
# live widget or a leftover node from the last one, and where its three parts are.
# Boxes are viewport CSS pixels - getBoundingClientRect - because that is the space
# the pointer actions take coordinates in.
SLIDER_LOCATE_SCRIPT = """
const vendors = %s;
function box(element) {
  const rect = element.getBoundingClientRect();
  return [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)];
}
function live(element, minWidth, minHeight) {
  if (!element) return false;
  const rect = element.getBoundingClientRect();
  if (rect.width < minWidth || rect.height < minHeight) return false;
  if (rect.right <= 0 || rect.bottom <= 0) return false;
  if (rect.left > window.innerWidth - 4 || rect.top > window.innerHeight - 4) return false;
  let node = element;
  for (let step = 0; step < 8 && node; step += 1) {
    const view = (node.ownerDocument && node.ownerDocument.defaultView) || window;
    if (!view) break;
    const style = view.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const opacity = parseFloat(style.opacity);
    if (!isNaN(opacity) && opacity <= 0.05) return false;
    node = node.parentElement || (node.parentNode && node.parentNode.host) || null;
  }
  return true;
}
function pick(root, selectors, minWidth, minHeight) {
  let best = null;
  const scope = root || document;
  for (const selector of selectors) {
    let elements = [];
    try { elements = Array.from(scope.querySelectorAll(selector)); } catch (error) { continue; }
    for (const element of elements) {
      if (!live(element, minWidth, minHeight)) continue;
      const rect = element.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (!best || area < best.area) best = {selector: selector, box: box(element), area: area};
    }
  }
  return best;
}
for (const [name, spec] of Object.entries(vendors)) {
  let root = null;
  for (const selector of spec.root) {
    const element = document.querySelector(selector);
    if (element && live(element, 60, 40)) { root = element; break; }
  }
  let frame = null;
  if (!root) {
    for (const selector of spec.frame) {
      const element = document.querySelector(selector);
      if (element && live(element, 120, 60)) { frame = element; break; }
    }
  }
  if (!root && !frame) continue;
  const scope = root || null;
  const bg = pick(scope, spec.bg, 60, 40) || pick(null, spec.bg, 60, 40);
  const handle = pick(scope, spec.handle, 18, 14) || pick(null, spec.handle, 18, 14);
  let solved = false;
  for (const selector of spec.success) {
    try { if (document.querySelector(selector)) { solved = true; break; } } catch (error) { /* not readable */ }
  }
  return {
    vendor: name, live: !solved, solved: solved,
    root: root ? box(root) : null, frame: frame ? box(frame) : null,
    bg: bg ? bg.box : null, bg_selector: bg ? bg.selector : null,
    handle: handle ? handle.box : null, handle_selector: handle ? handle.selector : null,
    dpr: window.devicePixelRatio || 1, viewport: [window.innerWidth, window.innerHeight],
    url: location.href,
  };
}
return {vendor: null, live: false, url: location.href};
""" % json.dumps(SLIDER_VENDORS)

# Did the drag work? A slider captcha answers that question by disappearing: the
# widget node is gone, hidden, or showing its own success mark.
SLIDER_CLEARED_SCRIPT = """
const vendors = %s;
function live(element, minWidth, minHeight) {
  if (!element) return false;
  const rect = element.getBoundingClientRect();
  if (rect.width < minWidth || rect.height < minHeight) return false;
  let node = element;
  for (let step = 0; step < 8 && node; step += 1) {
    const view = (node.ownerDocument && node.ownerDocument.defaultView) || window;
    if (!view) break;
    const style = view.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const opacity = parseFloat(style.opacity);
    if (!isNaN(opacity) && opacity <= 0.05) return false;
    node = node.parentElement || (node.parentNode && node.parentNode.host) || null;
  }
  return true;
}
const wanted = arguments[0] || null;
const names = wanted ? [wanted] : Object.keys(vendors);
for (const name of names) {
  const spec = vendors[name];
  if (!spec) continue;
  for (const selector of spec.success) {
    try { if (document.querySelector(selector)) return {cleared: true, vendor: name, evidence: 'success mark'}; }
    catch (error) { /* not readable */ }
  }
  const selectors = spec.root.concat(spec.frame);
  for (const selector of selectors) {
    let elements = [];
    try { elements = Array.from(document.querySelectorAll(selector)); } catch (error) { continue; }
    for (const element of elements) {
      if (live(element, 60, 40)) return {cleared: false, vendor: name, evidence: selector};
    }
  }
}
return {cleared: true, vendor: wanted, evidence: 'no live slider widget on the page'};
""" % json.dumps(SLIDER_VENDORS)


def local_solve_enabled() -> bool:
    """Whether the free local slider solve is allowed (it is, unless switched off)."""
    return (os.getenv(LOCAL_SOLVE_ENV) or "1").strip().lower() not in {"0", "false", "no", "off"}


def _css_distance(gap: dict[str, Any], bg_box: list[float]) -> float:
    """Turn an image-pixel offset into CSS pixels using the crop's own scale.

    The ratio between the cropped capture and the box it was cut from is measured
    from the two numbers that have to agree, so no assumption about the display's
    device pixel ratio is needed and none can be wrong.
    """
    measured = float(gap.get("image_width") or 0) or float(bg_box[2])
    return float(gap["distance"]) * (float(bg_box[2]) / measured)


def solve_slider(
    locate: Callable[[], dict[str, Any]],
    capture: Callable[[], bytes],
    drag: Callable[[tuple[float, float], list[tuple[float, float, float]]], None],
    cleared: Callable[[], dict[str, Any]],
    sleep: Callable[[float], None],
    *,
    budget_seconds: float = 30.0,
    attempts: int = 2,
    min_confidence: float = MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Measure the hole, drag the handle into it, and see whether the widget left.

    Each callable is one browser primitive the caller owns: ``locate`` runs the
    locate script, ``capture`` returns a viewport PNG, ``drag`` presses at a point
    and follows a path of relative moves with pauses, ``cleared`` asks the page
    whether the widget is gone.
    """
    deadline = time.monotonic() + max(5.0, float(budget_seconds))
    last: dict[str, Any] = {}
    for attempt in range(1, max(1, int(attempts)) + 1):
        found = locate() or {}
        last = {"attempt": attempt, **found}
        if not found.get("vendor"):
            return {"solved": False, "reason": "no slider widget on the page", "locate": last}
        if not found.get("live"):
            return {"solved": True, "vendor": found["vendor"], "reason": "already solved", "locate": last}
        if found.get("solved"):
            return {"solved": True, "vendor": found["vendor"], "reason": "success mark already shown", "locate": last}
        bg_box, handle = found.get("bg"), found.get("handle")
        if not bg_box or not handle:
            return {
                "solved": False, "vendor": found["vendor"], "locate": last,
                "reason": "the widget's picture or its handle could not be identified",
            }

        shot = capture() or b""
        # The page reports CSS pixels and the capture is in device pixels. The scale
        # is measured from the two widths that must agree - the picture and the
        # viewport the page just reported - rather than trusted from devicePixelRatio,
        # which a zoomed tab or an extension-hosted page can get wrong.
        size = image_size(shot)
        scale = 1.0
        viewport_width = float((found.get("viewport") or [0])[0] or 0.0)
        if size and viewport_width > 0:
            scale = max(0.25, min(8.0, size[0] / viewport_width))
        picture = crop_png(shot, bg_box[0], bg_box[1], bg_box[2], bg_box[3], scale)
        if not picture:
            return {
                "solved": False, "vendor": found["vendor"], "locate": last,
                "reason": "the capture or the crop of the widget came back empty",
            }
        gap = detect_gap(picture)
        last["gap"] = gap
        last["capture_scale"] = round(scale, 3)
        if not gap.get("ok"):
            return {
                "solved": False, "vendor": found["vendor"], "locate": last,
                "reason": f"the hole could not be measured: {gap.get('reason')}",
            }
        confidence = float(gap.get("confidence") or 0.0)
        if confidence < min_confidence:
            return {
                "solved": False, "vendor": found["vendor"], "locate": last,
                "reason": (
                    f"the measurement is too unsure to drag on ({confidence:.2f}); "
                    "clear it with mode='wait' instead of spending the attempt"
                ),
            }

        distance = _css_distance(gap, bg_box)
        if distance < 4 or distance > float(found["viewport"][0]):
            return {
                "solved": False, "vendor": found["vendor"], "locate": last,
                "reason": f"the measured drag of {distance:.0f} px is not a plausible slider move",
            }
        path = human_trajectory(distance, 0.9)
        start = (handle[0] + handle[2] / 2.0, handle[1] + handle[3] / 2.0)
        drag(start, path)

        verify_by = min(time.monotonic() + 6.0, deadline)
        while time.monotonic() < verify_by:
            sleep(0.4)
            answer = cleared() or {}
            if answer.get("cleared"):
                return {
                    "solved": True, "vendor": found["vendor"], "locate": last,
                    "method": gap.get("method"), "confidence": confidence,
                    "distance_px": round(distance, 1), "attempt": attempt,
                    "verify": answer,
                }
        if time.monotonic() >= deadline:
            break
    return {
        "solved": False, "locate": last, "vendor": last.get("vendor"),
        "reason": "the widget is still on the page after dragging the measured offset",
    }
