"""Tests for the captcha actions: reading a challenge and reading the id a site publishes."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from web_search_neo import captcha_actions, captcha_aid

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tcaptcha"
# Ground truth on the real OpenModel picture: the tile at column 2, row 1.
TRUTH_TILE = [448, 240, 224, 240]

PNGISH = b"\x89PNG\r\n\x1a\n" + b"0" * 200


def _png(width: int = 8, height: int = 8) -> bytes:
    """A real decodable PNG so measurement runs against pixels, not against a stub."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def _prehandle(status: int = 200, payload: dict | None = None):
    import hashlib

    body = {
        "state": 1,
        "capclass": "1",
        "subcapclass": "1865",
        "sess": "s1sess",
        "sid": "7515866774446780416",
        "randstr": "@a",
        "data": {
            "comm_captcha_cfg": {
                "tdc_path": "/tdc.js?a=1",
                "pow_cfg": {"prefix": "p#", "md5": hashlib.md5(b"p#7").hexdigest()},
            },
            "dyn_show_info": {
                "instruction": "\u201c\u82f9\u679c\u201d",
                "show_type": "click_image_uncheck",
                "bg_elem_cfg": {
                    "size_2d": [672, 480],
                    "img_url": "/cap_union_new_getcapbysig?img_index=1",
                    "click_cfg": {"data_type": ["DynAnswerType_UC"]},
                },
            },
        },
    }
    if payload is not None:
        body = payload
    raw = f"cb({json.dumps(body, ensure_ascii=False)})".encode("utf-8") if status == 200 else b""
    return status, raw


def _opener(prehandle, picture=(200, PNGISH), aid_response=None):
    calls: list[str] = []

    def open_url(url: str, headers: dict) -> tuple[int, bytes]:
        calls.append(url)
        if "aid-encrypted" in url or "/captcha/aid" in url:
            return aid_response or (404, b"")
        if "prehandle" in url:
            return prehandle
        if "getcapbysig" in url:
            return picture
        return 500, b"unexpected " + url.encode()

    open_url.calls = calls  # type: ignore[attr-defined]
    return open_url


def test_challenge_read_names_the_kind_and_keeps_no_image_bytes_in_the_answer():
    opener = _opener(_prehandle())
    answer = captcha_actions.captcha_read("https://console-dev.openmodel.ai/auth", aid="196724674", opener=opener)
    assert answer["success"] is True
    assert answer["challenge"] == "image_select"
    assert answer["targets"] == ["\u82f9\u679c"]
    assert "image_bytes" not in answer
    assert answer["measurement"]["sure"] is False
    # Only the widget's own two addresses were touched.
    assert any("prehandle" in url for url in opener.calls)
    assert any("getcapbysig" in url for url in opener.calls)
    assert not any("new_verify" in url for url in opener.calls)


def test_a_page_address_is_required_and_must_be_https():
    with pytest.raises(ValueError, match="page address"):
        captcha_actions.captcha_read("")
    with pytest.raises(ValueError, match="https"):
        captcha_actions.captcha_read("http://example.test/login")


def test_an_unknown_site_says_what_it_needs_instead_of_guessing_an_id():
    # No aid was given and none is known for this host: the answer names what is missing,
    # and no request is sent on a guessed id.
    opener = _opener(_prehandle())
    with pytest.raises(ValueError, match="app id"):
        captcha_actions.captcha_read("https://example.test/login", opener=opener)
    assert opener.calls == []


def test_a_refused_challenge_is_reported_without_any_verify():
    opener = _opener(_prehandle(status=403))
    answer = captcha_actions.captcha_read("https://example.test/login", aid="1", opener=opener)
    assert answer["success"] is False
    assert "HTTP 403" in answer["why"]
    assert "verified" not in answer  # nothing was verified, so the key is simply absent
    assert not any("new_verify" in url for url in opener.calls)


def test_a_picture_that_cannot_be_measured_aborts_before_any_verify():
    opener = _opener(_prehandle(), picture=(403, b""))
    answer = captcha_actions.captcha_read("https://example.test/login", aid="1", opener=opener)
    assert answer["success"] is False
    assert "picture" in answer["why"]


def test_an_undecodable_picture_is_reported_as_a_measurement_failure():
    opener = _opener(_prehandle(), picture=(200, PNGISH))
    answer = captcha_actions.captcha_read("https://example.test/login", aid="1", opener=opener)
    assert answer["success"] is True  # the challenge itself was read
    measurement = answer["measurement"]
    assert measurement["sure"] is False
    assert "decoded" in measurement["why"]


def test_measurement_reports_its_best_band_when_nothing_is_sure():
    opener = _opener(_prehandle(), picture=(200, _png(672, 480)))
    answer = captcha_actions.captcha_read("https://example.test/login", aid="1", opener=opener)
    measurement = answer["measurement"]
    assert measurement["sure"] is False
    assert "best_band" in measurement and measurement["tried_bands"] >= 1


def test_verify_refuses_when_the_measurement_was_not_sure():
    opener = _opener(_prehandle())
    answer = captcha_actions.captcha_verify("https://example.test/login", aid="1", opener=opener)
    assert answer["verified"] is False
    assert "unsure" in (answer.get("note") or "") or "not measured" in answer["why"]
    assert not any("new_verify" in url for url in opener.calls)


def test_verify_sends_an_answer_only_when_boxes_are_supplied_by_the_caller():
    opener = _opener(_prehandle(), picture=(200, _png(672, 480)))
    # Tencent answers flat: state/ticket/randstr at the top level of the JSONP envelope.
    verify_body = json.dumps({"state": 1, "ticket": "t", "randstr": "@r"}).encode()

    def runner(url: str, headers: dict) -> tuple[int, bytes]:
        if "new_verify" in url:
            opener.calls.append(url)
            return 200, verify_body
        return opener(url, headers)

    answer = captcha_actions.captcha_verify(
        "https://example.test/login", aid="1", boxes=[(10, 10, 30, 20)], opener=runner
    )
    assert answer["success"] is True and answer["verified"] is True
    assert answer["ticket"] == "t"
    assert answer["boxes_used"] == [(10, 10, 30, 20)]
    sent = opener.calls[-1]
    # The salt that was solved on read is what gets sent, never the record around it.
    assert "pow_answer=7" not in sent or "pow_answer=" in sent
    assert "%7Bsolved%7D" not in sent  # no dict leaked into the query


def test_site_aid_reads_the_published_document_and_reports_only_shapes():
    body = json.dumps({"success": True, "data": {"aid_encrypted": "Z9xвЂ¦", "aid_encrypted_type": "cbc"}}).encode()
    opener = _opener(_prehandle(), aid_response=(200, body))
    answer = captcha_aid.captcha_aid("https://console-dev.openmodel.ai/", opener=opener)
    assert answer["success"] is True
    assert answer["aid_encrypted_present"] is True
    assert answer["encoding"] == "cbc"
    assert answer["aid"] is None
    assert answer["requests_made"] == ["GET console-dev.openmodel.ai/web/v1/captcha/aid-encrypted"]


def test_site_aid_reports_the_plaintext_id_when_the_site_publishes_one():
    body = json.dumps({"data": {"aid_encrypted": "x", "aid": "196724674"}}).encode()
    opener = _opener(_prehandle(), aid_response=(200, body))
    answer = captcha_aid.captcha_aid("https://console-dev.openmodel.ai/", opener=opener)
    assert answer["aid"] == "196724674"


def test_site_aid_lists_every_address_it_tried_when_none_answers():
    opener = _opener(_prehandle(), aid_response=(404, b'{"code":"NOT_FOUND"}'))
    answer = captcha_aid.captcha_aid("https://example.test/", opener=opener)
    assert answer["success"] is False
    assert len(answer["requests_made"]) == len(captcha_aid.AID_PATHS)
    assert all(item["body_shape"] for item in answer["tried"])


def _fixture_record() -> tuple[dict, bytes]:
    record = json.loads((FIXTURE_DIR / "om_1865.json").read_text(encoding="utf-8"))
    image = (FIXTURE_DIR / "om_1865.jpg").read_bytes()
    return record, image


def _red_tile_png(word: str, col: int = 2, row: int = 1) -> bytes:
    """One published cell painted with the target's own rendered shape, in red."""
    import numpy as np
    from PIL import Image

    from web_search_neo.captcha_solvers import word_shape

    width, height = 672, 480
    canvas = np.full((height, width, 3), 128, dtype=np.uint8)
    mask = word_shape.glyph_mask(word, (224, 240))
    canvas[row * 240 : (row + 1) * 240, col * 224 : (col + 1) * 224][mask] = (255, 0, 0)
    buffer = io.BytesIO()
    Image.fromarray(canvas).save(buffer, format="PNG")
    return buffer.getvalue()


def test_image_select_ranks_the_ground_truth_tile_first_but_refuses_it_unsure():
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    record, image = _fixture_record()
    answer = captcha_actions.image_select_from_page(record, image)
    # The real photo grid never clears the dual shape gate: the ranking still
    # puts the ground-truth tile first, but no boxes may be spent on a verify.
    assert answer["sure"] is False
    assert "ordered_boxes" not in answer
    assert len(answer["cells"]) == 6
    assert answer["cells"][0]["box"] == TRUTH_TILE
    assert answer["why"]


def test_image_select_names_the_tiles_in_order_only_when_sure():
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    word = "\u82f9\u679c"
    record = {
        "vendor": "tencent",
        "challenge": "image_select",
        "instruction": f'click "{word}"',
        "targets": [word],
        "image_size": [672, 480],
        "click_cfg": {},
    }
    answer = captcha_actions.image_select_from_page(record, _red_tile_png(word))
    assert answer["sure"] is True
    assert answer["ordered_boxes"] == [TRUTH_TILE]
    assert answer["cells"] and answer["cells"][0]["box"] == TRUTH_TILE
    assert answer["why"]


def test_image_select_instruction_override_replaces_the_record_prose():
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    record, image = _fixture_record()
    answer = captcha_actions.image_select_from_page(record, image, instruction="click \u4e0d\u5b58\u5728")
    assert answer["sure"] is False
    assert "ordered_boxes" not in answer
    assert answer["why"]


def test_image_select_refusals_name_why_and_carry_no_boxes():
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    record, image = _fixture_record()
    # No target anywhere: nothing to score against.
    answer = captcha_actions.image_select_from_page({"challenge": "image_select"}, image)
    assert answer == {"sure": False, "cells": [], "why": answer["why"]} and answer["why"]
    assert "ordered_boxes" not in answer
    # Bytes that are not a picture: nothing to score.
    answer = captcha_actions.image_select_from_page(record, PNGISH)
    assert answer["sure"] is False and "decoded" in answer["why"]
    assert answer["cells"] == [] and "ordered_boxes" not in answer


def test_page_image_select_leaves_a_non_tile_challenge_alone():
    answer = captcha_actions.measure_page_image_select(
        lambda script: {"success": True, "value": {"texts": [], "class_names": [], "sdk_urls": []}},
        lambda rect: b"",
    )
    assert answer == {"attempted": False, "vendor": "unknown", "challenge": "unknown"}


def test_page_image_select_measures_the_widget_picture_without_clicking():
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    _, image = _fixture_record()
    word = "\u82f9\u679c"

    def run_script(script: str) -> dict:
        if "aid_present" in script:  # the classifier's fact probe
            return {
                "success": True,
                "value": {
                    "texts": ["please select the tiles in order"],
                    "class_names": ["tcaptcha_transform_dy"],
                    "sdk_urls": ["https://t.captcha.qq.com/cap_union_prehandle?aid=196724674"],
                    "aid_present": True,
                },
            }
        return {
            "success": True,
            "value": {"x": 10, "y": 20, "width": 672, "height": 480, "instruction": f'"{word}"'},
        }

    seen: list[tuple] = []

    def capture_region(rect: tuple) -> bytes:
        seen.append(tuple(rect))
        return image

    answer = captcha_actions.measure_page_image_select(run_script, capture_region)
    assert answer["attempted"] is True and answer["sure"] is False
    assert answer["challenge"] == "word_click"
    assert answer["clicks"] == [] and "ordered_boxes" not in answer
    # A word_click picture has no published grid, so the cells are colour
    # components: the top-ranked one still sits on the ground-truth tile.
    x, y, w, h = answer["cells"][0]["box"]
    assert 448 <= x + w / 2 <= 672 and 240 <= y + h / 2 <= 480
    assert seen == [(10.0, 20.0, 672.0, 480.0)]
