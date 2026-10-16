"""Shared shapes for macro validation outcomes."""
from __future__ import annotations

from typing import Any


def unreadable(name: str, path: Any, error: str, fix: str) -> dict[str, Any]:
    """One shape for every reason a file could not be checked at all."""
    return {
        "success": False,
        "macro": name,
        "valid": False,
        "path": str(path),
        "errors": [{"error": error, "fix": fix}],
        "warnings": [],
        "executed": False,
    }
