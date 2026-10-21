"""Live stdio MCP acceptance probe; run with the same Python executable as Hermes.

Uses public example.com and no browser actions. This is intentionally outside pytest.
The MCP SDK launches Windows children with CREATE_NO_WINDOW and a cleanup Job Object.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.com"
ALIASES = ("type", "name", "tool", "command", "op", "operation", "method")


def emit(check: str, payload: Any) -> None:
    print(json.dumps({"check": check, "payload": payload}, ensure_ascii=False, sort_keys=True), flush=True)


def unpack(result: Any) -> dict[str, Any]:
    payload = result.structuredContent
    if payload is None:
        text = next(block.text for block in result.content if block.type == "text")
        payload = json.loads(text)
    elif set(payload) == {"result"}:
        payload = payload["result"]
    assert isinstance(payload, dict), payload
    return payload


async def run() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "main.py")],
        cwd=str(ROOT),
        env={**os.environ, "WEB_SEARCH_NEO_BRIDGE_AUTOSPAWN": "0"},
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=timedelta(seconds=40)
        ) as session:
            await session.initialize()
            listed = await session.list_tools()
            schemas = {tool.name: tool.inputSchema for tool in listed.tools}
            assert "web_info" in schemas and "web_action" in schemas, schemas.keys()
            item = schemas["web_action"]["properties"]["actions"]["items"]
            assert "action" in item["properties"], item
            assert "action" in item["required"], item
            assert "url" in item["properties"], item
            emit("published_action_items", {
                "action": item["properties"]["action"],
                "url": item["properties"]["url"],
                "required": item["required"],
                "additionalProperties": item.get("additionalProperties"),
                "declared_property_count": len(item["properties"]),
            })

            for topic in ("capabilities", "actions", "action_schema", "browser_status"):
                arguments: dict[str, Any] = {"topic": topic}
                if topic == "action_schema":
                    arguments["params"] = {"action": "fetch_text"}
                result = await session.call_tool("web_info", arguments)
                payload = unpack(result)
                assert result.isError is False, payload
                if topic == "capabilities":
                    assert payload["public_tools"] == ["web_info", "web_action"], payload
                elif topic == "action_schema":
                    assert payload["action"] == "fetch_text", payload
                emit(f"web_info:{topic}", payload)

            async def action(check: str, entry: dict[str, Any], success: bool) -> dict[str, Any]:
                result = await session.call_tool("web_action", {"actions": [entry]})
                payload = unpack(result)
                emit(check, payload)
                assert payload["success"] is success, payload
                assert payload["results"][0]["success"] is success, payload
                if success:
                    assert not result.isError, payload
                    assert payload["results"][0]["action"] == "fetch_text", payload
                return payload

            canonical = {"action": "fetch_text", "url": URL}
            await action("canonical_fetch_text", canonical, True)
            # Model a client that drops every undeclared key, even with
            # additionalProperties=true. The former schema pruned this to {}.
            pruned = {key: value for key, value in canonical.items() if key in item["properties"]}
            emit("pruned_arguments", {"actions": [pruned]})
            await action("pruned_fetch_text", pruned, True)
            for alias in ALIASES:
                await action(f"alias:{alias}", {alias: "fetch_text", "url": URL}, True)

            empty = await action("empty_action", {}, False)
            error = empty["results"][0]["error"]
            assert "keys" in error.lower() and "[]" in error, error
            assert "items.properties.action" in error and "action_schema" in error, error
            conflict = await action(
                "conflicting_selectors", {"action": "fetch_text", "type": "search", "url": URL}, False
            )
            error = conflict["results"][0]["error"]
            assert "conflict" in error.lower() and "fetch_text" in error and "search" in error, error
            emit("acceptance", {"success": True, "aliases_checked": list(ALIASES)})


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(run(), timeout=180))
