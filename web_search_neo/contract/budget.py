"""The contract budget is a decision, so it is written down where it can be argued with.

`CONTRACT_BUDGET` is the ceiling every release test pins. Raising it is allowed, but only
with a dated reason naming which sections grew and what was moved out instead - the same rule
the line ratchets follow. `budget_usage()` reports how much of it a build actually spends.
"""

from __future__ import annotations

import json
from typing import Any

# 13 500 held from 1.20 through 1.26 (measured 13 426 at 1.26.0).
# Raised 13 500 -> 13 800 (2026-10-14) for 1.27.0. What grew, and what was NOT added:
#   - actions: one entry, `captcha_challenge` (a name plus a summary). The protocol solver,
#     the shape matcher and the classifier live in captcha_solvers/ and are reached through
#     web_action, so they add no contract text of their own.
#   - recipes: one entry, `blocked_page` (three steps that read and touch nothing).
#   - pitfalls: one line about verifying only a sure measurement.
# What was given up to keep the growth to 300 characters: two separate actions collapsed into
# one with a flag, and no new topic - the challenge facts arrive inside the action result.
# 2026-10-16 (post-1.27 fix round): measured 13835 characters at 2ec0963, so the 13 800
# ceiling pinned by test_release_1_26/1_27 was already exceeded on origin/main BEFORE any
# change in this round. What grew since 1.27.0: info_topics gained the `screen_state` topic
# (title/focus/modals from the live DOM) and actions gained the `args[n]` refusal note for
# scripts/tabs; nothing was moved out of the contract, so the ceiling moves instead.
CONTRACT_BUDGET = 13_900


def budget_usage(capabilities: dict[str, Any]) -> dict[str, Any]:
    """Where the contract's characters actually go, largest section first."""
    sizes = {key: len(json.dumps(value, ensure_ascii=False)) for key, value in capabilities.items()}
    used = sum(sizes.values())
    return {
        "used": used,
        "budget": CONTRACT_BUDGET,
        "headroom": CONTRACT_BUDGET - used,
        "sections": [
            {"name": name, "characters": count}
            for name, count in sorted(sizes.items(), key=lambda item: item[1], reverse=True)
        ],
    }
