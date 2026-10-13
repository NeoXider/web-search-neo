"""Tests for the protocol-side solver of a Tencent image/word challenge."""

from __future__ import annotations

import hashlib
import urllib.parse

import pytest

from web_search_neo.captcha_solvers import tc_image_solve as solve
from web_search_neo.captcha_solvers import tc_protocol

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


def _opener(routes: dict[str, tuple[int, bytes]]):
    calls: list[tuple[str, dict]] = []

    def open_url(url: str, headers: dict) -> tuple[int, bytes]:
        for needle, answer in routes.items():
            if needle in url:
                calls.append((url, headers))
                return answer
        raise AssertionError(f"unexpected url {url}")

    open_url.calls = calls  # type: ignore[attr-defined]
    return open_url


def _prehandle(url: str) -> dict:
    return {
        "state": 1,
        "capclass": "1",
        "subcapclass": "1885",
        "sess": "s1sess",
        "sid": "7515866774446780416",
        "randstr": "@xyz",
        "data": {
            "comm_captcha_cfg": {
                "tdc_path": "/tdc.js?a=1",
                "pow_cfg": {"prefix": "deadbeef#", "md5": hashlib.md5(b"deadbeef#7").hexdigest()},
            },
            "dyn_show_info": {
                "instruction": "\u201c\u68a8\u201d",
                "show_type": "click_image_uncheck",
                "bg_elem_cfg": {
                    "size_2d": [672, 480],
                    "img_url": "/cap_union_new_getcapbysig?img_index=1",
                    "click_cfg": {"data_type": ["DynAnswerType_UC"]},
                },
            },
        },
    }


def jsonp(payload: dict) -> bytes:
    import json

    return f"cb({json.dumps(payload, ensure_ascii=False)})".encode("utf-8")


def test_pow_is_solved_by_searching_the_prefix_the_challenge_gave():
    target = hashlib.md5(b"deadbeef#42").hexdigest()
    answer = solve.solve_pow("deadbeef#", target)
    assert answer == {"solved": True, "salt": "42", "tried": 43}


def test_pow_reports_no_answer_instead_of_guessing():
    answer = solve.solve_pow("", "")
    assert answer["solved"] is False
    assert "pow_cfg" in answer["why"]
    stalled = solve.solve_pow("deadbeef#", "0" * 32, limit=5)
    assert stalled["solved"] is False
    assert "5 tries" in stalled["why"]


def test_prepare_reads_the_challenge_and_its_picture_without_verifying():
    opener = _opener(
        {
            "cap_union_prehandle": (200, jsonp(_prehandle(""))),
            "getcapbysig": (200, PNG),
        }
    )
    challenge = solve.prepare(opener, "https://console-dev.openmodel.ai/auth")
    assert challenge["challenge"] == "image_select"
    assert challenge["targets"] == ["\u68a8"]
    assert challenge["image_bytes"] == PNG
    assert challenge["pow_answer"]["solved"] is True
    assert not any("new_verify" in url for url, _ in opener.calls)


def test_a_picture_that_cannot_be_read_aborts_before_any_verify():
    opener = _opener(
        {
            "cap_union_prehandle": (200, jsonp(_prehandle(""))),
            "getcapbysig": (403, b""),
        }
    )
    with pytest.raises(solve.SolveAborted) as caught:
        solve.prepare(opener, "https://example.test/login")
    assert "HTTP 403" in str(caught.value)


def test_collect_keeps_the_order_and_uses_region_centres():
    answer = solve.collect_payload([(10, 20, 40, 20), (100, 5, 20, 10)], image_size=(672, 480))
    clicks = answer["clicks"]
    assert [c["x"] for c in clicks] == [30, 110]
    assert [c["y"] for c in clicks] == [30, 10]
    # Later clicks take longer: the pointer has to travel from the previous one.
    assert clicks[1]["travel_ms"] > clicks[0]["travel_ms"]
    assert answer["image_size"] == [672, 480]


def test_verify_params_only_carry_fields_the_challenge_supplied():
    challenge = {"sess": "s1", "randstr": "@a", "sid": "s", "pow_answer": "7"}
    params = solve.verify_params(challenge, {"clicks": [{"x": 3, "y": 4}], "pow_answer": "7"}, "https://x.test/p")
    assert params["sess"] == "s1"
    assert params["randstr"] == "@a"
    assert params["pow_answer"] == "7"
    assert params["u"] == "https://x.test/p"
    assert "dyn_clicks%5E3%7C4" in urllib.parse.quote(solve._answer_string({"clicks": [{"x": 3, "y": 4}]}))
    assert params["collect"] == "dyn_clicks%5E3%7C4"


def test_finish_reports_the_ticket_when_tencent_accepts():
    opener = _opener({"cap_union_new_verify": (200, jsonp({"verify": {"state": 1, "ticket": "t1", "randstr": "@r"}}))})
    challenge = {
        "sess": "s1",
        "sid": "s",
        "randstr": "@a",
        "image_size": [672, 480],
        "pow_answer": "7",
    }
    result = solve.finish(opener, challenge, [(10, 10, 30, 20)], "https://x.test/p")
    assert result["ok"] is True
    assert result["ticket"] == "t1"
    assert result["randstr"] == "@r"
    assert result["clicks"][0]["x"] == 25


def test_finish_says_why_an_answer_was_rejected():
    opener = _opener({"cap_union_new_verify": (200, jsonp({"verify": {"state": 2, "msg": "\u9a8c\u8bc1\u9519\u8bef"}}))})
    challenge = {"sess": "s", "image_size": [10, 10], "pow_answer": "1"}
    result = solve.finish(opener, challenge, [(0, 0, 4, 4)], "https://x.test/p")
    assert result["ok"] is False
    assert result["why"]


def test_finish_refuses_to_verify_without_a_solved_pow():
    opener = _opener({})
    result = solve.finish(opener, {"pow_answer": ""}, [(0, 0, 1, 1)], "https://x.test/p")
    assert result == {"ok": False, "why": "no pow answer was solved"}
    assert opener.calls == []


def test_http_refusal_is_reported_as_a_refusal():
    opener = _opener({"cap_union_new_verify": (403, b"")})
    challenge = {"sess": "s", "image_size": [10, 10], "pow_answer": "1"}
    result = solve.finish(opener, challenge, [(0, 0, 4, 4)], "https://x.test/p")
    assert result["ok"] is False
    assert "HTTP 403" in result["why"]
