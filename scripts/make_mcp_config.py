"""Regenerate mcp_servers.json with paths that match this checkout.

The repository ships ``mcp_servers.json`` in a portable form (a relative
``"cwd": "."``) so no developer's machine path ever gets committed. MCP clients,
however, want an absolute working directory: run this script once on the machine
that will host the server and it rewrites ``mcp_servers.json`` for *this* clone.

    python scripts/make_mcp_config.py            # rewrite mcp_servers.json in place
    python scripts/make_mcp_config.py --print    # only show what would be written

Other entries in an existing mcp_servers.json are preserved; the
``web-search-neo`` entry is replaced. The output uses forward slashes so it stays
valid JSON on Windows (see INSTALL.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVER_NAME = "web-search-neo"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def entry_for(root: Path) -> dict:
    """The MCP-server entry pinned to ``root``."""
    return {
        "command": "python",
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
