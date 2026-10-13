"""Read a Tencent TCaptcha challenge over the protocol the widget itself uses.

Why this exists: a POST to cap_union_prehandle is refused by Tencent (HTTP 403), while the very
same URL as a JSONP GET carrying the page's Referer/Origin answers with the whole session -
sess, sid, the tdc.js path, the pow_cfg hash to solve, the instruction word, and the signed URL
of the picture. Reading the challenge this way needs no browser and no third-party service.

Only stdlib is used here: the caller injects `open_url` so the network primitive stays outside
this module (leaf-module rule).
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Callable

AID_DEFAULT = "196724674"
PREHANDLE_PATH = "/cap_union_prehandle"
GETCAP_PATH = "/cap_union_new_getcapbysig"
VERIFY_PATH = "/cap_union_new_verify"

EDGE_SERVER_HINTS = ("trpc", "tencent http server", "tcaptcha", "qq.com")


class ChallengeReadError(Exception):
    """The challenge could not be read; the reason is the message."""


def prehandle_params(entry_url: str, aid: str) -> dict[str, str]:
    """The query the widget sends. Keeping it a plain dict makes it testable and comparable."""
    return {
        "aid": aid,
        "protocol": "https",
        "incap": "0",
        "json": "1",
        "onload": "captcha_cb",
        "dcid": "0",
        "cs": "1",
        "u": entry_url,
        "referer": entry_url,
        "sess": "",
        "language": "0",
        "support_flash": "2",
        "interval": "0.6",
        "age": "0",
    }


def read_challenge(
    open_url: Callable[[str, dict[str, str]], tuple[int, bytes]],
    entry_url: str,
    *,
    aid: str = AID_DEFAULT,
    host: str = "t.captcha.qq.com",
) -> dict:
    """Fetch and unwrap one challenge. `open_url(url, headers)` -> (status, body bytes)."""
    query = urllib.parse.urlencode(prehandle_params(entry_url, aid))
    url = f"https://{host}{PREHANDLE_PATH}?{query}"
    headers = {
        "Referer": entry_url,
        "Origin": _origin(entry_url),
        "Accept": "*/*",
        "Sec-Fetch-Dest": "script",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    status, raw = open_url(url, headers)
    if status != 200:
        raise ChallengeReadError(f"prehandle refused with HTTP {status}")
    text = raw.decode("utf-8", "replace")
    payload = _unwrap_jsonp(text)
    if payload is None:
        raise ChallengeReadError("prehandle answered but not in the shape the widget expects")
    if payload.get("state") != 1:
        raise ChallengeReadError(f"prehandle state={payload.get('state')}")
    return describe_challenge(payload, host=host)


def _origin(entry_url: str) -> str:
    parts = urllib.parse.urlsplit(entry_url)
    return f"{parts.scheme}://{parts.netloc}"


def _unwrap_jsonp(text: str) -> dict | None:
    """Read the widget's answer: a JSONP callback, or plain JSON when no callback was asked for."""
    start = text.find("(")
    end = text.rfind(")")
    candidates: list[str] = []
    if 0 <= start < end:
        candidates.append(text[start + 1 : end])
    candidates.append(text.strip())
    for chunk in candidates:
        try:
            value = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def describe_challenge(payload: dict, *, host: str = "t.captcha.qq.com") -> dict:
    """Flatten the widget's own answer into the facts a solver needs."""
    inner = payload.get("data") or {}
    dynamic = inner.get("dyn_show_info") or {}
    if isinstance(dynamic, str):
        try:
            dynamic = json.loads(dynamic)
        except json.JSONDecodeError:
            dynamic = {}
    config = inner.get("comm_captcha_cfg") or {}
    background = dynamic.get("bg_elem_cfg") or {}
    pow_cfg = config.get("pow_cfg") or {}
    instruction = str(dynamic.get("instruction") or "")
    return {
        "vendor": "tencent",
        "challenge": challenge_kind(dynamic),
        "state": payload.get("state"),
        "capclass": payload.get("capclass"),
        "subcapclass": payload.get("subcapclass"),
        "sess": payload.get("sess") or "",
        "sid": payload.get("sid") or "",
        "randstr": payload.get("randstr") or "",
        "instruction": instruction,
        "targets": target_words(instruction),
        "show_type": dynamic.get("show_type"),
        "image_url": _absolute(background.get("img_url") or "", host),
        "image_size": background.get("size_2d") or [],
        "click_cfg": background.get("click_cfg") or {},
        "tdc_path": config.get("tdc_path") or "",
        "pow_prefix": pow_cfg.get("prefix") or "",
        "pow_md5": pow_cfg.get("md5") or "",
        "answer_type": ((background.get("click_cfg") or {}).get("data_type") or []),
    }


def challenge_kind(dynamic: dict) -> str:
    """Name the challenge from the widget's own show_type, never from a guess."""
    show = str(dynamic.get("show_type") or "")
    if "slide" in show:
        return "slider"
    if show.startswith("click_word"):
        return "word_click"
    if show.startswith("click_icon"):
        return "icon_click"
    if show.startswith("click_image"):
        return "image_select"
    return show or "unknown"


def target_words(instruction: str) -> list[str]:
    """The words the challenge asks for, in the order it asks for them.

    The instruction is prose around quoted targets: 'Select in order: 敞 啊 掣', '“梨”',
    '"喜鹊"'. Quoted runs are the targets; when nothing is quoted, bare non-ASCII tokens
    are the targets, in reading order.
    """
    if not instruction:
        return []
    quoted = _quoted_runs(instruction)
    if quoted:
        return [word for word in quoted if word]
    tokens = [token for token in _bare_tokens(instruction) if token]
    return tokens


_QUOTES = ("\u201c", "\u201d", '"', "'", "\u2018", "\u2019", "「", "」")


def _quoted_runs(text: str) -> list[str]:
    found: list[str] = []
    buffer: list[str] = []
    closing = set()
    for char in text:
        if char in _QUOTES:
            if buffer:
                found.append("".join(buffer).strip())
                buffer = []
                closing.clear()
            elif char in ("\u201c", '"', "「", "\u2018"):
                closing.add(_matching_quote(char))
            continue
        if closing and char not in closing:
            buffer.append(char)
    if buffer:
        found.append("".join(buffer).strip())
    return found


def _matching_quote(char: str) -> str:
    return {
        "\u201c": "\u201d",
        '"': '"',
        "「": "」",
        "\u2018": "\u2019",
    }.get(char, char)


def _bare_tokens(text: str) -> list[str]:
    out: list[str] = []
    buffer: list[str] = []
    for char in text:
        if ord(char) > 0x2E80 or char.isalnum():
            buffer.append(char)
        elif buffer:
            out.append("".join(buffer))
            buffer = []
    if buffer:
        out.append("".join(buffer))
    return [token for token in out if any(ord(char) > 0x2E80 for char in token)]


def _absolute(path: str, host: str) -> str:
    if path.startswith("http"):
        return path
    return f"https://{host}{path if path.startswith('/') else '/' + path}"
