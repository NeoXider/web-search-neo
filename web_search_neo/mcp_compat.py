"""The one place web-search-neo touches FastMCP internals.

FastMCP exposes no public accessor for a registered tool's generated argument
model or JSON schema, which action validation and ``action_schema`` both need.
Every such read goes through :func:`tool_registry`, so an ``mcp`` release that
reshapes those internals fails here with an explicit message instead of as a
scattered ``AttributeError``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent


def _mcp_version() -> str:
    try:
        from importlib.metadata import version

        return version("mcp")
    except Exception:
        return "unknown"


def tool_registry(server: FastMCP) -> dict[str, Any]:
    """FastMCP's name -> Tool map (``server._tool_manager._tools``)."""
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        raise RuntimeError(
            "web-search-neo needs FastMCP._tool_manager._tools, which the installed "
            f"mcp {_mcp_version()} no longer provides; update "
            "web_search_neo/mcp_compat.py for this mcp release or pin mcp<the new version"
        )
    return tools


def registered_tool(server: FastMCP, name: str) -> Any:
    """One registered FastMCP Tool; KeyError when ``name`` is not registered."""
    tools = tool_registry(server)
    if name not in tools:
        raise KeyError(name)
    return tools[name]


def _payload_of(result: Any) -> Any:
    """The structured value FastMCP produced for a tool call, if any."""
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)) and len(result) == 1:
        block = result[0]
        if isinstance(block, TextContent):
            try:
                return json.loads(block.text)
            except ValueError:
                return None
    return None


class ReportingFastMCP(FastMCP):
    """FastMCP that marks soft failures of selected tools with ``isError=true``.

    ``web_action`` answers a failed step with ``{"success": false, ...}``
    rather than raising, so a batch can report what ran. Over MCP that payload
    is still returned in full, but flagged as an error, matching how
    ``web_info`` failures (which raise) reach the client.
    """

    soft_failure_tools: frozenset[str] = frozenset({"web_action"})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await super().call_tool(name, arguments)
        if name not in self.soft_failure_tools:
            return result
        payload = _payload_of(result)
        if not (isinstance(payload, dict) and payload.get("success") is False):
            return result
        if isinstance(result, tuple) and len(result) == 2:
            content = list(result[0])
        else:
            content = [TextContent(type="text", text=json.dumps(payload, indent=2))]
        return CallToolResult(
            content=content,
            structuredContent=result[1] if isinstance(result, tuple) else None,
            isError=True,
        )
