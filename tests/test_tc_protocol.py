"""Tests for the protocol reader of a Tencent TCaptcha challenge."""

from __future__ import annotations

import json

import pytest

from web_search_neo.captcha_solvers import tc_protocol as proto

SAMPLE = {
    "state": 1,
    "capclass": "1",
    "subcapclass": "1885",
    "sess": "s1abc" + "x" * 300,
    "sid": "7515866774446780416",
    "randstr": "",
    "data": {
        "comm_captcha_cfg": {
            "tdc_path": "/tdc.js?app_data=1&appid=196724674",
            "pow_cfg": {"prefix": "449ff08c2e547974#", "md5": "c9424af0bfb7b8b0"},
        },
        "dyn_show_info": json.dumps(
            {
                "instruction": "\u201c\u68a8\u201d",
                "show_type": "click_image_uncheck",
                "lang": "zh-CN",
                "bg_elem_cfg": {
                    "size_2d": [672, 480],
                    "img_url": "/cap_union_new_getcapbysig?img_index=1&sess=s1abc",
                    "click_cfg": {"mark_style": "mask", "data_type": ["DynAnswerType_UC"]},
                },
            }
        ),
    },
}


def _open(status: int, body: bytes):
    def runner(url: str, headers: dict[str, str]):
        runner.calls = getattr(runner, "calls", [])
        runner.calls.append((url, headers))
        return status, body

    return runner


def jsonp(payload: dict) -> bytes:
    return f'captcha_cb({json.dumps(payload, ensure_ascii=False)})'.encode("utf-8")


def test_prehandle_params_carry_the_page_address():
    params = proto.prehandle_params("https://console-dev.openmodel.ai/auth", "196724674")
    assert params["aid"] == "196724674"
    assert params["u"] == "https://console-dev.openmodel.ai/auth"
    assert params["referer"] == "https://console-dev.openmodel.ai/auth"


def test_read_challenge_sends_the_page_origin_and_parses_everything():
    runner = _open(200, jsonp(SAMPLE))
    challenge = proto.read_challenge(
        runner, "https://console-dev.openmodel.ai/auth", aid="196724674"
    )
    url, headers = runner.calls[0]
    assert "/cap_union_prehandle?" in url
    assert headers["Referer"] == "https://console-dev.openmodel.ai/auth"
    assert headers["Origin"] == "https://console-dev.openmodel.ai"
    assert challenge["vendor"] == "tencent"
    assert challenge["challenge"] == "image_select"
    assert challenge["targets"] == ["\u68a8"]
    assert challenge["pow_prefix"] == "449ff08c2e547974#"
    assert challenge["image_url"].startswith("https://t.captcha.qq.com/cap_union_new_getcapbysig")
    assert challenge["image_size"] == [672, 480]
    assert challenge["answer_type"] == ["DynAnswerType_UC"]
    # The session is kept so a later verify call can reuse it; nothing here invents a path.
    assert challenge["sess"].startswith("s1abc")


def test_ordered_instruction_keeps_its_order():
    words = proto.target_words("Select in order: \u6557 \u554a \u63a3")
    assert words == ["\u6557", "\u554a", "\u63a3"]


def test_quoted_targets_win_over_prose():
    assert proto.target_words('Find the word "\u559c\u9e4a" in the picture') == ["\u559c\u9e4a"]


def test_bare_tokens_are_used_when_nothing_is_quoted():
    assert proto.target_words("\u6557 \u554a") == ["\u6557", "\u554a"]


def test_empty_instruction_yields_no_targets():
    assert proto.target_words("") == []


def test_challenge_kind_names_only_what_the_widget_declares():
    assert proto.challenge_kind({"show_type": "click_word_inorder"}) == "word_click"
    assert proto.challenge_kind({"show_type": "click_icon_uncheck"}) == "icon_click"
    assert proto.challenge_kind({"show_type": "slide_default"} ) == "slider"
    # An unknown show_type is reported as itself, never mapped onto something we can solve.
    assert proto.challenge_kind({"show_type": "brand_new_mode"}) == "brand_new_mode"
    assert proto.challenge_kind({}) == "unknown"


def test_refusal_is_reported_as_a_reason_not_a_guess():
    runner = _open(403, b"")
    with pytest.raises(proto.ChallengeReadError) as caught:
        proto.read_challenge(runner, "https://example.test/login")
    assert "HTTP 403" in str(caught.value)


def test_html_answer_is_not_mistaken_for_a_challenge():
    runner = _open(200, b"<html><body>blocked</body></html>")
    with pytest.raises(proto.ChallengeReadError):
        proto.read_challenge(runner, "https://example.test/login")


def test_state_other_than_one_is_a_refusal():
    payload = dict(SAMPLE, state=2)
    runner = _open(200, jsonp(payload))
    with pytest.raises(proto.ChallengeReadError) as caught:
        proto.read_challenge(runner, "https://example.test/login")
    assert "state=2" in str(caught.value)


def test_dyn_show_info_as_object_is_accepted():
    payload = json.loads(json.dumps(SAMPLE))
    payload["data"]["dyn_show_info"] = {
        "instruction": "\u201c\u67f0\u201d",
        "show_type": "click_word_uncheck",
        "bg_elem_cfg": {"size_2d": [330, 236], "img_url": "https://t.captcha.qq.com/x.png"},
    }
    runner = _open(200, jsonp(payload))
    challenge = proto.read_challenge(runner, "https://example.test/login")
    assert challenge["challenge"] == "word_click"
    assert challenge["targets"] == ["\u67f0"]
    assert challenge["image_url"] == "https://t.captcha.qq.com/x.png"
