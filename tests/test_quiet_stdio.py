"""The windowless stdio proxy for console MCP servers.

``scripts/quiet_stdio.py`` exists for MCP clients that spawn servers with the
default flags: on Windows a console-subsystem child of a detached client gets
its own visible console window per session. The proxy starts the real server
hidden and forwards stdio as bytes.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY = REPO_ROOT / "scripts" / "quiet_stdio.py"


def _import_proxy():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import quiet_stdio

        return quiet_stdio
    finally:
        sys.path.pop(0)


def test_hidden_kwargs_hide_the_child_on_windows():
    if sys.platform != "win32":
        pytest.skip("Windows-only console behaviour")
    quiet_stdio = _import_proxy()
    kwargs = quiet_stdio._hidden_kwargs()
    assert kwargs.get("creationflags", 0) & 0x08000000
    assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_hidden_kwargs_are_empty_off_windows():
    if sys.platform == "win32":
        pytest.skip("POSIX behaviour")
    assert _import_proxy()._hidden_kwargs() == {}


def test_stdio_round_trip_and_exit_code():
    inner = (
        "import sys;"
        "sys.stderr.write('on-stderr');"
        "sys.stdout.write(sys.stdin.read());"
        "sys.exit(7)"
    )
    completed = subprocess.run(
        [sys.executable, str(PROXY), sys.executable, "-c", inner],
        input=b"ping",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 7
    assert completed.stdout == b"ping"
    assert completed.stderr == b"on-stderr"


def test_long_running_child_streams_small_messages():
    """A child that stays alive must stream small output without waiting for 64KB."""
    import threading

    inner = (
        "import sys, time;"
        "sys.stdout.write('early');"
        "sys.stdout.flush();"
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), sys.executable, "-c", inner],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result: dict = {}

    def read_first() -> None:
        result["data"] = proc.stdout.read(5)

    try:
        reader = threading.Thread(target=read_first, daemon=True)
        reader.start()
        reader.join(timeout=10)
        assert not reader.is_alive(), "long-running child did not stream small output"
        assert result.get("data") == b"early"
    finally:
        proc.kill()
        proc.wait()


def test_missing_command_reports_not_found():
    completed = subprocess.run(
        [sys.executable, str(PROXY), "wsn-no-such-binary-xyz"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 127
    assert "cannot start" in completed.stderr


def test_bare_proxy_reports_usage():
    completed = subprocess.run(
        [sys.executable, str(PROXY)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 2
    assert "usage" in completed.stderr


def test_proxy_passes_hidden_flags_to_the_child(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows-only console behaviour")
    import io

    quiet_stdio = _import_proxy()

    class FakeProc:
        returncode = 0
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(b"")

        def wait(self):
            return 0

    seen = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(quiet_stdio.subprocess, "Popen", fake_popen)
    # Pytest replaces stdin with a non-readable stand-in; hand the pumps
    # an empty stream instead so the test never blocks on input.
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    assert quiet_stdio.main(["quiet_stdio.py", "anything.exe"]) == 0
    assert seen.get("creationflags", 0) & 0x08000000
    assert seen["startupinfo"].wShowWindow == subprocess.SW_HIDE
