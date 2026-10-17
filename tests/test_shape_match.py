"""The select-in-order reading is scored by stroke distance, not by filled overlap."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from web_search_neo.captcha_solvers.shape_match import match_glyph_shapes, match_sprite_tiles

STRIP_BOX = (4, 4, 124, 34)
PICTURE_BOX = (4, 40, 296, 178)


def _ring(canvas: np.ndarray, x: int, y: int, size: int) -> None:
    canvas[y:y + 3, x:x + size] = 8
    canvas[y + size - 3:y + size, x:x + size] = 8
    canvas[y:y + size, x:x + 3] = 8
    canvas[y:y + size, x + size - 3:x + size] = 8


def _zig(canvas: np.ndarray, x: int, y: int, height: int, wide: int) -> None:
    canvas[y:y + 2, x:x + wide + 3] = 8
    canvas[y + height - 2:y + height, x:x + wide + 3] = 8
    for i in range(height):
        col = x + wide - int(i * wide / max(1, height - 1))
        canvas[y + i:y + i + 2, col:col + 3] = 8


def _triangle(canvas: np.ndarray, x: int, y: int, size: int) -> None:
    for row in range(size):
        inset = int((size - row) * 0.45)
        canvas[y + row:y + row + 2, x + inset:x + inset + 3] = 8
        canvas[y + row:y + row + 2, x + size - inset - 3:x + size - inset] = 8
    canvas[y + size - 3:y + size, x:x + size] = 8


def _png(canvas: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(canvas).save(buffer, format="PNG")
    return buffer.getvalue()


def _widget() -> np.ndarray:
    canvas = np.full((178, 296, 3), 220, dtype="uint8")
    _ring(canvas, 10, 6, 21)
    _zig(canvas, 50, 8, 20, 8)
    _triangle(canvas, 90, 6, 22)
    _ring(canvas, 30, 60, 41)
    _zig(canvas, 110, 60, 42, 26)
    _triangle(canvas, 190, 58, 46)
    return canvas


def test_the_stroke_distance_picks_the_shape_the_glyph_draws():
    """Distinct strokes separate cleanly: every row names its own shape."""
    pytest.importorskip("cv2")
    found = match_glyph_shapes(_png(_widget()), STRIP_BOX, PICTURE_BOX)
    assert found["ok"] is True, found
    assert found["method"] == "chamfer"
    assert found["glyphs"] == 3, found
    assert found["shapes"] == 3, found
    assert found["all_sure"] is True, found
    for row in found["rows"]:
        assert row["best_score"] < 1.2, row


def test_an_identical_twin_in_the_picture_is_reported_as_unsure():
    """Two same strokes in the picture mean no honest winner: the row says unsure."""
    pytest.importorskip("cv2")
    canvas = _widget()
    _ring(canvas, 240, 60, 41)
    match = match_glyph_shapes(_png(canvas), STRIP_BOX, PICTURE_BOX)
    assert match["ok"] is True, match
    assert any(row["sure"] is False for row in match["rows"]), match
    assert match["all_sure"] is False, match

def _sprite() -> tuple[bytes, list[list[int]]]:
    """The widget's own target strip: three glyphs on a transparent ground, with its declared boxes."""
    import numpy as np

    layer = np.zeros((40, 120, 4), dtype="uint8")
    plane = np.full((40, 120), 220, dtype="uint8")
    boxes = [[4, 6, 24, 24], [44, 6, 24, 24], [80, 6, 24, 24]]
    _ring(plane, 5, 7, 21)
    _zig(plane, 46, 8, 20, 8)
    _triangle(plane, 82, 7, 22)
    ink = plane < 200
    layer[:, :, :3] = np.dstack([plane, plane, plane])
    layer[:, :, 3] = np.where(ink, 255, 0)
    buffer = io.BytesIO()
    Image.fromarray(layer, "RGBA").save(buffer, format="PNG")
    return buffer.getvalue(), boxes


def _tiles(canvas, shape_at: dict[str, tuple[int, int]]) -> list[list[int]]:
    """Three tiles of the picture, each holding one shape at a known spot."""
    cells = []
    for index, (name, (x, y)) in enumerate(shape_at.items()):
        left = 4 + index * 96
        cells.append([left, 40, 92, 138])
        if name == "ring":
            _ring(canvas, x, y, 41)
        elif name == "zig":
            _zig(canvas, x, y, 42, 26)
        else:
            _triangle(canvas, x, y, 46)
    return cells


def test_the_declared_strip_order_picks_the_tiles_in_that_order():
    """The challenge names its glyphs and their order; the reading answers in that order."""
    pytest.importorskip("cv2")
    import numpy as np

    sprite, boxes = _sprite()
    canvas = np.full((178, 296, 3), 220, dtype="uint8")
    cells = _tiles(canvas, {"triangle": (20, 58), "ring": (110, 60), "zig": (205, 60)})
    found = match_sprite_tiles(_png(canvas), sprite, boxes, cells=cells)
    assert found["ok"] is True, found
    assert found["method"] == "sprite_declared"
    assert found["glyphs"] == 3 and found["shapes"] == 3, found
    assert found["strip_ink"] == "alpha", found
    assert found["all_sure"] is True, found
    # Glyph order was ring, zig, triangle; the tiles hold them shuffled.
    assert [row["tile_box"][0] for row in found["rows"]] == [110, 205, 20], found
    assert [row["order"] for row in found["rows"]] == [0, 1, 2], found
    assert len(found["boxes_in_order"]) == 3, found


def test_a_second_same_tile_in_the_picture_is_reported_as_unsure():
    """Two matching tiles mean no honest winner: the order is not offered."""
    pytest.importorskip("cv2")
    import numpy as np

    sprite, boxes = _sprite()
    canvas = np.full((178, 296, 3), 220, dtype="uint8")
    cells = _tiles(canvas, {"ring": (110, 60), "zig": (205, 60), "triangle": (20, 58)})
    _ring(canvas, 220, 100, 41)  # a twin ring inside the last tile
    found = match_sprite_tiles(_png(canvas), sprite, boxes, cells=cells)
    assert found["ok"] is True, found
    assert any(row["sure"] is False for row in found["rows"]), found
    assert found["all_sure"] is False and found["boxes_in_order"] == [], found


def test_a_strip_drawn_at_device_scale_is_rescaled_and_a_fictional_one_is_refused():
    """The widget's own 2x factor is forgiven; a scale that fits nothing is not invented."""
    pytest.importorskip("cv2")
    import numpy as np

    sprite, boxes = _sprite()
    canvas = np.full((178, 296, 3), 220, dtype="uint8")
    cells = _tiles(canvas, {"ring": (110, 60), "zig": (205, 60), "triangle": (20, 58)})
    doubled = [[x * 2, y * 2, w * 2, h * 2] for x, y, w, h in boxes]
    found = match_sprite_tiles(_png(canvas), sprite, doubled, cells=cells)
    assert found["ok"] is True, found
    assert found["glyphs"] == 3, found
    assert found.get("strip_scale") == 0.5, found
    refused = match_sprite_tiles(_png(canvas), sprite, [[4, 6, 500, 30], [44, 6, 500, 30]], cells=cells)
    assert refused["ok"] is False and "no device-pixel scale fits them" in refused["reason"], refused
