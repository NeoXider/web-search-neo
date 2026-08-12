from __future__ import annotations

from pathlib import Path

import chrome_bootstrap


class _DisconnectedBridge:
    def start(self):
        return None

    def status(self, _wait_seconds):
        return {"connected": False, "host": "127.0.0.1", "port": 8765}


class _ConnectedBridge(_DisconnectedBridge):
    def status(self, _wait_seconds):
        return {
            "connected": True,
            "host": "127.0.0.1",
            "port": 8765,
            "browser": {"extension_version": chrome_bootstrap._expected_extension_version()},
        }


def test_setup_requires_explicit_confirmation_without_ui_actions(monkeypatch):
    monkeypatch.setattr(chrome_bootstrap, "get_chrome_bridge", lambda: _DisconnectedBridge())
    called = False

    def unexpected(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(chrome_bootstrap, "_install_with_windows_ui", unexpected)
    result = chrome_bootstrap.setup_current_chrome()

    assert result["success"] is True
    assert result["ready"] is False
    assert result["confirmation_required"] is True
    assert result["extension_directory"] == str(chrome_bootstrap.EXTENSION_DIR)
    assert called is False


def test_setup_is_noop_when_companion_is_connected(monkeypatch):
    monkeypatch.setattr(chrome_bootstrap, "get_chrome_bridge", lambda: _ConnectedBridge())
    result = chrome_bootstrap.setup_current_chrome(confirm_install=True)
    assert result["success"] is True
    assert result["already_connected"] is True
    assert result["confirmation_required"] is False


def test_companion_directory_contains_manifest():
    assert isinstance(chrome_bootstrap.EXTENSION_DIR, Path)
    assert (chrome_bootstrap.EXTENSION_DIR / "manifest.json").is_file()
