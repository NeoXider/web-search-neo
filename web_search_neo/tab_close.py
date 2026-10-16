"""Closing browser tabs with one retry when Chrome still lists them.

`close_current_chrome_tabs` decides every outcome against a fresh tab list
rather than the acknowledgement, because `chrome.tabs.remove` throws for tabs
the user already closed by hand. The mirror case - a remove that raced the
tab's own navigation and left it listed - used to come back failed although
the next attempt closes it, so the tabs still listed get exactly one more
attempt here. A tab listed after that is still reported failed, never assumed
gone.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def close_with_retry(
    remove: Callable[[int], Any],
    list_open: Callable[[], set[int]],
    targets: list[int],
) -> set[int]:
    """Remove every target, retrying once the ones still listed; return that set."""
    for tab_id in targets:
        try:
            remove(tab_id)
        except Exception as exc:  # noqa: BLE001 - the listing below decides, not this
            logger.warning("Closing tab %s failed: %s: %s", tab_id, type(exc).__name__, exc)
    still_open = set(list_open())
    retry = [tab_id for tab_id in targets if tab_id in still_open]
    if retry:
        time.sleep(0.5)
        for tab_id in retry:
            try:
                remove(tab_id)
            except Exception as exc:  # noqa: BLE001 - the listing below decides
                logger.warning(
                    "Closing tab %s on retry failed: %s: %s", tab_id, type(exc).__name__, exc
                )
        still_open = set(list_open())
    return still_open
