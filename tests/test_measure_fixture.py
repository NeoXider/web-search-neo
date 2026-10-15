"""Fixture test: the real OpenModel TCaptcha picture, scored per published grid cell.

The picture is a 3x2 grid of photo tiles (672x480); the challenge publishes that geometry as
image_size, and the ground truth tile is recorded in the fixture itself (column 2, row 1 of the
grid). No network is sent: the JPEG and the challenge record are read from fixtures/tcaptcha/.

The honest outcome today is refusal - nothing on a photo grid clears the dual shape gate - so
this test pins two things at once: the correct tile ranks FIRST in the evidence, and no box may
be spent. Both need a real CJK face to render the target; where none exists the scorer says so
("fallback") and the ranking claims are skipped rather than faked.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from web_search_neo import captcha_actions
from web_search_neo.captcha_solvers import word_shape

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tcaptcha"


def _load() -> tuple[dict, bytes]:
    record = json.loads((FIXTURE_DIR / "om_1865.json").read_text(encoding="utf-8"))
    image = (FIXTURE_DIR / "om_1865.jpg").read_bytes()
    return record, image


def _truth() -> tuple[int, int, int, int]:
    record, _ = _load()
    return tuple(record["ground_truth"]["tile"])


def _centre(box: tuple[float, float, float, float]) -> tuple[float, float]:
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


def _inside(point: tuple[float, float], tile: tuple[int, int, int, int]) -> bool:
    x, y, w, h = tile
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def test_cells_follow_the_published_geometry_not_a_guess():
    record, _ = _load()
    width, height = record["image_size"]
    truth = _truth()
    cells = word_shape.split_cells(width, height, record.get("click_cfg"))
    assert len(cells) == 6
    assert all((w, h) == (224, 240) for _, _, w, h in cells)
    assert truth in cells


def test_top_ranked_cell_is_the_ground_truth_tile():
    record, image = _load()
    from PIL import Image

    pixels = np.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=np.uint8)
    truth = _truth()
    cells = word_shape.split_cells(*record["image_size"], record.get("click_cfg"))
    matches = word_shape.score_cells(pixels, record["targets"][0], cells, hue=(0.10, 0.20), sat_min=0.45)
    assert len(matches) == 6
    if not _inside(_centre(matches[0].box), truth):
        assert word_shape.font_note() == "fallback", "the truth tile ranked second with a real font"
        pytest.skip("no CJK face available on this platform")


def test_weak_shape_match_is_refused_and_no_answer_is_sent():
    record, image = _load()
    answer = captcha_actions.measure_challenge(record, image)
    measurement = answer["measurement"]
    assert measurement["sure"] is False
    assert "boxes" not in measurement
    assert measurement["tried_bands"] == 6
    cells = measurement["cells"]
    assert len(cells) == 6
    truth = _truth()
    best = max(cells, key=lambda item: 0.5 * item["iou"] + 0.5 * max(0.0, item["correlation"]))
    if not _inside(_centre(tuple(best["box"])), truth):
        assert measurement["font"] == "fallback", "the truth tile ranked second with a real font"
        pytest.skip("no CJK face available on this platform")
