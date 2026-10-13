"""Finding the hole in a slider captcha picture.

The open-source slider solvers all reduce to the same two moves: edge detection
to strip the texture away, then matching the puzzle piece against the background
to find where its silhouette fits (slide_captcha_cracker, puzzle-slider-captcha,
GeetestSliderCrack). A browser session can only do the harder version of it. The
widget paints the piece *and* the hole into one region, and the CDN images behind
them are cross-origin, so there is no clean piece image to cut out - there is one
screenshot and a column profile.

That is what ``find_notch`` reads: a hole has two sharp vertical edges a plausible
piece width apart, and the piece sitting at the start of the track has the same
signature at the far left. Picking the pair that is not at the far left is the
whole trick, and it needs nothing but numpy. ``match_piece`` is the template
matching version for callers who really do have both images.

numpy is required to analyse anything; OpenCV is used only when it is already
installed. Without numpy this says so rather than guessing at pixels.
"""

from __future__ import annotations

from typing import Any

# A slider piece is a small square: 24 px is below the smallest one worth naming,
# 160 px above the largest. Outside that range every pair of edges looks plausible
# and none of them are the hole.
MIN_PIECE_WIDTH = 24
MAX_PIECE_WIDTH = 160

# The piece always sits near the start of the track, so a pair of edges this close
# to the left border is the piece itself and not the hole it belongs in.
LEFT_GUARD = 0.12


def _load(png: bytes) -> Any:
    """Decode PNG/JPEG bytes into an (height, width, 3) integer array, or None."""
    try:
        import numpy as np
    except ImportError:
        return None
    if not png:
        return None
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png)).convert("RGB")
        return np.asarray(image, dtype="int16")
    except Exception:  # noqa: BLE001 - a decoder that is not there is not an error
        pass
    try:
        import cv2
        import numpy as np

        decoded = cv2.imdecode(np.frombuffer(png, dtype="uint8"), cv2.IMREAD_COLOR)
        if decoded is None or decoded.ndim != 3:
            return None
        return decoded.astype("int16")
    except Exception:  # noqa: BLE001
        return None


def image_size(picture: bytes) -> tuple[int, int] | None:
    """Pixel size of a capture, or None when it cannot be decoded.

    A viewport screenshot is device pixels, and the page only ever reports CSS
    pixels, so the two have to be reconciled before anything measured in one can be
    used in the other.
    """
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(picture)) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001
        return None


def crop_png(picture: bytes, x: float, y: float, width: float, height: float, scale: float = 1.0) -> bytes:
    """Cut a box out of a screenshot, with the box given in CSS pixels.

    The widget's own numbers are CSS pixels and the capture is device pixels, so
    every side is scaled before it is used. Cropping is what makes the notch search
    honest: measured over a whole page, the columns of text vote as loudly as the
    hole does.
    """
    try:
        import io

        from PIL import Image
    except ImportError:
        return b""
    try:
        image = Image.open(io.BytesIO(picture)).convert("RGB")
    except Exception:  # noqa: BLE001 - an undecodable capture is not a solvable captcha
        return b""
    left = max(0, int(round(float(x) * scale)))
    top = max(0, int(round(float(y) * scale)))
    right = min(image.width, left + max(8, int(round(float(width) * scale))))
    bottom = min(image.height, top + max(8, int(round(float(height) * scale))))
    if right - left < 8 or bottom - top < 8:
        return b""
    buffer = io.BytesIO()
    image.crop((left, top, right, bottom)).save(buffer, format="PNG")
    return buffer.getvalue()


def _column_edges(array: Any) -> tuple[Any, Any]:
    """Per-column vertical edge energy of the picture's middle band.

    The frame line a widget draws around its own image is one or two pixels at the
    border and would otherwise be the strongest edge in the picture, so the band is
    trimmed before anything is measured.
    """
    import numpy as np

    gray = array.mean(axis=2)
    height = gray.shape[0]
    top, bottom = int(height * 0.06), max(int(height * 0.06) + 4, int(height * 0.94))
    band = gray[top:bottom, :]
    if band.shape[1] < 8:
        return None, None
    difference = np.abs(band[:, 1:] - band[:, :-1])
    return difference.sum(axis=0), gray


def find_notch(
    picture: bytes,
    *,
    min_width: int = MIN_PIECE_WIDTH,
    max_width: int = MAX_PIECE_WIDTH,
) -> dict[str, Any]:
    """Locate the hole in a rendered slider background.

    Returns the x of the hole's left edge and the x of the piece that has to be
    dragged into it, both in image pixels, plus how far apart they are - which is
    how far the handle has to move.
    """
    array = _load(picture)
    if array is None:
        return {"ok": False, "reason": "no image decoder (numpy is required)"}
    edges, _ = _column_edges(array)
    if edges is None:
        return {"ok": False, "reason": "the picture is too narrow to measure"}

    import numpy as np

    width = int(edges.shape[0])
    floor = float(np.median(edges))
    score = edges - floor
    low, high = max(8, min(min_width, width // 2)), max(9, min(max_width, width - 2))
    left_guard = int(width * LEFT_GUARD)

    def best_pair(start_from: int, start_to: int) -> tuple[float, int, int, int]:
        """Strongest pair of edges ``start_from``..``start_to`` apart, as (score, a, b, gap)."""
        best = (0.0, 0, 0, 0)
        for gap in range(low, high + 1):
            last = start_to - gap
            if last < start_from:
                continue
            left = score[start_from:last]
            right = score[start_from + gap : last + gap]
            total = left + right
            if total.size == 0:
                continue
            index = int(np.argmax(total))
            if float(total[index]) > best[0]:
                best = (float(total[index]), start_from + index, start_from + index + gap, gap)
        return best

    # The piece at the start of the track is found first so its edges are not
    # mistaken for the hole, and so the distance is measured from where the piece
    # actually is rather than from the left border.
    piece_x = 0
    near_left = best_pair(1, min(left_guard, width - low))
    if near_left[0] > floor * 1.5 and near_left[3] >= low:
        piece_x = near_left[1]

    hole_score, hole_x, hole_right, gap = best_pair(max(left_guard, piece_x + 2), width - low)
    if hole_score <= 0:
        return {"ok": False, "reason": "no hole-shaped pair of edges in this picture"}
    peak = max(hole_score / 2.0, 1.0)
    noise = max(float(np.median(np.abs(score))), 1.0)
    confidence = peak / (peak + 2.5 * noise)
    return {
        "ok": True,
        "method": "notch",
        "gap_x": hole_x,
        "piece_x": piece_x,
        "piece_width": gap,
        "distance": max(1, hole_x - piece_x),
        "score": round(hole_score, 2),
        "confidence": round(min(1.0, confidence), 3),
        "image_width": width,
    }


def match_piece(background: bytes, piece: bytes) -> dict[str, Any]:
    """Template-match a cut-out piece against the background it belongs in.

    The classical route - Canny on both images so the texture does not vote, then
    ``matchTemplate`` - and the reason it is rarely used here: getting the piece as
    its own image means either the CDN serving it to us or the page handing over a
    canvas it drew, and cross-origin images taint the canvas.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"ok": False, "reason": "OpenCV is not installed"}
    background_array = _load(background)
    piece_array = _load(piece)
    if background_array is None or piece_array is None:
        return {"ok": False, "reason": "one of the images could not be decoded"}
    bg_edges = cv2.Canny(cv2.cvtColor(background_array.astype("uint8"), cv2.COLOR_BGR2GRAY), 60, 160)
    piece_edges = cv2.Canny(cv2.cvtColor(piece_array.astype("uint8"), cv2.COLOR_BGR2GRAY), 60, 160)
    if piece_edges.shape[0] >= bg_edges.shape[0] or piece_edges.shape[1] >= bg_edges.shape[1]:
        return {"ok": False, "reason": "the piece is not smaller than the background"}
    result = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
    _, best, _, location = cv2.minMaxLoc(result)
    if best <= 0.2:
        return {"ok": False, "reason": f"no confident match (score {best:.2f})"}
    x, y = int(location[0]), int(location[1])
    return {
        "ok": True,
        "method": "template",
        "gap_x": x,
        "gap_y": y,
        "piece_width": int(piece_edges.shape[1]),
        "piece_height": int(piece_edges.shape[0]),
        "distance": x,
        "score": round(float(best), 4),
        "confidence": round(float(best), 3),
        "image_width": int(bg_edges.shape[1]),
    }


def detect_gap(
    picture: bytes,
    piece: bytes | None = None,
    *,
    min_width: int = MIN_PIECE_WIDTH,
    max_width: int = MAX_PIECE_WIDTH,
) -> dict[str, Any]:
    """Where the piece goes, by whichever detector the caller's images allow."""
    if piece:
        matched = match_piece(picture, piece)
        if matched.get("ok"):
            return matched
    found = find_notch(picture, min_width=min_width, max_width=max_width)
    if not found.get("ok") and piece:
        found["detector_note"] = matched.get("reason")
    return found
