"""Late document titles: wait for the page's own label before answering.

A single-page app often sets ``document.title`` from JavaScript after the
document has finished loading, so a reader that answers at ``readyState``
``complete`` can still report ``title: ""`` - which reads as "this page has
no title" when the truth is "the page has not said yet".
"""

from __future__ import annotations

import time
from typing import Any

# How long a perception call waits for a script-set title before admitting it
# is still pending: long enough for a framework to hydrate after load, short
# enough that a page with genuinely no title does not stall the caller.
TITLE_WAIT_SECONDS = 2.5
TITLE_POLL_SECONDS = 0.2

# One tiny read per poll: the title plus the ready state that says whether the
# page even finished loading. Kept separate from the big perception scripts so
# a wait costs small round-trips instead of re-running a whole traversal.
_TITLE_PROBE_SCRIPT = (
    "return {title: String(document.title || ''),"
    " ready: String(document.readyState || '')};"
)


def settle_document_title(
    driver: Any,
    initial: object,
    timeout_seconds: float = TITLE_WAIT_SECONDS,
) -> tuple[str | None, bool]:
    """Wait briefly for a non-empty title; report ``pending`` instead of ``""``.

    Returns ``(title, title_pending)``: the page's title when it has one, else
    ``None`` with ``title_pending`` true. ``None`` - not ``""`` - because an
    empty string answers "this page has no title" while the truth is "the page
    has not set one yet", and the caller cannot tell those apart.
    """
    text = str(initial or "")
    if text.strip():
        return text, False
    try:
        budget = max(0.0, float(timeout_seconds))
    except (TypeError, ValueError):
        budget = TITLE_WAIT_SECONDS
    deadline = time.monotonic() + budget
    while True:
        try:
            probe = driver.execute_script(_TITLE_PROBE_SCRIPT)
        except Exception:
            # The document went away mid-read (navigation, close): there is no
            # title to wait for, and no page left to ask again.
            return None, True
        if isinstance(probe, dict):
            current = str(probe.get("title") or "")
            if current.strip():
                return current, False
        if time.monotonic() >= deadline:
            return None, True
        time.sleep(TITLE_POLL_SECONDS)
