"""One shape for every check result, so the report, the grade and the tests agree.

A finding is a plain dict:

* ``id`` - stable machine name (``hsts-missing``), what a caller filters on;
* ``category`` - headers, csp, cookies, transport, cors, page, files;
* ``status`` - ``fail`` (fix it), ``warn`` (worth fixing), ``pass``, ``info``, ``skip``;
* ``severity`` - ``high``, ``medium``, ``low``, ``info``;
* ``title`` / ``detail`` - what was seen, in words;
* ``fix`` - the concrete change that resolves it (empty for a pass);
* ``evidence`` - the header value, cookie names or URLs the verdict rests on;
* ``points`` - the Mozilla Observatory modifier for this result (negative is a
  penalty, positive a bonus); only Observatory's own tests carry points, so the
  grade stays comparable with Observatory's;
* ``extra_points`` - this project's own penalty for a problem Observatory does
  not grade (mixed content, insecure forms, ...); it moves only ``extended``.
"""
from __future__ import annotations

from typing import Any

STATUSES = ("fail", "warn", "pass", "info", "skip")
SEVERITIES = ("high", "medium", "low", "info")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def finding(
    fid: str,
    category: str,
    status: str,
    severity: str,
    title: str,
    *,
    detail: str = "",
    fix: str = "",
    evidence: Any = None,
    points: int = 0,
    extra_points: int = 0,
) -> dict[str, Any]:
    """Build one finding; an unknown status or severity is a programming error."""
    if status not in STATUSES:
        raise ValueError(f"finding status must be one of {STATUSES}, not {status!r}")
    if severity not in SEVERITIES:
        raise ValueError(f"finding severity must be one of {SEVERITIES}, not {severity!r}")
    item: dict[str, Any] = {
        "id": fid, "category": category, "status": status, "severity": severity,
        "title": title, "points": int(points),
    }
    if extra_points:
        item["extra_points"] = int(extra_points)
    if detail:
        item["detail"] = detail
    if fix:
        item["fix"] = fix
    if evidence not in (None, "", [], {}):
        item["evidence"] = evidence
    return item


def passed(fid: str, category: str, title: str, *, detail: str = "", evidence: Any = None,
           points: int = 0) -> dict[str, Any]:
    """A check that found nothing to fix (a bonus when ``points`` > 0)."""
    return finding(fid, category, "pass", "info", title, detail=detail, evidence=evidence,
                   points=points)


def info(fid: str, category: str, title: str, *, detail: str = "", fix: str = "",
         evidence: Any = None) -> dict[str, Any]:
    """An observation without a verdict: context for the reader, never scored."""
    return finding(fid, category, "info", "info", title, detail=detail, fix=fix, evidence=evidence)


def is_problem(item: dict[str, Any]) -> bool:
    """A finding the owner should act on."""
    return item.get("status") in {"fail", "warn"}


def clip_list(values: list[Any], limit: int) -> dict[str, Any]:
    """A bounded evidence list that says how much it left out (never a silent cut)."""
    kept = list(values[:limit])
    out: dict[str, Any] = {"items": kept, "total": len(values)}
    if len(values) > limit:
        out["truncated"] = True
        out["omitted"] = len(values) - limit
    return out
