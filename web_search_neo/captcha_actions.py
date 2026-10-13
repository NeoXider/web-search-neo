"""Actions that read a captcha challenge over the protocol the widget itself uses.

`captcha_read {session_id}` answers two questions an agent cannot answer from a screenshot:
what kind of challenge this is, and whether it can be measured at all. It sends only the
requests the widget would have sent anyway (its own prehandle and its own picture URL), so it
adds no new surface to the site under test. `captcha_verify` is the deliberate second half:
it spends one answer when a measurement was sure.

Network primitives are injected, per the leaf-module rule; nothing here imports the browser.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any, Callable

from web_search_neo import captcha_aid
from web_search_neo.captcha_solvers import tc_image_solve, tc_protocol

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
    app_id = str(aid or known_aid)
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
    image_bytes = challenge.pop("image_bytes")
    measurement = measure_challenge(challenge, image_bytes)
    return {"success": True, "page_url": entry, "aid": app_id, **challenge, **measurement}


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
    attempts: list[dict[str, Any]] = []
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
