"""Чтение api_report из браузера: тела ошибок и опубликованный контракт origin'а.

Вынесено из audit_actions.py без изменения поведения: здесь только то, что
приходится делать рядом с Chrome (прочитать тело ответа из его памяти) и то, что
делает api_report единственные свои запросы (GET опубликованного контракта).
Размер audit_actions.py — ограниченный, а анализ живёт в audit/.

Ничего не угадывается: адреса берутся из объявленных страницей ссылок и из
стандартных адресов документа этого origin; каждый запрос идёт через верифицированную
выдачу web_client и записывается в общий бюджет аудита.
"""

from __future__ import annotations

from typing import Any, Callable

from web_search_neo.audit import api, api_contract

# Сколько тел ответов с ошибками читаем и на сколько символов. Chrome держит их в
# памяти — чтение не является новым запросом.
API_ERROR_BODIES = 8
API_ERROR_BODY_CHARS = 4_000

# Ссылки страницы: из них читается объявленный ею контракт (rel openapi/swagger/mcp).
LINK_SCRIPT = r"""
  const out = [];
  for (const node of document.querySelectorAll('link[href]')) {
    out.push({href: node.getAttribute('href'), rel: node.getAttribute('rel')});
    if (out.length >= 200) break;
  }
  return out;
"""

SPEC_TIMEOUT_SECONDS = 10.0


def error_bodies(read_body: Callable[[str], dict[str, Any]], rows: list[dict[str, Any]],
                 page_url: str, hosts: list[str] | None) -> tuple[dict[str, str], int]:
    """Тела ответов 4xx/5xx своих API-вызовов из памяти Chrome.

    ``read_body(request_id)`` читает тело того запроса, который страница уже сделала
    (audit_actions передаёт это рядом с session.lock); новых запросов не появляется.
    """
    own = api.own_sites(page_url, hosts)
    bodies: dict[str, str] = {}
    unread = 0
    wanted = [row for row in rows
              if api._status(row) >= 400 and api.channel(row) and row.get("id")
              and api.is_own(str(row.get("url") or ""), own)]
    for row in wanted[:API_ERROR_BODIES]:
        answer = read_body(str(row["id"])) or {}
        if answer.get("success"):
            bodies[str(row["id"])] = str(answer.get("body") or "")
        else:
            unread += 1
    return bodies, unread


def read_links(execute_script: Callable[[], Any]) -> list[dict[str, Any]]:
    """`<link rel href>` страницы — объявленные ею документы; ничего не запрашивается."""
    value = execute_script()
    return [item for item in (value if isinstance(value, list) else []) if isinstance(item, dict)]


def spec_fetcher() -> Any:
    """GET опубликованного контракта API: один верифицированный запрос на заявку.

    Только GET, только верифицированная выдача web_client (TLS проверяется,
    public→private redirect отвергается), тело не больше бюджета спецификации.
    Ответ отдаётся как {ok, status, content_type, text}; ошибка — как {ok: False, why}.
    """
    from web_search_neo import web_client

    def fetch(url: str) -> dict[str, Any]:
        try:
            response = web_client.request(url, method="GET", timeout_seconds=SPEC_TIMEOUT_SECONDS,
                                          max_response_bytes=api_contract.BODY_LIMIT)
        except Exception as exc:  # unreachable/TLS/refused redirect: контракт не найден
            return {"ok": False, "status": 0, "why": f"{type(exc).__name__}", "text": ""}
        body = getattr(response, "content", b"") or b""
        return {"ok": 200 <= int(getattr(response, "status_code", 0) or 0) < 400,
                "status": int(getattr(response, "status_code", 0) or 0),
                "content_type": str(getattr(response, "headers", {}).get("content-type") or ""),
                "text": body.decode("utf-8", "replace")}
    return fetch


def spec_budget() -> Any:
    """Общий бюджет аудита для поиска контракта: потолок заявок, не запросов к API."""
    from web_search_neo.audit import scope as scope_mod

    return scope_mod.Budget(limit=api_contract.SPEC_REQUEST_LIMIT)
