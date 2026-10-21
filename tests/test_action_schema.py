from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from web_search_neo import main, plugins
from web_search_neo.contract.action_schema import action_envelope
from web_search_neo.mcp_compat import registered_tool


def test_action_envelope_unions_real_types_defaults_and_allows_extra_keys():
    names = ("search", "http_request", "look", "test_run", "macro", "wait", "fetch_text")
    sources = [
        (name, registered_tool(main.legacy_mcp, main._ACTIONS[name].tool_name).parameters)
        for name in names
    ]
    before = deepcopy(sources)
    schema = action_envelope(sources)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for action in (
        {"action": "search", "query": "fixture"},
        {"action": "http_request", "url": "https://example.test/", "query": {"page": 2}},
        {"action": "look", "steps": 2},
        {"action": "test_run", "steps": [{"action": "fetch_text", "url": "https://example.test/"}]},
        {"action": "macro", "session_id": None},
        {"action": "wait", "timeout_seconds": 10.0},
        {"action": "fetch_text", "timeout_seconds": 20.0, "future_plugin_parameter": {"retained": True}},
    ):
        validator.validate(action)
    assert not validator.is_valid({"action": "search", "query": 42})
    assert not validator.is_valid({"action": "look", "steps": "two"})
    timeout_variants = schema["properties"]["timeout_seconds"]["anyOf"]
    assert {10.0, 20.0} <= {variant.get("default") for variant in timeout_variants}
    assert sources == before


def test_action_envelope_keeps_colliding_definitions_and_nested_references():
    def source(value_type: str) -> dict:
        return {
            "type": "object",
            "properties": {"payload": {"$ref": "#/$defs/Payload"}},
            "$defs": {
                "Payload": {
                    "type": "object",
                    "properties": {"value": {"$ref": "#/$defs/Leaf~1Value~0"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "Leaf/Value~": {"type": value_type},
            },
        }

    sources = [("integer_payload", source("integer")), ("string_payload", source("string"))]
    before = deepcopy(sources)
    schema = action_envelope(sources)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate({"action": "integer_payload", "payload": {"value": 42}})
    validator.validate({"action": "string_payload", "payload": {"value": "forty-two"}})
    assert not validator.is_valid({"action": "integer_payload", "payload": {"value": True}})
    assert not validator.is_valid({"action": "string_payload", "payload": {"unknown": 1}})
    assert len(schema["$defs"]) == 4
    assert sources == before


def test_startup_plugin_parameters_survive_schema_pruning_over_stdio(tmp_path):
    plugin_file = tmp_path / "transport_schema_plugin.py"
    plugin_file.write_text(
        'from web_search_neo import plugins\n'
        'async def transport_schema_plugin_echo(transport_plugin_marker: str = "hello") -> dict:\n'
        '    return {"echo": transport_plugin_marker}\n'
        'plugins.register_action("transport_schema_plugin", transport_schema_plugin_echo)\n',
        encoding="utf-8",
    )

    async def exercise_server():
        project_root = Path(__file__).resolve().parents[1]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(project_root / "main.py")],
            cwd=str(project_root),
            env={**os.environ, plugins.ENV_VAR: str(plugin_file)},
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                schema = next(tool.inputSchema for tool in listed.tools if tool.name == "web_action")
                Draft202012Validator.check_schema(schema)
                original = {"action": "transport_schema_plugin", "transport_plugin_marker": "from-pruned-client"}
                declared = schema["properties"]["actions"]["items"]["properties"]
                pruned = {key: value for key, value in original.items() if key in declared}
                assert pruned == original
                Draft202012Validator(schema).validate({"actions": [pruned]})
                result = await session.call_tool("web_action", {"actions": [pruned]})
                payload = json.loads(result.content[0].text)
                assert result.isError is False
                assert payload["success"] is True
                assert payload["results"][0]["data"] == {"echo": "from-pruned-client"}

    asyncio.run(exercise_server())
