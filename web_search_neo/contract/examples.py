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
}

_PITFALLS = [
    "For a noisy page use page_elements category='interactive', visible_only=true, enabled_only=true; narrow with role and text_pattern. Filters run before pagination. Follow range.interactive.next_offset for the next matching controls.",
    "web_action success=true is not task success: check failure_count and every results[i].success.",
    "Never guess optional names: call action_schema. timeout_ms does not exist; the exact action may use timeout_seconds or wait_seconds.",
    "page_elements takes no selector filter: read its category lists and filter the returned objects yourself.",
    "Selectors and screenshots die when the page changes; reread or recapture after navigation, rerender, scroll, zoom, resize, or animation.",
    "In current Chrome every action locator is plain CSS: never send ref: from page_outline or >>> to click/fill/wait/upload/submit/input. Other modes accept them only where action_schema says so; refs expire after rerender/navigation.",
    "Repeated text is not identity: compare the exact href/value/stable attribute from a fresh page_elements read, or use click_text with exact text plus role (it refuses ambiguity); never array index or nth-child alone.",
    "challenge_detected means a CAPTCHA is in the way: use captcha, never hammer clicks. captcha_widgets lists ones merely present; ignore those. invisible_challenge_pending is the one with no box: it holds the form, so a submit hangs with no request and no error until captcha clears it.",
    "find low_confidence=true means it is guessing: re-query with other words or a role, do not click matches[0].",
    "profile_mode=current drives the user's real Chrome; close closes a tab the agent opened and hands an attach_tab tab back. Closing tabs the agent never opened is close_tabs with their ids, not close - close on an attached tab detaches it and reports tab_closed=false.",
    "Parallel agents must each choose their own session_id: subagents share one MCP server, so two on the default drive one tab and navigate each other's page - browser_status reports shared_session. Pass agent_label on open so browser_status can say who is where, and so close_all (scope='mine' by default) ends only your own sessions; scope='all' still ends everybody's.",
    "page_elements, page_outline and find are bounded by max_chars as well as by limit: budget_truncated=true means the answer was cut to fit, and range[*].next_offset is where to continue. Raise max_chars only if your context can take it.",
    "Automation stays background-only by default and never changes window state. show is the sole foreground opt-in; call it only when the user explicitly asks to see the session.",
    "In render=step nothing moves until input or step runs, so a screenshot taken first shows the old frame.",
    "Always release_inputs after hold, and return render to normal before you finish.",
    "Pointer coordinates are viewport-local; inside an iframe pass frame_selector and frame-local x/y.",
    "For image-guided clicks use a fresh viewport screenshot and scale image pixels to reported viewport CSS size. Full-page/region pixels are not direct pointer coordinates.",
    "For a consequential submit verify all live choices, click once, and never retry after a timeout; inspect page state first because the first click may have succeeded.",
    "scroll delta_y is positive to move down and negative to move up; reread page_elements after scrolling a lazy/infinite page.",
    "Plain http:// to public hosts is refused; use https. Loopback and private addresses stay allowed.",
    "A macro saved without project_root lands in the per-user store, not the project's set. Every macro answer reports scope/project_root/storage: compare those before deciding a macro vanished.",
]


_EXAMPLES = {
    "search": {
        "actions": [
            {
                "action": "search",
                "query": "free browser automation MCP",
                "engine": "duckduckgo",
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
        "points=[{x,y}], swipe adds end_x/end_y; only release and cancel need "
        "none, and release may name ids instead to lift just those fingers"
    ),
}
