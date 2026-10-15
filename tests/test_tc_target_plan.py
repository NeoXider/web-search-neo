"""The instruction block a Tencent word/icon challenge publishes about its own targets."""

from __future__ import annotations

import json

import pytest

from web_search_neo.captcha_solvers import tc_target_plan


def test_the_widget_names_its_targets_in_order_with_their_boxes():
    payload = {
        "data": {
            "dyn_show_info": {
                "ins_elem_cfg": {
                    "ins_list": [
                        {"text": "苹果", "ins_pos": [12, 4], "size": [30, 30], "sprite_url": "/a.png"},
                        {"text": "梨", "ins_pos": [60, 4], "size": [30, 30], "sprite_url": "/b.png"},
                    ]
                }
            }
        }
    }
    plan = tc_target_plan.target_plan(payload)
    assert plan["targets"] == ["苹果", "梨"]
    assert plan["boxes"][0] == [12, 4, 30, 30]
    assert plan["complete"] is True


def test_a_widget_that_orders_without_naming_text_says_so():
    payload = {
        "dyn_show_info": {
            "ins_elem_cfg": {"ins_list": [{"ins_pos": [0, 0], "size": [24, 24], "sprite_url": "/s.png"}]}
        }
    }
    plan = tc_target_plan.target_plan(payload)
    assert plan["boxes"] == [[0, 0, 24, 24]]
    assert plan["complete"] is False
    assert "crop each sprite_url" in plan["why"]


def test_a_challenge_that_published_nothing_refuses_rather_than_inventing():
    assert tc_target_plan.target_plan({"data": {"dyn_show_info": {}}})["why"]
    assert tc_target_plan.target_plan({"data": {"dyn_show_info": "{not json"}})["why"]
