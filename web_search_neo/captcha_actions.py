"""Actions that read a captcha challenge over the protocol the widget itself uses.

`captcha_read {session_id}` answers two questions an agent cannot answer from a screenshot:
what kind of challenge this is, and whether it can be measured at all. It sends only the
requests the widget would have sent anyway (its own prehandle and its own picture URL), so it
adds no new surface to the site under test. `captcha_verify` is the deliberate second half:
it spends one answer when a measurement was sure. captcha_answer is the third shape:
it answers the very challenge this module just read, with the boxes an agent measured itself.

Network primitives are injected, per the leaf-module rule; nothing here imports the browser.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from web_search_neo import captcha_aid
from web_search_neo.captcha_solvers import classify, tc_image_solve, tc_protocol, tc_target_plan

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def open_url(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """The one place this module touches the network."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read()


_RECENT: dict[str, Any] = {}
RECENT_TTL_S = 900.0


def _remember(entry: str, aid: str, host: str, challenge: dict[str, Any]) -> None:
    """Keep the challenge just read, so an answer can be sent to that one and not a new one."""
    _RECENT.clear()
    _RECENT.update({"entry": entry, "aid": aid, "host": host,
                    "challenge": dict(challenge), "read_at": time.monotonic()})


def _recent(entry: str, aid: str, host: str) -> dict[str, Any] | None:
    """The kept challenge for this page/aid/host, or None when it is stale or a different one."""
    if not _RECENT:
        return None
    if (_RECENT.get("entry"), _RECENT.get("aid"), _RECENT.get("host")) != (entry, aid, host):
        return None
    if time.monotonic() - float(_RECENT.get("read_at") or 0) > RECENT_TTL_S:
        return None
    return _RECENT


def _page_origin(page_url: str, aid: str | None = None) -> tuple[str, str]:
    """Where to ask, and what to say we are asking from."""
    entry = (page_url or "").strip()
    if not entry:
        raise ValueError("captcha_challenge needs a page address; pass page_url")
    parts = urllib.parse.urlsplit(entry)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"captcha_challenge wants an https page address, got {entry!r}")
    host = parts.netloc.split(":")[0]
    app_id = str(aid) if aid else _aid_of(host)
    return entry, app_id


def _aid_of(host: str) -> str:
    """The captcha app id belongs to the site, not to us - so it is never invented here.

    A refusal is cheap and immediate; reading the site's own published document is a separate
    call (captcha_challenge site_aid=true), so an agent decides when one is worth sending.
    """
    raise ValueError(
        f"no captcha app id was given for {host!r}; pass aid=... "
        "(the number the page's captcha config uses; captcha_challenge site_aid=true reads the "
        "document the site publishes for itself)"
    )


def captcha_read(
    page_url: str = "",
    *,
    aid: str | None = None,
    host: str = "t.captcha.qq.com",
    opener: Callable[[str, dict[str, str]], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    """Read one challenge and report what it is and whether it can be measured.

    `aid` is required unless the site publishes its own; an agent reads that with
    captcha_challenge {site_aid: true}. Nothing here invents a number.
    """
    entry, app_id = _page_origin(page_url, aid)
    runner = opener or open_url
    try:
        challenge = tc_image_solve.prepare(runner, entry, aid=app_id, host=host)
    except tc_protocol.ChallengeReadError as refused:
        return {
            "success": False,
            "page_url": entry,
            "aid": app_id,
            "why": str(refused),
            "note": "the challenge could not be read; nothing was verified",
        }
    except tc_image_solve.SolveAborted as aborted:
        return {
            "success": False,
            "page_url": entry,
            "aid": app_id,
            "why": str(aborted),
            "note": "the challenge was read but its picture could not be measured",
        }
    _remember(entry, app_id, host, challenge)
    image_bytes = challenge.pop("image_bytes")
    measurement = measure_challenge(challenge, image_bytes)
    # What the widget declared about its own targets, kept next to the measurement so an
    # agent sees the order the challenge asked for and never has to infer it.
    plan = tc_target_plan.target_plan(challenge)
    return {"success": True, "page_url": entry, "aid": app_id, **challenge, "target_plan": plan, **measurement}


def measure_challenge(challenge: dict[str, Any], image_bytes: bytes) -> dict[str, Any]:
    """Try to find the target's shape; report an unsure measurement as unsure."""
    targets = challenge.get("targets") or []
    if not targets:
        return {"measurement": {"sure": False, "why": "the challenge named no target to look for"}}
    try:
        import numpy as np
        from PIL import Image

        import io

        pixels = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"), dtype=np.uint8)
    except Exception as missing:  # noqa: BLE001 - optional dependency, reported not crashed
        return {"measurement": {"sure": False, "why": f"the picture could not be decoded: {missing}"}}
    from web_search_neo.captcha_solvers import word_shape

    bands = ((0.03, 0.14), (0.10, 0.20), (0.75, 0.92), (0.95, 0.06), (0.28, 0.42), (0.45, 0.60))
    height, width = pixels.shape[:2]
    cells: list[tuple[int, int, int, int]] | None = None
    size = challenge.get("image_size") or []
    # Only trust a grid that tiles the picture we actually decoded; the kind rule lives in
    # _published_grid so both measurement paths agree on what has a grid at all.
    if len(size) == 2 and int(size[0]) == width and int(size[1]) == height:
        cells = _published_grid(word_shape, challenge, width, height)
    attempts: list[dict[str, Any]] = []
    if cells:
        for band in bands:
            matches = word_shape.score_cells(pixels, targets[0], cells, hue=band, sat_min=0.45)
            decision = word_shape.choose_ordered(matches, needed=len(targets))
            attempts.append(
                {
                    "hue": list(band),
                    "regions": len(matches),
                    "top_score": matches[0].score if matches else None,
                    "top_box": list(matches[0].box) if matches else None,
                    "sure": bool(decision.get("sure")),
                }
            )
            if decision.get("sure"):
                return {
                    "measurement": {
                        "sure": True,
                        "hue": list(band),
                        "boxes": decision["boxes"],
                        "scores": decision["scores"],
                        "tried_bands": len(attempts),
                    }
                }
        best = max(attempts, key=lambda item: item["top_score"] or 0.0)
        detail = [
            {"box": list(item.box), "iou": item.iou, "correlation": item.correlation}
            for item in word_shape.score_cells(
                pixels, targets[0], cells, hue=tuple(best["hue"]), sat_min=0.45
            )
        ]
        return {
            "measurement": {
                "sure": False,
                "why": "no colour band produced a shape match strong enough to trust",
                "best_band": best["hue"],
                "best_regions": best["regions"],
                "best_box": best["top_box"],
                "cells": detail,
                "font": word_shape.font_note(),
                "tried_bands": len(attempts),
            }
        }
    for band in bands:
        matches = word_shape.match_word_shape(pixels, targets[0], hue=band, sat_min=0.45)
        decision = word_shape.choose_ordered(matches, needed=len(targets))
        attempts.append(
            {
                "hue": list(band),
                "regions": len(matches),
                "top_score": matches[0].score if matches else None,
                "sure": bool(decision.get("sure")),
            }
        )
        if decision.get("sure"):
            return {
                "measurement": {
                    "sure": True,
                    "hue": list(band),
                    "boxes": decision["boxes"],
                    "scores": decision["scores"],
                    "tried_bands": len(attempts),
                }
            }
    best = max(attempts, key=lambda item: item["top_score"] or 0.0)
    return {
        "measurement": {
            "sure": False,
            "why": "no colour band produced a shape match strong enough to trust",
            "best_band": best["hue"],
            "best_regions": best["regions"],
            "tried_bands": len(attempts),
        }
    }


def captcha_verify(
    page_url: str = "",
    *,
    aid: str | None = None,
    host: str = "t.captcha.qq.com",
    boxes: list[tuple[int, int, int, int]] | None = None,
    opener: Callable[[str, dict[str, str]], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    """Spend one answer for a challenge that was measured sure. Refuses otherwise."""
    entry, known_aid = _page_origin(page_url, aid)
    app_id = str(aid or known_aid)
    runner = opener or open_url
    read = captcha_read(entry, aid=app_id, host=host, opener=runner)
    if not read.get("success"):
        return {**read, "verified": False}
    measurement = read.get("measurement") or {}
    chosen = boxes or measurement.get("boxes")
    if not measurement.get("sure") and not boxes:
        return {
            "success": False,
            "verified": False,
            "page_url": entry,
            "aid": app_id,
            "why": measurement.get("why") or "the challenge was not measured",
            "note": "no answer was sent: an unsure measurement would only waste the attempt",
        }
    challenge = dict(read)
    # finish() wants the salt, not the record solve_pow returned. prepare() stores the record
    # under `pow_answer`, so flatten it exactly once and hand over a plain string.
    pow_record = read.get("pow_answer") or {}
    if isinstance(pow_record, dict):
        # A record that was never solved is a refusal, not an empty answer to send.
        if not pow_record.get("solved"):
            return {
                "success": False,
                "verified": False,
                "page_url": entry,
                "aid": app_id,
                "why": pow_record.get("why") or "the challenge's hash was not solved",
                "note": "no answer was sent: an unsolved hash is not worth one attempt",
            }
        salt = str(pow_record.get("salt") or "")
    else:
        salt = str(pow_record)
    if not salt:
        return {
            "success": False,
            "verified": False,
            "page_url": entry,
            "aid": app_id,
            "why": "the challenge's hash was not solved",
            "note": "no answer was sent: an unsolved hash is not worth one attempt",
        }
    result = tc_image_solve.finish(
        runner, {**challenge, "pow_answer": salt}, list(chosen), entry, host=host
    )
    # finish() reports its own verdict under ok/ticket; flatten it once so the caller sees one
    # clear pair of names instead of both spellings.
    accepted = bool(result.get("ok"))
    return {
        "success": accepted,
        "verified": True,
        "page_url": entry,
        "aid": app_id,
        "challenge": read.get("challenge"),
        "boxes_used": list(chosen),
        "accepted": accepted,
        "ticket": result.get("ticket") or "",
        "randstr": result.get("randstr") or "",
        "why": result.get("why") or "",
        "clicks": result.get("clicks") or [],
    }


# Challenges this layer measures from a live page: Tencent's tile family asks for
# tiles or words in an order, and a slider drag would answer a question nobody asked.
IMAGE_SELECT_KINDS = ("image_select", "word_click", "icon_click")

# The same colour bands measure_challenge sweeps: one flat caption colour over a
# photo background, tried from the most common caption hues first.
_MEASURE_BANDS = ((0.03, 0.14), (0.10, 0.20), (0.75, 0.92), (0.95, 0.06), (0.28, 0.42), (0.45, 0.60))

# The widget picture on the page: the largest image inside the TCaptcha
# containers the classifier already knows, plus the instruction prose around it.
# A page script, kept here so the browser module stays a thin caller; it reads
# geometry and text and changes nothing.
IMAGE_LOCATE_SCRIPT = """
const picks = ['#tcaptcha_transform_dy', '#TCaptcha', '[class*="tencent-captcha"]',
  '#captcha_popup', '[id^="tCaptcha"]'];
let best = null;
for (const sel of picks) {
  let roots = [];
  try { roots = Array.from(document.querySelectorAll(sel)); } catch (err) { continue; }
  for (const root of roots.slice(0, 3)) {
    let imgs = [];
    try { imgs = Array.from(root.querySelectorAll('img')); } catch (err) { continue; }
    const text = ((root.innerText || '').trim().slice(0, 200));
    for (const img of imgs) {
      const r = img.getBoundingClientRect();
      if (r.width >= 80 && r.height >= 40
          && (!best || r.width * r.height > best.area)) {
        best = {x: r.x, y: r.y, width: r.width, height: r.height,
          area: r.width * r.height, instruction: text};
      }
    }
  }
}
if (!best) return {};
return {x: best.x, y: best.y, width: best.width, height: best.height,
  instruction: best.instruction};
"""


def captcha_answer(
    page_url: str = "",
    *,
    aid: str | None = None,
    host: str = "t.captcha.qq.com",
    boxes: list[tuple[int, int, int, int]] | None = None,
    opener: Callable[[str, dict[str, str]], tuple[int, bytes]] | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    """Answer the challenge this module just read - that one, not a freshly drawn one.

    oxes are regions (x, y, w, h) in the picture's own pixels, clicked in the order given; an
    agent that measured the puzzle itself passes them and its reading is what gets spent. With no
    boxes the library measures the kept picture and answers only when that measurement is sure:
    an unsure answer is refused, never sent. resh=True draws a new challenge instead.
    """
    entry, known_aid = _page_origin(page_url, aid)
    app_id = str(aid or known_aid)
    runner = opener or open_url
    record = None if fresh else _recent(entry, app_id, host)
    if record is None:
        read = captcha_read(entry, aid=app_id, host=host, opener=runner)
        if not read.get("success"):
            return {**read, "verified": False}
        record = _recent(entry, app_id, host)
        if record is None:
            return {"success": False, "verified": False, "page_url": entry, "aid": app_id,
                    "why": "the challenge that was read was not kept for answering",
                    "note": "nothing was verified"}
    challenge = dict(record["challenge"])
    image_bytes = challenge.pop("image_bytes", b"") or b""
    measurement = measure_challenge(challenge, image_bytes)
    chosen = boxes or (measurement.get("boxes") if measurement.get("sure") else None)
    if not chosen:
        return {"success": False, "verified": False, "page_url": entry, "aid": app_id,
                "why": "no boxes were given and the kept challenge was not measured sure",
                "note": "nothing was verified; pass boxes=[[x, y, w, h], ...] in picture pixels",
                "measurement": measurement}
    pow_record = challenge.get("pow_answer") or {}
    if isinstance(pow_record, dict):
        if not pow_record.get("solved"):
            return {"success": False, "verified": False, "page_url": entry, "aid": app_id,
                    "why": pow_record.get("why") or "the challenge's hash was not solved",
                    "note": "no answer was sent: an unsolved hash is not worth one attempt"}
        salt = str(pow_record.get("salt") or "")
    else:
        salt = str(pow_record)
    if not salt:
        return {"success": False, "verified": False, "page_url": entry, "aid": app_id,
                "why": "the challenge's hash was not solved",
                "note": "no answer was sent: an unsolved hash is not worth one attempt"}
    challenge["pow_answer"] = salt
    result = tc_image_solve.finish(runner, challenge, [tuple(b) for b in chosen], entry, host=host)
    return {"success": bool(result.get("ok")), "verified": bool(result.get("ok")),
            "page_url": entry, "aid": app_id, "ticket": result.get("ticket") or "",
            "randstr": result.get("randstr") or "", "code": result.get("code") or "",
            "why": result.get("why") or "", "clicks": result.get("clicks") or [],
            "boxes_used": [list(b) for b in chosen], "answer_order": "the order given",
            "challenge_age_s": round(time.monotonic() - float(record.get("read_at") or 0), 1)}


def _published_grid(word_shape: Any, record: dict[str, Any], width: int, height: int):
    """The grid cells to score, or None when this picture has no grid to score.

    Regions the challenge published are used whatever the challenge kind is - they are its own
    geometry. An even tiling is only assumed for a tile challenge (image_select ships 3x2); a
    word_click or icon_click picture is a ribbon of painted glyphs, so it keeps the component
    hunt. One implementation of both rules: word_shape.split_cells reads the published regions and
    does the tiling, and nothing here repeats its validation.
    """
    cfg = record.get("click_cfg") if isinstance(record.get("click_cfg"), dict) else None
    regions = (cfg or {}).get("select_regions") or (cfg or {}).get("regions")
    if isinstance(regions, list) and regions:
        return word_shape.split_cells(width, height, cfg) or None
    if str(record.get("challenge") or "") == "image_select":
        return word_shape.split_cells(width, height, None)
    return None


def _cell_of(match: Any) -> dict[str, Any]:
    return {
        "box": list(match.box),
        "iou": match.iou,
        "correlation": match.correlation,
        "score": match.score,
    }


def image_select_from_page(
    read: dict[str, Any] | None,
    page_bytes: bytes,
    *,
    instruction: str | None = None,
) -> dict[str, Any]:
    """Score a tile challenge's picture and name the tiles in order, when sure.

    `read` is a challenge record as captcha_read() produces (instruction,
    targets, click_cfg, challenge); `page_bytes` is the PNG/JPEG of the widget
    picture captured from the page. `instruction` overrides the record's prose
    when the caller read fresher text off the widget.

    Returns {"sure", "ordered_boxes", "cells", "why"} plus "picture_size" and
    the winning "hue" when the picture decoded. "ordered_boxes" is present only
    when every target cleared the shape gates (MIN_AGREE on both views, MIN_LEAD
    over the runner-up, one distinct tile per target, in instruction order); a
    refusal names why and carries no boxes, so there is nothing to click.
    """
    record = read or {}
    text = instruction if instruction is not None else (record.get("instruction") or "")
    targets = tc_protocol.target_words(text) or [
        word for word in (str(item) for item in (record.get("targets") or [])) if word
    ]
    if not targets:
        return {"sure": False, "cells": [], "why": "the challenge named no target to look for"}
    try:
        import io

        import numpy as np
        from PIL import Image

        pixels = np.asarray(Image.open(io.BytesIO(page_bytes)).convert("RGB"), dtype=np.uint8)
    except Exception as missing:  # noqa: BLE001 - optional dependency, reported not crashed
        return {"sure": False, "cells": [], "why": f"the widget picture could not be decoded: {missing}"}
    from web_search_neo.captcha_solvers import word_shape

    height, width = pixels.shape[:2]
    grid = _published_grid(word_shape, record, width, height)

    def score(word: str, band: tuple[float, float]):
        if grid is not None:
            return word_shape.score_cells(pixels, word, grid, hue=band, sat_min=0.45)
        return word_shape.match_word_shape(pixels, word, hue=band, sat_min=0.45)

    first_ranked: list[list[Any]] = []
    best_band = 0
    for index, band in enumerate(_MEASURE_BANDS):
        ranked = [score(word, band) for word in targets]
        first_ranked.append(ranked[0])
        if ranked[0] and (
            not first_ranked[best_band]
            or ranked[0][0].score > first_ranked[best_band][0].score
        ):
            best_band = index
        decisions = [word_shape.choose_ordered(matches, needed=1) for matches in ranked]
        if all(decision.get("sure") for decision in decisions):
            boxes = [tuple(decision["boxes"][0]) for decision in decisions]
            if len(set(boxes)) == len(boxes):
                return {
                    "sure": True,
                    "ordered_boxes": [list(box) for box in boxes],
                    "cells": [_cell_of(match) for match in ranked[0]],
                    "picture_size": [width, height],
                    "hue": list(band),
                    "why": (
                        f"{len(targets)} target(s) matched in the "
                        f"{band[0]:.2f}-{band[1]:.2f} colour band; click them in order"
                    ),
                }
    return {
        "sure": False,
        "cells": [_cell_of(match) for match in first_ranked[best_band]],
        "picture_size": [width, height],
        "hue": list(_MEASURE_BANDS[best_band]),
        "why": "no colour band produced a shape match strong enough to trust",
    }


def _widget_rect(box: Any) -> tuple[float, float, float, float] | None:
    """A located widget picture as page CSS pixels, or None when it is not one."""
    if not isinstance(box, dict):
        return None
    try:
        rect = (
            float(box.get("x")), float(box.get("y")),
            float(box.get("width")), float(box.get("height")),
        )
    except (TypeError, ValueError):
        return None
    if any(value != value or value in (float("inf"), float("-inf")) for value in rect):
        return None
    if rect[0] < 0 or rect[1] < 0 or rect[2] < 80 or rect[3] < 40:
        return None
    return rect


def _to_page(
    box: list[float], rect: tuple[float, float, float, float], size: list[Any]
) -> tuple[list[float], dict[str, float]]:
    """An image-space box and its click centre in page CSS pixels."""
    try:
        # The picture is `size` pixels wide shown inside a `rect[2]` CSS-pixel box, so one
        # image pixel is rect/size page pixels - inverting this put clicks far off the widget.
        scale_x = rect[2] / float(size[0])
        scale_y = rect[3] / float(size[1])
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        scale_x = scale_y = 1.0
    x, y, w, h = (float(value) for value in box)
    page_box = [
        round(rect[0] + x * scale_x, 1),
        round(rect[1] + y * scale_y, 1),
        round(w * scale_x, 1),
        round(h * scale_y, 1),
    ]
    centre_x = rect[0] + (x + w / 2.0) * scale_x
    centre_y = rect[1] + (y + h / 2.0) * scale_y
    return page_box, {
        "x": round(min(max(centre_x, rect[0]), rect[0] + rect[2]), 1),
        "y": round(min(max(centre_y, rect[1]), rect[1] + rect[3]), 1),
    }


def _refusal(
    kind: str, instruction: str, why: str, rect: tuple[float, ...] | None = None
) -> dict[str, Any]:
    """The unsure shape: per-cell evidence when there is any, clicks never."""
    return {
        "attempted": True,
        "sure": False,
        "vendor": "tencent",
        "challenge": kind,
        "instruction": instruction,
        "widget_rect": list(rect) if rect else [],
        "why": why,
        "cells": [],
        "clicks": [],
    }


def measure_page_image_select(
    run_script: Callable[[str], dict[str, Any]],
    capture_region: Callable[[tuple[float, float, float, float]], bytes],
    *,
    found: Any = None,
    slider: Any = None,
    status: Any = None,
) -> dict[str, Any]:
    """Measure a Tencent tile challenge from the live page. Nothing is clicked.

    `run_script` runs one page script and returns its answer dict; `capture_region`
    captures one page-CSS-pixel rect as PNG bytes. Detection reuses the classifier
    the reporting path already runs; anything but a Tencent image_select,
    word_click or icon_click is not attempted here (a slider belongs to the drag).
    Every failure below is a reported refusal with no clicks - this runs inside
    the captcha solve path, where raising would break the wait that follows.
    """
    try:
        answer = run_script(classify.CLASSIFY_FACTS_SCRIPT) or {}
    except Exception as stopped:  # noqa: BLE001 - a dead tab reports, never raises
        return _refusal("unknown", "", f"the page could not be read: {stopped}")
    facts = answer.get("value") if isinstance(answer, dict) else None
    named = classify.classify_from_harness(
        found=found, slider=slider, status=status, facts=facts if isinstance(facts, dict) else {}
    )
    kind = str(named.get("challenge") or "unknown")
    if named.get("vendor") != "tencent" or kind not in IMAGE_SELECT_KINDS:
        return {"attempted": False, "vendor": named.get("vendor"), "challenge": kind}
    try:
        located = run_script(IMAGE_LOCATE_SCRIPT) or {}
    except Exception as stopped:  # noqa: BLE001 - reported, see above
        return _refusal(kind, "", f"the widget picture could not be located: {stopped}")
    box = located.get("value") if isinstance(located, dict) else None
    rect = _widget_rect(box)
    instruction = str((box.get("instruction") if isinstance(box, dict) else "") or "")
    if rect is None:
        return _refusal(kind, instruction, "no widget picture big enough to be the puzzle was found on the page")
    try:
        picture = capture_region(rect)
    except Exception as stopped:  # noqa: BLE001 - reported, see above
        return _refusal(kind, instruction, f"the widget picture could not be captured: {stopped}", rect)
    if not picture:
        return _refusal(kind, instruction, "the widget picture came back empty", rect)
    record = {
        "vendor": "tencent",
        "challenge": kind,
        "instruction": instruction,
        "targets": tc_protocol.target_words(instruction),
        "image_size": [],
        "click_cfg": {},
    }
    measured = image_select_from_page(record, bytes(picture))
    size = measured.get("picture_size") or []
    if measured.get("sure"):
        ordered: list[list[float]] = []
        clicks: list[dict[str, float]] = []
        for item in measured["ordered_boxes"]:
            page_box, centre = _to_page(item, rect, size)
            ordered.append(page_box)
            clicks.append(centre)
        return {
            "attempted": True,
            "sure": True,
            "vendor": "tencent",
            "challenge": kind,
            "instruction": instruction,
            "widget_rect": list(rect),
            "picture_size": size,
            "ordered_boxes": ordered,
            "clicks": clicks,
            "next": "click each box in order, then the widget's confirm button; nothing was clicked or verified",
        }
    unsure = _refusal(kind, instruction, measured.get("why") or "the tiles could not be measured", rect)
    unsure["cells"] = measured.get("cells") or []
    unsure["picture_size"] = size
    unsure["hue"] = measured.get("hue")
    return unsure
