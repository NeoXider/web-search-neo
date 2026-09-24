"""The console topic: a session history read without side effects, honestly paged.

The topic used to read through a hidden per-session cursor: the first call
returned a tail and moved the cursor, after which every filter - even
``since_seq: 0`` - came back empty, and ``since_seq: N`` returned the newest
entries rather than the ones after N. Browser-log copies came with ``seq: 0``
and duplicated the hook's entries.

Now every read first ingests whatever is new into one session history, giving
each entry the history's own ``seq`` (browser-log entries included), then
answers from that history: entries after ``since_seq`` (0 = from the start of
what is kept), then the filters, then the first ``limit`` in order. ``next_seq``
is the seq of the last entry returned, so passing it back reads the next page;
``has_more``/``matched`` say whether there is one. Repeats can be folded
(``dedupe``) into one entry with ``count``/``first_seq``/``last_seq``. Paged in
ascending order, a fold stops before the entry that would open one group too
many, so it counts only repeats up to there and ``next_seq`` is the seq of the
last entry it took: ``A(1) B(2) A(3) C(4)`` with ``limit=1`` reads A, B, A, C
and never skips B (``next_seq`` from the last group's ``last_seq`` did). The
history keeps the newest ``HISTORY_LIMIT`` entries and counts what fell out.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from web_search_neo import diagnostics

HISTORY_LIMIT = 2000
# Chrome's browser-log copy of a hooked console call can arrive one read later
# than the hook's; it is recognised against this many recent entries.
_DUPLICATE_WINDOW = 200


def ingest(session: Any, collected: dict[str, Any]) -> None:
    """Append freshly collected entries to the session history with history seqs."""
    history: list[dict[str, Any]] = session.console_history
    recent = {diagnostics._console_key(e) for e in history[-_DUPLICATE_WINDOW:] if e.get("kind") != "browser"}
    for entry in collected.get("entries") or []:
        if entry.get("kind") == "browser" and diagnostics._console_key(entry) in recent:
            continue  # Chrome's copy of a message the hook already recorded
        session.console_hseq += 1
        stored = dict(entry, seq=session.console_hseq)
        if entry.get("seq"):
            stored["source_seq"] = entry["seq"]
        history.append(stored)
        if entry.get("kind") != "browser":
            recent.add(diagnostics._console_key(entry))
    overflow = len(history) - HISTORY_LIMIT
    if overflow > 0:
        del history[:overflow]
        session.console_history_dropped += overflow


def _fold(entries: list[dict[str, Any]], groups: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """Fold repeats in order; with ``groups``, stop before the entry opening one group more.

    Returns the groups and how many entries they consumed - every entry before
    that point is counted in exactly one group, none after it in any.
    """
    folded: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for index, entry in enumerate(entries):
        key = (entry.get("level"), entry.get("kind"), diagnostics._console_key(entry))
        if key not in folded:
            if groups is not None and len(order) >= groups:
                return [folded[item] for item in order], index
            folded[key] = dict(entry, count=1, first_seq=entry["seq"], last_seq=entry["seq"])
            order.append(key)
        else:
            folded[key]["count"] += 1
            folded[key]["last_seq"] = entry["seq"]
    return [folded[key] for key in order], len(entries)


def read(
    session: Any, collect: Callable[[], dict[str, Any]], *, levels: list[str] | None,
    contains: str | None, kinds: list[str] | None, limit: int, since_seq: int,
    since_ms: float | None, dedupe: bool, order: str,
) -> dict[str, Any]:
    """Ingest, then answer one page of the history (the caller holds the lock)."""
    collected = collect()
    ingest(session, collected)
    pool = [e for e in session.console_history if e["seq"] > max(0, int(since_seq or 0))
            and (since_ms is None or float(e.get("ts") or 0) >= float(since_ms))]
    pool = diagnostics.filter_console(pool, levels, contains, kinds, len(pool) + 1) if pool else []
    window = max(1, min(int(limit), 1000))
    descending = str(order or "asc").lower() == "desc"
    if dedupe and not descending:
        # A page folds only what it consumed, and the next page starts right after
        # that: taking last_seq of the last group skipped whatever lay between.
        matched = len(_fold(pool)[0])
        selected, consumed = _fold(pool, window)
        has_more = consumed < len(pool)
        last_seq = pool[consumed - 1]["seq"] if consumed else None
    else:
        pool = _fold(pool)[0] if dedupe else pool
        selected = (pool[::-1] if descending else pool)[:window]
        matched, has_more = len(pool), len(pool) > len(selected)
        last_seq = selected[-1]["seq"] if selected else None
    return {
        "entries": selected,
        "returned": len(selected),
        "matched": matched,
        "has_more": has_more,
        # The seq to pass back as since_seq for the next page (ascending order).
        "next_seq": last_seq if (last_seq is not None and not descending) else session.console_hseq,
        "history_seq": session.console_hseq,
        "history_dropped": session.console_history_dropped,
        "cursor_reset": bool(collected.get("cursor_reset")),
        "dropped": collected.get("dropped"),
    }
