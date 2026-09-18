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
