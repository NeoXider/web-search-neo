from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts import staged_batch


def _config(items, stages):
    return {
        "version": 1,
        "max_concurrency": 4,
        "settle_seconds": 0,
        "items": items,
        "stages": stages,
    }


def _item(number: int) -> dict:
    return {
        "id": f"item-{number}",
        "session_id": f"slot-{number}",
        "vars": {"slug": f"page-{number}"},
    }


class FakeNeo:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.fields: dict[str, list[dict]] = {}
        self.text: dict[str, str] = {}

    async def action(self, actions, continue_on_error):
        assert continue_on_error is False
        session = actions[0].get("session_id", "")
        name = actions[0]["action"]
        self.events.append((f"{name}-start", session))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.005)
        self.active -= 1
        self.events.append((f"{name}-end", session))
        return {
            "success": True,
            "requested_count": len(actions),
            "completed_count": len(actions),
            "failure_count": 0,
            "stopped_early": False,
            "results": [],
        }

    async def info(self, topic, params):
        session = params["session_id"]
        if topic == "page_elements":
            return {"fields": self.fields.get(session, [])}
        if topic == "page_text":
            return {"text": self.text.get(session, "ordinary page")}
        raise AssertionError(topic)


def test_stage_barrier_templates_actions_and_caps_batch_at_four(tmp_path: Path):
    fake = FakeNeo()
    items = [_item(number) for number in range(1, 5)]
    config = _config(
        items,
        [
            {
                "name": "open",
                "actions": [
                    {
                        "action": "open",
                        "url": "https://example.test/{slug}",
                        "profile_mode": "current",
                    }
                ],
            },
            {
                "name": "inspect-choice",
                "actions": [
                    {
                        "action": "click_text",
                        "text": "Choice for {item_id}",
                        "exact": True,
                        "role": "option",
                        "terminal": False,
                    }
                ],
            },
        ],
    )

    report = asyncio.run(
        staged_batch.run_workflow(
            config,
            tmp_path / "state.json",
            info_call=fake.info,
            action_call=fake.action,
        )
    )

    assert report["success"] is True
    assert report["completed"] == 4
    assert fake.max_active == 4
    last_open = max(index for index, event in enumerate(fake.events) if event[0] == "open-end")
    first_click = min(index for index, event in enumerate(fake.events) if event[0] == "click_text-start")
    assert first_click > last_open


def test_unexpected_form_pauses_only_that_item_and_skips_later_stages(tmp_path: Path):
    fake = FakeNeo()
    fake.fields["slot-1"] = [
        {"selector": "#employer-question", "visible": True, "disabled": False, "type": "text"}
    ]
    config = _config(
        [_item(1), _item(2)],
        [
            {"name": "open", "actions": [{"action": "open", "url": "https://example.test/{slug}"}]},
            {"name": "continue", "actions": [{"action": "scroll", "delta_y": 200}]},
        ],
    )

    report = asyncio.run(
        staged_batch.run_workflow(
            config,
            tmp_path / "state.json",
            info_call=fake.info,
            action_call=fake.action,
        )
    )

    by_id = {item["id"]: item for item in report["items"]}
    assert by_id["item-1"]["status"] == "paused"
    assert "unexpected visible form fields" in by_id["item-1"]["stages"][0]["reason"]
    assert by_id["item-2"]["status"] == "completed"
    assert ("scroll-start", "slot-1") not in fake.events
    assert ("scroll-start", "slot-2") in fake.events


def test_questionnaire_marker_pauses_even_when_controls_were_allowlisted(tmp_path: Path):
    fake = FakeNeo()
    fake.fields["slot-1"] = [
        {"selector": "#known", "visible": True, "disabled": False, "type": "text"}
    ]
    fake.text["slot-1"] = "Please answer the questions before continuing"
    config = _config(
        [_item(1)],
        [
            {
                "name": "open",
                "allowed_form_fields": ["#known"],
                "actions": [{"action": "open", "url": "https://example.test/{slug}"}],
            },
            {"name": "continue", "actions": [{"action": "scroll", "delta_y": 200}]},
        ],
    )

    report = asyncio.run(
        staged_batch.run_workflow(
            config,
            tmp_path / "state.json",
            info_call=fake.info,
            action_call=fake.action,
        )
    )

    assert report["paused"] == 1
    assert "questionnaire marker" in report["items"][0]["stages"][0]["reason"]
    assert ("scroll-start", "slot-1") not in fake.events


def test_question_heading_without_live_controls_is_only_boilerplate(tmp_path: Path):
    fake = FakeNeo()
    fake.text["slot-1"] = "Answer the questions only if a form appears later"
    config = _config(
        [_item(1)],
        [{"name": "open", "actions": [{"action": "open", "url": "https://example.test/{slug}"}]}],
    )

    report = asyncio.run(
        staged_batch.run_workflow(
            config,
            tmp_path / "state.json",
            info_call=fake.info,
            action_call=fake.action,
        )
    )

    assert report["success"] is True


def test_terminal_requires_named_approval_and_is_durably_at_most_once(tmp_path: Path):
    fake = FakeNeo()
    sent = False

    async def info(topic, params):
        if topic == "page_elements":
            return {"fields": []}
        if topic == "page_text":
            return {"text": "sent successfully" if sent else "ready to send"}
        raise AssertionError(topic)

    async def action(actions, continue_on_error):
        nonlocal sent
        result = await fake.action(actions, continue_on_error)
        sent = True
        return result

    config = _config(
        [_item(1)],
        [
            {
                "name": "send",
                "actions": [{"action": "submit", "form_selector": "#request"}],
                "precheck": {
                    "topic": "page_text",
                    "params": {"mode": "main", "max_chars": 2000},
                    "path": "text",
                    "contains_all": ["ready to send"],
                },
                "verify": {
                    "topic": "page_text",
                    "params": {"mode": "main", "max_chars": 2000},
                    "path": "text",
                    "contains_all": ["sent successfully"],
                },
            }
        ],
    )
    state_path = tmp_path / "state.json"

    unapproved = asyncio.run(
        staged_batch.run_workflow(
            config, state_path, info_call=info, action_call=action
        )
    )
    assert unapproved["items"][0]["stages"][0]["status"] == "paused_terminal_approval"
    assert not fake.events

    approved = asyncio.run(
        staged_batch.run_workflow(
            config,
            state_path,
            {"send"},
            info_call=info,
            action_call=action,
        )
    )
    assert approved["success"] is True
    assert len(fake.events) == 2
    assert ("submit-start", "slot-1") in fake.events

    sent = False
    # Even if another process lost/overwrote the friendly JSON journal, the
    # atomic per-attempt claim remains authoritative.
    state_path.write_text('{"version": 1, "terminal_attempts": {}}', encoding="utf-8")
    repeated = asyncio.run(
        staged_batch.run_workflow(
            config,
            state_path,
            {"send"},
            info_call=info,
            action_call=action,
        )
    )
    assert repeated["items"][0]["stages"][0]["status"] == "paused_terminal_already_attempted"
    assert len(fake.events) == 2


def test_validation_enforces_tab_limit_and_terminal_gates():
    with pytest.raises(staged_batch.ConfigError, match="1-4"):
        staged_batch.validate_config(
            _config([_item(number) for number in range(1, 6)], [{"name": "x", "actions": [{"action": "scroll"}]}])
        )

    with pytest.raises(staged_batch.ConfigError, match="escape the four-tab limit"):
        staged_batch.validate_config(
            _config([_item(1)], [{"name": "x", "actions": [{"action": "open_many"}]}])
        )

    with pytest.raises(staged_batch.ConfigError, match="precheck"):
        staged_batch.validate_config(
            _config([_item(1)], [{"name": "send", "actions": [{"action": "submit"}]}])
        )

    with pytest.raises(staged_batch.ConfigError, match="explicitly set terminal"):
        staged_batch.validate_config(
            _config(
                [_item(1)],
                [{"name": "ambiguous", "actions": [{"action": "click", "selector": "#primary"}]}],
            )
        )

    with pytest.raises(staged_batch.ConfigError, match="asserted precheck"):
        staged_batch.validate_config(
            _config(
                [_item(1)],
                [
                    {
                        "name": "send",
                        "actions": [{"action": "submit"}],
                        "precheck": {"topic": "page_text", "contains_all": []},
                        "verify": {"topic": "page_text", "contains_all": ["sent"]},
                    }
                ],
            )
        )
