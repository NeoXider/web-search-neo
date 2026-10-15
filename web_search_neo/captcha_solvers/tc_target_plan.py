"""The target order and glyph images a Tencent word/icon challenge publishes about itself.

`ins_elem_cfg.ins_list[].{sprite_url, ins_pos}` is the widget's own answer to "what are the
targets and in which order"; it was already in the prehandle body we read. This module reads
that record instead of trying to recognise painted glyphs from pixels — shape matching is
structurally blind to pictographs, and it refuses rather than guesses.

The boxes are where each target sits IN THE INSTRUCTION STRIP, not where it sits in the picture:
the block names what to look for and in which order, so it is a reference for a solver and never
itself an answer. Nothing here clicks or verifies.

No network: the prehandle record is handed in by the caller (captcha_read already has it).
"""

from __future__ import annotations

import json
from typing import Any

POSITION_KEYS = ("ins_pos", "pos")
SIZE_KEYS = ("size", "ins_size")
TEXT_KEYS = ("text", "word", "label")
SPRITE_KEYS = ("sprite_url", "url", "img_url")


def _pair(value: Any) -> tuple[int, int] | None:
    """Two whole numbers, or nothing at all — never a padded guess."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        first, second = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    if first < 0 or second < 0:
        return None
    return first, second


def target_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """The order and glyph images the challenge declares for its targets.

    ``payload`` is the prehandle answer (or a captcha_read record carrying the same block under
    ``ins_elem_cfg``). Returns ``{"targets": [...], "boxes": [[x,y,w,h], ...], "sprite_urls":
    [...]}`` in the instruction's own order plus ``complete`` — true only when every target names
    its text — or a refusal with ``why`` and no invented order. An item whose position or size is
    junk is dropped and counted, never replaced by zeroes.
    """
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    dynamic = inner.get("dyn_show_info") or {}
    if isinstance(dynamic, str):
        try:
            dynamic = json.loads(dynamic)
        except json.JSONDecodeError:
            return {"why": "the widget's own instruction block was not readable"}
    blocks = [dynamic.get("ins_elem_cfg"), inner.get("ins_elem_cfg")]
    items = None
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("ins_list"), list):
            items = block["ins_list"]
            break
    if not items:
        return {"why": "the challenge published no instruction element list"}

    targets: list[str] = []
    boxes: list[list[int]] = []
    sprites: list[str] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        position = None
        for key in POSITION_KEYS:
            if key in item:
                position = _pair(item[key])
                break
        size = None
        for key in SIZE_KEYS:
            if key in item:
                size = _pair(item[key])
                break
        if position is None or size is None:
            dropped += 1
            continue
        label = ""
        for key in TEXT_KEYS:
            if str(item.get(key) or "").strip():
                label = str(item[key]).strip()
                break
        url = ""
        for key in SPRITE_KEYS:
            if str(item.get(key) or "").strip():
                url = str(item[key]).strip()
                break
        targets.append(label)
        boxes.append([position[0], position[1], size[0], size[1]])
        sprites.append(url)

    if not boxes:
        return {"why": "the instruction list carried no usable positions"}
    out: dict[str, Any] = {
        "targets": targets,
        "boxes": boxes,
        "sprite_urls": sprites,
        "complete": all(targets),
    }
    if dropped:
        out["dropped"] = dropped
    if not any(out["targets"]):
        out["why"] = (
            "the widget names its order and boxes but not the target text here; "
            "crop each sprite_url to read the glyph"
        )
    return out
