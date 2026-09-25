"""Static contract data; no browser runtime imports."""


_RECIPES = {
    "lookup": ["search {query}", "open {url}", "page_elements", "read the answer"],
    "form": [
        "open {url}",
        "fresh page_elements",
        "fill (custom option: click_text exact text + role); reread key values",
        "submit exactly once with submit_attempted=true",
        "verify URL/text/elements; after a timeout never submit again",
    ],
    "visual_click": [
        "screenshot; note viewport_width/height",
        "scale the image point to viewport CSS x/y",
        "pointer {pointer_action: click, x, y}",
        "verify with fresh DOM/text",
    ],
    "macro": [
        "macro {op: list, project_root: auto}",
        "write <name>.json there with {{placeholders}}",
        "macro validate, then preview {variables}",
        "macro run {name, variables}",
    ],
    "existing_tab": ["browser_tabs", "attach_tab {tab_id}", "page_elements", "act"],
    "canvas_text": ["focus the canvas", "type_text {text, mode: 'keys'}", "verify via screenshot"],
    "lazy_page": ["page_elements (offscreen DOM included)", "scroll {delta_y: positive}",
                  "page_elements; offset until next_offset=null"],
    "game": [
        "open {url}", "game_probe (frame_selector, canvas rect)", "render mode=step",
        "input {key_actions/pointer_actions} or step {frames}", "screenshot between batches",
        "release_inputs, then render mode=normal",
    ],
    "fps_game": ["pointer_lock acquire", "look {dx, dy}", "wait_frames {frames: 2}",
                 "screenshot {wait_frames: 1}; frame_health if slow"],
    "release_check": ["security_report {url}", "secret_scan {url}", "fix priority[] top-down, rerun",
                       "perf_report {url}", "skill section=audit"],
    "form_regression": ["test_run {url, steps: [{fill}, {click, expect: {text}}]}", "skill section=testing"],
}

_PITFALLS = [
    "Noisy page: page_elements category='interactive', visible_only, role/text_pattern (filter before paging).",
    "web_action success=true is not task success: check every results[i].success.",
    "Never repeat a click or submit on verified=false/null or after a timeout: read the page first - it may have gone through.",
    "Never guess optional names: call action_schema (timeout_ms does not exist).",
    "Selectors and screenshots go stale when the page changes: reread after it.",
    "Locators are plain CSS: never send ref: from page_outline or >>> to actions.",
    "Repeated text is not identity: match href/value/attributes, or click_text exact text + role.",
    "challenge_detected: a CAPTCHA blocks the page; use captcha, never hammer clicks.",
    "find low_confidence=true is guessing: re-query, do not click matches[0].",
    "New sessions open isolated; profile_mode='current' is the user's Chrome and logins (close closes only the agent's tabs).",
    "Parallel agents: own session_id each, agent_label on open; close_all scope='mine' ends only yours.",
    "Every cut is flagged: truncated/budget_truncated/has_more=true means more exists; continue at next_offset/next_seq.",
    "Stay in the background; show only when the user asks.",
    "render=step freezes the page until input/step runs; then release_inputs and set render normal.",
    "Pointer x/y are viewport CSS pixels (iframe: frame_selector + frame-local x/y).",
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
        "none"
    ),
}
