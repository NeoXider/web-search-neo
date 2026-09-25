"""A regression verdict between two saved site-check reports: what got fixed, what broke.

Pure: two report dicts in, ``{fixed, added, summary}`` out. Findings match by
id - the stable machine name every check result carries - so a rerun of the
same action on the same scope reads as no change. Severity moves are reported
as a fix plus an addition, never silently merged.
"""

from __future__ import annotations

from typing import Any


def _index(report: Any) -> dict[str, dict[str, Any]]:
    findings = report.get("findings") if isinstance(report, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for item in findings if isinstance(findings, list) else []:
        if isinstance(item, dict) and item.get("id") and item["id"] not in out:
            out[str(item["id"])] = item
    return out


def _short(item: dict[str, Any]) -> dict[str, Any]:
    short: dict[str, Any] = {"id": item.get("id"), "status": item.get("status"),
                             "severity": item.get("severity"), "title": item.get("title")}
    if item.get("fix"):
        short["fix"] = item["fix"]
    return short


def compare(old: Any, new: Any) -> dict[str, Any]:
    """Fixed (in the baseline, gone now) and added (new, or worse than before)."""
    before, after = _index(old), _index(new)
    fixed = [_short(before[fid]) for fid in before if fid not in after]
    added = [_short(after[fid]) for fid in after
             if fid not in before or after[fid].get("status") != before[fid].get("status")]
    fixed.sort(key=lambda item: str(item["id"]))
    added.sort(key=lambda item: str(item["id"]))
    if not fixed and not added:
        summary = "No change: the same findings as the baseline."
    else:
        parts = []
        if fixed:
            parts.append(f"{len(fixed)} fixed")
        if added:
            parts.append(f"{len(added)} added")
        summary = f"Against the baseline: {', '.join(parts)}."
    return {"fixed": fixed, "added": added, "summary": summary}
