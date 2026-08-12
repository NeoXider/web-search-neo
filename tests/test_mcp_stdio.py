from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_stdio_server_exposes_compact_discovery_and_action_tools(local_site):
    def unpack(result):
        assert result.isError is False
        # MCP SDK/Pydantic combinations differ in whether Any return values are
        # omitted from structuredContent or wrapped under a synthetic "result".
        # The protocol text block is stable across supported Python versions.
        return json.loads(result.content[0].text)

    async def exercise_server() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "main.py")],
            cwd=str(PROJECT_ROOT),
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert names == {"web_info", "web_action"}
                schemas = {tool.name: tool.inputSchema for tool in listed.tools}
                info_properties = schemas["web_info"]["properties"]
                assert info_properties["topic"]["default"] == "capabilities"
                assert "action_schema" in info_properties["topic"]["enum"]
                assert schemas["web_action"]["required"] == ["actions"]

                capabilities = unpack(await session.call_tool("web_info", {}))
                assert capabilities["public_tools"] == ["web_info", "web_action"]
                assert capabilities["action_groups"]["game"] == [
                    "input",
                    "pointer",
                    "touch",
                    "touch_emulation",
                    "pointer_lock",
                    "render",
                    "step",
                    "release_inputs",
                ]
                assert "action_types" not in capabilities
                # The contract must stand alone, with no external skill file.
                assert capabilities["recipes"]["game"]
                assert capabilities["pitfalls"]
                assert capabilities["actions"]["input"]

                input_schema = unpack(
                    await session.call_tool(
                        "web_info",
                        {"topic": "action_schema", "params": {"action": "input"}},
                    )
                )
                assert input_schema["action"] == "input"
                assert input_schema["input_schema"]["properties"]["action"]["const"] == "input"
                assert "key_actions" in input_schema["input_schema"]["properties"]
                assert input_schema["notes"]["key_action"]["action"] == "tap|hold|release"

                batch = unpack(
                    await session.call_tool(
                        "web_action",
                        {
                            "actions": [
                                {
                                    "action": "fetch_text",
                                    "url": f"{local_site.base_url}/page",
                                },
                                {
                                    "action": "fetch_links",
                                    "url": f"{local_site.base_url}/page",
                                },
                            ]
                        },
                    )
                )
                assert batch["success"] is True
                assert batch["completed_count"] == 2
                assert "Local fixture" in batch["results"][0]["data"]
                assert f"{local_site.base_url}/relative" in batch["results"][1]["data"]

    asyncio.run(exercise_server())
