from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from web_search_neo import main, plugins
from web_search_neo.mcp_compat import tool_registry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _item_schema(schema: dict) -> dict:
    item_schema = schema["properties"]["actions"]["items"]
    while "$ref" in item_schema:
        target = schema
        for key in item_schema["$ref"].removeprefix("#/").split("/"):
            target = target[key]
        item_schema = target
    return item_schema


def _prune_action(action: dict, schema: dict) -> dict:
    """Reproduce clients that drop undeclared keys, even with extra allowed."""
    item_schema = _item_schema(schema)
    properties = item_schema.get("properties", {})
    return {key: value for key, value in action.items() if key in properties}


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


@asynccontextmanager
async def _stdio_session(env: dict[str, str] | None = None):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_ROOT / "main.py")],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def test_published_action_items_declare_real_parameters():
    tools = asyncio.run(main.mcp.list_tools())
    schema = next(tool.inputSchema for tool in tools if tool.name == "web_action")
    items = _item_schema(schema)
    assert items["type"] == "object"
    assert items["required"] == ["action"]
    assert items["properties"]["action"]["type"] == "string"
    assert items["additionalProperties"] is True
    real_parameters = {
        name
        for spec in main._ACTIONS.values()
        for name in main._argument_model(spec.tool_name).model_fields
    }
    assert real_parameters <= items["properties"].keys()


def test_published_schema_pruning_preserves_fetch_text_over_stdio(local_site):
    async def exercise_server() -> None:
        async with _stdio_session() as session:
            listed = await session.list_tools()
            schema = next(tool.inputSchema for tool in listed.tools if tool.name == "web_action")
            original = {"action": "fetch_text", "url": f"{local_site.base_url}/page"}
            pruned = _prune_action(original, schema)
            result = await session.call_tool("web_action", {"actions": [pruned]})
            payload = _payload(result)
            assert payload["success"] is True, payload
            assert pruned == original
            assert result.isError is False
            assert payload["results"][0]["action"] == "fetch_text"
            assert "Local fixture" in payload["results"][0]["data"]

            capabilities = _payload(await session.call_tool("web_info", {"topic": "capabilities"}))
            assert capabilities["public_tools"] == ["web_info", "web_action"]
            contract = json.dumps(capabilities)
            assert "actions=[...]" in contract
            assert "properties.action" in contract
            action_schema = _payload(await session.call_tool(
                "web_info", {"topic": "action_schema", "params": {"action": "fetch_text"}}
            ))
            assert action_schema["input_schema"]["properties"]["action"]["const"] == "fetch_text"

    asyncio.run(exercise_server())


def test_aliases_and_action_parameters_over_stdio(local_site, tmp_path):
    macro_root = tmp_path / "macros"
    macro_root.mkdir()
    (macro_root / "transport-fixture.json").write_text(
        json.dumps({"name": "transport-fixture", "steps": [{"action": "fetch_text", "url": "https://example.test/"}]}),
        encoding="utf-8",
    )
    env = {**os.environ, "WEB_SEARCH_NEO_MACRO_ROOT": str(macro_root)}
    env.pop("WEB_SEARCH_NEO_PROJECT_ROOT", None)

    async def exercise_server() -> None:
        async with _stdio_session(env) as session:
            aliases = ("type", "name", "tool", "command", "op", "operation", "method")
            batch = [{key: "fetch_text", "url": f"{local_site.base_url}/page"} for key in aliases]
            batch.extend([
                {"action": "fetch_text", "type": " FETCH_TEXT ", "url": f"{local_site.base_url}/page"},
                {"type": " FETCH_TEXT ", "op": "fetch_text", "url": f"{local_site.base_url}/page"},
                {"action": "http_request", "method": "GET", "url": f"{local_site.base_url}/page"},
                {"type": "http_request", "method": "GET", "url": f"{local_site.base_url}/page"},
                {"action": "macro", "op": "list"},
                {"type": "macro", "op": "show", "name": "transport-fixture"},
                {"op": "macro", "name": "transport-fixture"},
                {"operation": "macro", "op": "show", "name": "transport-fixture"},
            ])
            result = await session.call_tool("web_action", {"actions": batch})
            payload = _payload(result)
            assert result.isError is False, payload
            assert payload["success"] is True, payload
            assert payload["completed_count"] == len(batch)
            assert [item["action"] for item in payload["results"]] == ["fetch_text"] * 9 + ["http_request"] * 2 + ["macro"] * 4
            for item in payload["results"][:9]:
                assert "Local fixture" in item["data"]
            for index in (-4, -2):
                assert any(macro["name"] == "transport-fixture" for macro in payload["results"][index]["data"]["macros"])
            for index in (-3, -1):
                assert payload["results"][index]["data"]["name"] == "transport-fixture"

    asyncio.run(exercise_server())


def test_missing_action_diagnostics_over_stdio(local_site):
    async def exercise_server() -> None:
        async with _stdio_session() as session:
            actions = [
                {}, {"url": f"{local_site.base_url}/page"}, {"action": ""}, {"action": "   "},
                {"action": None}, {"action": None, "type": "fetch_text"},
            ]
            result = await session.call_tool("web_action", {"actions": actions, "continue_on_error": True})
            payload = _payload(result)
            assert result.isError is True
            assert payload["success"] is False
            assert payload["completed_count"] == len(actions)
            for item in payload["results"]:
                error = item["error"]
                assert item["action"] is None
                assert 'Every action object needs an "action" key' in error
                assert "received" in error.lower()
                assert "JSON" in error and "array" in error
                assert "action_schema" in error
                assert "items.properties.action" in error
                assert len(error) < 1600
            assert "url" in payload["results"][1]["error"]

    asyncio.run(exercise_server())


def test_conflicting_action_aliases_do_not_dispatch_over_stdio(local_site):
    async def exercise_server() -> None:
        async with _stdio_session() as session:
            before = len(local_site.requests)
            actions = [
                {"action": "http_request", "type": "fetch_text", "method": "POST", "url": f"{local_site.base_url}/submit"},
                {"type": "http_request", "operation": "fetch_text", "method": "POST", "url": f"{local_site.base_url}/submit"},
            ]
            actions.extend(
                {"action": "http_request", "type": value, "method": "POST", "url": f"{local_site.base_url}/submit"}
                for value in ("", None, 17, {}, [])
            )
            result = await session.call_tool("web_action", {"actions": actions, "continue_on_error": True})
            payload = _payload(result)
            assert result.isError is True
            assert payload["success"] is False
            assert payload["completed_count"] == len(actions)
            for item in payload["results"]:
                assert item["success"] is False
                error = item["error"]
                assert "conflict" in error.lower()
                assert "http_request" in error
                assert "type" in error
                assert len(error) < 1600
            assert "action" in payload["results"][0]["error"]
            assert "operation" in payload["results"][1]["error"]
            assert all("fetch_text" in item["error"] for item in payload["results"][:2])
            assert len(local_site.requests) == before

    asyncio.run(exercise_server())


def test_ambiguous_aliases_are_rejected_before_dispatch(monkeypatch):
    monkeypatch.setattr(main, "_ACTIONS", dict(main._ACTIONS))
    monkeypatch.setattr(main.legacy_mcp._tool_manager, "_tools", dict(tool_registry(main.legacy_mcp)))
    calls = []

    async def transport_ambiguous_first(name: str = "") -> dict:
        calls.append(("first", name))
        return {"success": True}

    async def transport_ambiguous_second(type: str = "") -> dict:
        calls.append(("second", type))
        return {"success": True}

    plugins.register_action("transport_first", transport_ambiguous_first)
    plugins.register_action("transport_second", transport_ambiguous_second)
    result = asyncio.run(main.mcp.call_tool(
        "web_action", {"actions": [{"type": "transport_first", "name": "transport_second"}]}
    ))
    payload = _payload(result)
    assert result.isError is True
    assert payload["success"] is False
    error = payload["results"][0]["error"]
    assert "conflict" in error.lower()
    assert "transport_first" in error and "transport_second" in error
    assert calls == []
