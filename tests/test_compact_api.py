from __future__ import annotations

import asyncio
import json

import pytest
from selenium.common.exceptions import WebDriverException

import main


INPUT_ACTIONS = ("input", "press_keys", "pointer", "touch")


def _published_properties(action: str) -> dict:
    schema = asyncio.run(main.web_info("action_schema", {"action": action}))
    return schema["input_schema"]["properties"]


def test_compact_web_action_runs_ordered_game_workflow(local_site):
    async def exercise():
        return await main.web_action(
            [
                {
                    "action": "open",
                    "url": f"{local_site.base_url}/game",
                    "session_id": "compact-game",
                    "headless": True,
                    "profile_mode": "temporary",
                },
                {
                    "action": "render",
                    "mode": "step",
                    "session_id": "compact-game",
                },
                {
                    "action": "input",
                    "session_id": "compact-game",
                    "target_selector": "#game-canvas",
                    "key_actions": [{"key": "S", "action": "hold"}],
                    "wait_seconds": 0,
                },
                {
                    "action": "input",
                    "session_id": "compact-game",
                    "key_actions": [
                        {"key": "W", "action": "hold"},
                        {"key": "S", "action": "release"},
                        {"key": "SPACE", "action": "tap"},
                        {"key": "E", "action": "tap"},
                    ],
                    "pointer_actions": [
                        {"action": "hover", "x": 200, "y": 150},
                        {
                            "action": "move",
                            "x": 10,
                            "y": -5,
                            "coordinate_mode": "delta",
                        },
                    ],
                    "wait_seconds": 0,
                },
                {"action": "step", "frames": 2, "session_id": "compact-game"},
                {"action": "release_inputs", "session_id": "compact-game"},
                {"action": "render", "mode": "normal", "session_id": "compact-game"},
                {"action": "close", "session_id": "compact-game"},
            ]
        )

    try:
        result = asyncio.run(exercise())
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    assert result["success"] is True
    assert result["completed_count"] == 8
    mixed = result["results"][3]["data"]
    assert mixed["held_keys"] == ["W"]
    assert mixed["frames_advanced"] == 1
    assert mixed["pointer_actions"][1]["x"] == 210
    assert mixed["pointer_actions"][1]["y"] == 145
    assert result["results"][4]["data"]["frames"] == 2
    assert result["results"][5]["data"]["held_keys"] == []
    assert result["results"][7]["data"]["closed"] is True


def test_input_actions_publish_the_fast_defaults_models_actually_send():
    # A default that only exists inside browser_tools is a default nobody uses:
    # the model sends what the published schema shows.
    for action in INPUT_ACTIONS:
        properties = _published_properties(action)
        assert properties["wait_seconds"]["default"] == 0.0, action
        assert properties["include_summary"]["default"] is True, action
    assert _published_properties("step")["include_summary"]["default"] is True


def test_input_actions_accept_include_summary_over_the_dispatcher(local_site):
    async def exercise():
        return await main.web_action(
            [
                {
                    "action": "open",
                    "url": f"{local_site.base_url}/game",
                    "session_id": "compact-summary",
                    "headless": True,
                    "profile_mode": "temporary",
                },
                {"action": "render", "mode": "step", "session_id": "compact-summary"},
                {
                    "action": "input",
                    "session_id": "compact-summary",
                    "key_actions": [{"key": "W", "action": "tap"}],
                    "include_summary": False,
                },
                {
                    "action": "pointer",
                    "session_id": "compact-summary",
                    "pointer_action": "move",
                    "x": 100,
                    "y": 90,
                    "include_summary": False,
                },
                {"action": "step", "frames": 1, "session_id": "compact-summary", "include_summary": False},
                {"action": "close", "session_id": "compact-summary"},
            ]
        )

    try:
        result = asyncio.run(exercise())
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    assert result["success"] is True, result
    quiet = result["results"][2]["data"]
    assert quiet["success"] is True
    # Skipping the page read is the whole point: no summary fields come back.
    assert "url" not in quiet and "title" not in quiet
    assert result["results"][3]["data"]["success"] is True


def test_press_keys_is_reachable_from_the_compact_surface(local_site):
    properties = _published_properties("press_keys")
    assert properties["action"]["const"] == "press_keys"  # the dispatcher key stays free
    assert properties["key_action"]["enum"] == ["tap", "hold", "release"]
    assert properties["hold_frames"]["default"] == 1
    assert properties["focus_mode"]["enum"] == ["focus", "click", "none"]

    async def exercise():
        return await main.web_action(
            [
                {
                    "action": "open",
                    "url": f"{local_site.base_url}/game",
                    "session_id": "compact-keys",
                    "headless": True,
                    "profile_mode": "temporary",
                },
                {"action": "render", "mode": "step", "session_id": "compact-keys"},
                {
                    "action": "press_keys",
                    "session_id": "compact-keys",
                    "keys": ["SPACE"],
                    "target_selector": "#game-canvas",
                    "key_action": "tap",
                    "hold_frames": 3,
                },
                {
                    "action": "press_keys",
                    "session_id": "compact-keys",
                    "keys": ["W"],
                    "key_action": "hold",
                },
                {"action": "release_inputs", "session_id": "compact-keys"},
                {"action": "close", "session_id": "compact-keys"},
            ]
        )

    try:
        result = asyncio.run(exercise())
    except WebDriverException as exc:
        pytest.skip(f"Chrome/Selenium is unavailable: {exc}")
    assert result["success"] is True, result
    tapped = result["results"][2]["data"]
    assert tapped["action"] == "tap"
    assert tapped["hold_frames"] == 3
    assert tapped["frames_advanced"] == 3  # the tap survived three released frames
    assert result["results"][3]["data"]["held_keys"] == ["W"]
    assert result["results"][4]["data"]["held_keys"] == []


def test_capabilities_names_required_parameters_and_where_the_rest_live():
    document = main._capabilities()
    actions = document["actions"]
    assert set(actions) == set(main._ACTIONS)
    assert actions["open"]["required"] == ["url"]
    assert actions["pointer"]["required"] == ["pointer_action", "x", "y"]
    assert actions["press_keys"]["required"] == ["keys"]
    assert "required" not in actions["close_all"]  # nothing to send at all
    assert all(entry["summary"] for entry in actions.values())
    # Optional names are deliberately absent, so the document must say so.
    assert "action_schema" in document["discovery"]["parameters"]
    assert not any("include_summary" in json.dumps(entry) for entry in actions.values())
    assert len(json.dumps(document)) < 10_000

    assert any(
        "ref:" in pitfall and "page_outline" in pitfall for pitfall in document["pitfalls"]
    )


def test_capabilities_states_the_requirements_that_python_defaults_hide():
    document = main._capabilities()
    actions = document["actions"]
    # "input" takes both lists as optional in Python and rejects a call with
    # neither, so silence here would teach the contract only by failing.
    also_required = actions["input"]["also_required"]
    assert "key_actions" in also_required and "pointer_actions" in also_required
    assert actions["touch"]["required"] == ["touch_action"]
    assert "points" in actions["touch"]["also_required"]
    assert "release" in actions["touch"]["also_required"]  # the exempt verbs
    # An unexplained key is a key a small model skips.
    assert "also_required" in document["discovery"]["parameters"]
    # Unconditional actions keep the plain shape, so the key stays a signal.
    assert "also_required" not in actions["pointer"]
    assert "also_required" not in actions["open"]
    assert len(json.dumps(document)) < 10_000


def test_input_and_touch_reject_exactly_what_the_document_calls_required():
    # The document is only honest while it still matches the handlers; both of
    # these fail before any browser session is touched.
    result = asyncio.run(
        main.web_action(
            [
                {"action": "input", "session_id": "documented"},
                {"action": "touch", "touch_action": "tap", "session_id": "documented"},
            ],
            continue_on_error=True,
        )
    )
    assert result["failure_count"] == 2
    assert "key action or pointer action" in result["results"][0]["error"]
    assert "touch points" in result["results"][1]["error"]


def test_compact_web_action_reports_and_controls_failures(monkeypatch):
    stopped = asyncio.run(
        main.web_action(
            [
                {"action": "unknown"},
                {"action": "fetch_text", "url": "https://example.com"},
            ]
        )
    )
    assert stopped["success"] is False
    assert stopped["stopped_early"] is True
    assert stopped["completed_count"] == 1

    continued = asyncio.run(
        main.web_action(
            [{"action": "unknown"}, {"action": "close", "session_id": "missing"}],
            continue_on_error=True,
        )
    )
    assert continued["stopped_early"] is False
    assert continued["completed_count"] == 2
    assert continued["failure_count"] == 1

    @main.legacy_mcp.tool()
    async def soft_failure() -> dict:
        """Test-only action that reports a soft failure."""
        return {"success": False, "reason": "native validation blocked submission"}

    monkeypatch.setitem(
        main._ACTIONS,
        "soft_failure",
        main.ActionSpec(
            "soft_failure", soft_failure, "soft_failure", "page", "test double"
        ),
    )
    reported = asyncio.run(
        main.web_action(
            [
                {"action": "soft_failure"},
                {"action": "close", "session_id": "never-reached"},
            ]
        )
    )
    assert reported["success"] is False
    assert reported["stopped_early"] is True
    assert reported["failure_count"] == 1
    assert reported["results"][0]["data"]["reason"].startswith("native validation")


def test_compact_web_info_discovers_one_action_at_a_time():
    capabilities = asyncio.run(main.web_info())
    assert capabilities["public_tools"] == ["web_info", "web_action"]
    assert "action_types" not in capabilities
    assert capabilities["discovery"]["params_example"] == {"action": "input"}

    schema = asyncio.run(main.web_info("action_schema", {"action": "render"}))
    assert schema["action"] == "render"
    assert schema["input_schema"]["properties"]["mode"]["enum"] == [
        "normal",
        "throttled",
        "step",
    ]

    open_schema = asyncio.run(main.web_info("action_schema", {"action": "open"}))
    assert open_schema["input_schema"]["properties"]["headless"]["default"] is None
    assert open_schema["input_schema"]["properties"]["profile_mode"]["default"] == "current"
    assert open_schema["input_schema"]["properties"]["tab_group"]["default"] == "🟢 AI"

    open_many_schema = asyncio.run(
        main.web_info("action_schema", {"action": "open_many"})
    )
    assert open_many_schema["input_schema"]["properties"]["headless"]["default"] is None
    assert open_many_schema["input_schema"]["properties"]["profile_mode"]["default"] == "current"
    assert open_many_schema["input_schema"]["properties"]["tab_group"]["default"] == "🟢 AI"

    setup_schema = asyncio.run(
        main.web_info("action_schema", {"action": "setup_current_chrome"})
    )
    assert setup_schema["input_schema"]["properties"]["wait_seconds"]["default"] == 1.0

    with pytest.raises(ValueError, match="Unknown action schema"):
        asyncio.run(main.web_info("action_schema", {"action": "unknown"}))


def test_scroll_screenshot_pagination_and_skill_are_published_for_small_models():
    scroll = asyncio.run(main.web_info("action_schema", {"action": "scroll"}))
    assert scroll["input_schema"]["required"] == ["action", "delta_y"]
    scroll_properties = scroll["input_schema"]["properties"]
    assert scroll_properties["delta_x"]["default"] == 0.0
    assert scroll_properties["x"]["default"] is None
    assert scroll_properties["wait_seconds"]["default"] == 0.1
    assert "positive" in scroll["notes"]["direction"]

    screenshot = asyncio.run(main.web_info("action_schema", {"action": "screenshot"}))
    shot_properties = screenshot["params_schema"]["properties"]
    assert shot_properties["mode"]["anyOf"][0]["enum"] == [
        "viewport",
        "full_page",
        "region",
    ]
    assert shot_properties["width"]["default"] is None
    assert screenshot["notes"]["region"].startswith("requires x/y/width/height")

    elements = asyncio.run(main.web_info("action_schema", {"action": "page_elements"}))
    assert elements["params_schema"]["properties"]["offset"]["default"] == 0
    assert "next_offset" in elements["notes"]["pagination"]

    skill = asyncio.run(main.web_info("skill"))
    assert [step["step"] for step in skill["loop"]] == ["inspect", "act", "verify"]
    assert "Positive" in skill["scroll"]["direction"]
    assert "selector filter" in skill["elements"]["filtering"]
    assert "not CSS" in skill["elements"]["refs"]
    assert "timeout_seconds" in skill["schema"]["timeouts"]
    assert "wait_seconds" in skill["schema"]["timeouts"]
    assert "timeout_ms does not exist" in skill["schema"]["timeouts"]
    assert "background" in skill["focus"]["default"]
    assert "Only web_action show" in skill["focus"]["opt_in"]
    assert "never minimizes, maximizes, restores, or resizes" in skill["focus"]["window_state"]
    assert len(skill["forms"]["final_submit_guard"]) == 3
    assert len(json.dumps(skill)) < 4_000
    skill_schema = asyncio.run(main.web_info("action_schema", {"action": "skill"}))
    assert skill_schema["params_schema"].get("properties", {}) == {}


def test_show_schema_and_dispatch_make_foreground_an_explicit_opt_in(monkeypatch):
    schema = asyncio.run(main.web_info("action_schema", {"action": "show"}))
    assert schema["input_schema"]["required"] == ["action"]
    assert set(schema["input_schema"]["properties"]) == {"action", "session_id"}
    assert schema["input_schema"]["properties"]["session_id"]["default"] == "default"
    assert "only action" in schema["notes"]["only_foreground"]
    assert "No minimize, maximize, restore, resize" in schema["notes"]["window_state"]

    monkeypatch.setattr(
        main.browser_tools,
        "show_session",
        lambda session_id: {
            "success": True,
            "session_id": session_id,
            "focus_requested": True,
            "warning": "foreground may interrupt the user",
        },
    )
    result = asyncio.run(
        main.web_action([{"action": "show", "session_id": "explicit"}])
    )
    assert result["success"] is True
    assert result["results"][0]["data"]["focus_requested"] is True
    assert result["results"][0]["data"]["session_id"] == "explicit"


def test_small_model_parameter_guesses_are_rejected_with_the_published_fix():
    bad_scroll = asyncio.run(
        main.web_action([{"action": "scroll", "delta_y": 600, "timeout_ms": 1000}])
    )
    assert bad_scroll["success"] is False
    assert "timeout_ms" in bad_scroll["results"][0]["error"]
    assert "timeout_seconds" not in bad_scroll["results"][0]["error"]  # scroll uses wait_seconds

    with pytest.raises(ValueError) as failure:
        asyncio.run(
            main.web_info(
                "page_elements", {"session_id": "unused", "selector": "button.apply"}
            )
        )
    assert "selector" in str(failure.value)
    assert "offset" in str(failure.value)
