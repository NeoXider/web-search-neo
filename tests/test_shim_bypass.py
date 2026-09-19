"""The venv launcher shim hand-off.

A shim-started daemon or MCP server (uv venv launchers spawn the real
interpreter as a child) owns a visible console under a console-less parent.
:func:`ensure_direct` replaces such a process with the hidden base interpreter
before it binds anything; everything else - terminals, one-shot commands,
other platforms - must pass through untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from web_search_neo import shim_bypass


def _make_venv(root, *, home=None):
    scripts = root / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    exe = scripts / "pythonw.exe"
    exe.write_text("", encoding="utf-8")
    (root / ".venv" / "pyvenv.cfg").write_text(
        f"home = {home or root / 'base'}\n", encoding="utf-8"
    )
    return exe


def test_long_running_modes_are_daemon_and_stdio_server():
    assert shim_bypass._long_running([]) is True
    assert shim_bypass._long_running(["--bridge"]) is True
    assert shim_bypass._long_running(["--bridge", "--stop"]) is False
    assert shim_bypass._long_running(["--help"]) is False
    assert shim_bypass._long_running(["--version"]) is False


def test_venv_base_resolves_the_pair(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "pythonw.exe").write_text("", encoding="utf-8")
    exe = _make_venv(tmp_path, home=base)
    venv_exe, base_exe = shim_bypass._venv_base(str(exe))
    assert venv_exe == exe.resolve()
    assert base_exe == base / "pythonw.exe"


def test_venv_base_ignores_non_venv_layouts(tmp_path):
    plain = tmp_path / "pythonw.exe"
    plain.write_text("", encoding="utf-8")
    assert shim_bypass._venv_base(str(plain)) is None


def test_venv_base_ignores_a_missing_base(tmp_path):
    exe = _make_venv(tmp_path, home=tmp_path / "nope")
    assert shim_bypass._venv_base(str(exe)) is None


def test_ensure_direct_is_a_noop_for_one_shot_commands(monkeypatch, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "pythonw.exe").write_text("", encoding="utf-8")
    _make_venv(tmp_path, home=base)
    monkeypatch.setattr(shim_bypass, "_console_window", lambda: 0)
    monkeypatch.delenv(shim_bypass._BYPASSED_VAR, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    called = []

    def fake_popen(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("one-shot commands must not re-execute")

    monkeypatch.setattr(shim_bypass.subprocess, "Popen", fake_popen)
    shim_bypass.ensure_direct(["--bridge", "--stop"])


@pytest.mark.parametrize("arguments", [[], ["--bridge"]])
def test_ensure_direct_replaces_a_shim_child(monkeypatch, tmp_path, arguments):
    if sys.platform != "win32":
        pytest.skip("Windows-only console behaviour")
    base = tmp_path / "base"
    base.mkdir()
    (base / "pythonw.exe").write_text("", encoding="utf-8")
    exe = _make_venv(tmp_path, home=base)
    monkeypatch.setattr(shim_bypass, "_console_window", lambda: 0)
    monkeypatch.delenv(shim_bypass._BYPASSED_VAR, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(shim_bypass.sys, "executable", str(exe))
    seen = {}

    class FakeProc:
        def wait(self):
            return 42

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(shim_bypass.subprocess, "Popen", fake_popen)
    with pytest.raises(SystemExit) as ended:
        shim_bypass.ensure_direct(arguments)
    assert ended.value.code == 42
    assert seen["cmd"][0] == str(base / "pythonw.exe")
    assert seen["cmd"][1:] == ["-m", "web_search_neo.main", *arguments]
    assert seen["env"]["__PYVENV_LAUNCHER__"] == str(exe.resolve())
    assert seen["env"][shim_bypass._BYPASSED_VAR] == "1"
    assert seen["stdin"] is sys.stdin
    assert seen["stdout"] is sys.stdout
    assert seen["stderr"] is sys.stderr
    assert seen.get("creationflags", 0) & 0x08000000
    assert seen["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_ensure_direct_keeps_a_console_owner_alone(monkeypatch, tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows-only console behaviour")
    base = tmp_path / "base"
    base.mkdir()
    (base / "pythonw.exe").write_text("", encoding="utf-8")
    exe = _make_venv(tmp_path, home=base)
    # A terminal run owns its console: re-executing hidden would steal the
    # output the user is watching (and their Ctrl+C), so it must not happen.
    monkeypatch.setattr(shim_bypass, "_console_window", lambda: 12345)
    monkeypatch.delenv(shim_bypass._BYPASSED_VAR, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(shim_bypass.sys, "executable", str(exe))

    def fake_popen(*args, **kwargs):
        raise AssertionError("a console owner must not re-execute")

    monkeypatch.setattr(shim_bypass.subprocess, "Popen", fake_popen)
    shim_bypass.ensure_direct([])


def test_ensure_direct_never_fires_under_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test")
    monkeypatch.setattr(shim_bypass, "_console_window", lambda: (_ for _ in ()).throw(
        AssertionError("must short-circuit before touching the OS")))
    shim_bypass.ensure_direct([])
