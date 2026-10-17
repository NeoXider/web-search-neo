"""Solve a Tencent TCaptcha image/word challenge over the protocol, not in the browser.

The widget's own answers give everything needed: prehandle hands back `sess`, `sid`, the
`pow_cfg` hash to solve and the signed URL of the picture; verify wants the collected mouse
telemetry plus the solved pow. This module keeps the two halves separate on purpose -
`prepare()` reads the challenge and proves it can be measured, `finish()` spends the answer.
That way an unreadable challenge never sends a doomed verify.

Network is injected (`open_url`), so this stays a leaf module with no browser imports.
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Callable

from web_search_neo.captcha_solvers import tc_protocol

UrlOpener = Callable[[str, dict[str, str]], tuple[int, bytes]]


class SolveAborted(Exception):
    """The challenge could not be measured; nothing was verified."""


def prepare(
    open_url: UrlOpener,
    entry_url: str,
    *,
    aid: str = tc_protocol.AID_DEFAULT,
    host: str = "t.captcha.qq.com",
) -> dict:
    """Read one challenge and download its picture. No verify is sent here."""
    challenge = tc_protocol.read_challenge(open_url, entry_url, aid=aid, host=host)
    if not challenge["image_url"]:
        raise SolveAborted("the challenge named no picture to read")
    status, image = open_url(challenge["image_url"], {"Referer": entry_url})
    if status != 200 or len(image) < 64:
        raise SolveAborted(f"the picture came back as HTTP {status} with {len(image)} bytes")
    challenge["image_bytes"] = image
    challenge["pow_answer"] = solve_pow(challenge.get("pow_prefix") or "", challenge.get("pow_md5") or "")
    return challenge


def solve_pow(prefix: str, target_md5: str, *, limit: int = 4_000_000) -> dict:
    """Find the salt whose md5(prefix+salt) matches. The widget solves this in JavaScript."""
    if not prefix or not target_md5:
        return {"solved": False, "why": "no pow_cfg was offered"}
    for counter in range(limit):
        candidate = f"{prefix}{counter}"
        if hashlib.md5(candidate.encode("utf-8")).hexdigest() == target_md5:
            return {"solved": True, "salt": str(counter), "tried": counter + 1}
    return {"solved": False, "why": f"no salt found within {limit} tries"}


def collect_payload(
    boxes: list[tuple[int, int, int, int]],
    *,
    image_size: tuple[int, int],
    started_at: float | None = None,
) -> dict:
    """Turn chosen regions into the click list verify expects, in the order given.

    A click is the centre of a region plus a plausible travel time; the order is preserved
    because the challenge asks for an order.
    """
    width, height = image_size or (0, 0)
    base = started_at if started_at is not None else time.monotonic()
    clicks: list[dict] = []
    for index, (x, y, w, h) in enumerate(boxes):
        centre_x = x + w // 2
        centre_y = y + h // 2
        travel_ms = 180 + 90 * index + int((centre_x + centre_y) / 4)
        clicks.append(
            {
                "x": centre_x,
                "y": centre_y,
                "at_ms": int(base * 1000) + travel_ms,
                "travel_ms": travel_ms,
            }
        )
    return {
        "clicks": clicks,
        "image_size": [width, height],
        "elapsed_ms": int((time.monotonic() - base) * 1000),
    }


def verify_params(challenge: dict, answer: dict, entry_url: str) -> dict[str, str]:
    """The form verify accepts. Only fields the challenge itself supplied are filled."""
    dyn_answer = urllib.parse.quote(_answer_string(answer), safe="")
    return {
        "sess": challenge.get("sess") or "",
        "randstr": challenge.get("randstr") or "",
        "pow_answer": answer.get("pow_answer") or challenge.get("pow_answer") or "",
        "collect": dyn_answer,
        "sid": challenge.get("sid") or "",
        "ec": "1",
        "interval": "0.6",
        "age": "0",
        "lst": "",
        "u": entry_url,
        "referer": entry_url,
    }


def _answer_string(answer: dict) -> str:
    clicks = answer.get("clicks") or []
    joined = ",".join(f"{click['x']}|{click['y']}" for click in clicks)
    return f"dyn_clicks^{joined}" if joined else "dyn_clicks^"


def _post(open_url, url: str, headers: dict, body: bytes):
    """POST when the caller opener can carry a body, None when it cannot."""
    import inspect
    try:
        params = inspect.signature(open_url).parameters
    except (TypeError, ValueError):
        return None
    if len(params) < 3:
        return None
    return open_url(url, headers, body)


def finish(
    open_url: UrlOpener,
    challenge: dict,
    boxes: list[tuple[int, int, int, int]],
    entry_url: str,
    *,
    host: str = "t.captcha.qq.com",
) -> dict:
    """Send the answer and report what Tencent said about it.

    `pow_answer` here is the salt string, not the record solve_pow returned: prepare() keeps
    the record under that key, callers that pass one must flatten it first. A missing or empty
    salt is a refusal to verify, never an attempt with an empty answer.
    """
    salt = challenge.get("pow_answer") or ""
    if not salt:
        return {"ok": False, "why": "no pow answer was solved"}
    answer = collect_payload(boxes, image_size=tuple(challenge.get("image_size") or (0, 0)))
    params = verify_params(challenge, {**answer, "pow_answer": salt}, entry_url)
    form = urllib.parse.urlencode(params)
    url = f"https://{host}{tc_protocol.VERIFY_PATH}"
    # Tencent answers the verify only to a POST form; the same URL asked as a GET is a 404.
    posted = _post(open_url, url, {"Referer": entry_url, "Origin": tc_protocol._origin(entry_url),
                                  "Content-Type": "application/x-www-form-urlencoded"}, form.encode())
    if posted is None:
        status, raw = open_url(f"{url}?{form}", {"Referer": entry_url})
    else:
        status, raw = posted
    if status != 200:
        return {"ok": False, "why": f"verify refused with HTTP {status}"}
    payload = tc_protocol._unwrap_jsonp(raw.decode("utf-8", "replace")) or {}
    # Tencent answers with a flat object (state/ticket/randstr/ec), not a nested "verify" key;
    # accept either shape so a real answer is never mistaken for an empty one.
    verify = payload.get("verify") if isinstance(payload.get("verify"), dict) else payload
    code = str(verify.get("errorCode") or verify.get("error_code")
             or verify.get("state") or verify.get("ec") or "")
    ok = bool(verify.get("ticket")) and code in {"", "0", "1"}
    return {
        "ok": ok,
        "ticket": verify.get("ticket") or "",
        "randstr": verify.get("randstr") or "",
        "code": code,
        "why": verify.get("errMessage") or verify.get("msg") or verify.get("error_msg")
               or ("" if ok else "no ticket returned"),
        "clicks": answer["clicks"],
    }
