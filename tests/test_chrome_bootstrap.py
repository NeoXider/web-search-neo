from __future__ import annotations

import json
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
    def __init__(self):
        self.waited = None

    def start(self):
        return None

    def status(self, wait_seconds):
        self.waited = wait_seconds
        return {"connected": False, "host": "127.0.0.1", "port": 8765}


class _ConnectedBridge(_DisconnectedBridge):
    version = None

    def status(self, wait_seconds):
        self.waited = wait_seconds
        return {
            "connected": True,
            "host": "127.0.0.1",
            "port": 8765,
            "browser": {
                "extension_version": self.version or chrome_bootstrap.expected_extension_version()
            },
        }


class _OutdatedBridge(_ConnectedBridge):
    version = "1.2.0"


def _use_bridge(monkeypatch, bridge):
    monkeypatch.setattr(chrome_bootstrap, "get_chrome_bridge", lambda: bridge)
    return bridge


def test_setup_returns_manual_steps_and_touches_no_browser(monkeypatch):
    _use_bridge(monkeypatch, _DisconnectedBridge())
    result = chrome_bootstrap.setup_current_chrome()

    assert result["success"] is True
    assert result["ready"] is False
    assert result["already_connected"] is False
    assert result["extension_directory"] == str(chrome_bootstrap.EXTENSION_DIR)
    assert result["extension_version"] == chrome_bootstrap.expected_extension_version()
    # The folder the user has to pick must be spelled out, not implied.
    assert any(
        str(chrome_bootstrap.EXTENSION_DIR) in step for step in result["manual_steps"]
    )
    assert any("chrome://extensions" in step for step in result["manual_steps"])
    assert any("Developer mode" in step for step in result["manual_steps"])


def test_setup_reports_ready_without_steps_when_the_companion_is_current(monkeypatch):
    _use_bridge(monkeypatch, _ConnectedBridge())
    result = chrome_bootstrap.setup_current_chrome()

    assert result["success"] is True
    assert result["ready"] is True
    assert result["already_connected"] is True
    assert result["update_required"] is False
    assert result["manual_steps"] == []
    assert result["next"] is None
    assert chrome_bootstrap.expected_extension_version() in result["message"]


def test_setup_demands_a_reload_when_the_connected_build_is_older(monkeypatch):
    _use_bridge(monkeypatch, _OutdatedBridge())
    result = chrome_bootstrap.setup_current_chrome()

    assert result["ready"] is False
    assert result["already_connected"] is True
    assert result["update_required"] is True
    assert result["connected_version"] == "1.2.0"
    assert any("Reload" in step for step in result["manual_steps"])
    assert "1.2.0" in result["next"]


def test_setup_publishes_the_bridge_token_before_touching_chrome(isolated_token, monkeypatch):
    _use_bridge(monkeypatch, _DisconnectedBridge())
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
    _use_bridge(monkeypatch, _DisconnectedBridge())
    result = chrome_bootstrap.setup_current_chrome()

    assert result["token_ready"] is False
    assert "PermissionError" in result["token_error"]


def test_wait_seconds_is_passed_to_the_bridge_and_kept_sane(monkeypatch):
    bridge = _use_bridge(monkeypatch, _DisconnectedBridge())

    chrome_bootstrap.setup_current_chrome()
    assert bridge.waited == 1.0

    chrome_bootstrap.setup_current_chrome(wait_seconds=5)
    assert bridge.waited == 5.0

    # A caller must not be able to park the MCP thread for an hour.
    chrome_bootstrap.setup_current_chrome(wait_seconds=3600)
    assert bridge.waited == 30.0


def test_setup_reports_a_broken_clone_instead_of_promising_an_install(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome_bootstrap, "EXTENSION_DIR", tmp_path / "chrome-extension")
    _use_bridge(monkeypatch, _DisconnectedBridge())
    result = chrome_bootstrap.setup_current_chrome()

    assert result["success"] is False
    assert result["manual_steps"] == []
    assert "manifest is missing" in result["error"]


def test_the_ui_automation_helpers_are_gone():
    """The removed pywinauto path must not creep back in through a merge."""
    for name in (
        "_install_with_windows_ui",
        "_try_enable_existing",
        "_select_chrome_window",
        "_wait_for_dialog",
        "_focus_window",
        "_escape_send_keys",
        "_find_control",
        "_visible_top_level_windows",
    ):
        assert not hasattr(chrome_bootstrap, name)
    source = Path(chrome_bootstrap.__file__).read_text(encoding="utf-8")
    assert "pywinauto" not in source


def test_companion_directory_contains_manifest():
    assert isinstance(chrome_bootstrap.EXTENSION_DIR, Path)
    assert (chrome_bootstrap.EXTENSION_DIR / "manifest.json").is_file()


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def test_companion_icons_exist_at_the_sizes_the_manifest_promises():
    # A manifest that points at a missing icon makes Chrome refuse the whole
    # extension, so the paths are worth checking without launching a browser.
    manifest = json.loads(
        (chrome_bootstrap.EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    declared = manifest["icons"]
    assert manifest["action"]["default_icon"] == declared  # toolbar button, not a blank square
    assert set(declared) == {"16", "32", "48", "128"}
    for size, relative in declared.items():
        icon = chrome_bootstrap.EXTENSION_DIR / relative
        assert icon.is_file(), relative
        assert _png_size(icon) == (int(size), int(size)), relative
