"""What release 1.27.0 promises, checked against the shipped package.

Every assertion here is about the published shape: the actions an agent sees in the
capabilities document, the fields it gets back, and the refusals that keep an agent from
spending a captcha attempt or guessing at a page's prose. Nothing contacts a site.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from web_search_neo import browser_tools, captcha_actions, main
from web_search_neo.audit import api_errors, probe_journal
from web_search_neo.captcha_solvers import classify, tc_image_solve, tc_protocol, word_shape
from web_search_neo.contract import budget, examples

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_consistent_across_the_package_and_the_companion():
    assert main.__version__ == "1.27.0"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8"))
    assert 'version = "1.27.0"' in pyproject
    assert manifest["version"] == "1.27.0"


def test_capabilities_publish_the_two_new_habits_under_the_written_budget():
    document = main._capabilities()
    encoded = json.dumps(document)
    assert len(encoded) <= budget.CONTRACT_BUDGET, (
        f"contract grew to {len(encoded)} against the written budget {budget.CONTRACT_BUDGET}"
    )
    actions = document["actions"]
    assert "captcha_challenge" in actions and "auto_fix" in actions
    assert "site_aid" in json.dumps(actions["captcha_challenge"]) or "captcha" in encoded
    # One action with a flag, never two near-identical ones.
    assert "captcha_aid" not in actions


def test_the_recipe_and_the_pitfall_teach_the_new_order_of_work():
    recipes = json.dumps(examples._RECIPES)
    pitfalls = json.dumps(examples._PITFALLS)
    assert "captcha_challenge" in recipes and "auto_fix" in recipes
    assert "verify only when sure" in pitfalls


def test_a_challenge_this_machine_cannot_measure_says_what_it_needs():
    report = classify.classify_captcha(
        texts=["select in order 苹果"],
        class_names=["tcaptcha_transform_dy", "tencent-captcha-dy__verify-bg-img"],
        sdk_urls=["https://t.captcha.qq.com/cap_union_prehandle?aid=196724674"],
    )
    assert report["vendor"] == "tencent" and report["challenge"] == "word_click"
    assert report["solver_available"] is False
    assert report["needs"]


def test_every_challenge_kind_the_protocol_can_name_is_a_known_word():
    # challenge_kind reads the widget's own dyn_show_info block, which is where show_type lives.
    kinds = {
        tc_protocol.challenge_kind({"show_type": show})
        for show in (
            "slide",
            "click_word",
            "click_icon",
            "click_image_uncheck",
            "checkbox",
        )
    }
    assert kinds == {"slider", "word_click", "icon_click", "image_select", "checkbox"}
    # A protocol read and a page classification name the same thing the same way, so an agent
    # gets one vocabulary whichever side it looked at.
    for show, expected in (
        ("slide", "slider"),
        ("click_word", "word_click"),
        ("click_image_uncheck", "image_select"),
    ):
        assert tc_protocol.challenge_kind({"show_type": show}) == expected
    verdict = classify.classify_captcha(
        texts=["select in order 苹果"],
        class_names=["tcaptcha_transform_dy"],
        sdk_urls=["https://t.captcha.qq.com/cap_union_prehandle?aid=196724674"],
    )
    assert verdict["challenge"] == tc_protocol.challenge_kind({"show_type": "click_word"})


def test_the_verify_answer_is_one_verdict_not_two_spellings():
    body = json.dumps({"state": 1, "ticket": "t9", "randstr": "@r"}).encode()
    opener_calls: list[str] = []

    def opener(url: str, headers: dict) -> tuple[int, bytes]:
        opener_calls.append(url)
        if "prehandle" in url:
            return 200, _prehandle_jsonp()
        if "getcapbysig" in url:
            return 200, _tiny_png()
        return 200, body

    answer = captcha_actions.captcha_verify(
        "https://console.example.com/auth", aid="196724674", boxes=[(10, 10, 30, 20)], opener=opener
    )
    assert answer["success"] is True
    assert answer["accepted"] == answer["success"]
    assert "ok" not in answer  # the flattened shape carries one name for the verdict
    assert answer["ticket"] == "t9"
    assert any("new_verify" in url for url in opener_calls)


def test_an_unsure_measurement_and_an_unsolved_hash_are_both_refusals_that_send_nothing():
    seen: list[str] = []

    def opener(url: str, headers: dict) -> tuple[int, bytes]:
        seen.append(url)
        if "prehandle" in url:
            return 200, _prehandle_jsonp()
        return 200, _tiny_png()

    refused = captcha_actions.captcha_verify(
        "https://console.example.com/auth", aid="196724674", opener=opener
    )
    assert refused["success"] is False and refused["verified"] is False
    assert not any("new_verify" in url for url in seen)


def test_an_edge_refusal_and_an_application_refusal_are_told_apart():
    # An empty body from an edge host says the edge stopped the call; a JSON body is the
    # service speaking, even behind the same CDN.
    edge = api_errors.error_meaning(403, {}, {"cf-ray": "x", "server": "cloudflare"})
    application = api_errors.error_meaning(
        403, {"code": "FORBIDDEN", "message": "admin access required"}, {"cf-ray": "x"}
    )
    assert edge["rejected_by"] == "edge"
    assert application["rejected_by"] == "application"


def test_the_probe_journal_is_seeded_from_traffic_and_costs_no_requests(tmp_path):
    rows = [
        {
            "method": "GET",
            "url": "https://api.example.test/web/v1/orders/0f9d8c1e2a3b4c5d",
            "status": 404,
        },
        {
            "method": "GET",
            "url": "https://api.example.test/web/v1/orders/7a6b5c4d3e2f1a0b",
            "status": 404,
        },
    ]
    seeded = probe_journal.seed_from_traffic(rows, {}, root=tmp_path)
    assert seeded["hosts"] == ["api.example.test"]
    host = probe_journal.query("api.example.test", root=tmp_path)
    assert host["entries"][0]["path"].count("{id}") == 1
    assert host["entries"][0]["count"] == 2


def test_the_solver_layers_are_leaf_modules_without_browser_imports():
    for module in (tc_protocol, tc_image_solve, word_shape, classify, captcha_actions):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "browser_tools" not in source, module.__name__


def test_changelog_and_docs_carry_the_new_names():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 1.27.0" in changelog
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("captcha_challenge", "rejected_by", "probe_journal", "dry_run", "undo"):
        assert name in readme, f"README does not document {name}"
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert "Migrating to 1.27" in install


def test_the_browser_module_did_not_grow_while_the_captcha_layer_grew():
    lines = len((ROOT / "web_search_neo" / "browser_tools.py").read_text(encoding="utf-8").splitlines())
    assert lines <= 8604, f"browser_tools.py is {lines} lines; keep the captcha layer in its own modules"
    assert hasattr(browser_tools, "captcha_challenge") or "captcha_challenge" in json.dumps(
        main._capabilities()
    )


def _prehandle_jsonp() -> bytes:
    import hashlib

    payload = {
        "state": 1,
        "sess": "s",
        "sid": "7",
        "randstr": "@a",
        "data": {
            "comm_captcha_cfg": {
                "tdc_path": "/tdc.js",
                "pow_cfg": {"prefix": "p#", "md5": hashlib.md5(b"p#4").hexdigest()},
            },
            "dyn_show_info": {
                "instruction": "\u82f9\u679c",
                "show_type": "click_image_uncheck",
                "bg_elem_cfg": {
                    "size_2d": [8, 8],
                    "img_url": "/cap_union_new_getcapbysig?img_index=0",
                },
            },
        },
    }
    return ("callback(" + json.dumps(payload, ensure_ascii=False) + ")").encode()


def _tiny_png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
