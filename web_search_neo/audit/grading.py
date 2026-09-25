"""Mozilla HTTP Observatory's grading, plus an extended score for what it does not grade.

Every finding carries ``points``: the modifier of the Observatory result it
stands for (CSP, cookies, COOP, COEP, CORS, redirection, Referrer-Policy, HSTS,
SRI, X-Content-Type-Options, X-Frame-Options, Cross-Origin-Resource-Policy - one
result per test, the worst one). Penalties always count; bonuses count only
when the score before bonuses is 90 or more (Observatory's rule). The score is
floored at 0; the letter comes from Observatory's table (A+ is 100 and above).
Observatory's maximum is 165; without the HSTS preload-list bonus, which this
report never awards, 160 is the highest score here.

This project's own penalties (``extra_points``: mixed content, forms posting
over http, passwords in GET forms, ...) never move that grade. They produce a
second, stricter number under ``extended``, so both are visible and neither is
mistaken for the other.
"""
from __future__ import annotations

from typing import Any

from web_search_neo.audit.findings import SEVERITY_RANK, is_problem

BASELINE = 100
BONUS_THRESHOLD = 90
GRADES = ((100, "A+"), (90, "A"), (85, "A-"), (80, "B+"), (70, "B"), (65, "B-"), (60, "C+"),
          (50, "C"), (45, "C-"), (40, "D+"), (30, "D"), (25, "D-"), (0, "F"))


def letter(score: int) -> str:
    for floor, grade in GRADES:
        if score >= floor:
            return grade
    return "F"


def _score(penalty_total: int, bonus_total: int) -> tuple[int, int, bool]:
    base = BASELINE + penalty_total
    applied = base >= BONUS_THRESHOLD
    return max(0, base + (bonus_total if applied else 0)), base, applied


def grade(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Score, letter and the arithmetic behind them; ``extended`` adds the own penalties."""
    penalties = [f for f in findings if f.get("points", 0) < 0]
    bonuses = [f for f in findings if f.get("points", 0) > 0]
    extras = [f for f in findings if f.get("extra_points", 0) < 0]
    penalty_total = sum(f["points"] for f in penalties)
    bonus_total = sum(f["points"] for f in bonuses)
    score, base, applied = _score(penalty_total, bonus_total)
    extra_total = sum(f["extra_points"] for f in extras)
    extended, _, extended_applied = _score(penalty_total + extra_total, bonus_total)
    return {
        "score": score,
        "grade": letter(score),
        "extended": {
            "score": extended, "grade": letter(extended),
            "extra_penalties": [{"id": f["id"], "points": f["extra_points"], "title": f["title"]} for f in extras],
            "note": "Observatory's score plus this report's own penalties for problems Observatory does not "
                    "grade; the headline grade never includes them.",
            "bonuses_applied": extended_applied,
        },
        "explanation": {
            "baseline": BASELINE,
            "penalties": [{"id": f["id"], "points": f["points"], "title": f["title"]} for f in penalties],
            "penalty_total": penalty_total,
            "bonuses": [{"id": f["id"], "points": f["points"], "title": f["title"]} for f in bonuses],
            "bonuses_applied": applied,
            "rule": (f"Mozilla HTTP Observatory: {BASELINE} minus the penalties of its tests (one result per "
                     f"test); bonuses count only when that is {BONUS_THRESHOLD}+ (here {base}); floored at 0. "
                     + ", ".join(f"{g} >= {floor}" for floor, g in GRADES[:-1]) + ", F below 25."),
        },
    }


def priority(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What to fix first: fails before warnings, then severity, then the points at stake."""
    problems = [f for f in findings if is_problem(f)]
    problems.sort(key=lambda f: (f["status"] != "fail", SEVERITY_RANK[f["severity"]],
                                 f.get("points", 0) + f.get("extra_points", 0), f["id"]))
    return [{"rank": rank, "id": f["id"], "severity": f["severity"], "points": f.get("points", 0),
             **({"extra_points": f["extra_points"]} if f.get("extra_points") else {}),
             "title": f["title"], "fix": f.get("fix", "")} for rank, f in enumerate(problems, 1)]


def counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    """Problems by severity plus passes, the one-line picture of a report."""
    out = {"high": 0, "medium": 0, "low": 0, "pass": 0, "info": 0}
    for item in findings:
        if is_problem(item):
            out[item["severity"] if item["severity"] in out else "low"] += 1
        elif item["status"] == "pass":
            out["pass"] += 1
        else:
            out["info"] += 1
    return out
