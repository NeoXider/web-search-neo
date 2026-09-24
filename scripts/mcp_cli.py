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

Persistent mode - one server process kept alive between calls, so sessions survive
and a call costs a round trip instead of a ~12 s cold start:
  python scripts/mcp_cli.py serve [--port 47811] [--out-dir out/] [--idle-minutes 30]
  python scripts/mcp_cli.py send web_action '{"actions":[...]}' [--port 47811]
  python scripts/mcp_cli.py send web_info @request.json          (@file reads the JSON from a file)
  python scripts/mcp_cli.py repl                                 (one "tool json" per line)
  python scripts/mcp_cli.py stop
The daemon listens on 127.0.0.1 only and answers only callers that present the
random token it writes to ~/.web-search-neo/cli-<port>.token (readable by you
only); images go to --out-dir (default ./downloads/cli) and are named in the answer.

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
import getpass
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
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
        try:
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "web-search-neo-cli", "version": "1.0"},
            })
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self._proc.kill()  # a failed handshake must not leave the server child behind
            raise

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
    # A big run_script value comes in windows (max_chars, offset -> next_offset), so large payloads
    # (a generated image as base64) can be fetched by consecutive steps and reassembled here.
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


DEFAULT_PORT = 47811


def _token_path(port: int) -> Path:
    return Path.home() / ".web-search-neo" / f"cli-{port}.token"


def _restrict_to_owner(path: Path) -> str | None:
    """Make ``path`` readable by the current user only; the problem text if that failed."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return None
        except OSError as exc:
            return str(exc)
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{user}" if domain else user
    try:
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"icacls could not run: {type(exc).__name__}: {exc}"
    return None if result.returncode == 0 else (result.stderr or result.stdout).strip()


TOKEN_REPLACE_ATTEMPTS = 3


def _write_token(port: int) -> str:
    """A fresh token, written atomically to a file only this user can read.

    The port was bound before this runs, so a token file already there belongs to
    a daemon that is no longer listening: it is replaced, never shared. Any failure
    removes the temporary file - a half-written secret is never left next to the
    token - and a ``PermissionError`` on the replace (Windows: a reader or an
    antivirus holding the old file for a moment) is retried before it is raised.
    """
    token = secrets.token_urlsafe(32)
    path = _token_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        # Empty first, private next, the secret last: the token is never on disk under
        # the folder's inherited permissions, not even for a moment.
        os.close(os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        problem = _restrict_to_owner(temporary)
        if problem:
            raise RuntimeError(f"could not make the token file private ({problem})")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(token)
        for attempt in range(1, TOKEN_REPLACE_ATTEMPTS + 1):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == TOKEN_REPLACE_ATTEMPTS:
                    raise
                time.sleep(0.2 * attempt)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return token


def _remove_token(port: int, token: str) -> None:
    """Remove the token file, but only if it is still ours."""
    path = _token_path(port)
    try:
        if path.read_text(encoding="utf-8").strip() == token:
            path.unlink()
    except OSError:
        pass


def _content_parts(result: dict, out_dir: Path, counter: list[int]) -> list[dict]:
    """Text as text (JSON decoded when it is JSON), images written to files."""
    parts: list[dict] = []
    for content in result.get("content", []):
        if content.get("type") == "text":
            text = content.get("text", "")
            try:
                parts.append({"type": "json", "value": json.loads(text)})
            except json.JSONDecodeError:
                parts.append({"type": "text", "text": text})
        elif content.get("type") == "image":
            counter[0] += 1
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"image_{int(time.time())}_{counter[0]:03d}.png"
            path.write_bytes(base64.b64decode(content.get("data", "")))
            parts.append({"type": "image", "file": str(path), "mime": content.get("mimeType")})
    return parts


def serve(port: int, out_dir: Path, idle_minutes: float) -> int:
    """Keep one server alive and answer one JSON request per TCP connection.

    Order matters: the port is bound first (a busy port is an error, and on
    Windows nobody else can bind it after us), then the token is written, then
    the MCP server child is started - and every step is undone if a later one
    fails, so neither a stray token nor a child process outlives the daemon.
    """
    token = ""
    client: StdioClient | None = None
    lock = threading.Lock()
    counter = [0]
    last_used = [time.monotonic()]
    stopping = threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                request = json.loads(self.rfile.readline(64 * 1024 * 1024).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return  # not ours (an HTTP probe, garbage): no answer at all
            if not isinstance(request, dict) or not secrets.compare_digest(
                    str(request.get("token", "")).encode("utf-8"), token.encode("utf-8")):
                self.wfile.write(b'{"error": "bad token"}\n')
                return
            started = time.monotonic()
            answer: dict[str, Any]
            try:
                if request.get("op") == "stop":
                    answer = {"stopped": True}
                    stopping.set()
                else:
                    with lock:
                        assert client is not None
                        result = client.call(str(request["tool"]), request.get("args") or {})
                    answer = {"isError": bool(result.get("isError")),
                              "content": _content_parts(result, out_dir, counter)}
            except Exception as exc:
                answer = {"error": f"{type(exc).__name__}: {exc}"}
            last_used[0] = time.monotonic()
            answer["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            self.wfile.write((json.dumps(answer, ensure_ascii=False) + "\n").encode("utf-8"))

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = False

        def server_bind(self) -> None:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no second listener on our port
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"Port {port} is not free ({exc}); is a daemon already running? "
              f"Use --port, or `mcp_cli.py stop --port {port}`.", file=sys.stderr, flush=True)
        return 2
    serving = False
    try:
        token = _write_token(port)
        client = StdioClient()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        serving = True
        print(f"READY {port} (token in {_token_path(port)})", flush=True)
        while not stopping.is_set():
            if idle_minutes > 0 and time.monotonic() - last_used[0] > idle_minutes * 60:
                print("idle timeout, stopping", flush=True)
                break
            stopping.wait(1.0)
    finally:
        if serving:
            server.shutdown()
        server.server_close()
        if client is not None:
            client.close()
        if token:
            _remove_token(port, token)
    return 0


def send(port: int, request: dict, timeout: float = 600.0) -> dict:
    try:
        token = _token_path(port).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"No daemon on port {port}: start one with `mcp_cli.py serve` ({exc})")
    payload = json.dumps({**request, "token": token}, ensure_ascii=False) + "\n"
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.sendall(payload.encode("utf-8"))
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = connection.recv(1 << 20)
            if not chunk:
                break
            buffer += chunk
    return json.loads(buffer.decode("utf-8"))


def _arguments(text: str) -> dict:
    return json.loads(Path(text[1:]).read_text(encoding="utf-8") if text.startswith("@") else text)


def _print_answer(answer: dict) -> int:
    print(json.dumps(answer, ensure_ascii=False, indent=1))
    return 1 if answer.get("error") or answer.get("isError") else 0


def repl(port: int) -> int:
    print("tool json   (web_info {...} / web_action {...}); empty line or 'quit' ends", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line or line in {"quit", "exit"}:
            break
        tool, _, raw = line.partition(" ")
        try:
            _print_answer(send(port, {"tool": tool, "args": _arguments(raw or "{}")}))
        except (ValueError, OSError) as exc:
            print(f"[{type(exc).__name__}: {exc}]")
    return 0


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
    daemon = sub.add_parser("serve", help="keep one server alive for send/repl")
    daemon.add_argument("--port", type=int, default=DEFAULT_PORT)
    daemon.add_argument("--out-dir", type=Path, default=ROOT / "downloads" / "cli")
    daemon.add_argument("--idle-minutes", type=float, default=30.0, help="0 = never")
    one = sub.add_parser("send", help="one call to a running daemon")
    one.add_argument("tool", choices=["web_info", "web_action"])
    one.add_argument("args", nargs="?", default="{}", help="JSON arguments, or @file")
    one.add_argument("--port", type=int, default=DEFAULT_PORT)
    one.add_argument("--timeout", type=float, default=600.0)
    loop = sub.add_parser("repl", help="interactive calls to a running daemon")
    loop.add_argument("--port", type=int, default=DEFAULT_PORT)
    halt = sub.add_parser("stop", help="stop a running daemon")
    halt.add_argument("--port", type=int, default=DEFAULT_PORT)
    ns = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if ns.command == "serve":
        return serve(ns.port, ns.out_dir, ns.idle_minutes)
    if ns.command == "send":
        return _print_answer(send(ns.port, {"tool": ns.tool, "args": _arguments(ns.args)}, ns.timeout))
    if ns.command == "repl":
        return repl(ns.port)
    if ns.command == "stop":
        return _print_answer(send(ns.port, {"op": "stop"}))

    if ns.command == "call":
        return run_steps([{"tool": ns.tool, "args": json.loads(ns.args)}], ns.out_dir)
    raw = sys.stdin.read() if ns.script == "-" else Path(ns.script).read_text(encoding="utf-8")
    return run_steps(json.loads(raw), ns.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
