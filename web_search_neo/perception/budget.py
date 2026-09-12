"""Leaf implementation independent of MCP registration."""

from __future__ import annotations
import json
from typing import Any

DEFAULT_RESPONSE_CHAR_BUDGET = 18_000
MIN_RESPONSE_CHAR_BUDGET = 2_000
MAX_RESPONSE_CHAR_BUDGET = 200_000


def _response_char_budget(max_chars: object) -> int:
    """Clamp a requested character budget; anything unreadable falls back to the default."""
    try:
        wanted = int(max_chars)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RESPONSE_CHAR_BUDGET
    return max(MIN_RESPONSE_CHAR_BUDGET, min(wanted, MAX_RESPONSE_CHAR_BUDGET))


def _measure(value: Any) -> int:
    """Size a payload the way the transport will: as JSON, non-ASCII unescaped."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _fit_lists_to_budget(
    payload: dict[str, Any],
    list_keys: tuple[str, ...],
    budget: int,
    finalize: Any = None,
) -> dict[str, Any]:
    """Trim the entry lists of a result until the whole answer fits ``budget``.

    Prefixes only. Every one of these topics pages with ``offset``, so keeping
    the first N of each list leaves ``next_offset`` meaning exactly what it
    meant; dropping from the middle would make the paging a lie.

    Entries are taken round-robin across the lists rather than filling the first
    one to the brim, because a page whose buttons matter is not helped by a
    budget spent entirely on its links.

    ``finalize`` is called with the trimmed payload whenever it changes. It is
    for the bookkeeping that depends on what survived - ``returned``, ``range``,
    ``next_offset`` - which grows the answer slightly as it fills in, and would
    otherwise push a payload that just fitted a few characters over the line it
    was trimmed to.

    Returns the report of what happened; the payload is trimmed in place.
    """
    lists = {key: list(payload.get(key) or []) for key in list_keys if key in payload}
    total_entries = sum(len(items) for items in lists.values())
    full_size = _measure(payload)
    if full_size <= budget or not total_entries:
        return {
            "chars_returned": full_size,
            "char_budget": budget,
            "budget_truncated": False,
        }
    # Everything that is not an entry list has to be paid for first: the counts,
    # the ranges and the page summary go out whatever happens, because they are
    # what tells the caller there is more to fetch.
    skeleton = {key: value for key, value in payload.items() if key not in lists}
    for key in lists:
        skeleton[key] = []
    remaining = budget - _measure(skeleton)
    costs = {key: [_measure(entry) + 1 for entry in items] for key, items in lists.items()}
    taken = {key: 0 for key in lists}
    index = 0
    progressing = True
    while progressing and remaining > 0:
        progressing = False
        for key in lists:
            position = taken[key]
            if position >= len(lists[key]):
                continue
            cost = costs[key][position]
            if cost > remaining:
                continue
            remaining -= cost
            taken[key] += 1
            progressing = True
        index += 1
        if index > 100_000:  # Defensive: no page has this many controls.
            break
    def _publish() -> int:
        for key, items in lists.items():
            payload[key] = items[: taken[key]]
        if finalize is not None:
            finalize(payload)
        return _measure(payload)

    size = _publish()
    # Give back what the bookkeeping took. One entry at a time from the longest
    # list, so the balance the round-robin built is kept; bounded because each
    # pass removes an entry and there are finitely many.
    while size > budget and sum(taken.values()) > 0:
        longest = max(taken, key=lambda key: taken[key])
        taken[longest] -= 1
        size = _publish()
    kept = sum(taken.values())
    return {
        "chars_returned": _measure(payload),
        "char_budget": budget,
        "chars_before_budget": full_size,
        "budget_truncated": True,
        "entries_returned": kept,
        "entries_before_budget": total_entries,
        "budget_note": (
            f"The full answer was {full_size} characters; {kept} of {total_entries} "
            f"entries were kept to stay inside max_chars={budget}. Read the rest with "
            "offset (see range[*].next_offset), narrow the request with the include_* "
            "flags, or raise max_chars if your context can take it."
        ),
    }


def _fit_text_to_budget(payload: dict[str, Any], text_key: str, budget: int) -> dict[str, Any]:
    """Clip one long text field so the whole answer fits ``budget``.

    Used where the bulk is a rendered blob rather than a list - the outline's
    text form - and the caller's lever is ``limit`` rather than ``offset``, which
    is what the note says instead of pointing at a page that does not exist.
    """
    full_size = _measure(payload)
    text = payload.get(text_key)
    if full_size <= budget or not isinstance(text, str) or not text:
        return {"chars_returned": full_size, "char_budget": budget, "budget_truncated": False}
    overhead = _measure({**payload, text_key: ""})
    room = max(0, budget - overhead)
    def _on_a_line_boundary(value: str) -> str:
        """Half a node is worse than one node fewer."""
        newline = value.rfind("\n")
        return value[:newline] if newline > len(value) // 2 else value

    clipped = _on_a_line_boundary(text[:room])
    payload[text_key] = clipped
    # A character of text is not a character of JSON - a newline is two, a quote
    # is two, and an outline is mostly newlines - so the first cut is an estimate
    # and this loop is what makes the promise true. It converges quickly because
    # each pass removes the whole measured excess.
    for _ in range(8):
        overshoot = _measure(payload) - budget
        if overshoot <= 0 or not clipped:
            break
        clipped = _on_a_line_boundary(clipped[: max(0, len(clipped) - overshoot)])
        payload[text_key] = clipped
    return {
        "chars_returned": _measure(payload),
        "char_budget": budget,
        "chars_before_budget": full_size,
        "budget_truncated": True,
        "budget_note": (
            f"The full answer was {full_size} characters and was clipped to stay inside "
            f"max_chars={budget}. Ask for fewer nodes with limit, scope the read with "
            "frame_selector, or raise max_chars if your context can take it."
        ),
    }
