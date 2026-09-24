"""Static contract data; no browser runtime imports."""


_RECIPES = {
    "lookup": ["search {query}", "open {url}", "page_elements", "read the answer"],
    "form": [
        "open {url}",
        "fresh page_elements: exact href/target plus live controls",
        "fill/select; use click_text with exact text plus role for a custom option, then reread every consequential value",
        "set submit_attempted=true and terminal submit exactly once",
        "verify URL/text/elements; after timeout never submit again",
    ],
    "visual_click": [
        "viewport screenshot plus reported viewport_width/height",
        "scale image point to viewport CSS x/y if dimensions differ",
        "pointer {pointer_action: click, x, y}",
        "verify with fresh DOM/text; recapture after any layout change",
    ],
    "macro": [
        "macro {op: list, project_root: auto} (which store, which macros)",
        "write <name>.json in that store: the step list, {{placeholders}} for what changes",
        "macro {op: validate, name} until it reports valid",
        "macro {op: preview, name, variables} to review the final steps",
        "macro {op: run, name, variables}",
    ],
    "existing_tab": ["browser_tabs", "attach_tab {tab_id}", "page_elements", "act"],
    "canvas_text": ["focus the canvas", "type_text {text, mode: 'keys'} (any script)", "verify via screenshot"],
    "lazy_page": [
        "page_elements (already includes offscreen existing DOM)",
        "scroll {delta_y: positive}",
        "page_elements again; paginate offset until next_offset=null",
    ],
    "game": [
        "open {url}",
        "game_probe (read frame_selector and canvas rect)",
        "render mode=step",
        "input {key_actions/pointer_actions} or step {frames}",
        "screenshot or game_probe between batches",
        "release_inputs, then render mode=normal",
    ],
    "fps_game": ["pointer_lock acquire", "look {dx, dy}", "wait_frames {frames: 2}",
                 "screenshot {wait_frames: 1}; frame_health if slow"],
    "audit": ["open {profile_mode: 'isolated'}", "skill section=audit"],
    "test_run": ["skill section=testing"],
}

_PITFALLS = [
    "Noisy page: page_elements category='interactive', visible_only, enabled_only, role/text_pattern (filters run before pagination).",
    "web_action success=true is not task success: check failure_count and every results[i].success.",
    "Never repeat a click or submit on verified=false/null or after a timeout: read the page first - it may have gone through.",
    "Never guess optional names: call action_schema (timeout_ms does not exist).",
    "Selectors and screenshots die when the page changes; reread after navigation, rerender, scroll, resize or animation.",
    "In current Chrome every action locator is plain CSS: never send ref: from page_outline or >>> to click/fill/wait/upload/submit/input.",
    "Repeated text is not identity: compare exact href/value/attributes from fresh page_elements, or click_text with exact text plus role; never nth-child alone.",
    "challenge_detected means a CAPTCHA blocks the page: use captcha, never hammer clicks.",
    "find low_confidence=true means it is guessing: re-query, do not click matches[0].",
    "profile_mode=current drives the user's real Chrome: close closes only a tab the agent opened; other tabs close via close_tabs with their ids.",
    "Parallel agents must each use their own session_id and pass agent_label on open, so close_all (scope='mine') ends only their own sessions.",
    "Every cut is flagged: truncated/budget_truncated/has_more=true means more exists; continue at next_offset/next_seq.",
    "Automation stays in the background; show is the only foreground opt-in, only when the user asks.",
    "In render=step nothing moves until input or step runs; always release_inputs after hold and return render to normal.",
    "Pointer coordinates are viewport CSS pixels (iframe: frame_selector plus frame-local x/y); scale screenshot pixels by the reported ratio.",
]


_EXAMPLES = {
    "search": {
        "actions": [
            {
                "action": "search",
                "query": "free browser automation MCP",
                "engine": "brave",
                "fallback": True,
            }
        ]
    },
    "input": {
        "actions": [
            {
                "action": "render",
                "mode": "step",
                "session_id": "game",
                "frame_selector": "#game-frame",
            },
            {
                "action": "input",
                "session_id": "game",
                "frame_selector": "#game-frame",
                "key_actions": [
                    {"key": "W", "action": "hold"},
                    {"key": "S", "action": "release"},
                    {"key": "SPACE", "action": "tap"},
                ],
                "pointer_actions": [
                    {"action": "hover", "x": 640, "y": 360},
                    {"action": "wheel", "x": 640, "y": 360, "delta_y": -240},
                    {"action": "move", "x": 20, "y": -5, "coordinate_mode": "delta"},
                ],
            },
        ]
    },
    "pointer_lock": {
        "actions": [
            {"action": "pointer_lock", "operation": "acquire", "session_id": "fps"},
            {
                "action": "input",
                "session_id": "fps",
                "pointer_actions": [
                    {"action": "move", "x": 400, "y": 0, "coordinate_mode": "relative"}
                ],
            },
        ]
    },
    "macro": {
        "actions": [
            {
                "action": "macro",
                "op": "validate",
                "name": "open-report",
                "project_root": "auto",
            },
            {
                "action": "macro",
                "op": "run",
                "name": "open-report",
                "project_root": "auto",
                "variables": {"url": "https://example.com/reports/2026-08-21"},
            },
        ]
    },
    "script": {
        "actions": [
            {
                "action": "run_script",
                "session_id": "s",
                "script": "return JSON.parse(localStorage.getItem('state'))",
            }
        ]
    },
}


# The contract carries the examples that answer a shape question the action list
# cannot: how a search call, a mixed input batch, a pointer-lock pair, and a
# script call are actually written. Every other example stays in _EXAMPLES,
# where action_schema serves it on request and it costs nothing until asked for.
_CONTRACT_EXAMPLE_NAMES = ("search", "input", "pointer_lock", "script")


# Requirements no Python signature can carry: these parameters have a default,
# so the schema calls them optional, yet the handler refuses the call without
# them. Left unsaid, the most-used action ("input") looks like it takes nothing
# and a small model learns the real contract only from a runtime error.
_RUNTIME_REQUIREMENTS = {
    "input": "at least one of key_actions=[{key,action}] or pointer_actions=[{action,x,y}]",
    "touch": (
        "points=[{x,y}] (swipe: end_x/end_y inside each point); release and cancel need "
        "none (release may list ids)"
    ),
}
