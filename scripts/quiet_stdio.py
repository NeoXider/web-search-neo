"""Run a console MCP server with no visible window and proxy its stdio.

Usage: pythonw quiet_stdio.py <command> [args ...]

On Windows a console-subsystem child of a detached parent (an MCP client, an
agent harness) gets its own visible console window. MCP clients that spawn
servers with the default flags - opencode among them - therefore pop a console
per session for every console-subsystem server. Pointing the client at a
windowless interpreter running this proxy instead keeps exactly one hidden
hop: the proxy starts the real server with ``CREATE_NO_WINDOW`` and forwards
stdin/stdout/stderr as bytes, so framing is never altered.

The exit code is the server's. A closed stdin is forwarded as EOF, which is
what tells a well-behaved stdio server to exit; its code is then reported.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading


def _hidden_kwargs() -> dict:
    """Popen kwargs that start the child with no console on Windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def _pump(source, destination, close_on_eof: bool) -> None:
    """Forward bytes until EOF or a broken pipe; never raise."""
    try:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            destination.write(chunk)
            destination.flush()
    except (OSError, ValueError):
        pass
    finally:
        if close_on_eof:
            try:
                destination.close()
            except (OSError, ValueError):
                pass


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: quiet_stdio.py <command> [args ...]", file=sys.stderr)
        return 2
    try:
        child = subprocess.Popen(
            argv[1:],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_hidden_kwargs(),
        )
    except OSError as exc:
        print(f"quiet_stdio: cannot start {argv[1]!r}: {exc}", file=sys.stderr)
        return 127
    pumps = [
        threading.Thread(
            target=_pump, args=(sys.stdin.buffer, child.stdin, True), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(child.stdout, sys.stdout.buffer, False), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(child.stderr, sys.stderr.buffer, False), daemon=True
        ),
    ]
    for pump in pumps:
        pump.start()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
