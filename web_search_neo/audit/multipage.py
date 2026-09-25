"""One page at a time, then merged: scope site/hosts for secret_scan and active_probe.

``page`` mode (with an optional browser load) lives in the wrappers; everything
beyond one page is served HTML plus ordinary GETs through the shared Checker,
so the budget and ``requests_made`` stay complete. No browser here on purpose:
this module never touches it, only the Checker, the snapshots and the pure
analysis - which also keeps it inside the audit dependency allowlist.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from web_search_neo.audit import active, api_parts, grading, page as page_checks, secrets
from web_search_neo.audit.findings import info as info_finding


def fetch_texts(checker: Any, scope: Any, base_url: str, refs: list[str],
                limit: int) -> list[dict[str, Any]]:
    """Same-scope texts for references (scripts already have theirs): skip the rest silently."""
    out: list[dict[str, Any]] = []
    done: set[str] = set()
    for ref in refs or []:
        if len(out) >= limit:
            break
        try:
            target = urljoin(base_url, str(ref))
        except ValueError:
            continue
        if target in done or not scope.allows(target):
            continue
        done.add(target)
        view = checker.get(target)
        if 200 <= int(view.get("status") or 0) < 300 and isinstance(view.get("body"), str):
            out.append({"url": target, "text": view["body"],
                        "bytes": len(view["body"].encode("utf-8"))})
    return out


def secret_page(checker: Any, scope: Any, target: str, hosts: list[str] | None,
                jar: list[Any] | None = None, storage: Any = None,
                journal_urls: list[str] | None = None,
                view: dict[str, Any] | None = None) -> dict[str, Any]:
    """One page fully analysed: served HTML (or a reused fetch), scripts, openapi,
    maps, agent docs, auth facts. ``view`` reuses an already-fetched page."""
    journal = list(journal_urls or [])
    page_view = view if isinstance(view, dict) else checker.get(target)
    if page_view.get("error"):
        return {"url": target, "error": str(page_view["error"])[:300]}
    html = page_view.get("body") if isinstance(page_view.get("body"), str) else ""
    headers = page_view.get("headers") if isinstance(page_view.get("headers"), dict) else {}
    link_header = str(headers.get("link") or "")
    snapshot = page_checks.snapshot_from_html(html, target)
    own = api_parts.own_sites(target, hosts)
    script_urls = secrets.script_urls(snapshot.get("scripts"), journal, target, own)
    scripts: list[dict[str, Any]] = []
    for script_url in script_urls:
        seen = checker.get(script_url)
        text = seen.get("body") if 200 <= int(seen.get("status") or 0) < 300 \
            and isinstance(seen.get("body"), str) else ""
        scripts.append({"url": script_url, "text": text,
                        "bytes": len(text.encode("utf-8")) if text else 0})
    texts = [item["text"] for item in scripts]
    openapi_docs: list[dict[str, Any]] = []
    for item in fetch_texts(checker, scope, target,
                            secrets.openapi_refs(html, texts, journal, link_header),
                            secrets.MAX_OPENAPI):
        seen = secrets.openapi_view(item["url"], item["text"])
        if seen:
            openapi_docs.append(seen)
    maps: list[dict[str, Any]] = []
    for item in scripts:
        for ref in secrets.sourcemap_refs(item["text"]):
            if len(maps) >= secrets.MAX_SOURCEMAPS:
                break
            for fetched in fetch_texts(checker, scope, item["url"], [ref], 1):
                seen = secrets.sourcemap_view(fetched["url"], fetched["text"])
                if seen:
                    maps.append(seen)
        if len(maps) >= secrets.MAX_SOURCEMAPS:
            break
    agent_texts = fetch_texts(checker, scope, target,
                              secrets.agent_refs(html, texts, journal, link_header), 3)
    section = secrets.build(target, html, scripts, openapi_docs, maps, agent_texts,
                            jar, storage, hosts, [])
    section["third_party_scripts"] = secrets.third_party_scripts(
        snapshot.get("scripts"), target, hosts, journal)
    section["counts"] = grading.counts(section["findings"])
    section["priority"] = grading.priority(section["findings"])
    return section


def active_page(checker: Any, scope: Any, target: str, page_url: str,
                hosts: list[str] | None, wanted: list[str], origin: str,
                paths: list[str], html: str | None = None) -> dict[str, Any]:
    """One origin front-to-back: preflight, methods, its links, one canary.

    ``html`` reuses an already-fetched page instead of requesting it again.
    """
    from web_search_neo.audit import report as report_mod
    findings: list[dict[str, Any]] = []
    targets = [urljoin(report_mod.origin_of(target), path) for path in paths]
    if "cors" in wanted:
        for probe in [target, *targets]:
            answer = checker.request("OPTIONS", probe, {
                "Origin": origin, "Access-Control-Request-Method": "GET"})
            if answer.get("error"):
                findings.append(info_finding(
                    "active-check-unreachable", "cors", "The preflight could not be sent",
                    evidence={"url": api_parts.safe_url(probe),
                              "error": str(answer.get("error"))[:200]}))
            else:
                findings += active.preflight_findings(probe, origin, answer.get("headers"))
    if "methods" in wanted:
        for probe in [target, *targets]:
            answer = checker.request("OPTIONS", probe)
            if answer.get("error"):
                continue
            headers = answer.get("headers") or {}
            findings += active.methods_findings(probe, answer.get("status"),
                                                str(headers.get("allow") or ""), None)
        trace = checker.request("TRACE", target)
        if not trace.get("error"):
            findings += active.methods_findings(target, None, "", trace.get("status"))
    if "redirects" in wanted or "canary" in wanted:
        if html is None:
            page_view = checker.get(page_url)
            html = page_view.get("body") if isinstance(page_view.get("body"), str) else ""
        snapshot = page_checks.snapshot_from_html(html or "", page_url)
        links = [link for link in secrets.link_urls(snapshot) if scope.allows(link)]
    if "redirects" in wanted:
        for link in links[: active.MAX_LINKS]:
            view = checker.get(link)
            if view.get("error"):
                continue
            findings += active.redirect_findings(
                link, view.get("route") or [], view.get("final_url") or link, page_url,
                view.get("not_followed"))
    if "canary" in wanted:
        token = active.new_canary()
        candidates = [page_url] + [link for link in links if "?" in link][: active.MAX_CANARY - 1]
        for candidate in candidates[: active.MAX_CANARY]:
            base = candidate.split("#", 1)[0]
            probe_url = f"{base}{'&' if '?' in base else '?'}{active.CANARY_PARAM}={token}"
            view = checker.get(probe_url)
            body = view.get("body")
            if view.get("error") or not isinstance(body, str):
                continue
            findings += active.reflection_findings(probe_url, token, body)
    return active.build(target, wanted, findings, [])


def merge(kind: str, url: str, mode: str, sections: list[dict[str, Any]],
          requests_made: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sections into one report: unique findings, summed counts, heads of sections."""
    seen: dict[str, dict[str, Any]] = {}
    for section in sections:
        for item in section.get("findings") or []:
            if isinstance(item, dict) and item.get("id") and item["id"] not in seen:
                seen[item["id"]] = item
    unique = list(seen.values())
    counts = grading.counts(unique)
    order = grading.priority(unique)
    first = "; ".join(item["title"] for item in order[:3]) or "nothing to fix"
    check_order = ("cors", "methods", "redirects", "canary")
    seen_checks: list[str] = []
    for section in sections:
        for name in section.get("checks") or []:
            if name not in seen_checks:
                seen_checks.append(name)
    checks = [name for name in check_order if name in seen_checks]
    report: dict[str, Any] = {
        "success": True, "url": api_parts.safe_url(str(url or "")), "scope_mode": mode,
        **({"checks": checks} if checks else {}),
        "sections": [{"url": section.get("url"), "counts": section.get("counts"),
                      "summary_line": section.get("summary_line"),
                      **({"error": section["error"]} if section.get("error") else {})}
                     for section in sections],
        "counts": counts,
        "summary_line": (f"{len(sections)} section(s): " if len(sections) != 1 else "")
                        + f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low. "
                        + f"Fix first: {first}.",
        "priority": order, "findings": unique,
        "requests_made": list(requests_made),
    }
    report.update(extra or {})
    if kind == "secret":
        endpoints: dict[tuple[str, str], dict[str, Any]] = {}
        openapi: list[dict[str, Any]] = []
        sourcemaps: list[dict[str, Any]] = []
        forms: list[dict[str, Any]] = []
        tokens: set[str] = set()
        scripts: list[dict[str, Any]] = []
        for section in sections:
            for script in section.get("scripts") or []:
                if isinstance(script, dict) and script.get("url") not in {
                        item.get("url") for item in scripts}:
                    scripts.append({"url": script.get("url"), "bytes": int(script.get("bytes") or 0)})
            for item in section.get("code_endpoints") or []:
                key = (item.get("method"), item.get("path"))
                slot = endpoints.setdefault(key, {"method": key[0], "path": key[1], "count": 0,
                                                  "sources": []})
                slot["count"] += int(item.get("count") or 0)
                for source in item.get("sources") or []:
                    if source not in slot["sources"]:
                        slot["sources"].append(source)
            openapi += section.get("openapi") or []
            sourcemaps += section.get("sourcemaps") or []
            forms += section.get("auth_forms") or []
            tokens.update(section.get("token_names") or [])
        docs: list[dict[str, Any]] = []
        for section in sections:
            for doc in section.get("agent_docs") or []:
                if doc not in docs:
                    docs.append(doc)
        report.update({
            "scripts": scripts,
            "code_endpoints": sorted(endpoints.values(), key=lambda item: (-item["count"], item["path"])),
            "openapi": openapi, "sourcemaps": sourcemaps, "auth_forms": forms,
            "token_names": sorted(tokens), "agent_docs": docs,
            "third_party_scripts": sorted({script for section in sections
                                           for script in section.get("third_party_scripts") or []}),
        })
    return report


def crawl_pages(start: str, checker: Any, scope: Any, paths: list[str], max_pages: int,
                max_depth: int, delay_ms: int, respect_robots: bool
                ) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """A bounded crawl of served pages with their views: sitemap plus links from
    snapshots, robots.txt honoured like a crawler's."""
    import re as _re
    import time as _time
    import urllib.robotparser as _robotparser
    from web_search_neo.audit import report as report_mod
    origin_of = report_mod.origin_of
    robots: dict[str, Any] = {}

    def allowed(target: str) -> bool:
        if not respect_robots:
            return True
        origin = origin_of(target)
        if origin not in robots:
            answer = checker.get(origin + "/robots.txt", small=True)
            body = answer.get("body") if 200 <= int(answer.get("status") or 0) < 300 \
                and isinstance(answer.get("body"), str) else None
            parser = _robotparser.RobotFileParser()
            parser.parse(str(body or "").splitlines())
            robots[origin] = parser
        return bool(robots[origin].can_fetch("*", target))

    queue: list[tuple[str, int]] = [(start, 0)] + [(urljoin(origin_of(start), path), 0)
                                                   for path in paths or []]
    sitemap = origin_of(start) + "/sitemap.xml"
    if scope.allows(sitemap):
        answer = checker.get(sitemap, small=True)
        if 200 <= int(answer.get("status") or 0) < 300 and isinstance(answer.get("body"), str):
            for loc in _re.findall(r"<loc>\s*([^<>\s]+)\s*</loc>", answer["body"]):
                if scope.allows(loc):
                    queue.append((loc, 1))
    seen: set[str] = set()
    pages: list[tuple[str, dict[str, Any]]] = []
    skipped = {"robots": 0, "out_of_scope": 0, "depth": 0}
    while queue and len(pages) < max(1, min(int(max_pages), 50)):
        target, depth = queue.pop(0)
        if target in seen:
            continue
        seen.add(target)
        if not scope.allows(target):
            skipped["out_of_scope"] += 1
            continue
        if not allowed(target):
            skipped["robots"] += 1
            continue
        view = checker.get(target)
        if view.get("error"):
            continue
        pages.append((target, view))
        if depth >= max(0, min(int(max_depth), 5)):
            skipped["depth"] += 1
            continue
        if len(pages) > 1 and delay_ms:
            _time.sleep(max(0, min(int(delay_ms), 10_000)) / 1000)
        if isinstance(view.get("body"), str):
            snapshot = page_checks.snapshot_from_html(view["body"], target)
            for link in secrets.link_urls(snapshot):
                if link.startswith(("http://", "https://")) and link not in seen:
                    queue.append((link, depth + 1))
    return pages, {"crawled": len(pages), "not_crawled": skipped}
