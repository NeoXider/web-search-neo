"""The target order and positions a Tencent word/icon challenge publishes about itself.

`ins_elem_cfg.ins_list[].{sprite_url, ins_pos}` is the widget's own answer to "what are the
targets and where do they sit"; it was already in the prehandle body we read. This module reads
that record instead of trying to recognise painted glyphs from pixels — shape matching is
structurally blind to pictographs, and it refuses rather than guesses.

No network here: the prehandle payload is handed in by the caller (captcha_read already has it).
"""

from __future__ import annotations

import json
from typing import Any


def target_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """The order and boxes the challenge itself declares for its targets.

    ``payload`` is the prehandle answer (or a captcha_read record carrying the same block).
    Returns ``{"targets": [...], "boxes": [[x,y,w,h], ...]}`` in the instruction's own order,
    plus the sprite URLs so a caller can crop each glyph, or ``{"why": ...}`` when the widget
    published no such block.
    """
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    dynamic = inner.get("dyn_show_info") or inner.get("challenge") or {}
    if isinstance(dynamic, str):
        try:
            dynamic = json.loads(dynamic)
        except json.JSONDecodeError:
            return {"why": "the widget's own instruction block was not readable"}
    plan = dynamic.get("ins_elem_cfg") or {}
    items = plan.get("ins_list")
    if not isinstance(items, list) or not items:
        return {"why": "the challenge published no instruction element list"}
    targets: list[str] = []
    boxes: list[list[int]] = []
    sprites: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        position = item.get("ins_pos") or item.get("pos")
        size = item.get("size") or item.get("ins_size")
        url = item.get("sprite_url") or item.get("url")
        label = item.get("text") or item.get("word")
        if not isinstance(position, list) or len(position) < 2:
            continue
        width, height = (size if isinstance(size, list) and len(size) >= 2 else [0, 0])
        boxes.append([int(position[0]), int(position[1]), int(width), int(height)])
        sprites.append(str(url) if url else "")
        targets.append(str(label) if label else "")
    if not boxes:
        return {"why": "the instruction list carried no positions"}
    out: dict[str, Any] = {
        "targets": targets,
        "boxes": boxes,
        "sprite_urls": sprites,
        "complete": all(targets),
    }
    if not any(out["targets"]):
        out["why"] = ("the widget names its order and boxes but not the target text here; "
                      "crop each sprite_url to read the glyph")
    return out
