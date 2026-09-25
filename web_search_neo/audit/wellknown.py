"""``/.well-known/security.txt`` (RFC 9116) and ``/robots.txt``: read if present, never more.

A catch-all route that answers every path with the app's HTML page is common,
so a 200 whose body is HTML counts as "not published" rather than as a file.
"""
from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Callable

from web_search_neo.audit.findings import finding, info, passed

_SECURITY_FIELDS = ("contact", "expires", "encryption", "policy", "acknowledgments",
                    "preferred-languages", "canonical", "hiring", "csaf")


def _looks_like_html(body: str) -> bool:
    head = body.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head" in head


def _text_file(result: dict[str, Any]) -> str | None:
    """The body when this is a real text file, else None."""
    if result.get("error") or not 200 <= int(result.get("status") or 0) < 300:
        return None
    body = str(result.get("body") or "")
    if not body.strip() or _looks_like_html(body):
        return None
    return body


def parse_security_txt(body: str) -> dict[str, Any]:
    """Fields of a security.txt (repeatable ones as lists); PGP armour is tolerated."""
    fields: dict[str, list[str]] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-----") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in _SECURITY_FIELDS:
            fields.setdefault(key, []).append(value.strip())
    return {"fields": fields, "signed": "-----BEGIN PGP SIGNED MESSAGE-----" in body}


def _expires(value: str) -> datetime | None:
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def analyze_security_txt(result: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    body = _text_file(result or {})
    if body is None:
        return [finding(
            "security-txt-missing", "files", "warn", "low", "No security.txt",
            detail="Researchers who find a problem have no published way to reach you.",
            fix="Publish /.well-known/security.txt with Contact: mailto:security@your-domain and "
                "Expires: <date within a year> (RFC 9116, generator at securitytxt.org).")], {"present": False}
    parsed = parse_security_txt(body)
    fields = parsed["fields"]
    view = {"present": True, "url": (result or {}).get("final_url"), "signed": parsed["signed"],
            "fields": {key: values[:5] for key, values in fields.items()}}
    out: list[dict[str, Any]] = []
    if not fields.get("contact"):
        out.append(finding("security-txt-no-contact", "files", "warn", "low", "security.txt has no Contact",
                           fix="Add Contact: mailto:... or https://... (required by RFC 9116)."))
    expires = _expires(fields.get("expires", [""])[0]) if fields.get("expires") else None
    if not fields.get("expires"):
        out.append(finding("security-txt-no-expires", "files", "warn", "low", "security.txt has no Expires",
                           fix="Add Expires: with a date less than a year ahead (required by RFC 9116)."))
    elif expires is None:
        out.append(finding("security-txt-bad-expires", "files", "warn", "low", "security.txt Expires is unreadable",
                           fix="Use an ISO 8601 date, e.g. Expires: 2027-01-01T00:00:00Z.",
                           evidence=fields["expires"][0]))
    else:
        moment = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
        if moment.timestamp() < time.time():
            out.append(finding("security-txt-expired", "files", "warn", "low", "security.txt has expired",
                               fix="Update Expires (and check the Contact still works).",
                               evidence=fields["expires"][0]))
    if not out:
        out.append(passed("security-txt-ok", "files", "security.txt is published", evidence=view["fields"]))
    return out, view


# Crawlers that feed AI training and agentic retrieval: naming one in robots.txt
# is currently the only polite opt-out, and not naming it leaves it to `*`.
_AI_BOTS = ("gptbot", "chatgpt-user", "claudebot", "anthropic-ai", "ccbot", "perplexitybot",
            "google-extended", "bytespider", "cohere-ai", "meta-ai", "applebot-extended",
            "amazonbot", "youbot", "diffbot", "omgilibot", "facebookbot")


def ai_crawler_policy(body: str) -> dict[str, list[str]]:
    """AI crawlers named in robots.txt, split into blocked (Disallow: /) and open."""
    groups: dict[str, list[str]] = {}
    group: list[str] = []
    had_rule = False
    for line in body.splitlines():
        key, _, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent" and value:
            if had_rule:
                group = []
                had_rule = False
            if value.lower() not in group:
                group.append(value.lower())
        elif key == "disallow" and group:
            had_rule = True
            for agent in group:
                groups.setdefault(agent, []).append(value)
    named = sorted({agent for agent in groups for token in _AI_BOTS if token in agent})
    blocked = sorted(agent for agent in named
                     if any(rule == "/" for rule in groups.get(agent, [])))
    return {"named": named, "blocked": blocked,
            "open": sorted(agent for agent in named if agent not in blocked)}


def analyze_robots(result: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    body = _text_file(result or {})
    if body is None:
        return [info("robots-missing", "files", "No robots.txt",
                     detail="Optional; crawlers then index everything they can reach.")], {"present": False}
    disallow, sitemaps, agents = [], [], set()
    for line in body.splitlines():
        key, _, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "disallow" and value:
            disallow.append(value)
        elif key == "sitemap" and value:
            sitemaps.append(value)
        elif key == "user-agent" and value:
            agents.add(value)
    view = {"present": True, "lines": len(body.splitlines()), "user_agents": sorted(agents)[:20],
            "disallow": disallow[:20], "disallow_total": len(disallow), "sitemaps": sitemaps[:10]}
    findings = [info("robots-present", "files", f"robots.txt disallows {len(disallow)} path(s)",
                     detail="robots.txt is public and only a request to polite crawlers: it hides nothing.",
                     fix="Never rely on Disallow to protect admin or private URLs; protect them with authentication.",
                     evidence={"disallow": disallow[:20], "sitemaps": sitemaps[:10]})]
    policy = ai_crawler_policy(body)
    view["ai_crawlers"] = policy
    findings.append(info(
        "robots-ai-policy", "files",
        f"robots.txt names {len(policy['named'])} AI crawler(s): "
        f"{len(policy['blocked'])} blocked" if policy["named"] else
        "robots.txt names no AI crawler: they follow the '*' rules",
        detail="AI training and agentic-retrieval bots obey robots.txt when they are named in it; "
               "unnamed ones follow the '*' group. This says nothing about impolite scrapers.",
        fix="Decide per crawler: 'Disallow: /' under its User-agent opts out, leaving it open "
            "is a deliberate choice - record it.",
        evidence=policy))
    return findings, view


def read(origin: str, fetch: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Fetch both files through ``fetch`` (security.txt falls back to the legacy root path)."""
    security = fetch(origin + "/.well-known/security.txt")
    urls = [origin + "/.well-known/security.txt"]
    if _text_file(security) is None:
        legacy = fetch(origin + "/security.txt")
        urls.append(origin + "/security.txt")
        if _text_file(legacy) is not None:
            security = legacy
    robots = fetch(origin + "/robots.txt")
    urls.append(origin + "/robots.txt")
    return {"security_txt": security, "robots_txt": robots, "requested": urls}
