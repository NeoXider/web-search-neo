"""The ``scope`` of a security report: one page, a crawl of the caller's site, or a list of hosts.

* ``page`` - the URL (plus the caller's ``paths`` on its origin);
* ``site`` - a passive crawl from the URL: links found in served pages and the
  entries of ``sitemap.xml``, only to hosts in scope, at most ``max_pages`` pages
  and ``max_depth`` links deep, ``delay_ms`` between requests, honouring
  ``robots.txt`` unless ``respect_robots`` is false;
* ``hosts`` - every named origin checked in full, a section each.

Nothing here guesses a path: pages come from links, the sitemap and the paths
the caller listed. Every request goes through one ``Budget`` and one ``Scope``,
so ``requests_made`` is the complete list and nothing outside the named hosts
is ever contacted.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from web_search_neo.audit import page as page_checks, report, transport
from web_search_neo.audit.findings import SEVERITY_RANK
from web_search_neo.audit.scope import MAX_PAGES, Budget, Scope, ScopeError, parse_target, redacted, scrub

MODES = ("page", "site", "hosts")
MAX_PATHS = 50
MAX_DEPTH = 5
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def validate_paths(paths: Any) -> list[str]:
    """The caller's own routes: absolute paths only, no scheme, no traversal, at most MAX_PATHS."""
    if paths is None:
        return []
    if not isinstance(paths, list) or len(paths) > MAX_PATHS:
        raise ScopeError(f"paths must be a list of at most {MAX_PATHS} paths")
    out = []
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ScopeError(f"{path!r}: a path starts with '/' and names a route of the checked host")
        if any(part in {"..", "."} for part in urlsplit(path).path.split("/")) or not path.isascii() \
                or any(ch.isspace() for ch in path):
            raise ScopeError(f"{path!r}: no '..', spaces or non-ASCII (percent-encode it)")
        if path not in out:
            out.append(path)
    return out


def _page_report(url: str, view: dict[str, Any]) -> dict[str, Any]:
    return report.build(url, view)


def _section(built: dict[str, Any]) -> dict[str, Any]:
    """A compact per-page (or per-host) view for the aggregate."""
    keep = ("url", "final_url", "status", "mode", "grade", "score", "summary_line", "counts", "not_graded",
            "production_forecast")
    section = {key: built[key] for key in keep if key in built}
    section["priority"] = built.get("priority", [])[:5]
    if not built.get("success", True):
        section["error"] = built.get("error")
    return section


def dedupe(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per problem id, with the pages it appears on (headers are usually shared)."""
    merged: dict[str, dict[str, Any]] = {}
    for built in reports:
        for item in built.get("findings", []):
            if item.get("status") not in {"fail", "warn"}:
                continue
            entry = merged.setdefault(item["id"], {key: item.get(key) for key in (
                "id", "category", "severity", "title", "fix", "points")} | {"pages": []})
            entry["pages"].append(built.get("url"))
    order = sorted(merged.values(), key=lambda e: (SEVERITY_RANK.get(e["severity"], 9), e.get("points") or 0, e["id"]))
    for entry in order:
        entry["page_count"] = len(entry["pages"])
        entry["pages"] = entry["pages"][:10]
    return order


def _aggregate(kind: str, reports: list[dict[str, Any]], budget: Budget, scope: Scope, extra: dict[str, Any]) -> dict[str, Any]:
    graded = [r for r in reports if r.get("success") and isinstance(r.get("score"), int)]
    worst = min(graded, key=lambda r: r["score"]) if graded else None
    out: dict[str, Any] = {
        "success": bool(reports) and any(r.get("success") for r in reports),
        "scope_mode": kind, "scope": scope.describe(),
        "grade": worst["grade"] if worst else None, "score": worst["score"] if worst else None,
        "grade_rule": "The weakest graded page (or host) decides the grade; each one's own grade is in sections.",
        "average_score": round(sum(r["score"] for r in graded) / len(graded)) if graded else None,
        "checked": len(reports), "graded": len(graded),
        "summary_line": (f"{worst['grade']} (weakest: {worst['url']}, {worst['score']}/100) over {len(graded)} "
                         f"graded of {len(reports)} checked" if worst else f"nothing graded of {len(reports)} checked"),
        "priority": dedupe(reports), "sections": [_section(r) for r in reports],
        "requests_made": list(budget.made), "request_budget": {"limit": budget.limit, "used": len(budget.made),
                                                               "refused": budget.refused},
        "reports": reports,
    }
    out.update(extra)
    return scrub(out)


def _robots(view: dict[str, Any], agent: str = "*") -> robotparser.RobotFileParser | None:
    robots = (view.get("files") or {}).get("robots_txt") or {}
    body = robots.get("body") if isinstance(robots, dict) and 200 <= int(robots.get("status") or 0) < 300 else None
    if not body or "<html" in str(body)[:200].lower():
        return None
    parser = robotparser.RobotFileParser()
    parser.parse(str(body).splitlines())
    return parser


def _sitemap_urls(checker: report.Checker, origin: str, view: dict[str, Any],
                  limit: int) -> tuple[list[str], list[str]]:
    """Page URLs from the sitemap(s) robots.txt names in scope (else /sitemap.xml), and those outside the scope."""
    robots = (view.get("files") or {}).get("robots_txt") or {}
    listed = [urljoin(origin + "/", line.split(":", 1)[1].strip()) for line in str(robots.get("body") or "").splitlines()
              if line.lower().startswith("sitemap:")]
    candidates = [u for u in listed if checker.scope.allows(u)] or [origin + "/sitemap.xml"]
    urls: list[str] = []
    outside = [u for u in listed if not checker.scope.allows(u)]
    for sitemap in candidates[:2]:
        answer = checker.get(sitemap, small=True)
        if 200 <= int(answer.get("status") or 0) < 300:
            for loc in _LOC.findall(str(answer.get("body") or "")):
                (urls if checker.scope.allows(loc) else outside).append(loc)
        if len(urls) >= limit:
            break
    return urls[:limit], outside


def _normal(url: str) -> str:
    clean, _ = urldefrag(url)
    parts = urlsplit(clean)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def crawl(start: str, checker: report.Checker, *, max_pages: int, max_depth: int, delay_ms: int,
          respect_robots: bool, paths: list[str], sleep: Callable[[float], None] = time.sleep,
          build: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pages reachable from ``start`` by links and the sitemap, inside the scope; one report each.

    ``build`` turns a fetched page into its entry (default: its security report).
    """
    build = build or _page_report
    origin_view = checker.facts(start)
    robots: dict[str, robotparser.RobotFileParser | None] = {}

    def rules_for(url: str) -> robotparser.RobotFileParser | None:
        """The robots.txt of ``url``'s own origin (read once per origin, in the budget)."""
        key = report.origin_of(url)
        if key not in robots:
            robots[key] = _robots(checker.facts(url)) if respect_robots else None
        return robots[key]

    origin = report.origin_of(start)
    queue: list[tuple[str, int]] = [(_normal(start), 0)] + [(_normal(urljoin(origin, p)), 0) for p in paths]
    from_sitemap, outside = _sitemap_urls(checker, origin, origin_view, max_pages)
    queue += [(_normal(u), 1) for u in from_sitemap]
    seen: set[str] = set()
    reports: list[dict[str, Any]] = []
    skipped: dict[str, Any] = {"robots": [], "out_of_scope": [redacted(u) for u in outside], "depth": 0}
    while queue and len(reports) < max_pages:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not checker.scope.allows(url):
            skipped["out_of_scope"].append(redacted(url))
            continue
        rules = rules_for(url)
        if rules is not None and not rules.can_fetch("*", url):
            skipped["robots"].append(redacted(url))
            continue
        if reports and delay_ms:
            sleep(delay_ms / 1000)
        view = report.collect_http(url, checker.timeout, checker=checker)
        if view.get("page", {}).get("skipped") == "budget":
            skipped["budget"] = (f"The request budget ({checker.budget.limit}) ran out at {redacted(url)}; "
                                 "the rest of the queue was not requested.")
            break
        reports.append(build(url, view))
        if depth >= max_depth:
            skipped["depth"] += 1
            continue
        served = page_checks.snapshot_from_html(str((view.get("page") or {}).get("body") or ""), url)
        for link in (served.get("links") or {}).get("items", []):
            if isinstance(link, str) and link.startswith(("http://", "https://")):
                queue.append((_normal(link), depth + 1))
    skipped["out_of_scope"] = sorted(set(skipped["out_of_scope"]))[:50]
    skipped["robots"] = skipped["robots"][:50]
    return reports, {"not_crawled": skipped, "queue_left": len(queue)}


def _netloc(url: str) -> str:
    """host[:port] of the start URL, without the caller's user:password (never part of a scope)."""
    if not isinstance(url, str) or "://" not in url:
        raise ScopeError(f"url must be an absolute http:// or https:// URL, not {str(url)[:80]!r}")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ScopeError(f"{redacted(url)!r}: only http:// and https:// URLs can be checked")
    try:
        port = parts.port
    except ValueError as exc:
        raise ScopeError(f"{redacted(url)!r}: {exc}") from None
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return host + (f":{port}" if port else "")


def _scope_for(url: str | None, hosts: list[Any] | None, include_subdomains: bool, mode: str) -> Scope:
    entries = list(hosts or [])
    if url is not None:
        start = parse_target(_netloc(url))
        entries = [start.label()] + entries
    elif mode != "hosts":
        raise ScopeError(f"scope '{mode}' needs url")
    scope = Scope.build(entries, include_subdomains=include_subdomains)
    for target in scope.targets:
        blocked = transport.destination_error(target.root("https"))
        if blocked:
            raise ScopeError(f"{target.label()}: {blocked}")
    return scope


def run(url: str | None, *, mode: str = "page", hosts: list[Any] | None = None, paths: list[Any] | None = None,
        include_subdomains: bool = False, max_pages: int = 10, max_depth: int = 2, delay_ms: int = 500,
        respect_robots: bool = True, timeout: float = 20.0, fetch: Callable[..., Any] = transport.fetch,
        certificate: Callable[..., Any] = transport.certificate, sleep: Callable[[float], None] = time.sleep,
        page_report: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Validate the scope, then check it; ``page_report`` builds the start page (the browser path)."""
    if mode not in MODES:
        raise ScopeError(f"scope must be one of {list(MODES)}")
    route_paths = validate_paths(paths)
    scope = _scope_for(url, hosts, include_subdomains, mode)
    budget = Budget()
    checker = report.Checker(scope, budget, timeout, fetch, certificate)
    max_pages = max(1, min(int(max_pages), MAX_PAGES))
    if mode == "site":
        reports, extra = crawl(url, checker, max_pages=max_pages, max_depth=max(0, min(int(max_depth), MAX_DEPTH)),
                               delay_ms=max(0, min(int(delay_ms), 10_000)), respect_robots=respect_robots,
                               paths=route_paths, sleep=sleep)
        return _aggregate("site", reports, budget, scope, extra)
    if mode == "hosts":
        reports = []
        for index, target in enumerate(scope.targets):
            given = url is not None and index == 0
            # paths are the start origin's routes: the first section only
            reports += _one(url if given else target.root("https"), checker, route_paths if index == 0 else [],
                            page_report if given else None, fallback_http=not given and target.scheme is None)
        return _aggregate("hosts", reports, budget, scope, {})
    reports = _one(url, checker, route_paths, page_report)
    if len(reports) == 1:
        return reports[0]
    return _aggregate("page", reports, budget, scope, {})


def _one(url: str, checker: report.Checker, paths: list[str],
         page_report: Callable[[str, dict[str, Any]], dict[str, Any]] | None, *,
         fallback_http: bool = False) -> list[dict[str, Any]]:
    """One origin: its start page (full) and then each listed path (sharing the origin's facts).

    ``fallback_http``: a host named without a scheme is tried over https first and
    over http when https does not answer at all (a local dev server).
    """
    view = report.collect_http(url, checker.timeout, checker=checker)
    page = view.get("page") or {}
    if fallback_http and view.get("error") and not page.get("skipped") \
            and not transport.is_certificate_error(view["error"]):
        url = "http" + url[len("https"):]
        view = report.collect_http(url, checker.timeout, checker=checker)
    first = (page_report or _page_report)(url, view)
    out = [first]
    for path in paths:
        target = urljoin(report.origin_of(url), path)
        path_view = report.collect_http(target, checker.timeout, checker=checker)
        out.append(_page_report(target, path_view))
    return out
