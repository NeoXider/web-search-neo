"""Free, local solving of the slider family of captchas.

``captcha.py`` covers the widgets a paid service can be asked about: they publish
a sitekey, and a service trades money for the token that sitekey mints. Slider
captchas are the other shape. Tencent TCaptcha and Geetest expose no sitekey at
all - they show a picture, ask for a piece to be dragged into a hole, and grade
the *movement* rather than a string. Nothing can be bought for them in the same
call, so this package does the two things that actually matter itself:

``images.py`` finds the hole in the rendered picture, in the style of the
open-source slider solvers (Canny/template matching when the cut-out piece is
available, a notch search on the background alone when it is not - which is what
a browser session can honestly provide, since the CDN images are cross-origin).

``trajectory.py`` turns that offset into a path a hand would produce, because
these vendors score the drag: a perfectly linear 300 px move in six identical
steps is the signature of a bot.

``slider.py`` holds what each vendor's widget looks like in the DOM and runs the
loop - find, measure, drag, check - against callables the caller injects, so the
whole thing is testable without a browser and depends on neither Selenium nor
the orchestrators.
"""

from web_search_neo.captcha_solvers.images import crop_png, detect_gap
from web_search_neo.captcha_solvers.slider import (
    SLIDER_CLEARED_SCRIPT,
    SLIDER_LOCATE_SCRIPT,
    SLIDER_VENDORS,
    local_solve_enabled,
    solve_slider,
)
from web_search_neo.captcha_solvers.trajectory import human_trajectory

__all__ = [
    "SLIDER_CLEARED_SCRIPT",
    "SLIDER_LOCATE_SCRIPT",
    "SLIDER_VENDORS",
    "crop_png",
    "detect_gap",
    "human_trajectory",
    "local_solve_enabled",
    "solve_slider",
]
