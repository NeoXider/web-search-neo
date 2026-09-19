"""Test runs must not pop console windows onto the user's screen.

A console-subsystem child (node.exe, python.exe, chromedriver) of a detached
parent - an MCP client, an agent harness, CI - gets its own visible console
window. Production spawns already pass CREATE_NO_WINDOW (daemon, drivers); the
conftest patch extends the same to every subprocess the suite starts.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_windows_test_runs_create_no_console(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows-only console behaviour")
    import conftest

    assert subprocess.Popen is conftest._WindowlessPopen
    assert issubclass(subprocess.Popen, conftest._ORIGINAL_POPEN)
    # The regression this guards: third-party annotations subscript the class.
    assert subprocess.Popen[bytes] is not None
    seen = {}

    def fake_init(self, *args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(conftest._ORIGINAL_POPEN, "__init__", fake_init)
    subprocess.Popen(["node", "--version"])
    assert seen.get("creationflags", 0) & 0x08000000, (
        "test subprocesses must carry CREATE_NO_WINDOW so no console appears"
    )


def test_spawn_transparency():
    # The flag must not change what a spawn does, only whether it is seen.
    completed = subprocess.run(
        [sys.executable, "-c", "print('quiet-ok')"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "quiet-ok"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native process boundary")
def test_direct_native_spawns_cannot_request_a_console(monkeypatch):
    import _winapi
    import conftest

    calls = []
    monkeypatch.setattr(conftest, "_ORIGINAL_CREATE_PROCESS", lambda *args: calls.append(args) or (1, 2, 3, 4))
    assert _winapi.CreateProcess is conftest._windowless_create_process
    for flags in (0, 0x10, 0x8, 0x18 | 0x200):
        result = _winapi.CreateProcess("python.exe", "fixture", None, None, False,
                                      flags, {"fixture": "1"}, "fixture-dir", None)
        assert result == (1, 2, 3, 4)
        assert calls[-1][5] == (flags & ~0x18) | 0x08000000
        assert calls[-1][6:8] == ({"fixture": "1"}, "fixture-dir")
