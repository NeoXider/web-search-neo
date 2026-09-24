"""Big script results: a window with a flag and a next offset, or a file.

A script that returned 3 000 objects used to answer with 212 KB (the response
budget did not apply to scripts, and ``value`` and ``value_json`` carried the
payload twice), while a string past 200 000 characters came back as its head
with the tail unreachable. Now a public ``run_script``/``execute_js`` answer
keeps ``value_json`` within ``max_chars`` and says what it cut:

- a string is windowed by characters, an array by whole items, anything else
  by the characters of its JSON text (``value`` is then left out and
  ``value_json`` holds that slice) - each time with ``truncated``,
  ``total_length`` (characters or items), ``offset`` and ``next_offset``;
- ``save_to`` writes the whole value as JSON into the download folder and
  answers with the path, size and a short preview instead (never over an
  existing file, at most ``MAX_SAVED_BYTES``).

Internal callers that pass no ``max_chars`` still get the old ceiling of
``DEFAULT_CEILING_CHARS``, flagged the same way - never an unbounded value.
"""
from __future__ import annotations

import json
from typing import Any

from web_search_neo.fetch.safety import write_download

PREVIEW_CHARS = 500
DEFAULT_CEILING_CHARS = 200_000
MAX_SAVED_BYTES = 50 * 1024 * 1024


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _save(answer: dict[str, Any], save_to: str) -> dict[str, Any]:
    text = _dumps(answer.get("value"))
    data = text.encode("utf-8")
    if len(data) > MAX_SAVED_BYTES:
        raise ValueError(f"The value is {len(data)} bytes as JSON, over the {MAX_SAVED_BYTES} byte "
                         "save_to limit; return less (or read it in windows with offset).")
    try:
        path = write_download(save_to, data, overwrite=False)
    except ValueError as exc:
        raise ValueError(str(exc).replace("; pass overwrite=true to replace it",
                                          "; choose another save_to name")) from None
    value = answer.get("value")
    shaped = {key: item for key, item in answer.items() if key not in {"value", "value_json"}}
    return {**shaped, "saved_to": str(path), "bytes": path.stat().st_size, "value_saved": True,
            **({"total_length": len(value)} if isinstance(value, (str, list)) else {}),
            "preview": text[:PREVIEW_CHARS], "preview_truncated": len(text) > PREVIEW_CHARS}


def _window_items(items: list[Any], offset: int, budget: int) -> tuple[list[Any], int]:
    kept: list[Any] = []
    spent = 2
    for item in items[offset:]:
        cost = len(_dumps(item)) + 2  # the ", " between items
        if kept and spent + cost > budget:
            break
        kept.append(item)
        spent += cost
    return kept, offset + len(kept)


def shape(answer: dict[str, Any], *, save_to: str | None = None, offset: int = 0,
          max_chars: int | None = None) -> dict[str, Any]:
    """Apply ``save_to`` or the ``max_chars`` window to a successful script answer."""
    if not answer.get("success") or "value" not in answer:
        return answer
    if save_to:
        return _save(answer, save_to)
    value = answer["value"]
    text = answer.get("value_json") or _dumps(value)
    offset = max(0, int(offset or 0))
    if max_chars is None:
        max_chars = DEFAULT_CEILING_CHARS
    if len(text) <= max_chars and not offset:
        return answer
    budget = max(200, int(max_chars))
    if isinstance(value, str):
        total = len(value)
        piece = value[offset:offset + budget]
        end = offset + len(piece)
        window: dict[str, Any] = {"value": piece, "value_json": _dumps(piece)}
    elif isinstance(value, list):
        total = len(value)
        piece, end = _window_items(value, min(offset, total), budget)
        window = {"value": piece, "value_json": _dumps(piece)}
    else:
        total = len(text)
        piece = text[offset:offset + budget]
        end = offset + len(piece)
        window = {"value": None, "value_json": piece, "value_windowed_as_text": True}
    truncated = end < total or offset > 0
    return {**answer, **window, "truncated": truncated, "total_length": total, "offset": offset,
            "next_offset": end if end < total else None, "max_chars": budget,
            "window_note": ("Part of the value: read on with offset=next_offset, or pass "
                            "save_to='name.json' to get all of it as a file.")}


_CARRIED_FLAGS = ("truncated", "total_length", "offset", "next_offset")


def carry(result: dict[str, Any], key: str = "value", hint: str | None = None) -> dict[str, Any]:
    """A script answer's value for a wrapper action, with every window flag kept.

    ``local_storage`` and ``replay_request`` used to copy ``value`` alone, so a
    windowed value arrived cut without ``truncated``, and one windowed as JSON
    text arrived as ``None``. The value now travels with its flags; a value
    windowed as text comes as ``<key>_json_part`` next to a ``None``.
    """
    carried: dict[str, Any] = {key: result.get("value")}
    if result.get("value_windowed_as_text"):
        carried[f"{key}_json_part"] = result.get("value_json")
        carried[f"{key}_windowed_as_text"] = True
    carried.update({flag: result[flag] for flag in _CARRIED_FLAGS if flag in result})
    if carried.get("truncated") and hint:
        carried["window_note"] = hint
    return carried
