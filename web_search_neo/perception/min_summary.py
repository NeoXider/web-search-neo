"""``summary: "min"``: the short form of an action's answer, with every cut named.

An agent that only needs "did it work, where am I" pays for the whole page
summary, element lists and report bodies on every step. The short form keeps
the scalars (success, error, url, title, grade, counts...), small objects, the
head of a few lists that *are* the answer (``priority``, ``failed_steps``,
``problems``, ``recommendations``), and names everything else it left out in
``summary_omitted`` and every shortened string in ``summary_clipped`` - the
project rule is that nothing is cut silently. ``summary_mode: "min"`` marks the
short form; a ``summary`` key of the answer itself is kept as it was.
"""
from __future__ import annotations

import json
from typing import Any

MODES = ("full", "min")
STRING_LIMIT = 240
SMALL_OBJECT_CHARS = 400
# Lists that carry the verdict itself: kept, but only their head.
HEAD_LISTS = {"priority": 5, "failed_steps": 10, "problems": 5, "recommendations": 5, "warnings": 5,
              "refused": 5, "not_deleted": 5}


def _clip(value: str, clipped: list[str], key: str) -> str:
    if len(value) <= STRING_LIMIT:
        return value
    if key not in clipped:
        clipped.append(key)
    return f"{value[:STRING_LIMIT]}...(+{len(value) - STRING_LIMIT} chars)"


def _small(value: Any) -> bool:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str)) <= SMALL_OBJECT_CHARS
    except (TypeError, ValueError):
        return False


def _item(value: Any, key: str, clipped: list[str], omitted: dict[str, str]) -> Any:
    """One kept list item: its scalars, strings clipped; dropped fields are named."""
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for name, item in value.items():
            if item is None or isinstance(item, (bool, int, float)):
                kept[name] = item
            elif isinstance(item, str):
                kept[name] = _clip(item, clipped, f"{key}[].{name}")
            else:
                omitted.setdefault(f"{key}[].{name}", type(item).__name__)
        return kept
    if isinstance(value, str):
        return _clip(value, clipped, f"{key}[]")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    omitted.setdefault(f"{key}[]", type(value).__name__)
    return None


def minimize(data: Any) -> Any:
    """The short form of one action's ``data`` (non-dicts pass through unchanged)."""
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    omitted: dict[str, str] = {}
    clipped: list[str] = []
    for key, value in data.items():
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = _clip(value, clipped, key)
        elif isinstance(value, list) and key in HEAD_LISTS:
            limit = HEAD_LISTS[key]
            out[key] = [_item(item, key, clipped, omitted) for item in value[:limit]]
            if len(value) > limit:
                omitted[key] = f"list: kept {limit} of {len(value)}"
        elif isinstance(value, dict) and _small(value):
            out[key] = value
        elif isinstance(value, list):
            omitted[key] = f"list[{len(value)}]"
        elif isinstance(value, dict):
            omitted[key] = f"object with {len(value)} key(s)"
        else:
            omitted[key] = type(value).__name__
    if omitted:
        out["summary_omitted"] = omitted
    if clipped:
        out["summary_clipped"] = clipped
    out["summary_mode"] = "min"
    return out


def step_mode(arguments: dict[str, Any], fields: Any, default: str = "full") -> str:
    """Pop an action's own ``"summary"`` key (unless the action defines one) and check it."""
    mode = arguments.pop("summary", default) if "summary" not in fields else default
    if mode not in MODES:
        raise ValueError(f"summary must be one of {list(MODES)}, not {mode!r}")
    return str(mode)
