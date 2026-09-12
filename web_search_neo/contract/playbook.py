"""Static contract data; no browser runtime imports."""

from typing import Any

_AUTOMATION_SKILL = {
    "name": "web-search-neo-browser",
    "goal": "Drive one named browser session with an inspect -> act -> verify loop.",
    "schema": {
        "rule": "Before guessing any optional parameter, call web_info(topic='action_schema', params={'action': name}).",
        "timeouts": "timeout_ms does not exist. The exact schema may name timeout_seconds for a wait or wait_seconds for post-action settling; unknown keys are refused.",
    },
    "loop": [
        {
            "step": "inspect",
            "calls": ["page_outline or page_elements", "page_text when content matters"],
            "rule": "Read the current DOM immediately before choosing a selector or final value.",
        },
        {
            "step": "act",
            "calls": ["fill/click_text/click/scroll/submit through web_action"],
            "rule": "Reuse session_id; after navigation or rerender discard old selectors and refs.",
        },
        {
            "step": "verify",
            "calls": ["page_text/page_elements, screenshot when pixels matter"],
            "rule": "Tool success means the event ran, not that the user's outcome happened.",
        },
    ],
    "current_chrome": {
        "setup": "Call setup_current_chrome if the companion is unavailable; show manual_steps verbatim.",
        "existing_tab": "browser_tabs -> attach_tab(tab_id, session_id) claims without navigating.",
        "new_tab": "open defaults to profile_mode=current and creates a background AI-group tab.",
        "session_rule": "Reusing session_id navigates that controlled page; use a different session_id when a second reference page must stay open.",
        "parallel_agents": "One session_id is one tab, and it is the only thing separating agents inside this MCP server: parallel agents must each choose their own, or they drive the same tab. Across servers the bridge refuses a tab another agent holds; inside one server nothing does.",
        "action_locators": "In current Chrome every action locator is plain CSS. Never send ref: or >>> to click, fill, wait, upload, submit.form_selector, or input; they are observation-only there.",
        "ownership": "close removes an agent-created tab but only detaches a claimed user tab.",
    },
    "focus": {
        "default": "Open, attach, navigate, input, and screenshot stay background; new temporary/persistent browsers default headless.",
        "opt_in": "Only web_action show(session_id) may request foreground focus.",
        "window_state": "show activates the tab or calls Page.bringToFront; it never minimizes, maximizes, restores, or resizes.",
    },
    "elements": {
        "scope": "page_elements counts the whole existing DOM, not only the viewport, including open shadow roots and same-origin frames.",
        "pagination": "Use limit plus offset per category; follow range.<category>.next_offset until null.",
        "filtering": "page_elements has no selector filter: get its lists and filter the returned links/fields/buttons yourself.",
        "duplicates": "When text repeats, match the exact links.href, value, or stable attribute from a fresh read; never choose by list index or a brittle nth-child path alone.",
        "semantic_click": "Use click_text for one visible interactive element when exact text plus role identifies it. Zero or multiple matches are refused; narrow with role or selector.",
        "refs": "Temporary/persistent/attach actions may use fresh refs and piercing paths where their schema says so. Current-Chrome actions may not. Every ref goes stale after its DOM epoch/rerender, so reread.",
        "dynamic": "Lazy/infinite/virtualized items do not exist yet: scroll, wait, then reread from offset=0 because the DOM may have changed.",
        "safety_cap": "collector_truncated.<category>=true means the 20000-item collector cap hid a tail; found is then not the true total.",
    },
"scroll": {
        "direction": "Positive delta_y moves down; negative delta_y moves up.",
        "point": "Omit x/y for viewport centre, or provide both to choose a nested scroll container.",
        "after": "Wait, then reread page_elements from offset=0 when scrolling may have changed the DOM.",
    },
    "screenshots": {
        "viewport": "Default; omit width/height to preserve actual size. Explicit size is Selenium-only and refused in current Chrome.",
        "full_page": "mode=full_page (or full_page=true); errors above 3840x10000 instead of returning a partial image.",
        "region": "mode=region requires page-CSS x/y/width/height and never resizes the browser.",
        "semantics": "Use DOM/text for labels and values. A background current-Chrome screenshot can be slow or unavailable when Chrome is not painting; that is not page failure.",
    },
    "visual_click": {
        "call": "Take a fresh viewport screenshot, then web_action pointer with pointer_action='click' and x/y in viewport CSS pixels.",
        "example": {"action": "pointer", "pointer_action": "click", "x": 640, "y": 360},
        "mapping": "Only viewport maps directly. If PNG size differs from reported viewport_width/height, scale each axis. Full-page/region can include offscreen pixels: scroll into view and recapture first.",
        "stale_guard": "After scroll, zoom, resize, navigation, animation, or rerender, recapture before clicking. Verify from fresh DOM/text.",
    },
    "forms": {
        "prepare": "Inspect fields/options, fill, then reread the form and its selected/current values.",
        "resource_rule": "Never infer a selected file/account/option from a URL parameter, remembered default, or prior page; verify the visible selected value in the live form.",
        "choice_widget": "Open it, reread its options, match exact visible text/value, click the visible option row rather than a hidden radio/input, then reread the collapsed control after rerender.",
        "question_rule": "A heading such as 'answer questions' is boilerplate, not proof that questions exist. Treat only live enabled form controls as questions; after a success message, stop.",
        "final_submit_guard": [
            "Keep submit_attempted=false. From a fresh DOM confirm the exact target and every critical selected value; ambiguity means do not submit.",
            "Set submit_attempted=true as the terminal submit is clicked exactly once.",
            "After any result or timeout, never click it again: inspect URL, text, elements, console/network. Stop on terminal success; only a clearly separate questionnaire may have its own later final submit.",
        ],
    },
}


# The playbook above is what a model reads once, at the start. These sections are
# what it reads when a particular job begins: each carries the part of the
# contract no schema can - the order of the calls, the check that goes between
# them, and the mistake a small model otherwise makes twice.
_SKILL_SECTIONS: dict[str, dict[str, Any]] = {
    "start": {
        "summary": "The first calls of any web task, and how to choose the surface for it.",
        "when": "At the beginning of a task, before opening anything.",
        "steps": [
            "Choose the surface. Reading public pages: search, then fetch_text - no browser at all. Anything that must be clicked, filled, or signed in: a browser session.",
            "Choose one session_id and keep it for the whole task; it names the one tab you control.",
            "web_info(topic='browser_status', params={'session_id': '<yours>'}) when the companion may not be connected.",
            "web_action open. profile_mode defaults to current: the user's own signed-in Chrome, in a background tab in the AI group.",
            "web_info(topic='page_outline') before choosing any selector.",
        ],
        "rules": [
            "web_info(topic='actions') lists every action with its required parameters. That plus action_schema is the whole tool surface; nothing else has to be remembered.",
            "Never guess an optional parameter name. web_info(topic='action_schema', params={'action': '<action or topic>'}) is the only place names, types, and defaults live, and it answers for observation topics too.",
            "A page you have not read in this turn is a page you do not know.",
        ],
        "avoid": [
            "Clicking a selector remembered from an earlier run or an earlier page.",
            "Calling show: it takes the user's focus. Only when they asked to watch.",
        ],
        "example": {
            "actions": [{"action": "open", "url": "https://example.com", "session_id": "work"}]
        },
    },
    "loop": {
        "summary": "Inspect, act once, verify from fresh state - the loop that makes a small model reliable.",
        "when": "Every mutation, without exception.",
        "steps": [
            "Inspect: page_outline for structure and refs, page_elements for selectors and form metadata, page_text when the wording is the answer.",
            "Act: exactly one intent per step - fill, click, click_text, scroll, submit.",
            "Verify: read again. Compare what changed against what you asked for.",
        ],
        "rules": [
            "A successful tool result proves an event was dispatched, not that the user's outcome happened.",
            "web_action returns success plus failure_count, stopped_early, and results[]. Read every results[i].success; a batch can be success=false with half its steps done.",
            "After navigation or a rerender, every ref and every remembered selector is stale. Re-read.",
            "fill reports field_values read back off the control: that, not success alone, is proof a field holds what you asked.",
        ],
        "avoid": [
            "Chaining act -> act -> act in one batch across a page that rerenders between them.",
            "Retrying the same failing selector. Read the page instead; the element usually moved or never existed.",
        ],
        "example": {
            "actions": [
                {"action": "fill", "session_id": "work", "fields": {"#email": "a@b.test"}},
                {"action": "click", "session_id": "work", "selector": "#next"},
            ]
        },
    },
    "locators": {
        "summary": "The three ways to name an element, and which actions accept which.",
        "when": "Every time a selector is chosen.",
        "steps": [
            "Prefer plain CSS. It works in every profile mode and for every action.",
            "Use find(params={'query': '...'}) when the markup is generated and no stable CSS exists.",
            "Use a ref: handle or an 'a >>> b' piercing path only for observation, or for actions in temporary/persistent/attach sessions.",
        ],
        "rules": [
            "In current Chrome (the default) every action locator is plain CSS. click, fill, wait, upload, submit.form_selector and the input actions refuse ref: and >>> there.",
            "frame_selector always names exactly one frame; ambiguous CSS is refused with the count. Coordinate actions (press_keys, pointer, touch, input, game_probe, pointer_lock) take plain CSS for it and nothing else.",
            "A ref carries the document epoch it came from. Pass it back exactly as returned; after navigation it resolves to nothing on purpose.",
            "Repeated visible text is not identity. Compare an exact href, control value, or stable attribute from a fresh page_elements read.",
        ],
        "avoid": [
            "nth-child, array index, or a long remembered CSS path as the only distinguishing feature.",
            "Copying the outline's '#host >>> #frame' path into game_probe or input.",
        ],
        "example": {
            "topic": "find",
            "params": {"query": "submit request", "session_id": "work"},
        },
    },
    "forms": {
        "summary": "Filling, choice widgets, uploads, and the one-shot terminal submit.",
        "when": "Any page with live form controls.",
        "steps": [
            "page_elements: read fields, their current values, and every <select>'s real options.",
            "fill by CSS. Check errors and field_values in the result before going on.",
            "For a custom dropdown or radio card: open it, re-read the options, click the visible option row (not its hidden input), then re-read the collapsed control.",
            "upload, or fill's files key, to attach a file - both replace the input's whole selection.",
            "Re-read every consequential value from the live DOM, then submit exactly once.",
        ],
        "rules": [
            "success is false whenever the errors map is non-empty. maxlength truncation and a rewritten value are failures; trimmed whitespace and a handler's case folding are not.",
            "Only a <select multiple> takes a list. Checkboxes take 1/yes/on/check or 0/no/off/uncheck. Date, time, range, and colour are set rather than typed, so a bad format is refused untouched.",
            "fill blurs each control it writes, so focus ends on the body: a following press_keys needs target_selector; submit needs no focus.",
            "challenge_detected means a challenge is blocking the page. Use the captcha action; never hammer clicks. captcha_widgets lists ones merely present and can be ignored.",
            "invisible_challenge_pending=true, or submit_blocked_by_challenge on a click, means a captcha with no box holds the form: the button sticks on 'Submitting…', nothing is sent and nothing is logged. Clear it with the captcha action; retrying or a new session changes nothing.",
            "upload_state=unconfirmed is not a failed upload: check the page for the file name and network for the request before attaching again. taken_by_widget means it worked and the widget emptied the input.",
            "A heading like 'answer questions' is boilerplate. Only live enabled controls are questions.",
        ],
        "avoid": [
            "Trusting a remembered default, a URL parameter, or a previous page for which file, account, or option is selected.",
            "Re-clicking a terminal submit after a timeout: the first click may already have succeeded. Inspect URL, text, elements, console, and network first.",
        ],
        "example": {
            "actions": [
                {
                    "action": "fill",
                    "session_id": "work",
                    "fields": {"#name": "Ada", "#role": "unity", "#remote": "yes"},
                }
            ]
        },
    },
    "macros": {
        "summary": "A macro is a JSON file of steps in the project's store; check it, preview it, replay it by name.",
        "when": "The second time you are about to drive the same flow, or when a project should own a reusable click path.",
        "steps": [
            "macro op='list' first: it answers which store is active (scope), where its files are (storage), and what is already saved.",
            "Write <name>.json in that storage directory: a JSON array of web_action step objects, or {name, description, steps, variables}. Put {{placeholders}} where values change and declare each one under variables.",
            "macro op='validate' name='<name>' project_root='auto' reads the file and dispatches nothing. Fix every error it reports; read the warnings.",
            "macro op='preview' name='<name>' variables={...} resolves every placeholder and returns the exact steps, still dispatching nothing. This is the review point.",
            "macro op='run' name='<name>' variables={...} replays it.",
        ],
        "rules": [
            "project_root='auto' finds the project from WEB_SEARCH_NEO_PROJECT_ROOT, then from an existing .web-search-neo directory, then from the repository root. Passing an absolute path is always exact.",
            "With no project_root at all, macros use the per-user store - a different set. Every macro answer reports scope, project_root, and storage, so check those before concluding a macro is missing.",
            "A macro file is one JSON file per macro, in <project_root>/.web-search-neo/macros/. The bare step list is a valid file; so is {name, description, steps, variables}. A README beside them states the format.",
            "A placeholder filling a whole string keeps its type, so a recorded number replays as a number. A run missing values names all of them at once.",
            "A macro cannot run another macro. Run them one after another.",
            "There is no write API. Creating, editing, renaming, copying and deleting a macro are file operations; op='validate' is how you check the result before a live run.",
            "Never put a {{placeholder}} inside a run_script script. It is pasted in as raw text, so any value with a newline or a quote breaks the JavaScript and the step fails with an opaque page error. Pass it through args and read arguments[0].",
            "A replay is a batch: check failure_count and every results[i].success. A saved click path is exactly as fragile as the page it was recorded from.",
        ],
        "avoid": [
            "Writing a project's macro into the per-user store and then looking for it with project_root, or the reverse.",
            "Running a consequential macro without a preview first.",
            "Putting domain rules in the server. The engine is neutral; the policy belongs in the project's macro files.",
        ],
        "example": {
            "actions": [
                {
                    "action": "macro",
                    "op": "run",
                    "name": "daily-report",
                    "project_root": "auto",
                    "variables": {"day": "2026-08-21"},
                }
            ]
        },
    },
    "guarded": {
        "summary": "The two-phase path for an action that cannot be taken twice.",
        "when": "A submit or click that sends, buys, applies, publishes, or otherwise cannot be undone.",
        "steps": [
            "Write a macro whose last step is exactly one consequential action: an explicit submit, or a click by plain CSS, or by exact text plus an explicit role.",
            "macro op='guarded_stage' with the guard object. Everything except that last step runs, and live assertions are checked against the fresh results.",
            "Review the staged result in full: the URL that was opened, the values that were read back, the assertions that passed.",
            "macro op='guarded_commit' once, with the checkpoint staging returned.",
        ],
        "rules": [
            "guard needs equal target_url and canonical_url, explicit allowed_hosts, an existing absolute resource_path the run uploads, its exact 64-hex resource_sha256, a 16-128 character idempotency_token, and at least one assertion.",
            "The digest is recomputed from the file's current bytes; a missing, malformed, or mismatched value fails closed before anything is dispatched.",
            "The checkpoint is consumed before the terminal action is dispatched, so an ambiguous timeout can never be retried automatically.",
            "Query parameters are part of target identity; only the URL fragment is ignored.",
            "The ledger is per store. Pass the same project_root to stage and commit.",
        ],
        "avoid": [
            "Guarding coordinates, trusted=true, substring text, ref handles, or piercing paths - all refused.",
            "Treating a successful commit as remote acceptance. It proves one attempt; collect confirmation separately with ordinary reads.",
        ],
        "example": {
            "actions": [
                {
                    "action": "macro",
                    "op": "guarded_commit",
                    "checkpoint": "guard-request-42-20260820",
                    "project_root": "auto",
                }
            ]
        },
    },
    "parallel": {
        "summary": "How two agents share one MCP server without driving each other's tab.",
        "when": "Any time subagents run at once, or the user has other work in the same Chrome.",
        "steps": [
            "Pick a session_id nobody else is using - your task name, not 'default'.",
            "web_info(topic='browser_tabs') then attach_tab to claim an existing tab by id, without navigating or moving it.",
            "Close your own session when finished.",
        ],
        "rules": [
            "One session_id is one tab, and it is the only boundary between agents inside one server. Two agents on the default id drive the same tab.",
            "browser_status reports shared_session when more than one caller is on an id.",
            "attach_tab on a tab another agent holds is refused with who holds it. Pick a different tab; do not retry.",
            "close removes a tab the agent opened and leaves a claimed user tab open. close_all ends every agent's sessions in this server, not only yours.",
        ],
        "avoid": [
            "close_all as cleanup while other agents are working.",
            "Assuming a session survived a Chrome restart or a companion self-update; it is dropped and must be opened again.",
        ],
        "example": {"topic": "browser_tabs", "params": {}},
    },
    "search": {
        "summary": "Getting current facts with no API key, and reading pages without a browser.",
        "when": "The task needs information rather than interaction.",
        "steps": [
            "web_action search with the query. Keep engine='duckduckgo', fallback=true, challenge_mode='fallback'.",
            "fetch_text on the promising URLs, or fetch_many for several at once.",
            "fetch_links when the target is a page's outgoing links rather than its prose.",
        ],
        "rules": [
            "Fallback across engines is automatic; search_status reports availability, latency, and cooldowns.",
            "challenge_mode='manual' hands a visible browser to the user for up to three minutes. The server never solves a CAPTCHA by itself.",
            "Plain http:// to a public host is refused; use https. Loopback and private addresses stay reachable.",
        ],
        "avoid": [
            "Opening a browser to read a page that fetch_text would return.",
            "Claiming a result is current without checking the page itself.",
        ],
        "example": {
            "actions": [
                {"action": "search", "query": "mcp browser automation", "engine": "duckduckgo"}
            ]
        },
    },
    "diagnostics": {
        "summary": "Why the page did not do what the click said it did.",
        "when": "An action reported success and the page did not change.",
        "steps": [
            "web_info(topic='console', params={'session_id': '<yours>'}) for uncaught errors with stack frames.",
            "web_info(topic='network', params={'only_errors': True, 'output': 'json'}) for failed requests and their ids.",
            "web_info(topic='network_body', params={'request_id': '<id>'}) for what the server actually answered.",
            "web_info(topic='execute_js') for state the DOM does not expose - localStorage, a framework store, a virtualised list.",
        ],
        "rules": [
            "network capture is armed when the session takes its tab; a tab claimed with attach_tab is recorded only from the claim onwards.",
            "dropped counts what the 500-entry buffer evicted: an empty list with a high dropped is not a quiet page.",
            "console pages with since_seq and keeps its own place, so it never competes with game_probe.",
            "replay_request re-sends a captured request from inside the page, with its cookies and origin - the cheapest way to ask whether a token still works.",
        ],
        "avoid": [
            "Using run_script where a form control read would do.",
            "Reading a screenshot to decide what a label says. Read the DOM.",
        ],
        "example": {"topic": "network", "params": {"session_id": "work", "only_errors": True}},
    },
    "games": {
        "summary": "Driving a canvas or WebGL page frame by frame, deterministically.",
        "when": "The page renders into a canvas and reads input per frame.",
        "steps": [
            "web_info(topic='game_probe') first; reuse its frame_selector for every input and render action.",
            "render mode='step' to freeze page time, or mode='throttled' with target_fps for continuous slow motion.",
            "One input action per frame with mixed key_actions and pointer_actions, or step {frames} to advance without input.",
            "screenshot or game_probe between batches.",
            "release_inputs, then render mode='normal', before handing back.",
        ],
        "rules": [
            "Both gated modes freeze performance.now() and Date.now(), so the game never measures your thinking time as deltaTime.",
            "A tapped key stays held for the whole released frame, which is what per-frame key polling needs.",
            "include_summary=false skips the post-action page read; step takes no wait_seconds and refuses one.",
            "Tapping a held key, pressing a touch id already down, or a point outside the window are refused before anything reaches the page: fix them, do not retry.",
            "After a failed input batch held_keys over-reports on purpose. Call release_inputs rather than reading it.",
        ],
        "avoid": [
            "Leaving render in step mode or keys held at the end of a task.",
            "Taking a screenshot as the first call in step mode; nothing has advanced yet, so it shows the old frame.",
        ],
        "example": {
            "actions": [
                {"action": "render", "mode": "step", "session_id": "game"},
                {
                    "action": "input",
                    "session_id": "game",
                    "key_actions": [{"key": "SPACE", "action": "tap"}],
                },
            ]
        },
    },
    "troubleshooting": {
        "summary": "Symptom, cause, and the call that fixes it.",
        "when": "Something returned an error or did nothing.",
        "cases": [
            {
                "symptom": "unknown parameter(s) [...]",
                "cause": "A parameter name was guessed. Every action refuses keys it does not publish.",
                "fix": "web_info(topic='action_schema', params={'action': '<name>'}) and use the names it lists.",
            },
            {
                "symptom": "Every action object needs an \"action\" key",
                "cause": "The step used type, name, tool, or command instead.",
                "fix": "Rename that key to \"action\".",
            },
            {
                "symptom": "A click succeeds and the page does not react.",
                "cause": "The handler checks event.isTrusted, or reads pointer position.",
                "fix": "Retry the same target with click trusted=true, then re-read the DOM.",
            },
            {
                "symptom": "A ref: locator resolves to nothing.",
                "cause": "The page navigated or rerendered, so the ref's document epoch is gone.",
                "fix": "Read page_outline again and use the new ref, or plain CSS.",
            },
            {
                "symptom": "A locator is refused in current Chrome.",
                "cause": "ref: and >>> are observation-only in the user's own Chrome.",
                "fix": "Give the action a plain CSS selector.",
            },
            {
                "symptom": "macro 'x' does not exist, but it was just saved.",
                "cause": "It was saved into the other store: a project store needs project_root on every call.",
                "fix": "macro op='list' and compare scope and storage; then pass the same project_root (or 'auto') that saved it.",
            },
            {
                "symptom": "macro needs value(s) for [...]",
                "cause": "The macro declares placeholders the run did not supply.",
                "fix": "Pass them all in variables, or give them defaults in the file's variables map.",
            },
            {
                "symptom": "The companion is disconnected.",
                "cause": "The Chrome extension is missing, stopped, or older than the bundled build.",
                "fix": "Read browser_status, call setup_current_chrome, and show any manual_steps to the user word for word.",
            },
            {
                "symptom": "challenge_detected is true.",
                "cause": "A CAPTCHA or interstitial covers the page.",
                "fix": "Use the captcha action. mode='wait' hands the visible browser to the user and returns when it clears.",
            },
            {
                "symptom": "The submit button says 'Submitting…' forever and no request is ever made.",
                "cause": "An invisible captcha is waiting for a token; the page's own handler waits with it, silently.",
                "fix": "Look for invisible_challenge_pending or submit_blocked_by_challenge in the answer, clear it with the captcha action, then click once more.",
            },
            {
                "symptom": "A screenshot times out in current Chrome.",
                "cause": "Chrome is not painting an obscured background window.",
                "fix": "Nothing. DOM reads and pointer actions still work; do not call show unless the user asked to watch.",
            },
            {
                "symptom": "Chrome keeps showing 'started debugging this browser' on agent tabs; closing it brings it back.",
                "cause": "That banner is Chrome's mandatory UI for chrome.debugger, not a defect: Cancel detaches the debugger and the next agent action re-attaches.",
                "fix": "Relaunch Chrome once with --silent-debugger-extension-api, or drive Selenium modes (profile_mode temporary/persistent/isolated), which show no banner. Full steps live in browser_status current_chrome.debug_banner.",
            },
            {
                "symptom": "The tab another agent is using keeps changing under me.",
                "cause": "Two agents share one session_id.",
                "fix": "Choose a distinct session_id and open or attach your own tab.",
            },
        ],
    },
}
