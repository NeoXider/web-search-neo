from __future__ import annotations

from pathlib import Path

import pytest

import bridge_auth
import chrome_bootstrap


@pytest.fixture(autouse=True)
def isolated_token(tmp_path, monkeypatch):
    """Keep the suite away from the real machine secret and the checked-out copy."""
    monkeypatch.setattr(bridge_auth, "token_path", lambda: tmp_path / "bridge-token")
    monkeypatch.setattr(bridge_auth, "EXTENSION_TOKEN_FILE", tmp_path / "bridge-token.js")
    return tmp_path


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
            "browser": {"extension_version": chrome_bootstrap.expected_extension_version()},
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


def test_setup_publishes_the_bridge_token_before_touching_chrome(isolated_token, monkeypatch):
    monkeypatch.setattr(chrome_bootstrap, "get_chrome_bridge", lambda: _DisconnectedBridge())
    result = chrome_bootstrap.setup_current_chrome()

    assert result["token_ready"] is True
    token = (isolated_token / "bridge-token").read_text(encoding="utf-8").strip()
    assert bridge_auth.is_token(token)
    assert (isolated_token / "bridge-token.js").read_text(encoding="utf-8") == (
        f'export const BRIDGE_TOKEN = "{token}";\n'
    )
    assert result["token_file"] == str(isolated_token / "bridge-token.js")


def test_setup_reports_a_token_failure_instead_of_raising(monkeypatch):
    def unwritable():
        raise PermissionError("no access to the profile directory")

    monkeypatch.setattr(bridge_auth, "load_or_create_token", unwritable)
    monkeypatch.setattr(chrome_bootstrap, "get_chrome_bridge", lambda: _DisconnectedBridge())
    result = chrome_bootstrap.setup_current_chrome()

    assert result["token_ready"] is False
    assert "PermissionError" in result["token_error"]


def test_companion_directory_contains_manifest():
    assert isinstance(chrome_bootstrap.EXTENSION_DIR, Path)
    assert (chrome_bootstrap.EXTENSION_DIR / "manifest.json").is_file()
