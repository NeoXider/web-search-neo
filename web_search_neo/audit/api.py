"""Карта вызовов страницы к бэкенду и проверки ответов API: чистый анализ.

api_report не шлёт ни одного запроса. На вход — строки сетевого журнала сессии
(то, что страница сама запросила, пока пользователь или сценарий test_run
работали с ней), cookie-джар, снимок localStorage/sessionStorage (только имена
и формат значений — никогда самих значений) и тексты тел ответов с ошибками,
которые Chrome уже держит в памяти. На выходе — карта вызовов (метод, шаблон
пути, частота, origin'ы, канал xhr/fetch/WebSocket/SSE/beacon), проверки CORS,
кэширования, Content-Type, текстов ошибок, транспорта, хранения токенов, CSRF
и чувствительных параметров в URL, список находок с приоритетом и
рекомендацией — в форме security_report. Значения cookie и токенов в отчёт не
попадают никогда: только имена, флаги и формат; значения в URL маскируются.
"""
from __future__ import annotations

from typing import Any

from web_search_neo.audit import grading
from web_search_neo.audit.findings import info
from web_search_neo.audit.api_checks import (auth_findings, cache_findings, content_findings, cors_findings,
                                             csrf_findings, error_findings, transport_findings, url_findings)
from web_search_neo.audit.api_parts import (ENDPOINT_LIMIT, channel, cookie_views, endpoint_map, is_own,
                                            jwt_facts, own_sites, path_template, safe_url, storage_views,
                                            _is_api_response, _status)
from web_search_neo.fetch.safety import redact_url

SCOPE_TEXT = (
    "api_report is passive and sends no requests of its own: it reads the session's network journal "
    "(the traffic the page itself made while the user or a test_run scenario worked with it), the browser "
    "cookie jar, the page's own localStorage/sessionStorage, and response bodies Chrome already holds for "
    "error responses - never re-requested. url loads the page once in a fresh isolated browser (as "
    "perf_report does), closed unless keep_open, so requests_made is always empty; repeating a request "
    "stays replay_request's explicit job. The analysis covers your own site (the page's registrable "
    "domain plus any hosts you name); third-party origins are classified, never requested. Cookie, "
    "storage and token values are never output - names, flags and formats only - and query values are "
    "masked."
)


STORAGE_SCRIPT = r"""
  const jwt = /^eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*$/;
  const decode = (part) => {
    try {
      const raw = part.replace(/-/g, "+").replace(/_/g, "/");
      return JSON.parse(atob(raw + "=".repeat((4 - raw.length % 4) % 4)));
    } catch (e) { return null; }
  };
  const view = (store) => {
    const out = [];
    for (let i = 0; i < store.length; i += 1) {
      const name = store.key(i);
      let value = "";
      try { value = store.getItem(name) || ""; }
      catch (e) { out.push({name: name, format: "unreadable"}); continue; }
      if (jwt.test(value)) {
        const parts = value.split(".");
        const header = decode(parts[0]) || {};
        const payload = decode(parts[1]) || {};
        out.push({name: name, format: "jwt", jwt: {
          alg: typeof header.alg === "string" ? header.alg : null,
          exp: typeof payload.exp === "number" ? payload.exp : null,
          iat: typeof payload.iat === "number" ? payload.iat : null,
          signed: Boolean(parts[2]),
        }});
      } else if (/^\s*[[{]/.test(value)) {
        out.push({name: name, format: "json"});
      } else {
        out.push({name: name, format: value.length ? "opaque" : "empty"});
      }
    }
    return out;
  };
  const answer = {};
  try { answer.local_storage = view(window.localStorage); }
  catch (e) { answer.storage_error = String(e).slice(0, 200); }
  try { answer.session_storage = view(window.sessionStorage); }
  catch (e) { answer.storage_error = String(e).slice(0, 200); }
  answer.origin = window.location.origin;
  return answer;
"""



def build(rows: list[dict[str, Any]], *, page_url: str, jar: list[Any] | None = None,
          storage: Any = None, bodies: dict[str, str] | None = None, unread: int = 0,
          hosts: list[str] | None = None, dropped: int = 0) -> dict[str, Any]:
    """Итоговый отчёт api_report: карта вызовов, находки с приоритетом и рекомендациями.

    rows — законченные и ещё идущие строки журнала; bodies — тексты тел ответов
    с ошибками, прочитанные из памяти Chrome; значения cookie и хранилища
    сюда попадают только чтобы определить формат, в ответ они не копируются.
    """
    rows = [row for row in rows if isinstance(row, dict)]
    own = own_sites(str(page_url or ""), hosts)
    own_rows = [row for row in rows if is_own(str(row.get("url") or ""), own)]
    responses = [row for row in own_rows if _is_api_response(row)]
    error_rows = [row for row in responses if _status(row) >= 400]
    headersless = [row for row in responses if not isinstance(row.get("headers"), dict)]
    findings: list[dict[str, Any]] = []
    findings += cors_findings(responses)
    findings += cache_findings(responses)
    findings += content_findings(responses)
    findings += error_findings(error_rows, bodies or {}, unread)
    findings += transport_findings(rows, str(page_url or ""))
    cookies = cookie_views(jar or [], own)
    local, session, storage_error = storage_views(storage)
    findings += auth_findings(cookies, local, session, str(page_url or "").startswith("https:"),
                              storage_error)
    findings += csrf_findings(rows, cookies, str(page_url or ""))
    findings += url_findings(rows)
    if headersless:
        findings.append(info(
            "api-headers-unavailable", "headers", "Response headers were not recorded for some responses",
            detail=f"{len(headersless)} of {len(responses)} API responses arrived without recorded "
                   "response headers (a companion session subscribes to network events without them), so "
                   "the CORS, caching and Content-Type checks could not run for them. Run api_report "
                   "against a Selenium or isolated session - those always carry the header set.",
            evidence={"responses_without_headers": len(headersless)}))
    counts = grading.counts(findings)
    order = grading.priority(findings)
    first = "; ".join(item["title"] for item in order[:3]) or "nothing to fix"
    endpoints = endpoint_map(rows)
    report: dict[str, Any] = {
        "success": True, "url": redact_url(str(page_url or "")),
        "requests_observed": len(rows), "api_calls": sum(item["count"] for item in endpoints),
        "dropped": int(dropped),
        "endpoints": endpoints[:ENDPOINT_LIMIT],
        "auth": {"cookies": cookies, "local_storage": local, "session_storage": session,
                 **({"storage_note": storage_error} if storage_error else {})},
        "counts": counts,
        "summary_line": f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low. "
                        f"Fix first: {first}.",
        "priority": order, "findings": findings, "scope": SCOPE_TEXT, "requests_made": [],
    }
    if len(endpoints) > ENDPOINT_LIMIT:
        report["endpoints_omitted"] = len(endpoints) - ENDPOINT_LIMIT
    return report
