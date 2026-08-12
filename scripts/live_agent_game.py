"""Drive a real MCP session with a small local model and finish a browser game.

This is the reproducible version of the "an agent can actually play a game"
claim: it starts the MCP server over stdio, hands the two advertised tools to a
model served by LM Studio, and lets the model play the bundled platformer
fixture to completion while every tool call is timed.

    python scripts/live_agent_game.py --model qwen3.5-4b-mtp

Requires LM Studio serving an OpenAI-compatible API on 127.0.0.1:1234 with the
model loaded, plus Chrome for the browser session.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
from pathlib import Path
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "games"

SYSTEM_PROMPT = """You control a web browser through two tools: web_info and web_action.

Play the platformer at the URL the user gives you and reach the finish.
Always pass "session_id": "game" in every action. Call one tool per turn.
Never explain, never write prose.

Setup, once:
1. web_action open, url, session_id "game", profile_mode "temporary", headless true
2. web_action render, mode "step", session_id "game"
3. web_action input, session_id "game", key_actions [{"key":"ARROW_RIGHT","action":"hold"}]

Then repeat, reading x from the status line you get after every turn:
- if onGround is true and x is 200 or more: jump with web_action input,
  session_id "game", key_actions [{"key":"SPACE","action":"tap"}]
- otherwise: web_action step, session_id "game", frames 3

A pit spans x 240 to 340. Jumping at x 200-235 clears it; walking into it kills you.
After a death x resets to 40 and you run the same loop again.
Stop as soon as the status line says won=true."""


def start_fixture_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(FIXTURE_DIR)
    )
    handler.log_message = lambda *_args, **_kwargs: None  # type: ignore[assignment]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def chat(model: str, messages: list[dict], tools: list[dict], base_url: str) -> tuple[dict, float]:
    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "temperature": 0.1,
        "max_tokens": 400,
        # Thinking triples latency for no benefit on a mechanical control loop.
        "reasoning_effort": "none",
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.load(response)
    return payload["choices"][0]["message"], time.perf_counter() - started


def as_openai_tools(listed) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "parameters": tool.inputSchema,
            },
        }
        for tool in listed.tools
    ]


def summarise(result) -> str:
    """Keep tool output small: a 4B model drowns in a full page summary."""
    try:
        text = result.content[0].text
    except (AttributeError, IndexError):
        return "ok"
    try:
        data = json.loads(text)
    except ValueError:
        return text[:600]
    if isinstance(data, dict):
        keep = {
            key: data[key]
            for key in ("success", "failure_count", "stopped_early", "error", "next", "results")
            if key in data
        }
        if isinstance(keep.get("results"), list):
            keep["results"] = [
                {
                    inner: (item.get(inner) if inner != "data"
                            else {k: item["data"][k] for k in ("next",) if k in item["data"]})
                    for inner in ("index", "action", "success", "error", "data")
                    if inner in item
                }
                for item in keep["results"]
            ]
        return json.dumps(keep, ensure_ascii=False)[:900]
    return text[:600]


async def run(model: str, base_url: str, max_turns: int) -> int:
    server, fixture_url = start_fixture_server()
    game_url = f"{fixture_url}/platformer.html"
    call_times: list[float] = []
    model_times: list[float] = []
    won = False
    try:
        parameters = StdioServerParameters(
            command=sys.executable, args=[str(PROJECT_ROOT / "main.py")], cwd=str(PROJECT_ROOT)
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                tools = as_openai_tools(listed)
                print(f"MCP advertises {len(tools)} tools: {[t['function']['name'] for t in tools]}")
                print(f"eager schema size: {len(json.dumps(tools))} chars")

                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Play and win: {game_url}"},
                ]
                started = time.perf_counter()
                for turn in range(max_turns):
                    message, model_seconds = chat(model, messages, tools, base_url)
                    model_times.append(model_seconds)
                    calls = message.get("tool_calls") or []
                    messages.append(
                        {
                            "role": "assistant",
                            "content": message.get("content") or "",
                            "tool_calls": calls,
                        }
                    )
                    if not calls:
                        print(f"[{turn}] model stopped: {(message.get('content') or '')[:120]}")
                        break
                    for call in calls:
                        name = call["function"]["name"]
                        try:
                            arguments = json.loads(call["function"]["arguments"] or "{}")
                        except ValueError:
                            arguments = {}
                        call_started = time.perf_counter()
                        result = await session.call_tool(name, arguments)
                        call_seconds = time.perf_counter() - call_started
                        call_times.append(call_seconds)
                        rendered = summarise(result)
                        print(
                            f"[{turn}] model {model_seconds:5.2f}s | {name} "
                            f"{json.dumps(arguments, ensure_ascii=False)[:110]} "
                            f"-> {call_seconds:5.2f}s {rendered[:110]}"
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": rendered,
                            }
                        )
                    # The fixture mirrors its state into a DOM status line, so the
                    # ordinary page_text topic is enough to follow the run.
                    status_started = time.perf_counter()
                    status = await session.call_tool(
                        "web_info",
                        {
                            "topic": "page_text",
                            "params": {"session_id": "game", "max_chars": 400},
                        },
                    )
                    call_times.append(time.perf_counter() - status_started)
                    line = ""
                    try:
                        line = json.loads(status.content[0].text).get("text", "")
                    except (AttributeError, IndexError, ValueError):
                        line = ""
                    # Only the status line matters; the page heading is noise.
                    line = line.replace(chr(10), " ").strip()
                    marker = line.find("frame=")
                    line = (line[marker:] if marker >= 0 else line)[:160]
                    if line:
                        print(f"[{turn}] state: {line}")
                        messages.append({"role": "user", "content": f"Status: {line}"})
                    won = "won=true" in line.lower()
                    if won:
                        print(f"[{turn}] the game reports a win")
                        break
                total = time.perf_counter() - started

        print("\n=== timings ===")
        if call_times:
            print(
                f"tool calls: n={len(call_times)} median={statistics.median(call_times) * 1000:.0f}ms "
                f"p95={sorted(call_times)[int(len(call_times) * 0.95) - 1] * 1000:.0f}ms "
                f"max={max(call_times) * 1000:.0f}ms"
            )
        if model_times:
            print(
                f"model turns: n={len(model_times)} median={statistics.median(model_times) * 1000:.0f}ms "
                f"max={max(model_times) * 1000:.0f}ms"
            )
        print(f"total: {total:.1f}s, won={won}")
        return 0 if won else 1
    finally:
        server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.5-4b-mtp")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--max-turns", type=int, default=25)
    arguments = parser.parse_args()
    try:
        return asyncio.run(run(arguments.model, arguments.base_url, arguments.max_turns))
    except urllib.error.URLError as exc:
        print(f"LM Studio is not reachable at {arguments.base_url}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
