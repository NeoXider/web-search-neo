"""Matching the widget's own target glyphs to the shapes painted in its picture.

A "select in order" captcha hands the widget a row of small target glyphs and scatters
bigger versions of some of them (plus decoys) over the picture. The open-source solvers
OCR the glyphs; TCaptcha draws AI-rendered pictographs and CJK glyphs no OCR reads here,
so the honest route is shape comparison. Filled-mask IoU is not enough: line drawings
of similar objects overlap badly (a real capture scored 0.42 for every building). What
does separate them is the stroke itself - a two-sided chamfer distance between the glyph
skeleton and each candidate, swept over small rotations because the widget tilts its
shapes. Scores are per pixel of candidate size, so a picture and its glyph agree on the
same number whatever the widget's scale.

A reading is only called sure when the best candidate is inside the score ceiling and
leads the runner-up by the required margin; confusable pairs (a house and a flat-roof
building) are reported as unsure rather than guessed, because one wrong click spends the
vendor's verification attempt.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _cv2():
    """OpenCV when it is installed; the reading refuses without it."""
    import cv2

    return cv2

# The widget tilts its shapes only slightly: 20 degrees covers what a real capture
# showed, and 5 degree steps are enough for a stroke to stay recognisable.
ROTATION_STEPS = range(-20, 21, 5)

SCORE_CEILING = 1.6   # mean chamfer distance per 25 px of candidate size
MIN_LEAD = 0.3        # the runner-up has to be this much worse to count as read


def _load(picture: bytes) -> Any:
    """Decode image bytes to a grayscale int array, or None."""
    try:
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(picture)).convert("L")
        return np.asarray(image, dtype="uint8")
    except Exception:  # noqa: BLE001 - a missing decoder is not an error here
        return None


def _ink_components(gray: np.ndarray, box: tuple[int, int, int, int],
                    threshold: int, min_side: int, max_side: int) -> list[dict[str, Any]]:
    """Dark ink shapes inside `box` with their stroke masks."""
    x0, y0, x1, y1 = box
    sub = gray[y0:y1, x0:x1]
    if sub.size == 0:
        return []
    cv2 = _cv2()
    mask = (sub < threshold).astype("uint8") * 255
    _count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    shapes = []
    for index in range(1, _count):
        x, y, width, height, area = (int(value) for value in stats[index])
        if area < 20 or not min_side <= width <= max_side or not min_side <= height <= max_side:
            continue
        shapes.append({
            "box": (x + x0, y + y0, width, height),
            "stroke": (labels[y:y + height, x:x + width] == index).astype("float32"),
        })
    return shapes


def _chamfer(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sided mean distance between two stroke masks, per 25 px of size."""
    ta = (a > 0.5).astype("uint8")
    tb = (b > 0.5).astype("uint8")
    if ta.sum() == 0 or tb.sum() == 0:
        return 99.0
    cv2 = _cv2()
    dt_b = cv2.distanceTransform(np.uint8(1 - tb), cv2.DIST_L1, 3)
    dt_a = cv2.distanceTransform(np.uint8(1 - ta), cv2.DIST_L1, 3)
    ys, xs = np.nonzero(ta)
    ys2, xs2 = np.nonzero(tb)
    there = float(dt_b[ys, xs].mean())
    back = float(dt_a[ys2, xs2].mean())
    scale = max(1.0, max(b.shape[0], b.shape[1]) / 25.0)
    return (there + back) / scale


def _best_chamfer(glyph: np.ndarray, shape: np.ndarray) -> float:
    """Best chamfer score over the small rotation sweep."""
    cv2 = _cv2()
    best = 99.0
    for angle in ROTATION_STEPS:
        matrix = cv2.getRotationMatrix2D((glyph.shape[1] / 2.0, glyph.shape[0] / 2.0), angle, 1.0)
        turned = cv2.warpAffine(glyph, matrix, (glyph.shape[1], glyph.shape[0]))
        size = (max(turned.shape[0], shape.shape[0]), max(turned.shape[1], shape.shape[1]))
        turned_r = cv2.resize(turned, (size[1], size[0]), interpolation=cv2.INTER_NEAREST)
        shape_r = cv2.resize(shape, (size[1], size[0]), interpolation=cv2.INTER_NEAREST)
        best = min(best, _chamfer(turned_r, shape_r))
    return best


def match_glyph_shapes(picture: bytes, strip_box: tuple[int, int, int, int],
                       picture_box: tuple[int, int, int, int], *,
                       strip_threshold: int = 130, picture_threshold: int = 95) -> dict[str, Any]:
    """Read the target row and score every shape in the picture against each glyph.

    `strip_box` is the widget's target row, `picture_box` its picture; both in pixels of
    this capture. Every glyph gets its candidates ranked by chamfer score, and a reading
    is marked sure only when the best candidate beats the ceiling and leads the runner-up
    by MIN_LEAD. Nothing here clicks or decides an order: the caller acts on `sure` rows
    only.
    """
    try:
        _cv2()
    except ImportError:
        return {"ok": False, "reason": "OpenCV is not installed"}
    gray = _load(picture)
    if gray is None:
        return {"ok": False, "reason": "the capture could not be decoded"}
    glyphs = _ink_components(gray, strip_box, strip_threshold, 8, 60)
    shapes = _ink_components(gray, picture_box, picture_threshold, 24, 95)
    if len(glyphs) < 2:
        return {"ok": False, "reason": "the target row is not readable in this capture"}
    if not shapes:
        return {"ok": False, "reason": "the picture holds no candidate shapes"}
    glyphs.sort(key=lambda glyph: glyph["box"][0])
    rows = []
    for glyph in glyphs:
        ranked = sorted(
            [(_best_chamfer(glyph["stroke"], shape["stroke"]), shape) for shape in shapes],
            key=lambda scored: scored[0],
        )
        best_score, best_shape = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 99.0
        rows.append({
            "glyph_box": glyph["box"],
            "sure": best_score <= SCORE_CEILING and (runner_up - best_score) >= MIN_LEAD,
            "best_score": round(best_score, 2),
            "lead": round(min(runner_up, 99.0) - best_score, 2),
            "candidates": [
                {"box": shape["box"], "score": round(score, 2)} for score, shape in ranked[:3]
            ],
        })
    return {
        "ok": True,
        "method": "chamfer",
        "glyphs": len(glyphs),
        "shapes": len(shapes),
        "rows": rows,
        "all_sure": all(row["sure"] for row in rows),
    }

def _load_sprite(picture: bytes, strip_threshold: int) -> tuple[np.ndarray, str] | None:
    """An ink mask for the widget's own target strip: 1.0 where the glyph is drawn.

    A target strip is usually dark glyphs on a transparent ground; then the alpha channel IS
    the glyph and thresholding the pixels would smear it into the ground. An opaque strip falls
    back to ink darkness instead. Which one was used is reported, because it changes how much
    of the reading an agent can trust.
    """
    try:
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(picture))
    except Exception:  # noqa: BLE001 - a missing decoder is reported, not crashed
        return None
    if image.mode in {'P', 'L', 'RGB'}:
        gray = np.asarray(image.convert('L'), dtype='uint8')
        return (gray < strip_threshold).astype('float32'), 'ink'
    try:
        rgba = np.asarray(image.convert('RGBA'), dtype='uint8')
    except Exception:  # noqa: BLE001
        return None
    alpha = rgba[:, :, 3]
    if int(alpha.min()) >= 250:  # nothing transparent: read the ink itself
        gray = rgba[:, :, :3].mean(axis=2).astype('uint8')
        return (gray < strip_threshold).astype('float32'), 'ink'
    gray = rgba[:, :, :3].mean(axis=2).astype('uint8')
    ink = (alpha > 16) & ((gray < strip_threshold) | (alpha < 250))
    return ink.astype('float32'), 'alpha'


def _tight(mask: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
    """Crop an ink mask to its own bounding box, or refuse when it is too thin to read."""
    ys, xs = np.nonzero(mask > 0.5)
    if len(ys) < 20:
        return None
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    if not 8 <= (x1 - x0) <= 160 or not 8 <= (y1 - y0) <= 160:
        return None
    return mask[y0:y1, x0:x1].astype("float32"), (x0, y0, x1 - x0, y1 - y0)


def match_sprite_tiles(picture: bytes, sprite: bytes, glyph_boxes: list[list[int]], *,
                       cells: list[list[int]] | None = None, picture_box: tuple[int, int, int, int] | None = None,
                       scale: float = 1.0, picture_threshold: int = 95, strip_threshold: int = 200) -> dict[str, Any]:
    """Match the glyphs the challenge declared in its own strip to the tiles of its picture.

    `glyph_boxes` is the order the challenge asked for (``tc_target_plan`` reads it out of the
    prehandle), `sprite` is the image that strip is drawn from, `cells` are the picture's own
    tile boxes. Each declared glyph gets its candidates ranked by stroke distance in that order;
    a row is sure only inside the score ceiling and ahead of the runner-up by MIN_LEAD. Glyphs
    that carry no ink are dropped and counted - never replaced by a placeholder.
    """
    try:
        _cv2()
    except ImportError:
        return {"ok": False, "reason": "OpenCV is not installed"}
    if not glyph_boxes:
        return {"ok": False, "reason": "the challenge declared no glyphs to match"}
    gray = _load(picture)
    if gray is None:
        return {"ok": False, "reason": "the capture could not be decoded"}
    strip = _load_sprite(sprite, strip_threshold)
    if strip is None:
        return {"ok": False, "reason": "the target strip could not be decoded"}
    ink_strip, strip_mode = strip
    height, width = ink_strip.shape[:2]
    boxes = [[int(round(box[0] * scale)), int(round(box[1] * scale)),
              int(round(box[2] * scale)), int(round(box[3] * scale))] for box in glyph_boxes]
    reach_x = max(box[0] + box[2] for box in boxes)
    reach_y = max(box[1] + box[3] for box in boxes)
    strip_scale = 1.0
    if reach_x > width or reach_y > height:
        # Only the device-pixel scales a widget really draws at are tried, largest first, and
        # the one that was used is reported. A factor that fits nothing is a refusal, not a guess.
        for candidate in (0.75, 2 / 3, 0.5, 1 / 3, 0.25):
            scaled = [[int(box[0] * candidate), int(box[1] * candidate),
                       int(box[2] * candidate), int(box[3] * candidate)] for box in boxes]
            if max(b[0] + b[2] for b in scaled) <= width and max(b[1] + b[3] for b in scaled) <= height:
                boxes, strip_scale = scaled, candidate
                break
        else:
            return {"ok": False, "reason": f"the declared glyphs reach {reach_x}x{reach_y} but the strip is "
                                           f"{width}x{height}; no device-pixel scale fits them"}

    glyphs: list[np.ndarray] = []
    dropped = 0
    for box in boxes:
        x, y, w, h = box
        crop = ink_strip[max(0, y):max(0, y + h), max(0, x):max(0, x + w)]
        tight = _tight(crop) if crop.size else None
        if tight is None:
            dropped += 1
            continue
        glyphs.append(tight[0])
    if not glyphs:
        return {"ok": False, "reason": "no declared glyph carried enough ink to match"}

    shapes: list[dict[str, Any]] = []
    if cells:
        for cell in cells:
            x, y, w, h = (int(value) for value in cell)
            sub = gray[max(0, y):max(0, y + h), max(0, x):max(0, x + w)]
            if sub.size == 0:
                continue
            tight = _tight((sub < picture_threshold).astype("float32"))
            if tight is None:
                continue
            shapes.append({"box": [x + tight[1][0], y + tight[1][1], tight[1][2], tight[1][3]],
                           "stroke": tight[0]})
    else:
        box = picture_box or (0, 0, gray.shape[1], gray.shape[0])
        shapes = [{"box": list(shape["box"]), "stroke": shape["stroke"]}
                  for shape in _ink_components(gray, box, picture_threshold, 24, 95)]
    if not shapes:
        return {"ok": False, "reason": "the picture holds no candidate tile ink"}

    rows = []
    for order, glyph in enumerate(glyphs):
        ranked = sorted(
            [(_best_chamfer(glyph, shape["stroke"]), shape) for shape in shapes],
            key=lambda scored: scored[0],
        )
        best_score, best_shape = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 99.0
        rows.append({
            "order": order,
            "box": boxes[order],
            "sure": bool(best_score <= SCORE_CEILING and (runner_up - best_score) >= MIN_LEAD),
            "best_score": round(best_score, 2),
            "lead": round(min(runner_up, 99.0) - best_score, 2),
            "tile_box": best_shape["box"],
            "candidates": [{"box": shape["box"], "score": round(score, 2)} for score, shape in ranked[:3]],
        })
    out: dict[str, Any] = {
        "ok": True,
        "method": "sprite_declared",
        "glyphs": len(glyphs),
        "shapes": len(shapes),
        "rows": rows,
        "all_sure": all(row["sure"] for row in rows),
        "boxes_in_order": [row["tile_box"] for row in rows if row["sure"]] if all(r["sure"] for r in rows) else [],
    }
    if dropped:
        out["dropped_glyphs"] = dropped
    if strip_scale != 1.0:
        out["strip_scale"] = round(strip_scale, 4)
    out["strip_ink"] = strip_mode
    return out
