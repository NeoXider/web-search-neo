"""Decode a fetched body the way a browser would, not the way HTTP defaults say.

``requests`` falls back to ISO-8859-1 for any ``text/*`` response whose
Content-Type carries no charset - what ``python -m http.server``, many nginx
configs and S3 send - so a UTF-8 Cyrillic page came back as mojibake and the
page's own ``<meta charset>`` was ignored. The order here is the browser's
(WHATWG "decode"): a byte-order mark first - it wins over everything and is
removed from the text - then the header's charset, then a ``<meta>``
declaration in the first kilobytes of HTML (where ``utf-16``/``utf-16le``/
``utf-16be`` mean UTF-8: a document that could declare it in ASCII is not
UTF-16; ``iso-8859-1``/``latin1``/``ascii`` mean windows-1252, as in every browser), then valid UTF-8, then charset detection (charset-normalizer, which
``requests`` already depends on - no new dependency).
"""
from __future__ import annotations

import codecs
import re
from typing import Any

_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)
_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
         (codecs.BOM_UTF16_BE, "utf-16-be"))
_META_UTF16 = {"utf-16", "utf-16-le", "utf-16-be", "utf_16", "utf_16_le", "utf_16_be"}


def _header_charset(response: Any) -> str | None:
    headers = getattr(response, "headers", None) or {}
    try:
        content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")
    except AttributeError:
        return None
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", content_type, re.IGNORECASE)
    return match.group(1) if match else None


def _usable(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


# WHATWG Encoding: these labels all mean windows-1252 (bytes 0x80-0x9F are the
# curly quotes and dashes pages actually contain, not control characters).
_WINDOWS_1252_LABELS = {"latin-1", "iso8859-1", "ascii", "cp819"}


def _whatwg_label(name: str | None) -> str | None:
    if name and (name in _WINDOWS_1252_LABELS or name.startswith("latin")):
        return "cp1252"
    return name


def decode_response(response: Any) -> tuple[str, str]:
    """``(text, charset_used)`` for a response; never raises on odd bytes."""
    raw = getattr(response, "content", None)
    if not isinstance(raw, (bytes, bytearray)):
        text = getattr(response, "text", "") or ""
        return str(text), str(getattr(response, "encoding", None) or "unknown")
    raw = bytes(raw)
    for bom, name in _BOMS:
        if raw.startswith(bom):
            return raw[len(bom):].decode(name, errors="replace"), name
    declared = _whatwg_label(_usable(_header_charset(response)))
    if declared:
        return raw.decode(declared, errors="replace"), declared
    meta = _META_CHARSET.search(raw[:8192])
    from_meta = _whatwg_label(_usable(meta.group(1).decode("ascii", "ignore")) if meta else None)
    if from_meta in _META_UTF16:
        from_meta = "utf-8"
    if from_meta:
        return raw.decode(from_meta, errors="replace"), from_meta
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    guessed = _usable(getattr(response, "apparent_encoding", None)) or "cp1252"
    return raw.decode(guessed, errors="replace"), guessed
