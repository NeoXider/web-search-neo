"""Tests for the shape matcher used by image/word selection captchas."""

from __future__ import annotations

import numpy as np
import pytest

ws = pytest.importorskip("web_search_neo.captcha_solvers.word_shape")

GOLD = (255, 210, 0)


def _canvas(width: int = 120, height: int = 80) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


def _paint(pixels: np.ndarray, box: tuple[int, int, int, int], colour: tuple[int, int, int]) -> None:
    x, y, w, h = box
    pixels[y : y + h, x : x + w] = colour


def test_color_mask_finds_the_flat_word_colour_only():
    pixels = _canvas()
    _paint(pixels, (4, 4, 30, 20), GOLD)  # the word: saturated gold
    _paint(pixels, (60, 10, 40, 40), (30, 40, 60))  # background: dark and desaturated
    mask = ws.color_mask(pixels, hue=(0.10, 0.20), sat_min=0.35)
    assert mask[4, 4] and not mask[60, 60]
    boxes = ws.candidate_boxes(mask, min_area=100)
    assert len(boxes) == 1
    assert boxes[0] == (4, 4, 30, 20)


def test_wrapping_hue_band_covers_a_red_word():
    pixels = _canvas()
    _paint(pixels, (10, 10, 20, 20), (220, 20, 20))
    mask = ws.color_mask(pixels, hue=(0.95, 0.05), sat_min=0.3)
    assert mask[20, 20]
    assert not mask[0, 0]


def test_candidate_boxes_orders_by_area_and_drops_noise():
    pixels = _canvas()
    _paint(pixels, (0, 0, 10, 10), (255, 0, 255))  # 100 px
    _paint(pixels, (40, 40, 30, 20), (255, 0, 255))  # 600 px
    mask = ws.color_mask(pixels, hue=(0.75, 0.92), sat_min=0.3)
    boxes = ws.candidate_boxes(mask, min_area=150)
    assert boxes == [(40, 40, 30, 20)]
    # The same two blocks with a lower floor keep both, biggest first.
    both = ws.candidate_boxes(mask, min_area=50)
    assert len(both) == 2
    assert both[0][2] * both[0][3] > both[-1][2] * both[-1][3]


def test_matching_ranks_the_region_that_shares_the_word_shape():
    pixels = _canvas(width=200, height=90)
    target = ws.glyph_mask("\u4e2d", (60, 40))
    assert target.any()
    # Left region: filled exactly like the glyph. Right region: a solid bar, wrong shape.
    offset_y, offset_x = 20, 10
    rows, cols = np.nonzero(target)
    pixels[offset_y + rows, offset_x + cols] = GOLD
    _paint(pixels, (120, 20, 60, 40), GOLD)
    matches = ws.match_word_shape(pixels, "\u4e2d", hue=(0.10, 0.20), sat_min=0.35)
    assert len(matches) == 2
    assert matches[0].box == (10, 20, 60, 40)
    assert matches[0].accepted is True
    # A solid bar cannot pass for a glyph: its shape views fall well below a real fit.
    assert matches[0].iou == 1.0
    assert matches[1].iou < 0.7
    assert matches[1].correlation < 0.5
    assert matches[1].accepted is False
    assert matches[0].score > matches[1].score
    assert matches[0].evidence


def test_the_same_component_is_not_counted_twice():
    mask = np.zeros((80, 120), dtype=bool)
    mask[4:24, 4:34] = True
    boxes = ws.candidate_boxes(mask, min_area=100)
    assert boxes == [(4, 4, 30, 20)]


def test_unsure_measurement_is_refused_not_guessed():
    pixels = _canvas(width=120, height=80)
    _paint(pixels, (10, 10, 50, 25), GOLD)
    matches = ws.match_word_shape(pixels, "\u9f8d", hue=(0.10, 0.20), sat_min=0.35)
    decision = ws.choose_ordered(matches, needed=2)
    assert decision["sure"] is False
    assert decision["needed"] == 2


def test_decision_needs_a_clear_lead_over_the_runner_up():
    strong = ws.ShapeMatch(box=(0, 0, 10, 10), iou=0.9, correlation=0.9, score=0.9)
    weak = ws.ShapeMatch(box=(20, 0, 10, 10), iou=0.6, correlation=0.6, score=0.6)
    near_second = ws.ShapeMatch(box=(40, 0, 10, 10), iou=0.85, correlation=0.85, score=0.85)
    sure = ws.choose_ordered([strong, weak], needed=1)
    assert sure["sure"] is True
    assert sure["boxes"] == [(0, 0, 10, 10)]
    unclear = ws.choose_ordered([strong, near_second], needed=1)
    assert unclear["sure"] is False
    assert "lead" in unclear


def test_empty_word_or_empty_mask_produces_no_candidates():
    pixels = _canvas()
    assert ws.match_word_shape(pixels, "", hue=(0.1, 0.2)) == []
    assert ws.match_word_shape(pixels, "\u4e2d", hue=(0.1, 0.2)) == []


def test_cache_key_changes_with_the_picture_and_the_word():
    pixels = _canvas()
    other = pixels.copy()
    other[0, 0] = (255, 255, 255)
    assert ws.cache_key(pixels, "\u4e2d") == ws.cache_key(pixels, "\u4e2d")
    assert ws.cache_key(pixels, "\u4e2d") != ws.cache_key(other, "\u4e2d")
    assert ws.cache_key(pixels, "\u4e2d") != ws.cache_key(pixels, "\u6557")
