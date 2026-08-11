from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_stdio_server_lists_tools_and_calls_status_tool():
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
                assert {
                    "fetch_url_text",
                    "fetch_page_links",
                    "fetch_urls_text",
                    "get_search_engines_status",
                    "search_web",
                    "browser_open_page",
                    "browser_open_pages",
                    "browser_get_page_elements",
                    "browser_wait_for",
                    "browser_fill_fields",
                    "browser_upload_file",
                    "browser_click",
                    "browser_submit_form",
                    "browser_screenshot",
                    "browser_get_status",
                    "browser_close",
                    "browser_close_all",
                } <= names

                called = await session.call_tool(
                    "get_search_engines_status", {"check_live": False}
                )
                assert called.isError is False
                payload = called.structuredContent
                if payload is None:
                    payload = json.loads(called.content[0].text)
                assert payload["default_engine"] == "duckduckgo"
                assert "duckduckgo" in payload["configured"]

    asyncio.run(exercise_server())
