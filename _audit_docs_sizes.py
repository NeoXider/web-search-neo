"""Audit helper: measure advertised tool-surface sizes and contract size."""
import asyncio
import json
import sys

sys.path.insert(0, r"C:\Git\PythonUrlFeatch")

import main  # noqa: E402


async def surface(server):
    tools = await server.list_tools()
    total = 0
    names = []
    for tool in tools:
        names.append(tool.name)
        total += len(tool.name or "")
        total += len(tool.description or "")
        total += len(json.dumps(tool.inputSchema, separators=(",", ":")))
    return names, total


async def surface_pretty(server):
    tools = await server.list_tools()
    total = 0
    for tool in tools:
        total += len(tool.name or "")
        total += len(tool.description or "")
        total += len(json.dumps(tool.inputSchema))
    return total


async def run():
    compact_names, compact = await surface(main.mcp)
    legacy_names, legacy = await surface(main.legacy_mcp)
    print("compact tools:", compact_names, "chars(compact json):", compact)
    print("compact chars (pretty-ish json.dumps default sep):", await surface_pretty(main.mcp))
    print("legacy tool count:", len(legacy_names), "chars:", legacy)
    print("legacy chars (default sep):", await surface_pretty(main.legacy_mcp))
    caps = main._capabilities()
    print("capabilities chars compact:", len(json.dumps(caps, separators=(",", ":"))))
    print("capabilities chars default:", len(json.dumps(caps)))
    print("capabilities top-level keys:", sorted(caps))


asyncio.run(run())
