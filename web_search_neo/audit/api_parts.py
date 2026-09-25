"""Чистые примитивы api_report: маскировка URL, каналы вызовов, карта эндпоинтов, JWT-факты.

Всё, что нужно и чистым проверкам (api_checks), и оболочке (api). Построение
отчёта не шлёт запросов и не трогает браузер — только строки журнала, cookie-джар
и снимок хранилища на вход. Значения cookie и токенов в возвращаемые формы не
копируются: только имена, флаги и формат.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web_search_neo.audit import sites
from web_search_neo.fetch.safety import REDACTED, _SENSITIVE_PARAM_PARTS, redact_url

# Сколько вызовов и доказательств уезжает в ответ: карта — топ по частоте,
# списки в evidence — с честным omitted через clip_list, никогда молча.
ENDPOINT_LIMIT = 60
EVIDENCE_LIMIT = 6

CHANNEL_BY_TYPE = {"xhr": "xhr", "fetch": "fetch", "websocket": "websocket",
                   "eventsource": "sse", "ping": "beacon"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_TOKEN_NAME = re.compile(r"token|jwt|auth|session|refresh|credential|api[-_]?key|secret|bearer|sid", re.I)
_CSRF_NAME = re.compile(r"csrf|xsrf|authenticity_token|_token|verificationtoken|requesttoken", re.I)
_EMAIL = re.compile(r"[^\s@<>'\"]+@[^\s@<>'\"]+\.[A-Za-z]{2,}")
_JWT_IN_TEXT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
# Имена параметров, которых нет в общем списке fetch.safety, но которые spec
# называет прямо: e-mail и идентификатор сессии.
_EXTRA_SENSITIVE = {"sid", "sessionid", "session_id", "phpsessid", "jsessionid", "email", "mail"}

_NUMERIC = re.compile(r"^\d+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^[0-9a-fA-F]{16,}$")

# Что не должно просочиться в тело ошибки в ответе: стек-трейсы, пути сервера,
# версии фреймворков — по тексту, который страница уже получила.
_STACK = re.compile(r'Traceback \(most recent call last\)|File "[^"]+", line \d+'
                    r"|\bat \S+ \([^()\n]+:\d+:\d+\)|in [^\n]+\.php on line \d+|stack ?trace",
                    re.IGNORECASE)
_SERVER_PATH = re.compile(r"(?:^|[\s\"'\x60(=])/(?:usr|var|home|app|opt|srv|root|etc|var/www|workspace|site-packages)/"
                          r"|[A-Za-z]:\\|file:///", re.IGNORECASE | re.MULTILINE)
_FRAMEWORK_VERSION = re.compile(
    r"Django[ /]v?\d|Werkzeug[/ ]\d|gunicorn/\d|PHP/[\d.]+|ASP\.NET|Microsoft-IIS|Apache Tomcat|Tomcat/[\d.]+"
    r"|Spring Framework|nginx/\d|Apache/\d|Node\.js v\d|Python/[\d.]+|Kestrel|Jetty/[\d.]+", re.IGNORECASE)


def safe_url(url: str) -> str:
    """URL для отчёта: без userinfo и фрагмента, чувствительные значения query замаскированы."""
    text = redact_url(str(url))
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.query:
        return text
    pairs = [(name, REDACTED if (_value_is_secret(value) or _name_is_sensitive(name)) else value)
               for name, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, safe=REDACTED), ""))


def mask_text(text: str) -> str:
    """Свободный текст (тело ошибки): e-mail и JWT-похожие значения вымараны."""
    return _JWT_IN_TEXT.sub(REDACTED, _EMAIL.sub(REDACTED, str(text)))


def _value_is_secret(value: str) -> bool:
    """Значение похоже на e-mail или на JWT — маскируется, даже если имя параметра невинное."""
    return bool(value) and (bool(_EMAIL.fullmatch(value)) or bool(_JWT_IN_TEXT.match(value)))


def _name_is_sensitive(name: str) -> bool:
    lowered = str(name).lower()
    return any(part in lowered for part in _SENSITIVE_PARAM_PARTS) or lowered in _EXTRA_SENSITIVE


def url_origin(url: str) -> str:
    """scheme://host[:port] запроса — из чего складывается карта вызовов."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.rsplit('@', 1)[-1]}"


def channel(row: dict[str, Any]) -> str | None:
    """Канал вызова по строке журнала; None — это не API-вызов (страница, картинка, скрипт)."""
    url = str(row.get("url") or "")
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme in {"ws", "wss"}:
        return "websocket"
    if "event-stream" in str(row.get("mime") or "").lower():
        return "sse"
    return CHANNEL_BY_TYPE.get(str(row.get("type") or "").strip().lower())


def path_template(url: str) -> str:
    """Путь без query с id-сегментами под шаблон: /api/users/123 -> /api/users/{id}."""
    try:
        path = urlsplit(str(url)).path or "/"
    except ValueError:
        return "/"
    out: list[str] = []
    for segment in path.split("/"):
        if _NUMERIC.match(segment) or _UUID.match(segment) or _HEX.match(segment):
            if not out or out[-1] != "{id}":
                out.append("{id}")
        else:
            out.append(segment)
    return "/".join(out) or "/"


def endpoint_map(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Карта вызовов: (метод, шаблон пути) -> частота, origin'ы и каналы."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        chan = channel(row)
        if chan is None:
            continue
        url = str(row.get("url") or "")
        key = (str(row.get("method") or "GET").upper(), path_template(url))
        group = groups.setdefault(key, {"method": key[0], "path": key[1], "count": 0,
                                        "origins": {}, "channels": {}})
        group["count"] += 1
        origin = url_origin(url)
        if origin:
            group["origins"][origin] = group["origins"].get(origin, 0) + 1
        group["channels"][chan] = group["channels"].get(chan, 0) + 1
    return sorted(groups.values(), key=lambda g: (-g["count"], g["path"], g["method"]))


def own_sites(page_url: str, hosts: list[str] | None = None) -> set[str]:
    """Регистрируемые домены «своего» сайта: страница плюс именованные хосты (как в scope)."""
    own = {sites.site_of(sites.host_of(page_url))}
    for entry in hosts or []:
        text = str(entry)
        if "://" not in text:
            text = "http://" + text
        host = sites.host_of(text)
        if host:
            own.add(sites.site_of(host))
    own.discard("")
    return own


def is_own(url: str, own: set[str]) -> bool:
    host = sites.host_of(url)
    return bool(host) and sites.site_of(host) in own


def _status(row: dict[str, Any]) -> int:
    try:
        return int(row.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def _is_api_response(row: dict[str, Any]) -> bool:
    """Ответ на API-вызов своего сайта — включая CORS-preflight (OPTIONS)."""
    return row.get("status") is not None and (channel(row) is not None
                                              or str(row.get("method") or "").upper() == "OPTIONS")


def _b64json(segment: str) -> Any:
    try:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None


def _epoch(payload: dict[str, Any], key: str) -> int | None:
    raw = payload.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def jwt_facts(value: Any) -> dict[str, Any] | None:
    """Факты JWT-подобного значения: alg, exp, iat, есть ли подпись. Само значение не возвращается."""
    parts = str(value or "").split(".")
    if len(parts) != 3 or not parts[0].startswith("ey"):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9_-]*", part) for part in parts):
        return None
    header = _b64json(parts[0])
    payload = _b64json(parts[1])
    header = header if isinstance(header, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    alg = header.get("alg")
    return {"alg": alg if isinstance(alg, str) else None, "exp": _epoch(payload, "exp"),
            "iat": _epoch(payload, "iat"), "signed": bool(parts[2])}


def cookie_views(jar: list[Any], own: set[str]) -> list[dict[str, Any]]:
    """Свои cookie: имя, домен, флаги и формат значения — но никогда само значение."""
    views: list[dict[str, Any]] = []
    for cookie in jar:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain and sites.site_of(domain) not in own:
            continue
        value = str(cookie.get("value") or "")
        facts = jwt_facts(value)
        view: dict[str, Any] = {
            "name": str(cookie.get("name") or ""), "domain": domain or None,
            "secure": bool(cookie.get("secure")), "httponly": bool(cookie.get("httpOnly")),
            "sameSite": str(cookie.get("sameSite") or "") or None,
            "format": "jwt" if facts else ("empty" if not value else "opaque"),
        }
        if facts:
            view["jwt"] = facts
        views.append(view)
    return views


def _facts_of(value: dict[str, Any]) -> dict[str, Any]:
    """JWT-факты, пришедшие из страницы: только известные поля, ничего лишнего."""
    alg = value.get("alg")
    return {"alg": alg if isinstance(alg, str) else None, "exp": _epoch(value, "exp"),
            "iat": _epoch(value, "iat"), "signed": bool(value.get("signed"))}


def storage_views(storage: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Снимок браузерного хранилища: имена и формат (и факты JWT), без значений."""
    if not isinstance(storage, dict):
        return [], [], None
    error = storage.get("storage_error") or storage.get("error")
    error = str(error)[:200] if error else None

    def _entries(key: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        raw = storage.get(key)
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            view: dict[str, Any] = {"name": str(entry["name"]),
                                    "format": str(entry.get("format") or "opaque")}
            if isinstance(entry.get("jwt"), dict):
                view["jwt"] = _facts_of(entry["jwt"])
            out.append(view)
        return out

    return _entries("local_storage"), _entries("session_storage"), error


def _token_evidence(views: list[dict[str, Any]], where: str) -> list[dict[str, Any]]:
    out = []
    for entry in views:
        if entry["format"] == "jwt" or _TOKEN_NAME.search(entry["name"]):
            item: dict[str, Any] = {"where": f"{where} {entry['name']}", "format": entry["format"]}
            if "jwt" in entry:
                item["jwt"] = entry["jwt"]
            out.append(item)
    return out


