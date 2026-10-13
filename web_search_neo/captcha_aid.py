"""Look up the captcha app id a site publishes, over the same protocol the widget uses.

`captcha_aid {page_url}` reads `GET /web/v1/captcha/aid-encrypted` on the page's own origin -
the address the site's console bundle calls - and reports what came back: the encrypted aid
blob, its encoding, and the plaintext aid when the response carries one. It sends one request
to an address the site itself declares; nothing is guessed.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

AID_PATHS = ("/web/v1/captcha/aid-encrypted", "/captcha/aid-encrypted", "/v1/captcha/aid")

# A console SPA answers every unknown path with its own index.html, so an HTML body means the
# document is not there. The API host behind it is where the widget's bundle actually calls.
API_HOST_MARKS = ("console", "app", "www", "dashboard", "client", "web")


def _api_candidates(origin: str) -> list[str]:
    """Origins that could publish the aid document, page origin first.

    A SPA host answers any path with index.html; an API subdomain is named by changing the
    first label of the host the page already has (console-dev -> api-dev, console -> api),
    never invented out of thin air.
    """
    parts = urllib.parse.urlsplit(origin)
    host = parts.netloc
    candidates = [origin]
    labels = host.split(".")
    if labels[0] in API_HOST_MARKS and len(labels) > 1:
        candidates.append(f"{parts.scheme}://api.{'.'.join(labels[1:])}")
    return candidates


def open_url(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """One plain GET that never raises: a refused or unreachable address is an answer too."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read()
    except (urllib.error.URLError, TimeoutError, OSError) as unreachable:
        # A TLS handshake that dies mid-flight is a fact about the host, not a crash to raise.
        return 0, str(unreachable).encode("utf-8", "replace")[:200]


def captcha_aid(
    page_url: str = "",
    *,
    api_origin: str | None = None,
    opener: Callable[[str, dict[str, str]], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    """Read the captcha aid a site publishes for itself."""
    entry = (page_url or "").strip()
    if not entry:
        raise ValueError("captcha_aid needs a page address; pass page_url")
    parts = urllib.parse.urlsplit(entry)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"captcha_aid wants an https page address, got {entry!r}")
    origin = (api_origin or "").strip() or f"{parts.scheme}://{parts.netloc}"
    runner = opener or open_url
    tried: list[dict[str, Any]] = []
    requests_made: list[str] = []
    for candidate in _api_candidates(origin):
        for path in AID_PATHS:
            url = candidate.rstrip("/") + path
            status, raw = runner(url, {"Referer": entry, "Accept": "application/json"})
            label = f"GET {urllib.parse.urlsplit(candidate).netloc or origin}{path}"
            body = _decode(raw)
            record: dict[str, Any] = {"path": path, "status": status}
            if status == 0:
                record["body_shape"] = f"unreachable: {_decode(raw) or 'no reason given'}"
                requests_made.append(label)
                if len(tried) < 6:
                    tried.append(record)
                continue
            if status == 200 and isinstance(body, dict):
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                record.update(
                    {
                        "aid_encrypted_present": bool(data.get("aid_encrypted")),
                        "encoding": data.get("aid_encrypted_type") or data.get("type"),
                        "aid": _plain_aid(data),
                        "keys": sorted(data.keys())[:12],
                    }
                )
                requests_made.append(label)
                return {
                    "success": True,
                    "origin": candidate,
                    "page_url": entry,
                    "requests_made": requests_made,
                    **record,
                    "note": (
                        "the blob is the site's own encrypted aid; the plaintext aid is present "
                        "only when the site publishes it"
                    ),
                }
            record["body_shape"] = _shape(body)
            requests_made.append(label)
            if len(tried) < 6:
                tried.append(record)
    return {
        "success": False,
        "origin": origin,
        "page_url": entry,
        "requests_made": requests_made,
        "tried": tried,
        "why": "no published aid document answered on this site (an HTML body means the SPA "
        "answered, not an aid document)",
    }


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:200]


def _shape(body: Any) -> str:
    if body is None:
        return "empty"
    if isinstance(body, dict):
        return f"object({len(body)})"
    if isinstance(body, list):
        return f"list({len(body)})"
    return "text"


def _plain_aid(data: dict[str, Any]) -> str | None:
    for key in ("aid", "captcha_app_id", "appid", "app_id"):
        value = data.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None
