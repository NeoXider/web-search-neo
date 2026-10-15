"""The select-in-order reading is scored by stroke distance, not by filled overlap."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from web_search_neo.captcha_solvers.shape_match import match_glyph_shapes

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
