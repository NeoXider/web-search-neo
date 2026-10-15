"""Fixture test: the live Turing click-word widget (subcapclass 2408), not the legacy grid.

On 2026-10-14 the widget on https://console-dev.openmodel.ai/auth served, for three
fresh instances, slider then click-word challenges - never the legacy 3x2 photo grid
(subcapclass 1865). This pair pins the third instance: a rendered-screenshot crop of
the 330x236 CSS picture (DPR 1) plus the record read from that same instance's DOM
and prehandle (instruction, subcapclass, native size). No network is sent here.

The honest outcome is refusal: the three ordered targets are black pictographs with
no saturated colour and no font rendering, so no band clears the dual shape gate.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from web_search_neo import captcha_actions

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tcaptcha"


def _load() -> tuple[dict, bytes]:
    record = json.loads((FIXTURE_DIR / "om_live_2408.json").read_text(encoding="utf-8"))
    image = (FIXTURE_DIR / "om_live_2408.png").read_bytes()
    return record, image


def test_live_crop_decodes_to_its_recorded_size():
    from PIL import Image

    record, image = _load()
    pixels = np.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=np.uint8)
    height, width = pixels.shape[:2]
    assert [width, height] == record["image_size"] == [330, 236]
    assert record["subcapclass"] == "2408"
    assert len(record["targets"]) == 3


def test_pictograph_targets_are_refused_and_no_answer_is_sent():
    record, image = _load()
    answer = captcha_actions.measure_challenge(
        {"targets": record["targets"], "image_size": record["image_size"], "click_cfg": {}},
        image,
    )
    measurement = answer["measurement"]
    assert measurement["sure"] is False
    assert "boxes" not in measurement
    assert measurement["tried_bands"] == 6
    assert measurement["font"] != "fallback"
    # The scorer still reports every cell it tried: all near zero, none trusted.
    cells = measurement["cells"]
    assert len(cells) == 6
    assert all(item["iou"] < 0.55 and item["correlation"] < 0.55 for item in cells)
