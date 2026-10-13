"""Что api_report читает из ответов бэкенда о самом API: формы ошибок и их смысл.

Ни одного нового запроса. Строки журнала, которые страница уже оставила, и тела
ошибок, которые Chrome уже держит в памяти, — вот и весь вход. Проверка того, что
ошибка *выдаёт лишнее* (стек, пути, версии), уже живёт в api_checks; здесь читается
то, что ошибка *сообщает про API*: какая роль защищает группу путей, есть ли платёжная
стена, какой лимит и в каких заголовках он назван, какой параметр отвергла валидация.

Значения в отчёт не копируются: форма тела (`object(2)`), длина строки, имена полей
и заголовков. Код ошибки и её название цитируются — это публичный словарь API, а не
данные пользователя; всё остальное в тексте сообщения вымарывается.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from web_search_neo.audit.api_parts import EVIDENCE_LIMIT, mask_text, path_template

SHAPE_FIELD_LIMIT = 12

# Что означает код: только то, что подтверждает сам ответ. Пустой список у кода,
# который ничего не сообщает, — не догадка.
CODE_MEANINGS: dict[int, tuple[str, str]] = {
    401: ("authentication", "The endpoint wants a credential the request did not carry."),
    403: ("authorization", "The credential was accepted but does not cover this route or action."),
    404: ("unknown_route", "No route answers here; the path is not published by this API."),
    405: ("method_not_allowed", "The path exists and answers another method."),
    409: ("conflict", "The resource already exists or another write won first."),
    412: ("precondition", "A condition the request must satisfy before the write runs."),
    422: ("validation", "The body or query was understood and rejected on its contents."),
    429: ("rate_limited", "The request was counted against a limit; headers usually name the window."),
    402: ("payment", "The account balance or plan blocks the call, not the credential."),
}

# Сообщения, которые сообщают про скоуп доступа, а не про сломанный запрос.
SCOPE_HINT = re.compile(r"\b(admin|superuser|staff|owner|privileged|internal only|beta)\b", re.I)
BALANCE_HINT = re.compile(r"insufficient balance|balance|credit|quota exceeded|out of credit|top ?up", re.I)
RATE_HINT = re.compile(r"rate limit|too many requests|throttl|retry after", re.I)
VALIDATION_HINT = re.compile(
    r"\b(required|must be|invalid|at least|at most|maximum|minimum|too long|too short|"
    r"unsupported|allowed values|expected)\b", re.I)
# Что отличает отказ ребра/WAF от отказа самого приложения: только наблюдаемое.
# Ребро (Cloudflare, CloudFront, Akamai, Fastly, Sucuri, Varnish, Incapsula)
# отвечает пустым или текстовым телом и подписывается своими заголовками;
# приложение отвечает JSON-конвертом ошибки с полем кода. Тело решает первым:
# JSON-конверт с кодом — это приложение заговорило, даже если поверх стоят
# заголовки CDN. Пустое/текстовое тело с краевыми заголовками — это ребро.
# Без того и другого — unknown: отсутствие доказательств не доказательство.
EDGE_HEADER_NAMES = frozenset({
    "cf-ray", "cf-mitigated", "cf-cache-status", "cf-chl-bypass", "cf-apo-via",
    "cf-chl-outcome", "x-amz-cf-id", "x-amz-cf-pop", "x-cache", "x-varnish",
    "x-fastly-request-id", "x-served-by", "x-akamai-request-id", "akamai-origin-hop",
    "x-sucuri-id", "x-sucuri-cache", "x-firewall", "x-waf", "x-waf-event",
    "x-protected-by", "x-cdn", "x-edge-location", "x-arvan-id", "x-incapsula-id",
    "x-iinfo", "x-cdn-cache-status",
})
EDGE_SERVER_HINT = re.compile(
    r"cloudflare|cloudfront|akamai|fastly|varnish|sucuri|incapsula|imperva|"
    r"edgecast|cachefly|arvan|ddos-guard|stormwall", re.I)
CHALLENGE_HINT = re.compile(
    r"just a moment|attention required|checking your browser|ddos protection|"
    r"cf-challenge|captcha-delivery|incapsula incident", re.I)
# говорит про него (must/is required/was...), либо нижний идентификатор перед
# `=` или `:`. Стоп-слова отсекают прозу сообщения: имена API, а не его текст.
_FIELD_BEFORE_VERB = re.compile(r"\b([A-Za-z][A-Za-z0-9_.\-]{2,40})\s+"
                                r"(?:must|is|are|was|were|required|cannot|should|has|have|exceeds|missing)")
_FIELD_ASSIGNMENT = re.compile(r"([a-zA-Z_][a-zA-Z0-9_.\-]{1,40})(?=\s*[=:])")
_STOPWORDS = {"the", "and", "but", "for", "with", "was", "is", "are", "be", "been", "this", "that",
              "value", "field", "request", "invalid", "required", "parameter", "parameters", "please",
              "not", "one", "any", "all", "must", "may", "can", "only", "given", "provided", "supplied",
              "expected", "allowed", "unsupported", "instead", "should", "cannot", "could", "when",
              "because", "which", "while", "after", "before", "into", "from", "than", "then", "also",
              "does", "did", "has", "have", "had", "its", "your", "their", "there", "here", "use",
              "used", "using", "valid", "invalid", "missing", "empty", "null", "true", "false"}


def shape(value: Any) -> str:
    """Форма значения: `string(18)`, `object(4)`, `list(2)`, `number`, `bool`, `null`."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, complex)):
        return "number"
    if isinstance(value, str):
        return f"string({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    if isinstance(value, dict):
        return f"object({len(value)})"
    return "unknown"


def _message_of(parsed: Any) -> str:
    """Текст ошибки: сообщение самого тела, а при `{"error": {...}}` — и его вложенное поле."""
    if not isinstance(parsed, dict):
        return ""
    parts: list[str] = []
    for name in ("msg", "message", "error", "detail", "reason"):
        value = parsed.get(name)
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, dict):
            for inner in ("msg", "message", "detail", "code"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested:
                    parts.append(nested)
    return " ".join(parts)[:400]


def parse_body(text: Any) -> dict[str, Any]:
    """Что в теле ответа: JSON-структура с формами вместо значений, иначе длина текста."""
    raw = str(text or "")
    if not raw.strip():
        return {"kind": "empty"}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {"kind": "text", "length": len(raw)}
    out: dict[str, Any] = {"kind": "json", "shape": shape(parsed)}
    if isinstance(parsed, dict):
        fields = sorted(parsed)
        out["fields"] = {name: shape(parsed[name]) for name in fields[:SHAPE_FIELD_LIMIT]}
        if len(fields) > SHAPE_FIELD_LIMIT:
            out["fields_omitted"] = len(fields) - SHAPE_FIELD_LIMIT
        code = parsed.get("code") or parsed.get("error_code") or parsed.get("errorCode")
        if isinstance(code, str) and code:
            out["code"] = mask_text(code)[:60]
        elif isinstance(parsed.get("error"), dict) and isinstance(parsed["error"].get("code"), str):
            out["code"] = mask_text(str(parsed["error"]["code"]))[:60]
        message = _message_of(parsed)
        if message:
            out["message"] = mask_text(message)[:160]
    return out


def classify_rejected_by(status: int, body: dict[str, Any], headers: dict[str, Any]) -> str:
    """Кто отказал: ребро/WAF, приложение — или неизвестно.

    Только наблюдаемые факты, без догадок по статусу: JSON-конверт ошибки
    (dict с кодом/сообщением или просто dict — приложение говорит своим
    словарём) — всегда application; пустое/текстовое тело при краевых
    заголовках (cf-ray, сервер cloudflare/cloudfront/...) — edge; всё
    остальное, включая не-ошибки (status < 400 — отказывать было некому), —
    unknown.
    """
    try:
        status = int(status)
    except (TypeError, ValueError):
        return "unknown"
    if status < 400 or not isinstance(body, dict):
        return "unknown"
    if body.get("kind") == "json":
        return "application"
    # A parsed error object is the service speaking its own dictionary; `parse_body` hands
    # back a shape record for a raw string, so a body that carries a code or a message is
    # JSON by content whatever envelope it arrived in.
    if any(isinstance(body.get(key), str) and body.get(key) for key in ("code", "message", "msg")):
        return "application"
    names = {str(name).lower() for name in headers}
    if names & EDGE_HEADER_NAMES:
        return "edge"
    for name in ("server", "via", "x-powered-by", "x-protected-by"):
        if EDGE_SERVER_HINT.search(str(headers.get(name) or headers.get(name.title()) or "")):
            return "edge"
    return "unknown"


def _field_names(message: str) -> list[str]:
    """Имена параметров, названные в сообщении валидации (не значения)."""
    found = [match.group(1) for pattern in (_FIELD_BEFORE_VERB, _FIELD_ASSIGNMENT)
             for match in pattern.finditer(message)]
    return sorted({name for name in found if name.lower() not in _STOPWORDS})[:EVIDENCE_LIMIT]


def error_meaning(status: int, body: dict[str, Any], headers: dict[str, Any]) -> dict[str, Any]:
    """Смысл ошибки из её собственных слов: баланс, скоуп доступа, лимит, валидация."""
    words = " ".join(part for part in (body.get("code"), body.get("message")) if isinstance(part, str))
    lowered = words.lower()
    out: dict[str, Any] = {"status": status, "rejected_by": classify_rejected_by(status, body, headers)}
    if status in CODE_MEANINGS:
        out["class"], out["meaning"] = CODE_MEANINGS[status]
    header_names = sorted(str(name).lower() for name in headers)
    rate_headers = [name for name in header_names
                    if name.startswith(("x-ratelimit", "retry-after", "ratelimit"))]
    scoped = SCOPE_HINT.search(lowered)
    # Порядок решает: `insufficient_balance` с `retry-after` — платёжная стена, а не
    # лимит запросов; названная в сообщении роль важнее валидации тела.
    if bool(BALANCE_HINT.search(lowered)) or status == 402:
        out["hint"] = "balance_or_plan"
        out["hint_note"] = ("The account state blocks the call rather than the credential: this is a "
                            "billing or plan wall, and it names itself in the response.")
    elif scoped:
        out["hint"] = "access_scope"
        out["scope_words"] = sorted({m.group(1).lower() for m in SCOPE_HINT.finditer(words)})[:EVIDENCE_LIMIT]
        out["hint_note"] = ("The response names a role or scope of its own: routes behind it exist and are "
                            "published by this API, one role check deep.")
    elif rate_headers or (status == 429 and RATE_HINT.search(lowered)):
        out["hint"] = "rate_limited"
        out["limit_headers"] = rate_headers
    elif VALIDATION_HINT.search(lowered) or status == 422:
        names = _field_names(words)
        out["hint"] = "validation"
        if names:
            out["named_parameters"] = names
            out["hint_note"] = ("The rejection names the parameter it looked at. That is the API publishing "
                                "its own input contract, and it is worth asking those parameters directly.")
    return out


def error_shapes(rows: list[dict[str, Any]], bodies: dict[str, str]) -> list[dict[str, Any]]:
    """Сводка ошибок по (метод, путь, статус): форма тела, частота, смысл."""
    groups: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        try:
            status = int(row.get("status") or 0)
        except (TypeError, ValueError):
            continue
        if status < 400:
            continue
        url = str(row.get("url") or "")
        key = (str(row.get("method") or "GET").upper(), path_template(url), status)
        group = groups.setdefault(key, {"method": key[0], "path": key[1], "status": status,
                                       "count": 0, "bodies": Counter(), "codes": Counter(),
                                       "messages": Counter(), "meanings": {}, "header_names": set(),
                                       "rejected": Counter()})
        group["count"] += 1
        body = parse_body(bodies.get(str(row.get("id") or "")))
        group["bodies"][body.get("shape") if body.get("kind") == "json" else body.get("kind", "empty")] += 1
        for name in sorted(str(header).lower() for header in (row.get("headers") or {})):
            group["header_names"].add(name)
        if body.get("code"):
            group["codes"][body["code"]] += 1
        if body.get("message"):
            group["messages"][body["message"]] += 1
        headers = row.get("headers") if isinstance(row.get("headers"), dict) else {}
        meaning = error_meaning(status, body, headers)
        rejected = meaning.get("rejected_by", "unknown")
        group["rejected"][rejected] += 1
        hint = meaning.pop("hint", None)
        if hint:
            entry = group["meanings"].setdefault(hint, {"hint": hint, "rejected": Counter()})
            entry["rejected"][rejected] += 1
            for field in ("scope_words", "named_parameters", "limit_headers", "hint_note"):
                if meaning.get(field):
                    entry[field] = meaning[field]
    out: list[dict[str, Any]] = []
    for group in groups.values():
        item: dict[str, Any] = {
            "method": group["method"], "path": group["path"], "status": group["status"],
            "count": group["count"],
            "body_shapes": [{"shape": name, "count": count}
                            for name, count in group["bodies"].most_common(3)],
            "rejected_by": group["rejected"].most_common(1)[0][0],
        }
        if group["header_names"]:
            item["response_headers"] = sorted(group["header_names"])[:EVIDENCE_LIMIT]
        if group["codes"]:
            item["codes"] = [{"code": code, "count": count}
                             for code, count in group["codes"].most_common(EVIDENCE_LIMIT)]
        if group["messages"]:
            item["messages"] = [message for message, _ in group["messages"].most_common(2)]
        meanings = sorted(group["meanings"].values(), key=lambda entry: entry["hint"])
        for entry in meanings:
            rejected_counts = entry.pop("rejected")
            entry["rejected_by"] = rejected_counts.most_common(1)[0][0]
        if meanings:
            item["meaning"] = meanings[0]
            if len(meanings) > 1:
                item["other_hints"] = [entry["hint"] for entry in meanings[1:]]
        out.append(item)
    return sorted(out, key=lambda item: (-item["count"], item["status"], item["method"], item["path"]))
