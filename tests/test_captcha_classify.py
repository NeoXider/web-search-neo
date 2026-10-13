"""Captcha vendor+challenge classifier: pure string facts in, a name out."""

from __future__ import annotations

from pathlib import Path

from web_search_neo.captcha_solvers.classify import (
    classify_captcha,
    classify_from_harness,
)


def test_tencent_slider_from_class_names():
    out = classify_captcha(
        class_names=["tencent-captcha-dy__bg-placeholder--slider", "slider-handler"]
    )
    assert out["vendor"] == "tencent"
    assert out["challenge"] == "slider"
    assert out["solver_available"] is True
    assert out["evidence"]


def test_tencent_word_click_from_order_instruction():
    out = classify_captcha(
        texts=["Select in order: \u4f60\u597d\u5417"],
        class_names=["tencent-captcha-dy__wrap"],
    )
    assert out["vendor"] == "tencent"
    assert out["challenge"] == "word_click"
    assert out["solver_available"] is False
    assert "click-order" in out["needs"]


def test_tencent_icon_click_from_instruction():
    out = classify_captcha(
        texts=["Click the icon below"], sdk_urls=["https://turing.captcha.qcloud.com/TCaptcha.js"]
    )
    assert (out["vendor"], out["challenge"]) == ("tencent", "icon_click")
    assert out["solver_available"] is False


def test_aid_presence_alone_names_tencent():
    out = classify_captcha(aid_present=True)
    assert out["vendor"] == "tencent"
    assert out["solver_available"] is False


def test_recaptcha_checkbox_by_default_and_image_grid_from_text():
    checkbox = classify_captcha(class_names=["g-recaptcha"])
    assert (checkbox["vendor"], checkbox["challenge"]) == ("recaptcha", "checkbox")
    assert checkbox["solver_available"] is False
    grid = classify_captcha(
        texts=["Select all images with buses"], class_names=["g-recaptcha"]
    )
    assert grid["challenge"] == "image_select"


def test_hcaptcha_and_turnstile():
    assert classify_captcha(class_names=["h-captcha"])["vendor"] == "hcaptcha"
    turnstile = classify_captcha(sdk_urls=["https://challenges.cloudflare.com/turnstile/v0/api.js"])
    assert (turnstile["vendor"], turnstile["challenge"]) == ("turnstile", "checkbox")
    assert turnstile["solver_available"] is False


def test_geetest_slider_is_locally_solvable():
    out = classify_captcha(
        class_names=["geetest_widget", "geetest_slider_button"],
        sdk_urls=["https://static.geetest.com/v4/gt4.js"],
    )
    assert (out["vendor"], out["challenge"]) == ("geetest", "slider")
    assert out["solver_available"] is True


def test_generic_image_select_without_a_vendor():
    out = classify_captcha(texts=["Select all squares with traffic lights"])
    assert (out["vendor"], out["challenge"]) == ("generic", "image_select")
    assert out["solver_available"] is False


def test_nothing_known_is_unknown():
    out = classify_captcha()
    assert (out["vendor"], out["challenge"]) == ("unknown", "unknown")
    assert out == {
        "vendor": "unknown", "challenge": "unknown", "evidence": [],
        "solver_available": False, "needs": out["needs"],
        "vendor_label": out["vendor_label"],
    }


def test_harness_adapter_merges_status_and_slider_facts():
    out = classify_from_harness(
        found={"vendor": None},
        slider={"vendor": "tcaptcha", "handle_selector": "[class*='slider-handler']"},
        status={"captcha_widgets": ['[class*="tencent-captcha-dy__"]']},
        facts={"texts": [], "class_names": [], "sdk_urls": [], "aid_present": False},
    )
    assert (out["vendor"], out["challenge"]) == ("tencent", "slider")
    assert out["solver_available"] is True


def test_harness_adapter_prefers_sitekey_vendor_and_tolerates_junk():
    out = classify_from_harness(
        found={"vendor": "recaptcha", "sitekey": "6Ltest"},
        slider={},
        status={"captcha_widgets": ["div.g-recaptcha"]},
        facts={"unexpected": "shape"},
    )
    assert out["vendor"] == "recaptcha"
    # A probe that answered garbage degrades to unknown, never an exception.
    assert classify_from_harness(facts=["not", "a", "dict"])["vendor"] == "unknown"


def test_leaf_keeps_no_orchestrator_or_browser_imports():
    source = Path(__file__).resolve().parents[1].joinpath(
        "web_search_neo", "captcha_solvers", "classify.py"
    ).read_text(encoding="utf-8")
    for banned in ("web_search_neo.main", "web_search_neo.browser_tools", "selenium", "numpy", "PIL", "cv2"):
        assert banned not in source
