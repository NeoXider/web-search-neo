"""Command-line MCP client for Web Search Neo over stdio.

Lets agents and scripts that have no MCP connector drive the server the same way an MCP host would:
it launches ``main.py`` as a child process, performs the MCP handshake and calls ``web_info`` /
``web_action``. Browser sessions live inside the server process, so multi-step work (open a page,
type, wait for a result, grab a screenshot) runs as one ``run`` script against one server.

Usage:
  python scripts/mcp_cli.py call web_info '{}'
  python scripts/mcp_cli.py call web_action '{"actions":[{"action":"search","query":"mcp"}]}'
  python scripts/mcp_cli.py run steps.json --out-dir out/
  python scripts/mcp_cli.py run - < steps.json

A ``run`` script is a JSON list of steps:
  {"tool": "web_action", "args": {...}}            one tool call
  {"tool": "web_info", "args": {"topic": "..."}}   one read
  {"sleep": 5}                                     wait N seconds
  {"until": {"tool": "web_info", "args": {...},    repeat a call until its text contains
             "contains": "text", "not_contains": "...", "timeout": 180, "interval": 5}}
  A tool step may add "write_value_to": "file" / "append_value_to": "file" to store the ``value`` of its
  last action that returned one (e.g. a run_script return), and "quiet": true to skip printing the result.
Image content returned by a tool (for example the ``screenshot`` topic) is written to ``--out-dir``
as numbered PNG files; the text part of every result is printed to stdout.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_VERSION = "2025-03-26"


def _server_python() -> str:
    venv = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


class StdioClient:
    """Minimal JSON-RPC over newline-delimited stdio, enough for tools/list and tools/call."""

    def __init__(self) -> None:
        self._proc = subprocess.Popen(
            [_server_python(), str(ROOT / "main.py")],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 0
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "web-search-neo-cli", "version": "1.0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout (check the server log for the reason)")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Server-initiated notifications and requests are ignored; only our response matters.
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message["result"]

    def call(self, tool: str, args: dict) -> dict:
        return self._request("tools/call", {"name": tool, "arguments": args})

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()


class Output:
    def __init__(self, out_dir: Path | None) -> None:
        self._dir = out_dir
        self._count = 0
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

    def text_of(self, result: dict) -> str:
        return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")

    def emit(self, result: dict) -> str:
        text = self.text_of(result)
        if text:
            print(text)
        for content in result.get("content", []):
            if content.get("type") != "image":
                continue
            self._count += 1
            if not self._dir:
                print(f"[image {self._count}: {content.get('mimeType')} — pass --out-dir to save it]")
                continue
            path = self._dir / f"image_{self._count:02d}.png"
            path.write_bytes(base64.b64decode(content.get("data", "")))
            print(f"[image saved: {path}]")
        if result.get("isError"):
            print("[tool reported an error]")
        return text


def _run_until(client: StdioClient, output: Output, spec: dict) -> None:
    deadline = time.monotonic() + float(spec.get("timeout", 180))
    interval = float(spec.get("interval", 5))
    contains, not_contains = spec.get("contains"), spec.get("not_contains")
    while True:
        result = client.call(spec["tool"], spec.get("args", {}))
        text = output.text_of(result)
        ok = (contains is None or contains in text) and (not_contains is None or not_contains not in text)
        if ok or time.monotonic() >= deadline:
            output.emit(result)
            if not ok:
                print(f"[until: timed out after {spec.get('timeout', 180)} s]")
            return
        time.sleep(interval)


def _action_value(text: str) -> Any:
    """``value`` of the last action result that has one (run_script and similar), or None.

    A step often clicks or types before its run_script, so the value is not always in the first result.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    for result in reversed(results or []):
        value = (result.get("data") or {}).get("value")
        if value is not None:
            return value
    return None


def _store_value(step: dict, text: str) -> None:
    # run_script clips strings at 200k characters, so large payloads (a generated image as base64)
    # are returned in slices by consecutive steps and reassembled here.
    target = step.get("append_value_to") or step.get("write_value_to")
    if not target:
        return
    value = _action_value(text)
    if value is None:
        print(f"[no value to store in {target}]")
        return
    chunk = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    mode = "a" if "append_value_to" in step else "w"
    with open(target, mode, encoding="utf-8") as handle:
        handle.write(chunk)
    print(f"[{'appended' if mode == 'a' else 'wrote'} {len(chunk)} chars to {target}]")


def run_steps(steps: list[dict], out_dir: Path | None, quiet: bool = False) -> int:
    client = StdioClient()
    output = Output(out_dir)
    try:
        for index, step in enumerate(steps, 1):
            print(f"--- step {index}: {json.dumps(step, ensure_ascii=False)[:160]}")
            if "sleep" in step:
                time.sleep(float(step["sleep"]))
            elif "until" in step:
                _run_until(client, output, step["until"])
            else:
                result = client.call(step["tool"], step.get("args", {}))
                text = output.text_of(result) if quiet or step.get("quiet") else output.emit(result)
                _store_value(step, text)
        return 0
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    call = sub.add_parser("call", help="one tool call")
    call.add_argument("tool", choices=["web_info", "web_action"])
    call.add_argument("args", nargs="?", default="{}", help="JSON arguments")
    call.add_argument("--out-dir", type=Path)
    run = sub.add_parser("run", help="run a JSON list of steps in one server process")
    run.add_argument("script", help="path to the steps JSON, or - for stdin")
    run.add_argument("--out-dir", type=Path)
    ns = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if ns.command == "call":
        return run_steps([{"tool": ns.tool, "args": json.loads(ns.args)}], ns.out_dir)
    raw = sys.stdin.read() if ns.script == "-" else Path(ns.script).read_text(encoding="utf-8")
    return run_steps(json.loads(raw), ns.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
