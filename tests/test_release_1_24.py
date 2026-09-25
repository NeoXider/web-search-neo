"""1.24: scope site/hosts for secret_scan and active_probe, agent docs, AI crawlers.

Pure unit tests pin the multipage merge, the agent-surface discovery and the
robots AI policy; the fixture servers prove the modes end to end. secret_scan
site/hosts and active_probe hosts need no browser at all, so no Chrome skips
here - but no test reaches the internet either.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import active_fixture_site
import secret_fixture_site
from web_search_neo import audit_actions, browser_tools, main
from web_search_neo.audit import multipage, wellknown
from web_search_neo.audit import secrets as secret_checks
from web_search_neo.contract import notes, param_docs

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ids(findings):
    return {item["id"]: item for item in findings}


class _Scope:
    def __init__(self, allowed):
        self._allowed = set(allowed)

    def allows(self, url):
        return url in self._allowed


class _Checker:
    """A stub Checker: canned pages, every request recorded like the real budget."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.made: list[str] = []
        self.budget = self

    def take(self, label):
        self.made.append(label)
        return True

    def get(self, url, small=False):
        _ = small
        self.made.append(f"GET {url}")
        if url in self.pages:
            body = self.pages[url]
            return {"url": url, "status": 200,
                    "headers": {"content-type": "text/html"} if url.endswith("/") or
                    url.endswith("2") else {"content-type": "text/javascript"},
                    "body": body}
        return {"url": url, "error": "not found"}

    def request(self, method, url, headers=None):
        _ = (method, headers)
        self.made.append(f"{method} {url}")
        return {"url": url, "status": 405, "headers": {}}


# --- merge -----------------------------------------------------------------------------------

def test_merge_dedupes_counts_and_keeps_sections():
    sections = [
        {"url": "https://h.test/", "counts": {"high": 1, "medium": 0, "low": 0, "pass": 0, "info": 0},
         "summary_line": "one", "code_endpoints": [{"method": "GET", "path": "/a", "count": 2,
                                                    "sources": ["s1"]}],
         "findings": [{"id": "x", "status": "fail", "severity": "high", "title": "X",
                       "points": 0}]},
        {"url": "https://h.test/2", "counts": {"high": 1, "medium": 1, "low": 0, "pass": 0, "info": 0},
         "summary_line": "two", "code_endpoints": [{"method": "GET", "path": "/a", "count": 3,
                                                    "sources": ["s2"]}],
         "findings": [{"id": "x", "status": "fail", "severity": "high", "title": "X",
                       "points": 0},
                      {"id": "y", "status": "warn", "severity": "medium", "title": "Y",
                       "points": 0}]},
    ]
    report = multipage.merge("secret", "https://h.test/", "site", sections, ["GET https://h.test/"])
    assert report["scope_mode"] == "site" and len(report["sections"]) == 2
    assert [item["id"] for item in report["findings"]] == ["x", "y"]  # deduped, order kept
    assert report["counts"] == {"high": 1, "medium": 1, "low": 0, "pass": 0, "info": 0}
    assert report["priority"][0]["id"] == "x" and report["priority"][0]["rank"] == 1
    assert report["code_endpoints"] == [{"method": "GET", "path": "/a", "count": 5,
                                         "sources": ["s1", "s2"]}]
    assert report["summary_line"].startswith("2 section(s): ")


# --- secret_page with a stub checker ------------------------------------------------------------

def test_secret_page_reads_scripts_openapi_maps_and_agent_docs():
    pages = {
        "https://h.test/": ('<html><head><script src="/a.js"></script>'
                             '<link rel="openapi" href="/openapi.json">'
                             '<link rel="alternate" type="text/markdown" href="/llms.txt">'
                             '</head><body></body></html>'),
        "https://h.test/a.js": 'const k = "AKIAIOSFODNN7EXAMPLE";\n//# sourceMappingURL=/a.js.map',
        "https://h.test/openapi.json": '{"openapi": "3.0.0", "paths": {"/a": {}}}',
        "https://h.test/a.js.map": '{"version": 3, "sources": ["a.ts"]}',
        "https://h.test/llms.txt": "# docs\n\nContact admin@example.test for access.\n",
    }
    pages["https://h.test/a.js"] += "\n//# sourceMappingURL=/a.js.map"
    pages["https://h.test/a.js.map"] = '{"version": 3, "sources": ["a.ts"]}'
    checker = _Checker(pages)
    section = multipage.secret_page(checker, _Scope(pages), "https://h.test/", None)
    found = ids(section["findings"])
    assert found["secret-aws-key"]["status"] == "fail"
    assert found["secret-openapi-exposed"]["status"] == "warn"
    assert found["secret-sourcemap-exposed"]["status"] == "warn"
    assert found["secret-agent-docs"]["status"] == "info"
    assert section["agent_docs"] == [{"url": "https://h.test/llms.txt", "bytes": len(pages["https://h.test/llms.txt"])}]
    assert section["openapi"] == [{"url": "https://h.test/openapi.json", "version": "3.0.0",
                                   "paths": 1}]
    dump = json.dumps(section)
    assert "AKIAIOSFODNN7EXAMPLE" not in dump and "admin@example.test" not in dump


def test_secret_page_error_section():
    checker = _Checker({})
    section = multipage.secret_page(checker, _Scope([]), "https://h.test/gone", None)
    assert section["error"] == "not found" and section["url"] == "https://h.test/gone"


def test_agent_refs_from_link_headers():
    link = ('</llms.txt>; rel="alternate"; type="text/markdown", '
            '</.well-known/openapi.json>; rel="service-desc", '
            '</style.css>; rel="stylesheet"')
    refs = secret_checks.agent_refs("<html></html>", [], [], link)
    # API descriptions belong to openapi[], even when Link-advertised.
    assert refs == ["/llms.txt"]
    assert secret_checks.openapi_refs("<html></html>", [], [], link) == [
        "/.well-known/openapi.json"]
    assert secret_checks.agent_refs("", [], [], None) == []


# --- crawl_pages ----------------------------------------------------------------------------------

def test_crawl_pages_walks_links_and_honours_robots():
    pages = {
        "https://h.test/": '<html><body><a href="/a">a</a><a href="/private">p</a></body></html>',
        "https://h.test/a": '<html><body><a href="/">home</a></body></html>',
        "https://h.test/private": "<html><body>no</body></html>",
        "https://h.test/robots.txt": "User-agent: *\nDisallow: /private\n",
    }
    checker = _Checker(pages)
    urls, info = multipage.crawl_pages("https://h.test/", checker, _Scope(pages),
                                       [], 10, 2, 0, True)
    assert [url for url, _view in urls] == ["https://h.test/", "https://h.test/a"]
    assert info["not_crawled"]["robots"] == 1
    assert all(view["status"] == 200 for _url, view in urls)


# --- robots AI policy -------------------------------------------------------------------------------

def test_robots_ai_policy_blocked_and_open():
    body = ("User-agent: *\nDisallow: /admin\n\n"
            "User-agent: GPTBot\nDisallow: /\n\n"
            "User-agent: ClaudeBot\nDisallow: /drafts\n")
    findings, view = wellknown.analyze_robots({"status": 200, "body": body})
    found = ids(findings)
    assert found["robots-ai-policy"]["status"] == "info"
    assert view["ai_crawlers"] == {"named": ["claudebot", "gptbot"],
                                   "blocked": ["gptbot"], "open": ["claudebot"]}
    assert "gptbot" in json.dumps(found["robots-ai-policy"]["evidence"])


def test_robots_ai_policy_silent_when_unnamed():
    findings, view = wellknown.analyze_robots({"status": 200, "body": "User-agent: *\nDisallow: /x\n"})
    assert ids(findings)["robots-ai-policy"]["evidence"] == {"named": [], "blocked": [],
                                                             "open": []}


# --- validation ---------------------------------------------------------------------------------------

def test_scope_modes_validated_before_anything():
    with pytest.raises(ValueError, match="one of"):
        asyncio.run(audit_actions.browser_secret_scan(url="http://127.0.0.1:9/", scope="crawl"))
    with pytest.raises(ValueError, match="page mode only"):
        asyncio.run(audit_actions.browser_secret_scan(url="http://127.0.0.1:9/",
                                                      scope="site", session_id="s"))
    with pytest.raises(ValueError, match="one of"):
        asyncio.run(audit_actions.browser_active_probe(url="http://127.0.0.1:9/", scope="site"))


# --- contract --------------------------------------------------------------------------------------------

def test_scope_params_in_action_schema():
    schema = asyncio.run(main.web_info("action_schema", {"action": "secret_scan"}))
    assert {"scope", "paths", "max_pages", "include_subdomains"} <= set(
        schema["input_schema"]["properties"])
    schema = asyncio.run(main.web_info("action_schema", {"action": "active_probe"}))
    assert {"scope", "include_subdomains"} <= set(schema["input_schema"]["properties"])
    assert "secret_scan" in param_docs._BY_ACTION and "active_probe" in param_docs._BY_ACTION
    assert len(json.dumps(main._capabilities())) <= 13_500


# --- against the local fixture servers ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def secret_site():
    site = secret_fixture_site.start()
    try:
        yield site
    finally:
        site.stop()


@pytest.fixture(scope="module")
def probe_site():
    site = active_fixture_site.start()
    try:
        yield site
    finally:
        site.stop()


def test_secret_scan_site_crawls_pages(secret_site):
    result = asyncio.run(audit_actions.browser_secret_scan(
        url=secret_site.base_url + "/", scope="site", max_pages=5, delay_ms=0))
    assert result["success"], result
    assert result["scope_mode"] == "site"
    urls = [section["url"] for section in result["sections"]]
    assert secret_site.base_url + "/" in urls and secret_site.base_url + "/page2" in urls
    assert result["crawl"]["crawled"] >= 2
    assert all(line.split(" ", 1)[1].split(" (", 1)[0].startswith(secret_site.base_url)
               for line in result["requests_made"])
    assert not browser_tools._sessions  # browserless


def test_secret_scan_hosts_reads_each_origin(secret_site):
    port = secret_site.base_url.rsplit(":", 1)[-1]
    result = asyncio.run(audit_actions.browser_secret_scan(
        url=secret_site.base_url + "/", scope="hosts",
        hosts=[f"localhost:{port}"]))
    assert result["success"], result
    assert result["scope_mode"] == "hosts"
    assert len(result["sections"]) == 2
    assert not browser_tools._sessions  # browserless


def test_secret_scan_finds_agent_docs_live(secret_site):
    result = asyncio.run(audit_actions.browser_secret_scan(url=secret_site.base_url + "/"))
    assert result["success"], result
    assert result["agent_docs"] == [{"url": secret_site.base_url + "/llms.txt",
                                     "bytes": len(secret_fixture_site.LLMS_TXT.encode("utf-8"))}]
    assert "secret-agent-docs" in {item["id"] for item in result["findings"]}


def test_active_probe_hosts_probes_each_origin(probe_site):
    from urllib.parse import urlsplit

    port = probe_site.base_url.rsplit(":", 1)[-1]
    result = asyncio.run(audit_actions.browser_active_probe(
        url=probe_site.base_url + "/", scope="hosts", hosts=[f"localhost:{port}"],
        checks=["methods"]))
    assert result["success"], result
    assert result["scope_mode"] == "hosts" and len(result["sections"]) == 2
    assert sum(1 for line in result["requests_made"] if line.startswith("TRACE")) == 2
    hosts_seen = {urlsplit(line.split(" ", 1)[1].split(" (", 1)[0]).hostname
                  for line in result["requests_made"]}
    assert hosts_seen <= {"127.0.0.1", "localhost"}
    assert not browser_tools._sessions  # browserless
