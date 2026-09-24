"""What a screenshot shows: its geometry, its freshness, and how to aim from it.

Three things an agent used to work out by itself, and got wrong:

- Scale. A PNG is in device pixels and pointer coordinates are viewport CSS
  pixels; on a 1.25 DPR screen every click aimed from the image missed by a
  quarter. Every capture now reports ``image_width/height``, the CSS box it
  covers, ``device_pixel_ratio`` and ``scale``, and ``pointer`` accepts
  ``coordinate_space='image'`` to aim in the last capture's pixels.
- Freshness. A capture right after an action could show the frame before it.
  ``wait_frames`` lets that many animation frames render first, and every
  capture carries ``frame_id`` (this session's capture counter),
  ``captured_at_ms`` and ``changed_since_last`` (a byte hash compared with the
  previous capture).
- A stalled page. When requestAnimationFrame does not tick (a hidden tab, a
  covered window) the wait gives up after a bound and says ``raf_stalled``.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

MAX_WAIT_FRAMES = 120

_WAIT_FRAMES_SCRIPT = r"""
const wanted = arguments[0], done = arguments[arguments.length - 1];
const start = performance.now();
let frames = 0, finished = false;
const finish = stalled => {
  if (finished) return;
  finished = true;
  done({frames, elapsed_ms: Math.round(performance.now() - start), stalled});
};
const guard = setTimeout(() => finish(true), 1500 + wanted * 50);
const tick = () => { frames += 1; if (frames >= wanted) { clearTimeout(guard); finish(false); } else requestAnimationFrame(tick); };
requestAnimationFrame(tick);
"""

_GEOMETRY_SCRIPT = (
    "return {width: innerWidth, height: innerHeight, dpr: devicePixelRatio || 1,"
    " scroll_x: scrollX, scroll_y: scrollY,"
    " page_width: document.documentElement.scrollWidth, page_height: document.documentElement.scrollHeight};"
)


def wait_frames(driver: Any, frames: int) -> dict[str, Any] | None:
    """Let ``frames`` animation frames render; None when nothing was asked."""
    wanted = max(0, min(int(frames or 0), MAX_WAIT_FRAMES))
    if not wanted:
        return None
    try:
        answer = driver.execute_async_script(_WAIT_FRAMES_SCRIPT, wanted)
    except Exception as exc:
        return {"frames_waited": 0, "raf_stalled": True, "error": f"{type(exc).__name__}"}
    answer = answer if isinstance(answer, dict) else {}
    return {"frames_waited": int(answer.get("frames") or 0), "wait_ms": answer.get("elapsed_ms"),
            "raf_stalled": bool(answer.get("stalled"))}


def png_size(png: bytes) -> tuple[int | None, int | None]:
    if len(png) < 24 or not png.startswith(b"\x89PNG"):
        return None, None
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def describe(session: Any, png: bytes, mode: str | None, x: float | None, y: float | None,
             width: int | None, height: int | None) -> dict[str, Any]:
    """Geometry and freshness of a capture; remembers it for coordinate_space='image'."""
    image_width, image_height = png_size(png)
    try:
        geometry = session.driver.execute_script(_GEOMETRY_SCRIPT) or {}
    except Exception:
        geometry = {}
    selected = str(mode or "viewport")
    if selected == "region":
        css = {"x": float(x or 0), "y": float(y or 0), "width": float(width or 0), "height": float(height or 0)}
    elif selected == "full_page":
        css = {"x": 0.0, "y": 0.0, "width": float(geometry.get("page_width") or 0),
               "height": float(geometry.get("page_height") or 0)}
    else:
        css = {"x": float(geometry.get("scroll_x") or 0), "y": float(geometry.get("scroll_y") or 0),
               "width": float(geometry.get("width") or 0), "height": float(geometry.get("height") or 0)}
    scale = round(image_width / css["width"], 4) if image_width and css["width"] else None
    digest = hashlib.sha1(png).hexdigest()
    session.capture_seq = int(getattr(session, "capture_seq", 0) or 0) + 1
    changed = None if getattr(session, "capture_hash", None) is None else digest != session.capture_hash
    session.capture_hash = digest
    session.capture_geometry = {"css": css, "scale": scale, "mode": selected,
                                "scroll_x": float(geometry.get("scroll_x") or 0),
                                "scroll_y": float(geometry.get("scroll_y") or 0)}
    return {
        "image_width": image_width, "image_height": image_height,
        "css_box": css, "viewport_css_width": geometry.get("width"),
        "viewport_css_height": geometry.get("height"),
        "device_pixel_ratio": geometry.get("dpr"), "scale": scale,
        "frame_id": session.capture_seq, "captured_at_ms": int(time.time() * 1000),
        "changed_since_last": changed,
        "coordinates_note": ("css = image_px / scale + css_box origin (page CSS px); pointer "
                             "coordinate_space='image' does this for you from the last capture."),
    }


def to_viewport(session: Any, x: float, y: float) -> tuple[float, float]:
    """Image pixels of the session's last capture -> viewport CSS pixels for pointer."""
    geometry = getattr(session, "capture_geometry", None)
    if not geometry or not geometry.get("scale"):
        raise ValueError("coordinate_space='image' needs a screenshot of this session first "
                         "(web_info topic=screenshot or the screenshot action)")
    scale = float(geometry["scale"])
    css = geometry["css"]
    # Every box is in page coordinates; the viewport is the page minus the scroll
    # of that moment (so scrolling after the capture needs a new capture).
    page_x, page_y = css["x"] + float(x) / scale, css["y"] + float(y) / scale
    return page_x - geometry["scroll_x"], page_y - geometry["scroll_y"]
