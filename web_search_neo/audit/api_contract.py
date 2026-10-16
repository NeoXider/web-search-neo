"""Публичный контракт API против того, чем он пользуется на самом деле.

Дrift — это расхождение между тем, что сайт опубликовал о своём API (OpenAPI/Swagger,
MCP-карточка, `llms.txt`), и путями, которые страница действительно запросила. Оба
списка получены из самого сайта: ни один путь не угадывается и ни один не пробуется
запросом — это ровно то, что разрешает политика этого инструмента.

Зачем это нужно при аудите: путь из спецификации, который страница никогда не зовёт, —
это незанятая дверь (её существование подтверждён документом, а её поведение — уже
вопрос к `replay_request`). Путь из трафика, которого в спецификации нет, — это то, чем
пользуется интерфейс и чего документ не описывает: именно там обычно живут внутренние
эндпоинты. Query-имена сравниваются отдельно: они сообщают, какие параметры API
публикует и какие принимает молча.

Спецификация читается как текст: её содержимое в отчёт не копируется, только пути,
методы и имена параметров.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit

from web_search_neo.audit import sites
from web_search_neo.audit.api_parts import ENDPOINT_LIMIT, path_template
from web_search_neo.fetch.safety import redact_url

# Где сайт обычно публикует контракт. Имена проверяются по факту существования на
# origin'е страницы; ничего не выдумывается: без найденной ссылки drift не сообщается.
SPEC_SUFFIXES = ("openapi.json", "openapi.yaml", "swagger.json", "api-docs", "docs/openapi.json")
_LINK_REL = re.compile(r"(?:openapi|swagger|api[-_]?doc|mcp)", re.I)
_MCP_SUFFIXES = ("mcp", ".well-known/mcp.json", ".well-known/mcp", "llms.txt")
_JSON_CONTENT = re.compile(r"json|yaml|openapi", re.I)
BODY_LIMIT = 400_000
# Сколько GET тратит поиск контракта: только стандартные адреса origin'а и объявленные
# страницей ссылки. Потолок маленький, потому что это проверка наличия документа.
SPEC_REQUEST_LIMIT = 6

# Sitemap опублиchaft paths (вторый опубличашы источник). Cap на display: measured
# openmodel.ai sitemap answered 20 locs; cap stays above that with headroom.
SITEMAP_PATH_LIMIT = 40


def discover_specs(links: Iterable[Any], page_url: str) -> list[dict[str, Any]]:
    """Ссылки страницы, которые объявляют контракт API: rel или имя пути говорят об этом."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links or []:
        if not isinstance(link, dict):
            continue
        href = str(link.get("href") or "")
        rel = str(link.get("rel") or "")
        if not href or href.startswith(("data:", "javascript:")):
            continue
        absolute = urljoin(str(page_url or ""), href)
        if urlsplit(absolute).scheme not in ("http", "https"):
            continue
        declared = bool(_LINK_REL.search(rel)) or bool(_LINK_REL.search(urlsplit(absolute).path))
        if not declared or absolute in seen:
            continue
        seen.add(absolute)
        out.append({"url": absolute, "rel": rel or None,
                    "kind": ("mcp" if "mcp" in (rel + absolute).lower() else "api_spec")})
    return out[:8]


def spec_urls(page_url: str) -> list[str]:
    """Стандартные адреса контракта на origin'е страницы (для проверки по факту, не по догадке)."""
    parts = urlsplit(str(page_url or ""))
    if parts.scheme not in ("http", "https"):
        return []
    root = f"{parts.scheme}://{parts.netloc}"
    base = parts.path.rsplit("/", 1)[0]
    paths = [root + "/" + suffix for suffix in SPEC_SUFFIXES] + \
            [f"{root}{base}/{suffix}" for suffix in SPEC_SUFFIXES] + \
            [root + "/" + suffix for suffix in _MCP_SUFFIXES]
    # /foo//bar/openapi.json (и пустой base) дают двойные слеши: чистим перед запросом.
    return sorted({re.sub(r"(?<!:)//+", "/", path) for path in paths})


def sitemap_paths(xml_text: Any) -> list[str]:
    """Публизь шуя из sitemap.xml (вторый опубличашы источник, weaker than a contract).

    Sitemap опублиchaft paths без спецификации о methods/parameters: это вторый
    верифied source публышы путями (first — openapi/swagger/mcp document). Шуя из
    sitemap только path, нет method, поэтому сравится к трафику по path nom only.
    """
    raw = str(xml_text or "")
    if "<loc>" not in raw.lower() and "<loc " not in raw.lower():
        return []
    found: list[str] = []
    home = None
    for match in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", raw, flags=re.I):
        parts = urlsplit(match.group(1))
        if parts.scheme not in ("http", "https"):
            continue
        # Sitemap публышы путями на ONE origin: первый loc fixat netloc, others skip.
        if home is None:
            home = (parts.scheme, parts.netloc)
        elif (parts.scheme, parts.netloc) != home:
            continue
        found.append(parts.path or "/")
    return sorted(set(found))[:SITEMAP_PATH_LIMIT]


def compare_paths_to_sitemap(observed: dict[Any, Any], published: list[str]) -> list[dict[str, Any]]:
    """Трафику путями, which the published sitemap set does not name."""
    named = set(published)
    out = []
    for (method, path) in observed:
        if path not in named:
            out.append({"method": method, "path": path})
    return sorted(out, key=lambda item: (item["path"], item["method"]))[:SITEMAP_PATH_LIMIT]


def declared_parameters(operations: Any) -> list[str]:
    """Имена параметров операции: её собственные `parameters` и объявленные на пути."""
    names: list[str] = []
    if not isinstance(operations, dict):
        return names
    shared = operations.get("parameters")
    if isinstance(shared, list):
        names += [str(item.get("name")) for item in shared
                  if isinstance(item, dict) and item.get("name")]
    for entry in operations.values():
        if isinstance(entry, dict) and isinstance(entry.get("parameters"), list):
            names += [str(item.get("name")) for item in entry["parameters"]
                      if isinstance(item, dict) and item.get("name")]
    return names


def parse_spec(text: Any, base_url: str = "") -> dict[str, Any]:
    """Что спецификация объявляет: пути с методами и имена query-параметров."""
    raw = str(text or "")
    if not raw.strip():
        return {"ok": False, "why": "empty"}
    if len(raw) > BODY_LIMIT:
        return {"ok": False, "why": f"too_large({len(raw)})"}
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        # YAML/текст: пути читаются по строкам `paths:`-блока без парсера зависимостей.
        return _parse_yaml_paths(raw)
    if not isinstance(document, dict):
        return {"ok": False, "why": "not_an_object"}
    declared = document.get("paths")
    if not isinstance(declared, dict):
        return {"ok": False, "why": "no_paths_key",
                "keys": sorted(str(key) for key in document)[:12]}
    endpoints: list[dict[str, Any]] = []
    for path, operations in declared.items():
        if not isinstance(operations, dict):
            continue
        methods = sorted(name.upper() for name in operations
                         if name.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
        parameters = declared_parameters(operations)
        endpoints.append({"path": path_template(str(path)), "methods": methods,
                          "query_names": sorted(set(parameters))[:ENDPOINT_LIMIT]})
    return {"ok": True, "format": "openapi_json", "title": str(document.get("info", {}).get("title") or "")[:120]
            if isinstance(document.get("info"), dict) else "",
            "servers": [str(item) for item in (document.get("servers") or []) if isinstance(item, (str, dict))][:6],
            "endpoints": endpoints}


def _parse_yaml_paths(raw: str) -> dict[str, Any]:
    """Пути из YAML-спецификации без зависимостей: отступы `paths:` и методы под ними."""
    endpoints: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_paths = False
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip())
        text = stripped.strip()
        if indent == 0:
            in_paths = text.rstrip(":") == "paths"
            current = None
            continue
        if not in_paths:
            continue
        if indent <= 2 and text.endswith(":"):
            current = {"path": path_template(text[:-1].strip()), "methods": [], "query_names": []}
            endpoints.append(current)
        elif indent >= 4 and current is not None:
            name = text.rstrip(":").upper()
            if name in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} and name not in current["methods"]:
                current["methods"].append(name)
    if not endpoints:
        return {"ok": False, "why": "no_paths_found"}
    return {"ok": True, "format": "openapi_yaml", "endpoints": endpoints}


def observed_endpoints(rows: list[dict[str, Any]], own_hosts: set[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Что страница действительно звала на своих origin'ах: (метод, шаблон пути) -> частота."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        url = str(row.get("url") or "")
        if sites.site_of(sites.host_of(url)) not in own_hosts:
            continue
        key = (str(row.get("method") or "GET").upper(), path_template(url))
        entry = out.setdefault(key, {"method": key[0], "path": key[1], "count": 0,
                                    "statuses": set(), "query_names": set()})
        entry["count"] += 1
        try:
            entry["statuses"].add(int(row.get("status") or 0))
        except (TypeError, ValueError):
            pass
        for name in urlsplit(redact_url(url)).query.split("&"):
            field = name.split("=", 1)[0]
            if field:
                entry["query_names"].add(field)
    return out


def _normalize_declared(path: str) -> tuple[str, ...]:
    """Путь спецификации к тому же виду, что путь трафика: `{uid}` и `{id}` — один шаблон."""
    segments = []
    for segment in str(path or "/").split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            segments.append("{id}" if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", segment[1:-1]) else segment)
        else:
            segments.append(segment)
    return tuple(segments)


def compare(specs: list[dict[str, Any]], rows: list[dict[str, Any]], own_hosts: set[str]) -> dict[str, Any]:
    """Сравнение опубликованного с запрошенным; пустой результат — это тоже ответ."""
    declared: dict[tuple[str, str], dict[str, Any]] = {}
    formats: list[str] = []
    for spec in specs:
        if not spec.get("ok"):
            continue
        formats.append(str(spec.get("format") or ""))
        for endpoint in spec.get("endpoints", []):
            methods = endpoint.get("methods") or ["ANY"]
            normalized = "/".join(_normalize_declared(str(endpoint.get("path") or "/")))
            for method in methods:
                key = (str(method).upper(), normalized)
                entry = declared.setdefault(key, {"method": key[0], "path": normalized,
                                                  "query_names": set()})
                entry["query_names"].update(str(name) for name in endpoint.get("query_names", []))
    observed = observed_endpoints(rows, own_hosts)
    declared_keys = set(declared)
    observed_keys = set(observed)
    unpublished = [{"method": key[0], "path": key[1], "count": observed[key]["count"],
                    "statuses": sorted(status for status in observed[key]["statuses"] if status),
                    **({"query_names": sorted(observed[key]["query_names"])[:ENDPOINT_LIMIT]}
                       if observed[key]["query_names"] else {})}
                   for key in sorted(observed_keys - declared_keys)]
    uncalled = [{"method": key[0], "path": key[1],
                 **({"query_names": sorted(declared[key]["query_names"])[:ENDPOINT_LIMIT]}
                    if declared[key]["query_names"] else {})}
                for key in sorted(declared_keys - observed_keys)]
    both = sorted(declared_keys & observed_keys)
    parameter_drift: list[dict[str, Any]] = []
    for key in both:
        accepted = observed[key]["query_names"] - declared[key]["query_names"]
        published = declared[key]["query_names"] - observed[key]["query_names"]
        if accepted or published:
            parameter_drift.append({"method": key[0], "path": key[1],
                                    "accepted_not_published": sorted(accepted)[:ENDPOINT_LIMIT],
                                    "published_not_used": sorted(published)[:ENDPOINT_LIMIT]})
    return {
        "spec_formats": sorted(set(formats)),
        "declared_endpoints": len(declared), "observed_endpoints": len(observed),
        "matched": len(both),
        "unpublished_in_use": unpublished[:ENDPOINT_LIMIT],
        "published_never_called": uncalled[:ENDPOINT_LIMIT],
        **({"unpublished_omitted": max(0, len(unpublished) - ENDPOINT_LIMIT)}
           if len(unpublished) > ENDPOINT_LIMIT else {}),
        **({"uncalled_omitted": max(0, len(uncalled) - ENDPOINT_LIMIT)}
           if len(uncalled) > ENDPOINT_LIMIT else {}),
        "parameter_drift": parameter_drift[:ENDPOINT_LIMIT],
    }


def _sitemap_source(page_url: str, fetch: Callable[[str], dict[str, Any]], budget: Any) -> dict[str, Any]:
    """Read `/sitemap.xml` as the SECOND published-paths source (weaker than a contract).

    Sitemap опублиchaft только путями (no methods, no parameters), поэтому его
    сравится к трафику по path nom only and never calls itself a contract.
    """
    parts = urlsplit(str(page_url or ""))
    if parts.scheme not in ("http", "https"):
        return {"attempted": False}
    url = f"{parts.scheme}://{parts.netloc}/sitemap.xml"
    if budget is not None and not budget.take(f"GET {redact_url(url)}"):
        return {"attempted": False, "why": "request_budget_exhausted"}
    answer = fetch(url)
    status = int(answer.get("status") or 0)
    if not answer.get("ok") or status >= 400:
        return {"attempted": True, "status": status}
    paths = sitemap_paths(answer.get("text"))
    return {"attempted": True, "status": status, **({"paths": paths} if paths else {})}


def collect(page_url: str, rows: list[dict[str, Any]], own_hosts: set[str], links: Iterable[Any],
            fetch: Callable[[str], dict[str, Any]], budget: Any = None) -> dict[str, Any]:
    """Собрать контракт с origin'а страницы и сравнить с трафиком.

    `fetch` — чужая функция GET (одна верифицированная выдача, не этот модуль): она
    обязана вернуть `{"ok", "status", "content_type", "text"}` или `{"ok": False, ...}`.
    `budget` — чужой Budget из audit.scope: каждый запрос записывается в него и
    упирается в общий потолок, поэтому requests_made отчёта остаётся полным.
    """
    found = discover_specs(links, page_url)
    candidates = [{"url": url, "rel": None, "kind": "api_spec"} for url in spec_urls(page_url)]
    known = {item["url"] for item in found}
    candidates += [item for item in found if item["url"] not in known]
    specs: list[dict[str, Any]] = []
    probed: list[dict[str, Any]] = []
    spent = 0
    for candidate in candidates[:12]:
        if budget is not None and not budget.take(f"GET {redact_url(candidate['url'])}"):
            probed.append({"url": candidate["url"], "kind": candidate["kind"],
                           "status": 0, "ok": False, "why": "request_budget_exhausted"})
            continue
        spent += 1
        answer = fetch(candidate["url"])
        status = int(answer.get("status") or 0)
        if not answer.get("ok") or status >= 400:
            continue
        content_type = str(answer.get("content_type") or "")
        text = str(answer.get("text") or "")
        if content_type and not _JSON_CONTENT.search(content_type) and not text.lstrip().startswith(("{", "paths:")):
            continue
        parsed = parse_spec(text, candidate["url"])
        probed.append({"url": candidate["url"], "kind": candidate["kind"],
                       "status": status, "ok": bool(parsed.get("ok")),
                       **({"why": parsed["why"]} if not parsed.get("ok") else {})})
        if parsed.get("ok"):
            specs.append({**parsed, "url": candidate["url"], "kind": candidate["kind"]})
    comparison = compare(specs, rows, own_hosts)
    if not specs:
        sitemap = _sitemap_source(page_url, fetch, budget)
        spent += 1 if sitemap.get("attempted") else 0
        published = sitemap.get("paths") or []
        extra: dict[str, Any] = {}
        if published:
            observed = observed_endpoints(rows, own_hosts)
            extra["published_paths_from_sitemap"] = {
                "paths": published,
                "observed_not_published": compare_paths_to_sitemap(observed, published),
            }
        return {"found": False, "probed": probed[:12], "requests_made": spent,
                "note": ("No published API contract was found on this origin (no openapi/swagger/mcp link or "
                         "standard document answered). Contract drift needs a published specification; without "
                         "one the endpoint map and error meanings in this report are still the truth about "
                         "what was called."),
                **({"spec_candidates_probed": len(probed)} if probed else {}),
                **extra}
    return {"found": True, "requests_made": spent,
            "sources": [{"url": spec["url"], "kind": spec.get("kind"),
                                        **({"title": spec["title"]} if spec.get("title") else {}),
                                        **({"servers": spec["servers"]} if spec.get("servers") else {})}
                                       for spec in specs],
            **comparison}
