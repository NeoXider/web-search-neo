"""Regenerate mcp_servers.json with paths that match this checkout.

The repository ships ``mcp_servers.json`` in a portable form (a relative
``"cwd": "."``) so no developer's machine path ever gets committed. MCP clients,
however, want an absolute working directory: run this script once on the machine
that will host the server and it rewrites ``mcp_servers.json`` for *this* clone.

    python scripts/make_mcp_config.py            # rewrite mcp_servers.json in place
    python scripts/make_mcp_config.py --print    # only show what would be written

Other entries in an existing mcp_servers.json are preserved; the
``web-search-neo`` entry is replaced. The output uses forward slashes so it stays
valid JSON on Windows (see INSTALL.md). When the checkout has a virtualenv
(``.venv/Scripts/python.exe`` or ``.venv/bin/python``) its interpreter is used
instead of a bare ``python``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SERVER_NAME = "web-search-neo"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


VENV_INTERPRETERS = (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python")


def _venv_home(root: Path) -> Path | None:
    """The base interpreter directory of ``root/.venv``, from its pyvenv.cfg."""
    try:
        text = (root / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "home":
            home = Path(value.strip())
            if home.is_dir():
                return home
    return None


def _windowless_venv_entry(root: Path) -> dict | None:
    """An MCP entry that bypasses the venv launcher redirector, if there is one.

    Some venv implementations (uv's included) ship ``pythonw.exe`` as a small
    launcher that starts the real interpreter as a *child* process. A GUI-less
    MCP client then gets two processes: a windowless shim and a visible console
    child, which is exactly the window this setting exists to avoid. Pointing
    the command at the base ``pythonw.exe`` directly - with
    ``__PYVENV_LAUNCHER__`` naming the venv, the same hand-off the bridge
    daemon uses - starts one windowless process instead.
    """
    venv_pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if not venv_pythonw.is_file():
        return None
    home = _venv_home(root)
    if home is None:
        return None
    base_pythonw = home / "pythonw.exe"
    if not base_pythonw.is_file() or base_pythonw.resolve() == venv_pythonw.resolve():
        return None
    return {
        "command": base_pythonw.as_posix(),
        "args": ["main.py"],
        "cwd": root.as_posix(),
        "env": {"__PYVENV_LAUNCHER__": venv_pythonw.as_posix()},
    }


def interpreter_for(root: Path) -> str:
    """The checkout's own virtualenv interpreter when there is one, else ``python``.

    A bare ``python`` is whatever the MCP client finds first on PATH, which is
    rarely the environment the dependencies were installed into.

    On Windows the windowless interpreter is preferred: a console python under
    a windowed MCP client gets its own visible console for the whole session,
    while pythonw runs the same stdio pipes with no console at all. The venv
    console interpreter stays ahead of a bare PATH pythonw, which would be
    windowless but run the wrong environment.
    """
    if os.name == "nt":
        for relative in (
            Path(".venv") / "Scripts" / "pythonw.exe",
            Path(".venv") / "Scripts" / "python.exe",
        ):
            candidate = root / relative
            if candidate.is_file():
                return candidate.as_posix()
        return "pythonw"
    for relative in VENV_INTERPRETERS:
        candidate = root / relative
        if candidate.is_file():
            return candidate.as_posix()
    return "python"


def entry_for(root: Path) -> dict:
    """The MCP-server entry pinned to ``root``."""
    if os.name == "nt":
        bypass = _windowless_venv_entry(root)
        if bypass is not None:
            return bypass
    return {
        "command": interpreter_for(root),
        "args": ["main.py"],
        "cwd": root.as_posix(),
    }


def build_config(root: Path, existing: dict | None = None) -> dict:
    config = {"mcpServers": {}} if existing is None else json.loads(json.dumps(existing))
    servers = config.setdefault("mcpServers", {})
    servers[SERVER_NAME] = entry_for(root)
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the JSON to this file (default: <repo>/mcp_servers.json)",
    )
    parser.add_argument(
        "--print",
        dest="just_print",
        action="store_true",
        help="print the configuration instead of writing it",
    )
    args = parser.parse_args()

    root = repo_root()
    if not (root / "main.py").is_file():
        print(f"error: no main.py found under {root}", file=sys.stderr)
        return 1

    existing: dict | None = None
    out_path = args.out or root / "mcp_servers.json"
    if out_path.is_file():
        try:
            loaded = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError) as error:
            print(f"error: cannot read {out_path}: {error}", file=sys.stderr)
            return 1

    text = json.dumps(build_config(root, existing), indent=2, ensure_ascii=False) + "\n"
    if args.just_print:
        print(text, end="")
    else:
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
