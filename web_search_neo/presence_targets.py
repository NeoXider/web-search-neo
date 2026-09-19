"""Remember visual click positions without changing the real input origin."""
from __future__ import annotations

import math
from typing import Any, Callable


def remember_click_target(
    session: Any, element: Any, *, enabled: bool = True,
    frame_selector: str | None = None,
    map_frame: Callable | None = None, restore_frame: Callable | None = None,
) -> None:
    """Capture before a clicked element removes itself or navigates away.

    Frame mapping is supplied by the browser's existing input geometry code;
    it may switch context, so restore the action frame before the actual click.
    """
    session.presence_click_point = None
    if not enabled:
        return
    try:
        point = session.driver.execute_script(
            "const r=arguments[0].getBoundingClientRect();"
            " return r.width>0 && r.height>0 ? {x:r.left+r.width/2,y:r.top+r.height/2,top:window===window.top} : null;",
            element,
        )
        if not isinstance(point, dict) or not all(
            isinstance(point.get(k), (int, float)) and math.isfinite(point[k]) for k in ("x", "y")
        ):
            return
        x, y = float(point["x"]), float(point["y"])
        if not point.get("top"):
            if not frame_selector or map_frame is None or restore_frame is None:
                return
            try:
                frame = map_frame()
                x, y = frame.to_page(x, y)
            finally:
                restore_frame()
        session.presence_click_point = (x, y)
    except Exception:
        # A failed decoration cannot prevent input from reaching the page.
        pass
