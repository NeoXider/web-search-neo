"""The shipped MCP client configuration must stay valid for its own checkout.

Before 1.10 the committed file hard-coded a developer's clone path, so every other
machine inherited a stale ``cwd`` and the server failed at startup. The portable
form (a relative cwd) plus scripts/make_mcp_config.py is what this test guards:
any absolute cwd that does ship must point at a directory containing main.py on the
machine where the tests run, otherwise regeneration was forgotten after a move.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_COMMANDS = {".venv/Scripts/python.exe", ".venv/bin/python"}


def _expected_interpreter(root: Path) -> str:
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python"):
        if (root / relative).is_file():
            return (root / relative).as_posix()
    return "python"


def _server_entry() -> dict:
    config = json.loads((REPO_ROOT / "mcp_servers.json").read_text(encoding="utf-8"))
    entry = config["mcpServers"]["web-search-neo"]
    assert isinstance(entry, dict), "the web-search-neo entry must be an object"
    return entry


def test_shipped_mcp_config_points_at_a_real_checkout():
    entry = _server_entry()

    command = str(entry.get("command"))
    # A bare interpreter, the checkout's virtualenv (relative to cwd, as the
    # portable form ships), or an absolute virtualenv path the generator wrote.
    portable = command in {"python", "python.exe"} or command in VENV_COMMANDS
    pinned = Path(command).is_absolute() and command.replace("\\", "/").endswith(
        tuple(VENV_COMMANDS)
    )
    assert portable or pinned, f"unexpected interpreter command: {command!r}"
    if pinned:
        assert Path(command).is_file(), f"the pinned interpreter is missing: {command!r}"
    assert entry.get("args") == ["main.py"], f"unexpected args: {entry.get('args')!r}"

    raw_cwd = entry.get("cwd", ".")
    cwd_path = Path(raw_cwd)
    if not cwd_path.is_absolute():
        # A relative cwd is resolved the way an MCP client would resolve it:
        # against the location of this configuration file, which is the repo root.
        cwd_path = REPO_ROOT / raw_cwd

    assert cwd_path.is_dir(), (
        f"mcp_servers.json names a cwd that does not exist on this machine: {raw_cwd!r}. "
        "Run `python scripts/make_mcp_config.py` to pin the configuration to this "
        "checkout, or restore the portable form with \"cwd\": \".\"."
    )
    assert (cwd_path / "main.py").is_file(), (
        f"the configured cwd {raw_cwd!r} has no main.py; it does not look like a web-search-neo checkout"
    )


def test_make_mcp_config_pins_this_checkout_and_preserves_foreign_servers():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "make_mcp_config.py"), "--print"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"generator failed: {result.stderr}"

    config = json.loads(result.stdout)
    entry = config["mcpServers"]["web-search-neo"]

    # Forward slashes keep the JSON portable across platforms (INSTALL.md).
    expected_cwd = REPO_ROOT.as_posix().replace("\\", "/")
    assert entry == {
        "command": _expected_interpreter(REPO_ROOT),
        "args": ["main.py"],
        "cwd": expected_cwd,
    }


def test_make_mcp_config_prefers_the_checkout_virtualenv(tmp_path):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import make_mcp_config
    finally:
        sys.path.pop(0)
    assert make_mcp_config.entry_for(tmp_path)["command"] == "python"
    unix = tmp_path / ".venv" / "bin" / "python"
    unix.parent.mkdir(parents=True)
    unix.write_text("", encoding="utf-8")
    assert make_mcp_config.entry_for(tmp_path)["command"] == unix.as_posix()
    windows = tmp_path / ".venv" / "Scripts" / "python.exe"
    windows.parent.mkdir(parents=True)
    windows.write_text("", encoding="utf-8")
    assert make_mcp_config.entry_for(tmp_path)["command"] == windows.as_posix()


def test_make_mcp_config_out_rewrites_only_its_own_entry(tmp_path):
    foreign = tmp_path / "mcp_servers.json"
    foreign.write_text(
        json.dumps({"mcpServers": {"other-server": {"command": "node", "args": ["x.js"]}}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "make_mcp_config.py"), "--out", str(foreign)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"generator failed: {result.stderr}"

    config = json.loads(foreign.read_text(encoding="utf-8"))
    servers = config["mcpServers"]
    # The foreign entry survives the regeneration...
    assert servers["other-server"] == {"command": "node", "args": ["x.js"]}
    # ...while web-search-neo is pinned to this checkout.
    assert servers["web-search-neo"]["cwd"] == REPO_ROOT.as_posix().replace("\\", "/")
    assert servers["web-search-neo"]["args"] == ["main.py"]
