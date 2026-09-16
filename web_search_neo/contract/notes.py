"""Static contract data; no browser runtime imports."""


_INFO_TOPICS = {
    "capabilities": "This contract: topics, actions, recipes, and pitfalls.",
    "skill": "Built-in automation playbook; params.section='<name>' returns one section in full.",
    "actions": "Every action with its summary and required parameters; params.group narrows it.",
    "action_schema": "Full JSON Schema for one action or topic; pass params.action.",
    "page_outline": "Roles, names, states, refs, and boxes - start looking here.",
    "page_text": "Readable text of the rendered page; params.mode=main|full.",
    "element_text": "One element's whole content: params.selector (CSS/ref/piercing), params.mode=text|html|outer|both, params.full_text for overflow-unclipped text.",
    "find": "Find an element by meaning: params.query='submit request'.",
    "page_elements": "Links, forms, fields, buttons by selector: CSS, '#host >>> #leaf' in a shadow root or frame, or '' when none is unique.",
    "console": "console.log/warn/error and uncaught errors; params.levels, params.contains.",
    "network": "HTTP requests with status, type, ms, size; params.only_errors=true.",
    "network_body": "One response body; params.request_id is the id from a network read with output='json'.",
    "execute_js": "Run a JavaScript snippet in a session's page and read its return value.",
    "screenshot": "PNG viewport, full-page, or exact page-region image.",
    "game_probe": "Canvas/WebGL/iframe surfaces, FPS, focus, console, held input.",
    "browser_status": "Chrome availability, one session's state, and the roster of every session this server holds: owner label, tab, last page, idle time, busy flag, N of M in use.",
    "browser_tabs": "Tabs open in the user's Chrome, with ids and groups.",
    "search_status": "Search providers, live availability, latency, cooldowns.",
}

# Repeated verbatim on every hot-path action, because a model reads one schema.
_HOT_PATH_SPEED = (
    "wait_seconds=0 (the default) and include_summary=false skip the post-action "
    "page read; both matter when you drive a game frame by frame."
)

# step is the one hot-path action without wait_seconds, and the argument check
# refuses an unknown key - so the shared note above advertised a hard error.
_STEP_SPEED = (
    "include_summary=false skips the post-action page read; step takes no "
    "wait_seconds and refuses one."
)

# frame_selector always names exactly one frame - ambiguous CSS is refused with
# the count everywhere. Only the accepted form differs: whatever aims by
# coordinate needs the frame's box in the top document, so it takes CSS alone.
_FRAME_ANY = "Frame to work inside: CSS, a ref: handle, or '#host >>> #inner'; ambiguous CSS is refused with the count."
_FRAME_CSS = "Frame to work inside: plain CSS only - a ref or a '>>>' path is refused before any event is sent; ambiguous CSS is refused with the count."

# Keyed by action name or info topic name; both are described from here.
_ACTION_NOTES = {
    "macro": {
        "ops": (
            "list/show read the store; validate checks a file without running it; "
            "preview resolves its placeholders and dispatches nothing; run replays it; "
            "guarded_stage/guarded_commit are the two-phase path for an action that must "
            "happen at most once. A macro is a JSON file: create, edit, rename and delete "
            "it with ordinary file operations."
        ),
        "storage": (
            "project_root='auto' resolves WEB_SEARCH_NEO_PROJECT_ROOT, then an existing "
            ".web-search-neo directory, then the repository root, walking up from the "
            "working directory. An absolute path is exact. With neither, macros use the "
            "per-user store, which is a different set: every answer reports scope, "
            "project_root and storage so the two are never confused."
        ),
        "files": (
            "One JSON file per macro under <project_root>/.web-search-neo/macros/. The "
            "bare step list is a valid file, and so is {name, description, steps, "
            "variables}; a README written beside them states the format. Write and edit "
            "them directly - a file that cannot be read is reported as broken by "
            "op='list' instead of hiding the ones beside it."
        ),
        "variables": (
            "{{placeholder}} marks what changes between runs; declare each one under "
            "'variables', where its value is the default. op='show' says what a macro "
            "wants, and a run missing values names all of them at once. A placeholder "
            "filling a whole string keeps the type of its value. Never put one inside a "
            "script: it is pasted in as raw text and breaks the JavaScript. Pass it "
            "through args - op='validate' refuses this one as an error."
        ),
        "validate": (
            "op='validate' reads the file and dispatches nothing: unknown actions, "
            "missing or unknown parameters, placeholders inside a script, placeholders "
            "no 'variables' entry declares. Warnings for a declared-but-unused variable, "
            "steps that drift between session ids, and a macro that ends without reading "
            "its result back. Each finding carries index, error and fix."
        ),
        "preview": (
            "op='preview' resolves every placeholder and returns the exact steps with "
            "executed=false, dispatching nothing. It is the review point before a "
            "consequential run."
        ),
        "limits": (
            "A macro cannot run another macro; run them in order. A replay reports like a "
            "batch, so check failure_count and every results[i].success."
        ),
    },
    "fill": {
        "results": "filled took your value, field_values answers for every selector you sent including the failures, errors maps selector to the driver's own message, success=false if errors is non-empty.",
        "field_values": "null means nothing could be read back - the selector matched nothing, or the control is gone. A refused control reports what it still holds, so you can see the write did not land.",
        "checkbox": "1|yes|y|on|check|checked or 0|no|n|off|uncheck|unchecked|''; anything else is refused, and field_values reports a JSON boolean.",
        "multi_select": "A <select multiple> reads back as a list and only it takes a list of values; a scalar replaces its whole selection rather than adding to it.",
        "sanitisation": "The browser's own tidying is accepted - trimmed whitespace on email/url, CRLF, a handler's case folding - while maxlength truncation and a rewritten value still fail.",
        "typed_controls": "date/time/datetime-local/month/week/range/color are set, not typed: an unparseable value is refused without touching the control and the error names the format.",
        "contenteditable": "TipTap/ProseMirror/Slate/Quill and Gmail's body are written as a real edit: the content is selected and typed in through the browser's input channel, a line at a time with a soft break (Shift+Enter - never Enter, which sends in a chat composer) between them, so paragraphs survive. The read-back is innerText.",
        "contenteditable_limits": "An editor that folds the breaks anyway is named as such in errors; the fix is a real paste - run_script with user_gesture=true and navigator.clipboard.writeText(text), then Ctrl+V through input. Telegram Web (#editable-message-text, Teact) never updates its own state from a write at all - its send button stays a microphone - so paste there always.",
        "files": "A file input is refused in fields; pass files={selector: path}, which replaces the input's selection rather than adding to it. upload_states/upload_notes appear when a widget emptied the input; they mean what upload's upload_state means.",
        "blur": "By default every control written is blurred (blur_after=true), which is how the last field fires its change event - so focus ends on the body and a following press_keys needs target_selector to reach a field. Pass blur_after=false to leave focus in the last control, and typing=true to emulate keystroke-by-keystroke input for masked/autocomplete fields.",
        "frame_selector": _FRAME_ANY,
    },
    "local_storage": {
        "ops": "op is read (whole store without key, one value with key), write (needs key and value), or delete (needs key); kind is local (localStorage) or session (sessionStorage, cleared when the tab closes).",
        "scope": "Reads and writes the open page's own Web Storage for its origin; a typo in kind is refused rather than written to the other store.",
    },
    "context": {
        "scope": "Owned browsers only (temporary/isolated/persistent): user_agent, timezone, locale, geolocation and viewport size. On current/attach the same call is refused - one real profile means one fingerprint, so open profile_mode='isolated' (one account = one isolated session) instead.",
    },
    "upload": {
        "replaces": "The input is cleared first: this sets its selection to exactly file_paths. Two files means one call with two paths; a second call discards the first file.",
        "files_uploaded": "{selector: [names]}, read back off the input - the same shape fill returns.",
        "upload_state": "attached = the input holds the files. taken_by_widget = a Dropzone-style widget emptied the input and the page names the file or posted it, so it worked. unconfirmed = nothing vouches either way; that is not a refusal, so read note, check page_text/elements for the name and network for the request before attaching again. success is false only when the attach itself failed.",
        "frame_selector": _FRAME_ANY,
    },
    "submit": {
        "validation": "validation_passed=false means native validation blocked it and nothing was sent; validation_errors names the fields by id/name, never match on the message.",
        "proof": "submit_triggered is submit_event_fired or navigation_observed; submit_default_prevented marks a handler that cancelled the navigation on purpose. Neither says the server accepted it.",
        "evidence": "submit_evidence is a sentence naming what the verdict rests on; new_tab_opened=true means a target='_blank' result is in a tab this session does not own, so the url and title here are still this page's.",
        "submit_selector": "Plain CSS only, unlike form_selector.",
        "frame_selector": _FRAME_ANY,
    },
    "click": {
        "choice": "Provide exactly one target: selector (CSS, ref handle, or 'a >>> b' piercing path) for the element, text with role for a strict rendered-text match that refuses ambiguity, or x+y for a viewport CSS-pixel point. In current Chrome a CSS selector must be plain CSS, never ref: or >>>.",
        "text": "text clicks the one visible interactive element whose rendered text matches; role narrows by ARIA role and exact=false switches to substring matching. Zero or several matches are refused with samples, so narrow with role or selector rather than retrying.",
        "coords": "x/y click the viewport point in CSS pixels, useful for image-guided clicks from a fresh screenshot. Scale image pixels to reported viewport width/height and recapture after any layout change.",
        "stalled_submit": "submit_blocked_by_challenge=true means the click produced no network request at all while an unsolved invisible captcha sits on the page: clicking again cannot help, clear it with the captcha action first.",
        "trusted": "trusted=true sends a real trusted mouse sequence at the element's centre (scrolled into view first), so pages that require isTrusted events or read pointer position behave as if a user clicked. Use it when a synthetic click is ignored. It lands on whatever is at that point, like a human pointer.",
        "no_box": "trusted=true needs a visible box; an element with zero size refuses with a clear error instead of falling back silently.",
        "frame_selector": _FRAME_ANY,
    },
"run_script": {
        "scope": "Runs in the top document of the session's current tab; there is no frame_selector - address a frame from inside the script if needed.",
        "args": "args arrive as arguments[0..n]; only JSON-serialisable values can cross into the page.",
        "body": "Your script is the body of a wrapper function: end with `return <value>;` for value to be returned - an expression statement or an IIFE without an outer return comes back as null. A promise result needs await_promise=true.",
        "result": "value is the JSON-serialisable return value; a promise is awaited when await_promise=true (Chrome bridge driver). Long strings are clipped at 200k characters and reported as {clipped, length, head}. attempts reports how many tries the call took.",
        "retry": "Single-shot by default (retry_on_uncaught=false). An exception may follow a completed mutation. Only enable retries for scripts safe to repeat; retries=2, retry_delay_ms=300. wait_ready=true additionally settles readiness first.",
        "safety": "This is raw page-side JavaScript: it can navigate, mutate, or delete state. Prefer fill/click/pointer for input-shaped work and reserve scripts for state only the page holds (localStorage, virtualised rows, framework stores).",
    },
    "click_text": {
        "strict": "Clicks only when exactly one visible interactive candidate matches. Zero or multiple matches are refused with samples; narrow using role or selector.",
        "matching": "exact=true compares whitespace-normalized rendered text. exact=false is substring matching and should normally be paired with role.",
        "selector": "Optional CSS candidate filter, not the click target. Omit it to search buttons, links, labels, options, checkboxes, radios, tabs, and menu items.",
        "background": "hit_test_unavailable=true can occur in an unpainted background tab; the unique DOM target is still clicked and must be verified from fresh state.",
        "frame_selector": _FRAME_CSS,
    },
    "captcha": {
        "paid": "mode='auto' waits for a human. A paid solving service is used only with mode='solve' (or auto with WEB_SEARCH_NEO_CAPTCHA_AUTO_SOLVE=1) and always needs WEB_SEARCH_NEO_CAPTCHA_KEY.",
        "timeout": "timeout_seconds is capped at 300; timeout_note says when the cap applied.",
    },
    "wait": {
        "state": "present|visible|clickable; timeout_seconds defaults to 10 and is capped at 300 s (timeout_note says when).",
        "script": "Pass script (a JS expression, e.g. \"window.__hydrated === true\") instead of selector to poll a hydration/framework condition atomically server-side; selector and script are mutually exclusive. poll_ms sets the poll interval.",
        "sleep": "With neither selector nor script the call is a plain sleep for timeout_seconds and returns success.",
        "frame_selector": _FRAME_ANY,
    },
    "find": {
        "scores": "match_score is how well the query matched that element alone; score adds ranking context. low_confidence is derived from match_score only.",
        "role": "A filter, not a nudge: another role is dropped. Under low_confidence the guesses come from the unfiltered set, so a wrong role can reappear there.",
        "ambiguous": "The top two matched and ranked equally, so document order alone chose; say which you mean instead of taking matches[0].",
        "limits": "visible_only=true by default; limit is clamped to 25.",
    },
    "page_elements": {
        "selector": "CSS in the top document, '#host >>> #leaf' inside an open shadow root or same-origin frame, and '' when nothing addresses the element uniquely.",
        "scope": "Always the whole page: this topic takes no frame_selector, and it is the only read topic reporting challenge_detected/captcha_widgets.",
        "duplicates": "For repeated labels, compare each returned link href or stable value/attribute. Never choose by array index or nth-child alone.",
        "captcha_scan_incomplete": "true means the captcha walk stopped early, so an empty captcha_widgets is not proof there is none. Every page summary carries this key.",
        "invisible_challenge_pending": "true means a captcha with no box - an invisible Turnstile and the like - is on the page with an empty token field. It blocks the form, not the page, so challenge_detected stays false; invisible_challenge names the vendor and the evidence. Clear it with the captcha action before submitting. Every page summary carries this key too.",
        "contenteditable": "Only [contenteditable=\"true\"] is listed; a bare contenteditable attribute is a field to page_outline and invisible here.",
        "pagination": "The whole existing DOM is counted before each category is sliced. Use offset plus limit, then follow range.<category>.next_offset until null; reread after scrolling a lazy/infinite page.",
        "limits": "limit is clamped to 1000 per top-level category and offset to 0-20000. collector_truncated.<category>=true means the 20000-item safety cap was hit and found is only the collected prefix. include_forms=false also omits fields.",
    },
    "page_text": {
        "fallback": "mode='main' on a page that is one big form would be empty, so it re-reads the whole body and says so with fallback_used=true and mode_used='full'.",
    },
    "network": {
        "id": "The default output='text' carries no ids. Pass output='json' and hand that row's id to network_body as request_id.",
    },
    "execute_js": {
        "scope": "Top document of the session's current tab only; reach into a frame from inside the script when you must.",
        "result": "value is the JSON-serialisable return value, promise-awaited on the Chrome bridge driver; strings over 200k characters come back as {clipped, length, head}.",
        "prefer_actions": "Use fill/click/pointer for anything a user gesture should do; a script cannot simulate a trusted interaction.",
    },
    "game_probe": {
        "frame_selector": _FRAME_CSS,
        "why_strict": "It sends nothing, but the canvas rects it reports are aimed at with this same string, so it is as strict as the input actions.",
    },
    "open": {
        "profile_mode": {
            "current": "the user's signed-in Chrome through the companion extension (default)",
            "auto": "current, falling back to a headless temporary profile",
            "temporary": "clean disposable profile",
            "isolated": "disposable separate browser profile and storage, with per-session user_agent/timezone/locale/geolocation; does not guarantee an unlinkable hardware or network fingerprint",
            "persistent": "durable server-owned profile, keeps logins",
            "attach": "a Chrome you started with a DevTools port",
        },
        "headless": "temporary/persistent default headless; headless=false explicitly opens a visible window. attach preserves the launcher's window mode when omitted. headless=true is refused with current and makes auto resolve straight to temporary.",
        "claimed_tab": "open on a session claimed by attach_tab does not navigate the user's tab: it takes a new one and reports the released id as left_claimed_tab.",
    },
    "show": {
        "only_foreground": "This is the only action allowed to request browser or OS foreground focus; never call it unless foreground was explicitly requested.",
        "window_state": "Current Chrome activates its tab/window; Selenium and attach use Page.bringToFront. No minimize, maximize, restore, resize, or other window-state request is sent.",
        "result": "focus_requested=true confirms the explicit request was sent; warning names the user interruption risk.",
    },
    "attach_tab": {
        "ownership": "Refused, naming the holder, when another agent is already driving that tab. Pick another tab or open your own; do not retry. A tab claimed by another session in this server is refused the same way: it is busy, so observe it read-only through its own session or open your own tab.",
        "capture": "Console and network are recorded from the claim onwards; whatever the tab did before it was claimed is unrecoverable.",
        "badge": "The tab gets the agent-activity favicon dot while driven (gone after 5 quiet minutes); a dotted tab in the strip is agent-held - do not act on it from another session.",
    },
    "close_tabs": {
        "ids": "tab_ids are the ids from web_info(topic='browser_tabs'). There is no close-everything switch on purpose: closing a tab cannot be undone, so each one is named.",
        "refusals": "Pinned tabs and tabs another agent is driving are skipped rather than closed - the first is the set the user keeps on purpose, the second would pull the page out from under a running session. include_pinned / include_claimed override each. Every skip says which rule it hit.",
        "outcome": "closed / failed / skipped are decided against a fresh tab list, not against Chrome's acknowledgement: tabs.remove reports failure for a tab the user had already closed by hand. A tab that was already gone lands in skipped with already_gone=true, because that is the outcome the caller wanted.",
        "sessions": "sessions_dropped names sessions that were sitting on a closed tab and have been forgotten, so a later action cannot dispatch at a tab id Chrome has since reused.",
    },
    "close": {
        "tab": "Closes a tab the agent opened, reported as tab_closed; an attach_tab tab is only detached and stays open unless close_tab=true. To close tabs the agent never opened, use close_tabs with their ids.",
        "browser_gone": "browser_gone=true with a note means the Chrome it was opened in is gone: nothing was sent, because the tab id now names someone else's tab. Open again, do not retry close.",
    },
    "close_all": {
        "browser_gone": "A list of session ids left alone because their Chrome is gone; closed_all stays true, since nothing of ours was left to leak.",
        "scope": "Defaults to scope='mine': only the sessions opened with your agent_label (or, with no label, the unlabelled ones). kept_sessions names what was left running and who owns it. scope='all' closes every agent's sessions in this MCP server - that is the old behaviour, and it ends other subagents' work.",
    },
    "mock": {
        "pattern": "url_pattern is a CDP-style wildcard ('*' matches anything) matched against the full request URL; the first matching stub wins and everything else reaches the network untouched.",
        "mechanism": "On the Chrome companion the stub is answered inside the extension over the CDP Fetch domain; on Selenium browsers it is an in-page fetch/XHR patch. Either way the page sees the stubbed status/headers/body as the real response.",
        "lifetime": "Stubs survive navigation and service-worker suspension; clear removes one pattern or all. Detaching or closing the tab ends interception. Selenium covers page fetch/XHR only, not workers or subresources; use mock_list to inspect configured rules.",
    },
    "reload": {
        "fallback": "When the installed companion predates Page.reload it refuses the method; the call then serves the reload via same-URL navigation and says so with reload_fallback=navigate plus a companion_note telling the user to press Reload at chrome://extensions. browser_status always carries the shipped allowlist size/hash (allowed_cdp_methods_size/hash) so the skew is visible before the call.",
    },
    "browser_status": {
        "roster": "sessions lists every session in this server with agent_label, current_tab_id, tab_group, last_url/last_title, created_at, last_used_at, idle_seconds and busy; sessions_open/max_sessions/sessions_free are the occupancy, and max_sessions_source says whether the cap came from the environment, the companion popup, or the default. last_url/last_title are where a session was last seen, not a fresh read - another agent's tab is never touched to answer this.",
        "browser_gone": "session_open=false with browser_gone=true means the session was dropped because its Chrome restarted; follow the 'next' field and open the page again.",
        "co_tenants": "sessions_in_use names the sessions another caller of this server is inside right now; shared_session=true means this very session is one of them. current_chrome.daemon.clients counts the MCP servers sharing the browser and .claims lists every tab any of them drives, with mine telling ours apart.",
    },
    "browser_tabs": {
        "ownership": "A tab another agent is already driving carries driven_by (and driven_by_me when it is this server's). Attaching to one of those is refused, so pick an unmarked tab or open your own.",
    },
    "input": {
        "key_action": {"key": "W|SPACE|ARROW_LEFT|F5|NUMPAD1|...", "action": "tap|hold|release"},
        "pointer_action": {
            "action": "click|double_click|hover|move|drag|press|release|wheel",
            "coordinates": "x/y absolute, deltas when coordinate_mode=delta or relative",
            "wheel": "pass delta_x/delta_y; x/y is where the wheel is scrolled",
        },
        "atomicity": "Every change lands before the single released frame; taps stay down for it.",
        "limits": "At most 16 key and 16 pointer entries.",
        "refusals": "Raised before anything is sent: tapping a key this session already holds (release it first), and a point that maps outside the window or onto another element, which is named.",
        "held_keys": "After a failed batch this over-reports on purpose - any event may have landed - so call release_inputs rather than trusting the list.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
    "type_text": {
        "text": "Non-empty string; inserted whole in one command instead of key by key.",
        "selector": "Optional CSS target, located and focused first. Omit it to type into whatever already has focus - fill and click leave their target there.",
        "note": "The bridge driver sends CDP Input.insertText per call, so controlled inputs (React) see one composed edit instead of a key event storm.",
        "speed": _HOT_PATH_SPEED,
    },
    "press_keys": {
        "key_action": "tap|hold|release",
        "note": "The dispatcher key is 'action'; the keyboard verb is 'key_action'.",
        "hold_frames": "With key_action='tap' in render=step, the key stays down for N released frames - so one call releases hold_frames per repeat, not one. Read frames_advanced.",
        "limits": "1-8 keys, repeat 1-50, hold_frames 1-30.",
        "refusals": "Tapping a key this session already holds is refused before anything is sent; release it first, or drop the tap.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
    "render": {
        "modes": ["normal", "throttled", "step"],
        "determinism": (
            "step freezes performance.now()/Date.now() and queues timers, so each "
            "released frame is a fixed frame_delta_ms. Without it a game measures "
            "your thinking time as its frame delta."
        ),
        "monotonic": "Stepping carries page time ahead of wall time and returning to normal keeps the gap, so the page clock never goes backwards - and never matches yours again.",
        "frame_selector": _FRAME_ANY,
    },
    "pointer": {
        "pointer_action": "click|double_click|hover|move|drag|press|release|wheel",
        "note": "The dispatcher key is 'action'; the pointer verb is 'pointer_action'.",
        "visual_click": "For image-guided clicking, take a fresh viewport screenshot and send pointer_action='click' with viewport CSS x/y. If PNG dimensions differ from viewport_width/height, scale both axes. Full-page/region coordinates do not directly map to the viewport.",
        "stale_image": "After scroll, zoom, resize, navigation, animation, or rerender, recapture before using image coordinates; then verify with fresh DOM/text.",
        "refusals": "A point that maps outside the window, or onto an element covering the frame there, is refused with the blocker named - never clamped or dropped.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
"scroll": {
        "direction": "positive delta_y scrolls down; negative delta_y scrolls up",
        "point": "omit x/y for viewport centre; provide both to scroll the container painted under that point",
        "selector": "pass selector (CSS, ref handle, or a 'a >>> b' piercing path) to scroll the container that holds that element: it is brought into view first, then the wheel lands on its centre; selector and frame_selector are mutually exclusive",
        "result": "before/after are selected document window metrics; a nested container can move while those page metrics stay unchanged",
        "lazy_pages": "page_elements already sees offscreen controls in the existing DOM. Scroll only to materialise lazy/infinite content, then read page_elements again.",
        "frame_selector": _FRAME_CSS,
    },
    "screenshot": {
        "modes": "viewport (default), full_page, region; full_page=true remains an alias for mode='full_page'",
        "viewport": "omit width/height to preserve the actual viewport. An explicit pair resizes Selenium sessions exactly and is refused in current Chrome.",
        "region": "requires x/y/width/height in page CSS pixels, captures without resizing, and works in current Chrome and Selenium",
        "full_page": "captures the whole current layout up to 3840x10000; an oversize page errors instead of returning an unlabelled partial image",
        "background": "A current-Chrome screenshot can wait or fail while its window is obscured because Chrome is not painting pixels. DOM/actions still work; do not use pixels to prove labels or selected values.",
    },
    "pointer_lock": {
        "operation": "acquire|release|status",
        "note": "After acquire, move with coordinate_mode='relative'.",
        "movement": "Each move reports exactly the delta you sent; nothing recentres.",
        "frame_selector": _FRAME_CSS,
    },
    "touch": {
        "touch_action": "tap|press|move|release|swipe|cancel",
        "points": [{"x": 0, "y": 0, "id": 0, "end_x": 0, "end_y": 0}],
        "partial_release": (
            "release with points=[{id}] lifts only those fingers and leaves the "
            "rest down; release with no points lifts all of them."
        ),
        "refusals": "Pressing an id that is already down is refused - Chrome ignores it - so move that finger or release it first; a point landing outside the window or on another element is refused and the blocker named.",
        "frame_selector": _FRAME_CSS,
        "speed": _HOT_PATH_SPEED,
    },
    "step": {
        "frames": "1-120. step has no frame_selector - it reuses the one render stored - and fails unless render mode=step is active.",
        "speed": _STEP_SPEED,
    },
    "setup_current_chrome": {
        "opens_no_page": (
            "It publishes the shared secret and reads state. Its one browser effect "
            "is reloading a stale companion; self_update reports done, unsupported "
            "or timeout. Show manual_steps to the user verbatim when they are "
            "present; they contain the absolute folder to pick."
        ),
        "why": (
            "No program can add an unpacked extension to a Chrome that is already "
            "open, so the first install belongs to the user. Updates after that do "
            "not: the companion re-reads its own folder when asked."
        ),
        "wait_seconds": "Raise it right after the user pressed Load unpacked.",
    },
    "http_request": {
        "method": "GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS; anything else is refused.",
        "headers": "Optional {name: value} map sent with the request.",
        "query": "Optional {name: value} map sent as the URL query string.",
        "body": "Raw str body; mutually exclusive with body_json.",
        "body_json": "Any JSON value sent as the body; adds Content-Type: application/json unless headers set one.",
        "timeout": "timeout_seconds (capped at 120) bounds each read, the whole body gets twice that; DNS/timeout failures raise like fetch_text.",
        "save_to": "Writes raw bytes to a path inside WEB_SEARCH_NEO_DOWNLOAD_DIR (default ./downloads; relative paths resolve there, escapes are refused); an existing file needs overwrite=true. The answer carries saved_to and size_bytes instead of body.",
        "network": "Explicit localhost/private URLs are allowed; cloud metadata and link-local hosts are always refused, as is a redirect from a public host to a private one. A cross-origin redirect keeps only non-credential headers (Accept*, User-Agent, Content-Type...).",
        "response": "Always {success, url, status, headers, body/size}: 4xx/5xx return success True with their status and the server's error body, not an error. fetch_text is GET-only and returns str; replay_request re-sends inside the page with cookies/CORS, this one never touches a browser.",
    },
}
