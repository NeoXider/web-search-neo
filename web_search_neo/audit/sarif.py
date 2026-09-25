"""SARIF 2.1.0 for the site-check reports: findings as tool results for CI.

Pure: a report dict in, a SARIF document out. Rule ids are the finding ids,
so two runs of the same tool diff cleanly; levels follow the verdict
(fail -> error, warn -> warning, everything else -> note). Values never reach
a SARIF file for the same reason they never reach the report: only finding
titles, details, fixes and masked evidence travel.
"""

from __future__ import annotations

import json
from typing import Any

TOOL_NAME = "web-search-neo"
SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

_LEVELS = {"fail": "error", "warn": "warning"}


def _rule(item: dict[str, Any]) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "id": str(item.get("id") or "finding"),
        "name": str(item.get("title") or item.get("id") or "finding"),
        "shortDescription": {"text": str(item.get("title") or "")[:300]},
    }
    if item.get("fix"):
        rule["help"] = {"text": str(item["fix"])[:1000]}
    return rule


def _result(item: dict[str, Any], uri: str) -> dict[str, Any]:
    message = str(item.get("title") or "")
    if item.get("detail"):
        message += ": " + str(item["detail"])[:500]
    return {
        "ruleId": str(item.get("id") or "finding"),
        "level": _LEVELS.get(str(item.get("status") or ""), "note"),
        "message": {"text": message},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}}}],
    }


def build(report: dict[str, Any], *, tool: str = TOOL_NAME, version: str = "") -> dict[str, Any]:
    """A SARIF run for one report's findings (every severity, passes included)."""
    findings = report.get("findings") if isinstance(report, dict) else None
    findings = findings if isinstance(findings, list) else []
    uri = str((report or {}).get("url") or "")
    rules: dict[str, dict[str, Any]] = {}
    for item in findings:
        if isinstance(item, dict) and str(item.get("id") or "") not in rules:
            rules[str(item.get("id") or "finding")] = _rule(item)
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {"driver": {
                "name": tool,
                **({"version": str(version)} if version else {}),
                "rules": sorted(rules.values(), key=lambda rule: rule["id"]),
            }},
            "results": [_result(item, uri) for item in findings if isinstance(item, dict)],
        }],
    }


def dumps(report: dict[str, Any], *, tool: str = TOOL_NAME, version: str = "") -> bytes:
    """The SARIF document as UTF-8 JSON, ready for ``write_download``."""
    return json.dumps(build(report, tool=tool, version=version),
                      ensure_ascii=False, indent=1).encode("utf-8")
