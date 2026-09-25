"""MCP wrappers for the site checks: security_report, api_report, perf_report, har_export, test_run.

Like ``extra_actions``, these live outside the size-ratcheted facade and are
registered on the same legacy FastMCP instance main's action table resolves
schemas on. The analysis itself is pure and lives in ``web_search_neo/audit``;
this module only opens and closes sessions, reads the browser and dispatches.

``test_run`` replays ordinary actions, so it needs main's dispatcher: main hands
its own module over in ``register`` (``bind``), which also works when main runs
as ``__main__``. Every browser read happens off the event loop.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
import time
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

from web_search_neo import __version__, browser_tools, macros, network_log
from web_search_neo.audit import active, api, api_parts, diff, har, multipage, page as page_checks
from web_search_neo.audit import perf, report, sarif, scenario, secrets as secret_checks, site
from web_search_neo.audit import scope as scope_mod
from web_search_neo.audit import transport as transport_mod
from web_search_neo.fetch.safety import resolve_save_path, write_download
from web_search_neo.perception import min_summary

_FACADE: Any = None
INLINE_HAR_CHARS = 100_000
# Set while a test_run executes: a step (or a macro it runs) cannot start another one.
_IN_TEST_RUN: contextvars.ContextVar[bool] = contextvars.ContextVar("wsn_in_test_run", default=False)


def bind(facade: Any) -> None:
    """Remember the facade module whose dispatcher test_run replays steps through."""
    global _FACADE
    _FACADE = facade


def _facade() -> Any:
    if _FACADE is None:
        raise RuntimeError("test_run needs the MCP facade; import web_search_neo.main first")
    return _FACADE


def _session_exists(session_id: str) -> bool:
    with browser_tools._sessions_lock:
        return session_id in browser_tools._sessions


def _open_isolated(url: str, session_id: str, timeout: float) -> dict[str, Any]:
    return browser_tools.open_page(url, session_id=session_id, width=1366, height=900,
                                   timeout_seconds=timeout, headless=True, profile_mode="isolated",
                                   label_tab=False)


def _script_value(session_id: str, source: str, args: list[Any] | None, timeout: float) -> Any:
    answer = browser_tools.execute_js(source, args=args, session_id=session_id,
                                      timeout_seconds=timeout, max_chars=5_000_000)
    if not answer.get("success"):
        raise RuntimeError(str(answer.get("error") or "the page script failed"))
    return answer.get("value")


def _browser_view(url: str, session_id: str, timeout: float, keep_open: bool) -> dict[str, Any]:
    """Load the page once in an isolated browser and read DOM facts, cookies and requests.

    The session stays open only when keep_open was asked for and everything
    worked; after a failure the caller gets no session_id, so it is closed.
    """
    failed = True
    try:
        _open_isolated(url, session_id, timeout)
        snapshot = _script_value(session_id, page_checks.SNAPSHOT_SCRIPT, None, 15.0)
        jar = browser_tools.cookies(op="get", session_id=session_id, limit=1000).get("cookies") or []
        session = browser_tools._get_session(session_id)
        with session.lock:
            rows, _dropped, pending = network_log.drain(session)
        failed = False
        return {"snapshot": snapshot, "cookies": jar, "network": rows + pending}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if failed or not keep_open:
            browser_tools.close_session(session_id)


async def browser_security_report(
    url: str | None = None,
    scope: Literal["page", "site", "hosts"] = "page",
    hosts: list[str] | None = None,
    paths: list[str] | None = None,
    include_subdomains: bool = False,
    max_pages: int = 10,
    max_depth: int = 2,
    delay_ms: int = 500,
    respect_robots: bool = True,
    session_id: str | None = None,
    browser: bool = True,
    keep_open: bool = False,
    timeout_seconds: float = 20.0,
    save_to: str | None = None,
    sarif_to: str | None = None,
    baseline: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Grade your site's security configuration with Mozilla HTTP Observatory's tests, a fix per finding.

    scope='page' (default) checks url; 'site' crawls links and sitemap.xml of
    your hosts (max_pages <= 50, delay_ms, robots.txt); 'hosts' checks each named
    origin (<= 10). paths are your own routes to check too. Requests go only to
    hosts in scope and are all listed in requests_made. save_to writes the full
    report as JSON (download folder).
    """
    sid = session_id or f"security-report-{secrets.token_hex(3)}"
    if browser and scope == "page" and _session_exists(sid):
        raise ValueError(f"security_report opens its own isolated session; '{sid}' is already open - "
                         "close it or pass another session_id.")

    def page_report(target: str, http_view: dict[str, Any]) -> dict[str, Any]:
        view = _browser_view(target, sid, timeout, keep_open) if browser and not http_view.get("error") else None
        result = report.build(target, http_view, view)
        if view is not None and keep_open and not view.get("error"):
            result["session_id"] = sid
        return result

    timeout = max(1.0, min(float(timeout_seconds), 120.0))

    def run() -> dict[str, Any]:
        result = site.run(url, mode=scope, hosts=hosts, paths=paths, include_subdomains=include_subdomains,
                          max_pages=max_pages, max_depth=max_depth, delay_ms=delay_ms,
                          respect_robots=respect_robots, timeout=timeout,
                          page_report=page_report if scope == "page" else None)
        return _write_outputs(result, save_to, sarif_to, baseline, bool(overwrite))
    return await asyncio.to_thread(run)


async def browser_perf_report(
    url: str | None = None,
    session_id: str | None = None,
    wait_seconds: float = 5.0,
    keep_open: bool = False,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Load metrics from the Performance API: TTFB, FCP, LCP, CLS, resources, render-blocking.

    With url and no open session_id: a cold load in a fresh isolated browser,
    closed afterwards unless keep_open. With session_id alone: the page already
    open there. wait_seconds bounds the wait for the load event.
    """
    if url is None and not session_id:
        raise ValueError("perf_report needs url (a fresh isolated load) or the session_id of an open page")
    sid = session_id or f"perf-report-{secrets.token_hex(3)}"

    def run() -> dict[str, Any]:
        opened = False
        timeout = max(1.0, min(float(timeout_seconds), 120.0))
        if url is not None:
            with browser_tools._sessions_lock:
                live = browser_tools._sessions.get(sid)
            if live is not None:  # the open session's own browser and profile, warm cache
                browser_tools.open_page(url, session_id=sid, timeout_seconds=timeout,
                                        profile_mode=live.profile_mode, profile_id=live.profile_id,
                                        debugger_address=live.debugger_address)
            else:
                _open_isolated(url, sid, timeout)
                opened = True
        failed = True
        try:
            wait = max(0.0, min(float(wait_seconds), 30.0))
            raw = _script_value(sid, perf.PERF_SCRIPT, [int(wait * 1000)], wait + 15.0)
            shaped = perf.shape(raw if isinstance(raw, dict) else {})
            failed = False
            return {"success": True, "session_id": sid if not opened or keep_open else None,
                    "fresh_isolated_load": opened, **shaped}
        finally:
            if opened and (failed or not keep_open):  # a failed run returns no session_id to close later
                browser_tools.close_session(sid)
    return await asyncio.to_thread(run)


# api_report: сколько тел ответов с ошибками читаем и на сколько символов.
# Chrome держит их в памяти — чтение не является новым запросом.
API_ERROR_BODIES = 8
API_ERROR_BODY_CHARS = 4_000


def _api_error_bodies(session: Any, session_id: str, rows: list[dict[str, Any]], page_url: str,
                      hosts: list[str] | None) -> tuple[dict[str, str], int]:
    """Тела ответов 4xx/5xx своих API-вызовов из памяти Chrome (замок под session.lock)."""
    own = api.own_sites(page_url, hosts)
    bodies: dict[str, str] = {}
    unread = 0
    wanted = [row for row in rows
              if api._status(row) >= 400 and api.channel(row) and row.get("id")
              and api.is_own(str(row.get("url") or ""), own)]
    for row in wanted[:API_ERROR_BODIES]:
        answer = network_log.read_body(session.driver, str(row["id"]), session_id, API_ERROR_BODY_CHARS,
                                       lambda exc: f"{type(exc).__name__}: {exc}")
        if answer.get("success"):
            bodies[str(row["id"])] = str(answer.get("body") or "")
        else:
            unread += 1
    return bodies, unread


async def browser_api_report(
    url: str | None = None,
    session_id: str | None = None,
    hosts: list[str] | None = None,
    keep_open: bool = False,
    wait_seconds: float = 2.0,
    timeout_seconds: float = 20.0,
    save_to: str | None = None,
    har_to: str | None = None,
    sarif_to: str | None = None,
    baseline: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Карта вызовов страницы к бэкенду: CORS, кэш, Content-Type, ошибки, транспорт, токены, CSRF.

    Пассивно: читается только журнал сети страницы (трафик, который страница
    сама сделала, пока пользователь или сценарий test_run работали с ней),
    cookie-джар, localStorage/sessionStorage (имена и формат; значения токенов
    и cookie никогда не выводятся) и тела ответов с ошибками, которые Chrome
    уже держит. Своих запросов api_report не шлёт: url грузит страницу в свежей
    изолированной сессии (закрывается, если не keep_open), session_id работает с
    уже открытой страницей, hosts (≤10, те же правила, что у scope) добавляет
    твои хосты в «свои». wait_seconds — пауза после загрузки, чтобы вызовы успели
    долететь в журнал. save_to пишет отчёт JSON, har_to — HAR того же журнала
    (папка загрузок). summary='min' оставляет counts, priority и summary_line.
    """
    if url is None and not session_id:
        raise ValueError("api_report needs url (a fresh isolated load) or the session_id of an open page")
    if hosts:
        scope_mod.Scope.build(hosts)  # отказ до открытия браузера: wildcards/суффиксы/ >10 хостов
    sid = session_id or f"api-report-{secrets.token_hex(3)}"

    def run() -> dict[str, Any]:
        opened = False
        timeout = max(1.0, min(float(timeout_seconds), 120.0))
        if url is not None:
            with browser_tools._sessions_lock:
                live = browser_tools._sessions.get(sid)
            if live is not None:  # открытая сессия: тёплый переход её же браузером
                browser_tools.open_page(url, session_id=sid, timeout_seconds=timeout,
                                        profile_mode=live.profile_mode, profile_id=live.profile_id,
                                        debugger_address=live.debugger_address)
            else:
                _open_isolated(url, sid, timeout)
                opened = True
        failed = True
        try:
            wait = max(0.0, min(float(wait_seconds), 30.0))
            if wait:
                time.sleep(wait)
            try:
                storage = _script_value(sid, api.STORAGE_SCRIPT, None, 15.0)
                if not isinstance(storage, dict):
                    storage = {"storage_error": "the storage script returned no object"}
            except Exception as exc:
                storage = {"storage_error": f"{type(exc).__name__}: {exc}"[:200]}
            jar = browser_tools.cookies(op="get", session_id=sid, limit=1000).get("cookies") or []
            session = browser_tools._get_session(sid)
            with session.lock:
                rows, dropped, pending = network_log.drain(session)
                page_url = network_log.page_url(session)
                bodies, unread = _api_error_bodies(session, sid, rows + pending, page_url, hosts)
            result = api.build(rows + pending, page_url=page_url, jar=jar, storage=storage,
                               bodies=bodies, unread=unread, hosts=hosts, dropped=dropped)
            if har_to:
                document = har.build(rows + pending, page_url=page_url,
                                     title=str(getattr(session, "last_title", "") or ""),
                                     version=__version__, dropped=dropped)
                path = write_download(har_to, json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8"),
                                      overwrite=bool(overwrite))
                result = {**result, "har_saved_to": str(path)}
            result = _write_outputs(result, save_to, sarif_to, baseline, bool(overwrite))
            failed = False
            return {"success": True, "session_id": sid if not opened or keep_open else None,
                    "fresh_isolated_load": opened, **result}
        finally:
            if opened and (failed or not keep_open):  # при ошибке session_id не возвращается — сессия закрыта
                browser_tools.close_session(sid)
    return await asyncio.to_thread(run)

def _write_outputs(result: dict[str, Any], save_to: str | None, sarif_to: str | None,
                   baseline: str | None, overwrite: bool) -> dict[str, Any]:
    """Shared report outputs: JSON file, SARIF for CI, a regression against a saved baseline."""
    if save_to:
        text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
        path = write_download(save_to, text.encode("utf-8"), overwrite=overwrite)
        result = {**result, "saved_to": str(path)}
    if sarif_to:
        path = write_download(sarif_to, sarif.dumps(result, version=__version__), overwrite=overwrite)
        result = {**result, "sarif_saved_to": str(path)}
    if baseline:
        anchor = resolve_save_path(baseline)
        if not anchor.is_file():
            raise ValueError(f"baseline is not a file in the download folder: {baseline!r}")
        try:
            old = json.loads(anchor.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"baseline is not a JSON report: {baseline!r} ({exc})") from exc
        result = {**result, "regression": diff.compare(old, result)}
    return result


async def browser_secret_scan(
    url: str | None = None,
    session_id: str | None = None,
    scope: str = "page",
    hosts: list[str] | None = None,
    paths: list[str] | None = None,
    include_subdomains: bool = False,
    max_pages: int = 10,
    max_depth: int = 2,
    delay_ms: int = 500,
    respect_robots: bool = True,
    keep_open: bool = False,
    wait_seconds: float = 2.0,
    timeout_seconds: float = 20.0,
    save_to: str | None = None,
    sarif_to: str | None = None,
    baseline: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Секреты и эндпоинты в коде: ключи, токены, openapi, sourcemaps, agent-файлы, формы входа.

    scope='page' (default): url в свежей изолированной сессии или session_id
    открытой страницы. 'site': обход ссылок и sitemap от url (только served
    HTML, без браузера). 'hosts': первые страницы именованных origins.
    Везде пассивно: обычные GET в своей области, все в requests_made.
    """
    if scope not in {"page", "site", "hosts"}:
        raise ValueError(f"secret_scan scope must be one of ['page', 'site', 'hosts'], not {scope!r}")
    if scope != "page" and session_id:
        raise ValueError("secret_scan session_id is page mode only: site/hosts read served HTML")
    if url is None and not session_id:
        raise ValueError("secret_scan needs url (a fresh isolated load) or the session_id of an open page")
    if hosts:
        scope_mod.Scope.build(hosts)  # отказ до открытия браузера: wildcards/суффиксы/ >10 хостов
    routes = site.validate_paths(paths) if paths else []
    sid = session_id or f"secret-scan-{secrets.token_hex(3)}"

    def run() -> dict[str, Any]:
        timeout = max(1.0, min(float(timeout_seconds), 120.0))
        if scope != "page":
            own_scope = site._scope_for(url, hosts, include_subdomains, scope)
            checker = report.Checker(own_scope, scope_mod.Budget(), timeout)
            if scope == "site":
                assert url is not None
                crawled, crawl_info = multipage.crawl_pages(
                    url, checker, own_scope, routes, max_pages, max_depth, delay_ms, respect_robots)
                sections = [multipage.secret_page(checker, own_scope, target, hosts, view=view)
                            for target, view in crawled]
                result = multipage.merge("secret", url, scope, sections, checker.budget.made,
                                         {"crawl": crawl_info})
            else:
                sections = []
                for index, target in enumerate(own_scope.targets):
                    start = url if url is not None and index == 0 else target.root("https")
                    view = checker.get(start)
                    if view.get("error") and (url is None or index > 0) \
                            and target.scheme is None \
                            and not transport_mod.is_certificate_error(str(view.get("error"))):
                        start = "http" + start[len("https"):]
                        view = checker.get(start)
                    sections.append(multipage.secret_page(
                        checker, own_scope, start, hosts, view=None if view.get("error") else view))
                    for path in routes if index == 0 else []:
                        stop = urljoin(report.origin_of(start), path)
                        sections.append(multipage.secret_page(checker, own_scope, stop, hosts))
                result = multipage.merge("secret", url or own_scope.targets[0].root("https"),
                                         scope, sections, checker.budget.made)
            return _write_outputs(result, save_to, sarif_to, baseline, bool(overwrite))
        opened = False
        if url is not None:
            with browser_tools._sessions_lock:
                live = browser_tools._sessions.get(sid)
            if live is not None:  # открытая сессия: тёплый переход её же браузером
                browser_tools.open_page(url, session_id=sid, timeout_seconds=timeout,
                                        profile_mode=live.profile_mode, profile_id=live.profile_id,
                                        debugger_address=live.debugger_address)
            else:
                _open_isolated(url, sid, timeout)
                opened = True
        failed = True
        try:
            wait = max(0.0, min(float(wait_seconds), 30.0))
            if wait:
                time.sleep(wait)
            try:
                storage = _script_value(sid, api.STORAGE_SCRIPT, None, 15.0)
                if not isinstance(storage, dict):
                    storage = {"storage_error": "the storage script returned no object"}
            except Exception as exc:
                storage = {"storage_error": f"{type(exc).__name__}: {exc}"[:200]}
            jar = browser_tools.cookies(op="get", session_id=sid, limit=1000).get("cookies") or []
            session = browser_tools._get_session(sid)
            with session.lock:
                rows, dropped, pending = network_log.drain(session)
                page_url = network_log.page_url(session)
            scope_url = url or page_url
            if not scope_url:
                raise ValueError("secret_scan found no page URL: open a page in the session first")
            own_scope = site._scope_for(scope_url, hosts, include_subdomains, "page")
            checker = report.Checker(own_scope, scope_mod.Budget(), timeout)
            journal_urls = [str(row.get("url") or "") for row in rows + pending]
            page_view = checker.get(scope_url)
            sections = [multipage.secret_page(checker, own_scope, scope_url, hosts, jar, storage,
                                              journal_urls, view=None if page_view.get("error")
                                              else page_view)]
            for path in routes:
                stop = urljoin(report.origin_of(scope_url), path)
                sections.append(multipage.secret_page(checker, own_scope, stop, hosts))
            result = multipage.merge("secret", scope_url, scope, sections, checker.budget.made)
            result = {**result, "session_id": sid if not opened or keep_open else None,
                      "fresh_isolated_load": opened, "dropped": int(dropped)}
            failed = False
            return _write_outputs(result, save_to, sarif_to, baseline, bool(overwrite))
        finally:
            if opened and (failed or not keep_open):  # при ошибке session_id не возвращается — сессия закрыта
                browser_tools.close_session(sid)
    return await asyncio.to_thread(run)


async def browser_active_probe(
    url: str | None = None,
    scope: str = "page",
    hosts: list[str] | None = None,
    paths: list[str] | None = None,
    checks: list[str] | None = None,
    origin: str | None = None,
    include_subdomains: bool = False,
    timeout_seconds: float = 20.0,
    save_to: str | None = None,
    sarif_to: str | None = None,
    baseline: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Активные проверки своей площадки: CORS-префлайт, методы, редиректы, отражение.

    Вызов и есть согласие: только OPTIONS, TRACE и обычные GET своих страниц,
    ссылок и одного инертного query-токена ([a-z0-9]+, исполниться нигде не
    может). scope='page' (default) проверяет url; 'hosts' - первые страницы
    именованных origins. Всё в своей области (Scope), в общем бюджете и в
    requests_made. Никаких POST/PUT/DELETE, пейлоадов, авторизации, фаззинга
    и чужих хостов.
    """
    if not url:
        raise ValueError("active_probe needs url (http or https, in scope)")
    if scope not in {"page", "hosts"}:
        raise ValueError(f"active_probe scope must be one of ['page', 'hosts'], not {scope!r}")
    known = ("cors", "methods", "redirects", "canary")
    wanted = list(checks) if checks else list(known)
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise ValueError(f"active_probe checks must be a subset of {list(known)}, not {unknown}")
    wanted = list(dict.fromkeys(wanted))
    probe_origin = origin or active.PROBE_ORIGIN
    parts = urlsplit(probe_origin)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username \
            or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("origin must be a bare https://host origin, e.g. https://probe.example")
    routes = site.validate_paths(paths) if paths else []

    def run() -> dict[str, Any]:
        timeout = max(1.0, min(float(timeout_seconds), 120.0))
        own_scope = site._scope_for(url, hosts, include_subdomains, scope)
        checker = report.Checker(own_scope, scope_mod.Budget(), timeout)
        if scope == "hosts":
            sections = []
            for index, target in enumerate(own_scope.targets):
                root = url if index == 0 else target.root("https")
                view = checker.get(root)
                if view.get("error") and index > 0 and target.scheme is None \
                        and not transport_mod.is_certificate_error(str(view.get("error"))):
                    root = "http" + root[len("https"):]
                    view = checker.get(root)
                if view.get("error"):
                    sections.append({"url": root, "error": str(view.get("error"))[:300]})
                    continue
                sections.append(multipage.active_page(
                    checker, own_scope, root, root, hosts, wanted, probe_origin,
                    routes if index == 0 else [],
                    html=view.get("body") if isinstance(view.get("body"), str) else ""))
            result = multipage.merge("active", url, scope, sections, checker.budget.made)
            return _write_outputs(result, save_to, sarif_to, baseline, bool(overwrite))
        findings: list[dict[str, Any]] = []
        page_view = checker.get(url)
        if page_view.get("error"):
            return {"success": False, "url": url,
                    "error": f"active_probe could not read the page: {page_view.get('error')}",
                    "requests_made": checker.budget.made}
        html = page_view.get("body") if isinstance(page_view.get("body"), str) else ""
        section = multipage.active_page(checker, own_scope, url, url, hosts, wanted,
                                        probe_origin, routes, html=html)
        result = multipage.merge("active", url, scope, [section], checker.budget.made)
        return _write_outputs(result, save_to, sarif_to, baseline, bool(overwrite))
    return await asyncio.to_thread(run)


async def browser_har_export(
    session_id: str = "default",
    save_to: str | None = None,
    overwrite: bool = False,
    third_party_only: bool = False,
    include_pending: bool = True,
    inline: bool = False,
) -> dict[str, Any]:
    """Write the session's network journal as a HAR 1.2 file (download folder).

    inline=true also returns the document when it is under 100000 characters.
    The journal keeps no request headers and no bodies; the HAR comment says so.
    """
    def run() -> dict[str, Any]:
        session = browser_tools._get_session(session_id)
        with session.lock:
            rows, dropped, pending = network_log.drain(session)
            page_url = network_log.page_url(session)
            title = str(getattr(session, "last_title", "") or "")
        selected = rows + (pending if include_pending else [])
        party: dict[str, Any] = {}
        if third_party_only:
            selected, party = network_log.third_party_rows(selected, page_url)
        document = har.build(selected, page_url=page_url, title=title, version=__version__, dropped=dropped)
        text = json.dumps(document, ensure_ascii=False, indent=1)
        name = save_to or f"{session_id}-{time.strftime('%Y%m%d-%H%M%S')}.har"
        path = write_download(name, text.encode("utf-8"), overwrite=bool(overwrite))
        answer: dict[str, Any] = {
            "success": True, "session_id": session_id, "saved_to": str(path), "entries": len(selected),
            "in_flight": sum(1 for row in selected if row.get("done") is False), "dropped": dropped,
            "bytes": len(text.encode("utf-8")), "page_url": page_url, "note": har.LIMITATIONS, **party,
        }
        if inline and len(text) <= INLINE_HAR_CHARS:
            answer["har"] = document
        elif inline:
            answer["inline_refused"] = (f"The HAR is {len(text)} characters (inline limit {INLINE_HAR_CHARS}); "
                                        "read the file at saved_to.")
        return answer
    return await asyncio.to_thread(run)


def _console_seq(session_id: str) -> int:
    try:
        return int(browser_tools.get_console(session_id=session_id, limit=1, order="desc").get("history_seq") or 0)
    except Exception:
        return 0


def _new_console_errors(session_id: str, since_seq: int) -> list[str]:
    answer = browser_tools.get_console(session_id=session_id, levels=["error"], since_seq=since_seq, limit=20)
    return [str(entry.get("text") or entry.get("message") or entry)[:300] for entry in answer.get("entries") or []]


def _new_failed_requests(session_id: str, since_ms: float) -> list[str]:
    answer = browser_tools.get_network(session_id=session_id, only_errors=True, output="json", limit=500)
    return [f"{row.get('method', 'GET')} {row.get('status') or row.get('error') or 'failed'} {row.get('url', '')}"[:300]
            for row in answer.get("requests") or [] if float(row.get("ts") or 0) >= since_ms]


async def _dispatch(action: dict[str, Any]) -> dict[str, Any]:
    outcome = await _facade()._execute_actions([dict(action)], False)
    return outcome["results"][0]


async def _run_check(check: dict[str, Any], session_id: str, marks: dict[str, float]) -> dict[str, Any]:
    if "action" in check:
        result = await _dispatch(check["action"])
        return scenario.judged(check, result.get("success") is True, result.get("error"))
    if check["read"] == "console":
        errors = await asyncio.to_thread(_new_console_errors, session_id, int(marks["console_seq"]))
        return scenario.judged(check, not errors, errors)
    failures = await asyncio.to_thread(_new_failed_requests, session_id, marks["started_ms"])
    return scenario.judged(check, not failures, failures)


async def _run_step(step: dict[str, Any], session_id: str, include_data: bool) -> dict[str, Any]:
    started = time.monotonic()
    marks = {"started_ms": time.time() * 1000 - 50,
             "console_seq": await asyncio.to_thread(_console_seq, session_id) if step["needs_console"] else 0}
    record: dict[str, Any] = {"index": step["index"], "name": step["name"], "action": step["action_name"]}
    checks: list[dict[str, Any]] = []
    action_ok = True
    if step["action"] is not None:
        result = await _dispatch(step["action"])
        action_ok = result.get("success") is True
        checks.append(scenario.action_verdict(step, action_ok, result.get("error")))
        if include_data and "data" in result:
            record["data"] = min_summary.minimize(result["data"])
    if action_ok or step["expect_failure"]:
        for check in scenario.checks_for(step, session_id):
            checks.append(await _run_check(check, session_id, marks))
    record["checks"] = checks
    record["status"] = "passed" if all(c["passed"] for c in checks) else "failed"
    failed = next((c for c in checks if not c["passed"]), None)
    if failed:
        record["error"] = scenario.describe(failed)
    record["duration_ms"] = round((time.monotonic() - started) * 1000)
    return record


async def browser_test_run(
    steps: list[dict[str, Any]],
    session_id: str = "test",
    url: str | None = None,
    stop_on_failure: bool = True,
    screenshot_on_failure: bool = False,
    keep_open: bool = False,
    timeout_seconds: float = 5.0,
    include_data: bool = False,
) -> dict[str, Any]:
    """Run a scenario - actions plus expect checks per step - and report pass/fail per step.

    A step is an ordinary action object with optional "step_name" and "expect"
    ({selector, absent, text, no_text, url_contains, title_contains, script,
    no_console_errors, no_failed_requests, action_fails, timeout_seconds}), or an
    expect-only step; every step is validated before the first runs. url opens
    the session first (isolated when it is new).
    """
    facade = _facade()
    if _IN_TEST_RUN.get():
        raise ValueError("test_run cannot run inside another test_run (directly or through a macro)")
    plan = scenario.plan(steps, session_id, facade, default_timeout=timeout_seconds)
    await asyncio.to_thread(_refuse_nested_macros, plan)
    token = _IN_TEST_RUN.set(True)
    try:
        return await _run_plan(plan, session_id, url, stop_on_failure, screenshot_on_failure, keep_open, include_data)
    finally:
        _IN_TEST_RUN.reset(token)


def _refuse_nested_macros(plan: list[dict[str, Any]]) -> None:
    """A macro step whose saved steps start a test_run is refused before the first step runs.

    A macro that does not exist yet (an earlier step may save it) is left to the
    replay; the runtime guard (``_IN_TEST_RUN``) still refuses the nesting then.
    """
    for step in plan:
        action = step.get("action") or {}
        if step.get("action_name") != "macro" or str(action.get("op") or "").lower() != "run" or not action.get("name"):
            continue
        try:
            record = macros.load(str(action["name"]), action.get("project_root"))
        except (ValueError, OSError):
            continue
        for inner in record.get("steps") or []:
            if str(inner.get("action") or "").strip().lower() in scenario.FORBIDDEN_IN_STEPS:
                raise ValueError(f"step {step['index']}: macro '{record['name']}' runs a test_run, and a test_run "
                                 "cannot run inside another one")


async def _run_plan(plan: list[dict[str, Any]], session_id: str, url: str | None, stop_on_failure: bool,
                    screenshot_on_failure: bool, keep_open: bool, include_data: bool) -> dict[str, Any]:
    started = time.monotonic()
    opened_here = False
    records: list[dict[str, Any]] = []
    if url is not None:
        opened_here = not _session_exists(session_id)
        setup = {"action": "open", "url": url, "session_id": session_id,
                 **({"profile_mode": "isolated"} if opened_here else {})}
        result = await _dispatch(setup)
        if result.get("success") is not True:
            return {"success": False, "error": f"test_run could not open {url}: {result.get('error')}",
                    "session_id": session_id, "passed": 0, "failed": 0, "skipped": len(plan), "total": len(plan)}
    try:
        stopped = False
        for step in plan:
            if stopped:
                records.append({"index": step["index"], "name": step["name"], "action": step["action_name"],
                                "status": "skipped"})
                continue
            record = await _run_step(step, session_id, include_data)
            if record["status"] == "failed" and screenshot_on_failure:
                shot = await _dispatch({"action": "screenshot", "session_id": session_id, "overwrite": True,
                                        "path": f"test-run-{session_id}-step{step['index']}.png"})
                record["screenshot"] = (shot.get("data") or {}).get("path") or shot.get("error")
            records.append(record)
            stopped = record["status"] == "failed" and stop_on_failure
    finally:
        if opened_here and not keep_open:
            await asyncio.to_thread(browser_tools.close_session, session_id)
    return scenario.summarize(records, session_id, round((time.monotonic() - started) * 1000),
                              kept_open=bool(opened_here and keep_open) or not opened_here)


ACTION_SPECS = (
    ("security_report", browser_security_report, "audit", "Passive A-F security grade: page, site or hosts; fixes."),
    ("api_report", browser_api_report, "audit", "Map page-to-backend calls: CORS, cache, tokens, CSRF; fixes."),
    ("secret_scan", browser_secret_scan, "audit", "Secrets, endpoints, openapi in page code."),
    ("active_probe", browser_active_probe, "audit", "Active CORS/methods/redirect/canary probes."),
    ("perf_report", browser_perf_report, "audit", "Load metrics: TTFB, FCP, LCP, CLS, resources, blocking."),
    ("har_export", browser_har_export, "audit", "Save the session's network journal as a HAR file."),
    ("test_run", browser_test_run, "audit", "Run steps with expect checks; pass/fail per step."),
)

__all__ = ["ACTION_SPECS", "bind", "browser_active_probe", "browser_api_report", "browser_har_export",
           "browser_perf_report", "browser_secret_scan", "browser_security_report", "browser_test_run"]
