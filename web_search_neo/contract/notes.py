"""Static contract data; no browser runtime imports."""


_INFO_TOPICS = {
    "capabilities": "This contract: topics, actions, recipes, and pitfalls.",
    "skill": "Built-in automation playbook; params.section='<name>' returns one section in full.",
    "actions": "Every action with its summary and required parameters; params.group narrows it.",
    "action_schema": "Full JSON Schema for one action or topic; pass params.action.",
    "page_outline": "Roles, names, states, refs, and boxes - start looking here.",
    "page_text": "Readable text of the rendered page; params.mode=main|full.",
    "element_text": "One element's content: params.selector, params.mode=text|html|outer|both.",
    "find": "Find an element by meaning: params.query='submit request'.",
    "page_elements": "Links, forms, fields, buttons with selectors (CSS or '#host >>> #leaf'; '' when none is unique).",
    "console": "console.log/warn/error and uncaught errors; params.levels, params.contains.",
    "network": "HTTP requests with status, type, ms, size; params.only_errors, third_party_only.",
    "network_body": "One response body; params.request_id is the id from a network read with output='json'.",
    "execute_js": "Run page JavaScript (async body) and read its value.",
    "screenshot": "PNG viewport, full-page, or exact page-region image.",
    "game_probe": "Canvas/WebGL surfaces, FPS, frame_health (throttling), focus, console, held input.",
    "browser_status": "Chrome availability and every session: owner, tab, last page, idle, busy, cap.",
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
        "arguments": "fields is a map from fresh CSS selectors to values, e.g. fields={'#email': 'a@example.test'}, not a list or separate selector/value parameters.",
        "occurrence": "A selector matching several controls fills the first; append [N] (0-based document order, e.g. 'input.qty[1]' for the second match) to choose explicitly. An N beyond the match count is refused naming how many matched.",
        "typing_react": "The default write is React-compatible: text goes in through the browser's input channel, and a React-controlled input whose value tracker still missed it gets one bubbling input+change event so onChange runs - listed in framework_resynced. typing=true (one input event per character, focus kept) is for masked inputs, autocomplete and handlers that only react per keystroke; use it when field_values shows your value but the app still did not react.",
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
    "cookies": {
        "paging": "get returns one window in Chrome's own order: total (= count) matched, limit/offset, returned, truncated and next_offset. Follow next_offset until it is null to read every cookie. domain means the domain and its subdomains (never a substring) for get and clear alike. clear needs a domain (a name alone exists on every site) and deletes exactly the matches - partitioned (CHIPS) cookies with their partition - reporting deleted_cookies from a fresh read and anything still there as not_deleted (success=false). A domain without a dot or a public suffix (com, co.uk, github.io) would hit every site under it and, like clearing without a domain (the whole profile - every login of the user in current Chrome), is refused unless confirm_clear_all=true.",
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
        "attach_method": "set_file_input_files = Chrome read the path itself. streamed (current Chrome) = Chrome refused access to the file ('Not allowed'), so the server read it and handed the bytes to the page; stream_reason says why, and the input/change events were synthetic (isTrusted=false). File access switched off and an administrator's ban give the same 'Not allowed'; a refusal naming a policy is an error instead.",
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
        "verification": "Every selector click reports verified/effect_detected: true when the URL/title changed, the clicked element's subtree or ancestors mutated (the agent's own overlays excluded), focus moved, or the element left the document; false only for a top-document target that showed none of that (no_observable_change, change_note); null when it cannot be measured - a frame_selector, ref or '>>>' (shadow) target, or a probe that did not survive. post_state carries the raw counts (dom_mutations, dom_mutations_near_target), focus, target_still_attached and dialog_open. Neither false nor null means 'click again': read the page first - a submit, order or payment may already be on its way.",
        "choice": "Provide exactly one target: selector (CSS, ref handle, or 'a >>> b' piercing path) for the element, text with role for a strict rendered-text match that refuses ambiguity, or x+y for a viewport CSS-pixel point. In current Chrome a CSS selector must be plain CSS, never ref: or >>>.",
        "text": "text clicks the one visible interactive element whose rendered text matches; role narrows by ARIA role and exact=false switches to substring matching. Zero or several matches are refused with samples, so narrow with role or selector rather than retrying.",
        "coords": "x/y click the viewport point in CSS pixels, useful for image-guided clicks from a fresh screenshot. Scale image pixels to reported viewport width/height and recapture after any layout change.",
        "stalled_submit": "submit_blocked_by_challenge=true means the click produced no network request at all while an unsolved invisible captcha sits on the page: clicking again cannot help, clear it with the captcha action first.",
        "trusted": "trusted=true sends a real trusted mouse sequence at the element's centre (scrolled into view first), so pages that require isTrusted events or read pointer position behave as if a user clicked. Use it when a synthetic click is ignored. It lands on whatever is at that point, like a human pointer.",
        "no_box": "trusted=true needs a visible box; an element with zero size refuses with a clear error instead of falling back silently.",
        "frame_selector": _FRAME_ANY,
    },
"run_script": {
        "scope": "Runs in the top document of the session's current tab; frame_selector runs it inside one frame (same- or cross-origin).",
        "args": "args arrive as arguments[0..n]; only JSON-serialisable values can cross into the page.",
        "body": "One semantics for run_script, execute_js and wait.script: the script is the body of an async function - await works at top level, `return <value>;` returns, and a one-line expression without return returns itself (document.title). A statement without return comes back as null with value_note. A SyntaxError fails at once (syntax_error=true); a thrown error or rejected promise is a script error.",
        "result": "value is the JSON-serialisable return value; returned promises are awaited. A value over max_chars (default 18000) comes back as a window: truncated=true, total_length (characters, items or JSON characters), offset, next_offset - strings by characters, arrays by whole items, objects as a slice of value_json. save_to='x.json' writes the whole value to the download folder instead. attempts reports how many tries the call took.",
        "retry": "Single-shot by default (retry_on_uncaught=false). An exception may follow a completed mutation. Only enable retries for scripts safe to repeat; retries=2, retry_delay_ms=300. wait_ready=true additionally settles readiness first.",
        "timeout": "Every script runs under a limit: timeout_seconds, default 15, capped at 600; past it the call answers timed_out=true instead of hanging. A slow await leaves the page working. Only when a browser the server launched stops answering (an endless loop froze its tab) does close stop that browser outright (forced); the user's own Chrome is never killed - there close detaches and releases the tab as usual.",
        "safety": "This is raw page-side JavaScript: it can navigate, mutate, or delete state. Prefer fill/click/pointer for input-shaped work and reserve scripts for state only the page holds (localStorage, virtualised rows, framework stores).",
    },
    "click_text": {
        "strict": "Clicks only when exactly one visible interactive candidate matches. Zero or multiple matches are refused with samples; narrow using role or selector.",
        "matching": "exact=true compares whitespace-normalized rendered text. exact=false is substring matching and should normally be paired with role.",
        "selector": "Optional CSS candidate filter, not the click target. Omit it to search buttons, links, labels, options, checkboxes, radios, tabs, and menu items.",
        "reach": "The search covers open shadow roots and same-origin frames; frame/shadow_path in the answer say where the match was. Cross-origin frames cannot be read by a page script: they are counted (cross_origin_frames, frames_note); pass frame_selector naming one to match inside it.",
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
        "sleep": "seconds=N is a plain delay (capped at 300 s) that needs no selector, no script and no open session; it cannot be combined with either. With neither selector, script nor seconds the call sleeps timeout_seconds (legacy form).",
        "frame_selector": _FRAME_ANY,
    },
    "find": {
        "scores": "match_score is how well the query matched that element alone; score adds ranking context. low_confidence is derived from match_score only.",
        "role": "A filter, not a nudge: another role is dropped. Under low_confidence the guesses come from the unfiltered set, so a wrong role can reappear there.",
        "ambiguous": "The top two matched and ranked equally, so document order alone chose; say which you mean instead of taking matches[0].",
        "limits": "visible_only=true by default; limit is clamped to 25.",
    },
    "page_elements": {
        "filters": "category='interactive' returns one deduplicated interactive list (links, buttons, inputs, editable controls and ARIA widgets). Other categories: all (legacy default), links, forms, fields, buttons, iframes. Combine visible_only=true, enabled_only=true, role='button', text_pattern='Save', href_pattern='/settings'. Filters run before offset/limit and the character budget; found and next_offset describe matching rows.",
        "selector": "CSS in the top document, '#host >>> #leaf' inside an open shadow root or same-origin frame, and '' when nothing addresses the element uniquely.",
        "scope": "Always the whole page: this topic takes no frame_selector, and it is the only read topic reporting challenge_detected/captcha_widgets.",
        "duplicates": "For repeated labels, compare each returned link href or stable value/attribute. Never choose by array index or nth-child alone.",
        "captcha_scan_incomplete": "true means the captcha walk stopped early, so an empty captcha_widgets is not proof there is none. Every page summary carries this key.",
        "invisible_challenge_pending": "true means a captcha with no box - an invisible Turnstile and the like - is on the page with an empty token field. It blocks the form, not the page, so challenge_detected stays false; invisible_challenge names the vendor and the evidence. Clear it with the captcha action before submitting. Every page summary carries this key too.",
        "contenteditable": "The legacy fields list includes [contenteditable=\"true\"]; category='interactive' also includes bare contenteditable and plaintext-only editors.",
        "stable_locators": "A row whose selector is fragile (an nth-of-type chain or a generated React id) carries stable_selector=false and suggested_locator={text, role} - pass those to click instead. A testing hook is reported as testid with suggested_locator={selector}. Rows with a plain stable selector carry neither.",
        "pagination": "The whole existing DOM is counted before each category is sliced. Use offset plus limit, then follow range.<category>.next_offset until null; reread after scrolling a lazy/infinite page.",
        "limits": "limit is clamped to 1000 per top-level category and offset to 0-20000. collector_truncated.<category>=true means the 20000-item safety cap was hit and found is only the collected prefix. include_forms=false also omits fields.",
    },
    "page_text": {
        "fallback": "mode='main' on a page that is one big form would be empty, so it re-reads the whole body and says so with fallback_used=true and mode_used='full'. It does the same when main keeps only a sliver of what the page renders.",
        "title": "A title set by script after load is waited for up to 2.5 s; if it never came, title is null and title_pending=true (not an error). Action summaries carry title_pending=true while an HTML page's title is still empty (never for about:blank, JSON or images).",
    },
    "network": {
        "id": "The default output='text' carries no ids. Pass output='json' and hand that row's id to network_body as request_id.",
        "third_party_only": "Keeps requests to other registrable domains than the page's (cdn.example.com is the same site as www.example.com); the answer names first_party_site and third_party_sites.",
    },
    "security_report": {
        "scope": "Passive and bounded to the hosts you name. scope='page' (default) checks url plus your paths (<= 50 routes, GET); 'site' crawls links and the sitemap of the scope hosts (max_pages 10, <= 50; max_depth 2; delay_ms 500; robots.txt unless respect_robots=false); 'hosts' checks each named origin (<= 10: example.com, localhost:3000, http://127.0.0.1:8080), a section each. include_subdomains widens each domain. Wildcards, public suffixes, bare LAN words, paths and credentials in hosts are refused before any request; a host without a port covers ports 80/443 only. Redirects, links and sitemap entries outside the scope are named (not_crawled, redirect-out-of-scope), never requested. requests_made lists every request (redacted, budget 200): pages, http://host/ (redirect check), the https read for HSTS, security.txt, robots.txt, sitemap.xml, one TLS handshake per https host. No path guessing, port scanning, Origin probing or fuzzing. browser_requests summarises the page load in the isolated browser (scope='page' only; browser=false reads the served HTML).",
        "checks": "headers (CSP directives, HSTS, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, X-Frame-Options, COOP/COEP/CORP, Server/X-Powered-By versions), cookies (Secure, HttpOnly, SameSite, __Host-/__Secure-, wide Domain; values never reported), transport (https, http->https redirect, certificate issuer/expiry), CORS on the page response (no Origin sent), page (third-party scripts and SRI, mixed content, forms, password autocomplete, target=_blank without noopener, inline code under a strict CSP), security.txt, robots.txt.",
        "grade": "Mozilla HTTP Observatory v1.7.1's tests and modifiers, reimplemented (one result per test, the worst): CSP, cookies, redirection, Referrer-Policy, HSTS, SRI, X-Content-Type-Options, X-Frame-Options, COOP, COEP, Cross-Origin-Resource-Policy; 100 minus penalties, bonuses only from a 90+ base, A+ >= 100 ... F < 25, 165 at most. Only 2xx/3xx/401/403 are graded. Not awarded: the HSTS preload bonus (needs a preload-list lookup) and Observatory's CORS -50 (needs an Origin request), so 160 is the highest here. This report's own penalties (mixed content, insecure forms, CORS as served, broken cookie prefixes) are extra_points and move only extended.score. score_explanation lists every modifier; priority[] orders the fixes. Aggregates (site, hosts, paths) take the weakest page's grade, list sections and dedupe priority with the pages each problem is on.",
        "local": "localhost, private IPs and .local/.lan/.internal names are graded in local_development mode: https, HSTS, the redirect, the certificate (self-signed is fine), cookie Secure, mixed content and http forms are skip (not applicable in dev); the headline grade covers the rest, as_served keeps the plain numbers and production_forecast predicts the grade over https.",
        "certificate": "An untrusted certificate: the page is read once more without verification (as Observatory) and graded in full; tls-invalid plus Observatory's -20 for redirection and -20 for HSTS.",
    },
    "api_report": {
        "scope": "Passive: it sends nothing. It reads the session's network journal (the traffic the page itself made while you or a test_run scenario worked with it), the cookie jar, localStorage/sessionStorage (names and formats; values are never output) and error bodies Chrome already holds. url loads the page in a fresh isolated session (closed unless keep_open); session_id analyses an open page; hosts (<= 10, same rules as scope) adds your API hosts to 'own'. Own-site responses get the full checks; third-party origins are classified, never contacted. requests_made is always empty - repeating a request stays replay_request's explicit job.",
        "checks": "endpoints[] in frequency order: method, path template (ids as {id}), count, origins, channels (xhr/fetch/websocket/sse/beacon). Per own API response: CORS (allow-origin vs credentials, preflight methods/headers, wildcard/null origins), caching (public vs no-store/private on JSON and cookie-setting responses), Content-Type + X-Content-Type-Options on JSON, error bodies (stack traces, server paths, framework versions, snippets masked). Transport: ws:// vs wss://, http requests from an https page, third-party API origins. Auth: token cookies (flags, HttpOnly/Secure/SameSite) and localStorage/sessionStorage by name and format; JWT alg, exp/iat and signature presence without any value. CSRF: SameSite, a visible token in state-changing requests, writes leaving your site. Sensitive query params (token, e-mail, session id) with values masked.",
        "modes": "summary='min' keeps counts, priority and summary_line. save_to writes the report as JSON; har_to writes the journal as HAR 1.2 - a HAR can carry what its URLs carried, treat it as a secret.",
    },
    "secret_scan": {
        "scope": "Passive: the page, its same-scope scripts and a referenced API description over ordinary GETs (all in requests_made). Third-party scripts are named, never fetched.",
        "checks": "secrets in code (cloud keys, tokens, JWT, private keys, high-entropy literals - values masked), endpoints from fetch/axios literals, openapi/swagger when referenced, sign-in forms (http post, autocomplete).",
        "modes": "summary='min' keeps counts, priority and summary_line. save_to writes JSON, sarif_to SARIF 2.1.0; baseline adds regression {fixed, added}.",
    },
    "perf_report": {
        "source": "The page's own Performance API: navigation timing (TTFB, DOMContentLoaded, load), paint (FCP), buffered largest-contentful-paint and layout-shift observers (LCP, CLS as the largest session window), resource timing (count, transfer, by initiator, third-party share, renderBlockingStatus).",
        "lab": "Lab numbers from the automation browser: compare runs of the same page, do not read them as real-visitor field data.",
        "modes": "url alone: a cold load in a fresh isolated session, closed afterwards unless keep_open. session_id alone: the page open there. Both: that session navigates to url (warm cache).",
    },
    "har_export": {
        "content": "HAR 1.2 from the session's network journal (the newest 500 finished requests plus in-flight ones): method, URL, status, type, timing, transfer size, the security-relevant response headers, post data. Request headers and bodies are not recorded - the file's comment says so; _dropped counts what fell out of the history.",
        "file": "Written under the download folder (WEB_SEARCH_NEO_DOWNLOAD_DIR); save_to names it, an existing file needs overwrite. HAR files can hold tokens from URLs and post data: treat them as secrets.",
    },
    "test_run": {
        "step": "An ordinary web_action object plus optional step_name and expect, or an expect-only step. Every other key belongs to the action (cookies and macro take a name of their own). session_id is filled in from the run for every action that takes one. The whole plan - every action's arguments against its schema, every expectation - is validated before the first step runs; a test_run cannot run inside another, directly or through a macro.",
        "expect": "selector (+selector_state), absent, text / no_text (string or list), url_contains, title_contains, script (truthy), no_console_errors, no_failed_requests (since the step began), action_fails (negative test), timeout_seconds per check. Each check runs as a wait, so it waits up to its timeout.",
        "result": "success, passed/failed/skipped/total, summary_line, failed_steps first, then steps[] with every check's verdict and duration_ms. stop_on_failure=false runs every step; include_data adds each action's short result.",
    },
    "execute_js": {
        "arguments": "params.script is the body of an async function: script='return document.title;' - or the bare one-line expression 'document.title', which returns itself. Top-level await works. Same semantics as run_script.",
        "scope": "Top document of the session's current tab by default; frame_selector enters one frame first (same- or cross-origin - the bridge attaches to it, Selenium switches target) and the driver is left back at the top document. A top-document script cannot read a cross-origin frame - the browser refuses, not us - so a framed page needs frame_selector instead of a deeper querySelector.",
        "result": "One contract, always a JSON object: {success, value, value_json, value_type, attempts, ...page summary}. value is plain JSON (objects and arrays arrive as JSON, never '[object Object]' and never MCP content parts), promise-awaited on the Chrome bridge driver; DOM elements arrive as {element: tag} descriptors. value_json is the same value as one JSON string; value_type is null|boolean|number|string|array|object. A script with no return reports value_note. A cyclic or window object fails with a note to return a plain object. A value over max_chars comes back as a flagged window (truncated, total_length, next_offset); save_to writes it whole to a file.",
        "frames": "Without frame_selector an empty value (null, '', [], {}) on a page with frames a top-document script cannot see comes with cross_origin_frames (count) and frames_note. With frame_selector, a frame that cannot be entered (cross-origin and not yet loaded, not a frame, ambiguous) is a clear ValueError naming why - never a silently partial result.",
        "prefer_actions": "Use fill/click/pointer for anything a user gesture should do; a script cannot simulate a trusted interaction.",
    },
    "game_probe": {
        "frame_selector": _FRAME_CSS,
        "why_strict": "It sends nothing, but the canvas rects it reports are aimed at with this same string, so it is as strict as the input actions.",
    },
    "open": {
        "profile_mode": {
            "current": "the user's signed-in Chrome through the companion extension (explicit only since 1.20)",
            "auto": "current, falling back to a headless temporary profile",
            "temporary": "clean disposable profile",
            "isolated": "the default for a new session (1.20): disposable separate headless browser profile and storage, with per-session user_agent/timezone/locale/geolocation; does not guarantee an unlinkable hardware or network fingerprint",
            "persistent": "durable server-owned profile, keeps logins",
            "attach": "a Chrome you started with a DevTools port",
        },
        "headless": "temporary/persistent default headless; headless=false explicitly opens a visible window. attach preserves the launcher's window mode when omitted. headless=true is refused with current and makes auto resolve straight to temporary.",
        "claimed_tab": "open on a session claimed by attach_tab does not navigate the user's tab: it takes a new one and reports the released id as left_claimed_tab.",
        "persist": "persist=true (explicit session_id, never 'default'; a new session must name profile_mode current or isolated/temporary, an open or parked one keeps its own) with profile_mode temporary/isolated parks the whole browser the server launched: at exit it is recorded instead of quit, a later server continues it with reattach or open+persist, and a watchdog retires it when WEB_SEARCH_NEO_PARKED_SESSION_TTL runs out or its server died unparked; persist_warning says when the MCP client's job object will end it anyway; persistent/attach are never parked. In current Chrome (only for tabs the server opens) it keeps the tab open when this MCP client process exits: it is detached and recorded. A later client continues it only explicitly, with web_action reattach {session_id} (or open with persist=true and the same session_id); nothing picks it up implicitly. Re-attaching is refused - the record dropped, the tab left alone - unless it is provably the same tab: the same Chrome run, still in the agent tab group, still on the recorded origin, not driven by another client; a server tab that only shows another site now is named in left_open_tab. A live session keeps its record fresh while used and is never touched by expiry. An explicit close closes a parked server tab (retired_parked); records expire after WEB_SEARCH_NEO_PARKED_SESSION_TTL (default 24 h, minimum 60 s) and their tabs are closed the same way. browser_status lists parked_sessions.",
        "tab_followed": "If Chrome itself replaces the session's tab (chrome.tabs.onReplaced: a prerendered/instant navigation, a restored discarded tab), the companion redirects commands to the new tab and the session moves to it (tab_followed in the next answer), keeping its ownership - it is the same page. If the new tab is driven by another session or agent, the session is dropped instead. Only that record is followed: a tab the lost one opened (a popup, a payment window) or a tab on the same URL never is. Read-only topics and wait/screenshot are repeated once; every other step fails with SessionTabFollowed - read the page before re-issuing it. A tab that is really gone still drops the session with the exact open call to redo.",
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
        "alias": "action 'attach' is accepted as attach_tab. Attaching the tab a parked session of the same session_id holds continues it (like reattach); attaching another tab under a parked name retires that record first (retired_parked says what happened to its tab).",
        "unloaded": "A tab Chrome has discarded (status 'unloaded' in browser_tabs) hangs the attach for ~25 s; it is activated through the companion first and reported as restored_tab=true with restore_wait_seconds. A tab that no longer exists fails fast instead of hanging: list live tabs with web_info(topic='browser_tabs') and pick one of those.",
        "parked": "A claimed tab is the user's and is never parked across MCP clients (persist exists only on open). Attaching the tab a parked session of the same session_id holds continues it, like reattach.",
    },
    "reattach": {
        "what": "Continues a persist=true session an earlier MCP client left parked, by session_id. Refused with success=false (record dropped, tab untouched) unless the tab is provably the server's parked tab: same Chrome run, agent tab group, recorded origin, and no other client driving it; a server tab that now shows another site comes back as left_open_tab for you to close.",
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
        "orphans": "idle_for_seconds=N closes only sessions untouched for N seconds and never one a thread is inside; with scope='all' it releases orphaned slots whoever owns them. The session-cap error lists every holder (session, agent, tab, age, idle, busy, url); sessions idle past WEB_SEARCH_NEO_SESSION_IDLE_TTL (default 30m) are also reaped automatically at the cap, and WEB_SEARCH_NEO_MAX_SESSIONS (or the companion popup) raises the cap up to 64.",
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
        "parked": "parked_sessions lists persist=true sessions left open by an earlier MCP client (URLs redacted, no titles); continue one with reattach {session_id}. parked_expired lists records that just expired and what happened to their tabs.",
        "roster": "sessions lists every session in this server with agent_label, current_tab_id, tab_group, last_url/last_title, created_at, last_used_at, idle_seconds and busy; sessions_open/max_sessions/sessions_free are the occupancy, and max_sessions_source says whether the cap came from the environment, the companion popup, or the default. last_url/last_title are where a session was last seen, not a fresh read - another agent's tab is never touched to answer this.",
        "browser_gone": "session_open=false with browser_gone=true means the session was dropped because its Chrome restarted; follow the 'next' field and open the page again.",
        "co_tenants": "sessions_in_use names the sessions another caller of this server is inside right now; shared_session=true means this very session is one of them. current_chrome.daemon.clients counts the MCP servers sharing the browser and .claims lists every tab any of them drives, with mine telling ours apart.",
    },
    "browser_tabs": {
        "reconnect": "connected=false with companion_note means the bridge is up but the Chrome companion has not reconnected yet (it retries on its own within about a minute of a bridge restart): call again with wait_seconds=75 (max 90). A connected companion whose version differs from its folder is reloaded automatically while no agent drives a tab, at most every 5 minutes (companion_refresh, also on open); a reload that did not take is not retried (self_update=ineffective with manual_steps), and a same-version companion with older code is only reported (not_attempted).",
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
        "text": "Non-empty string, any script (Cyrillic and other non-Latin text arrive unchanged).",
        "selector": "Optional CSS target, located and focused first. Omit it to type into whatever already has focus - fill and click leave their target there. Focus is found through open shadow roots and same-origin frames; a cross-origin frame counts as unknown and the text is sent. With nothing focused, or a read-only/disabled control, the call is refused instead of dropping the text.",
        "mode": "mode='insert' (default) sends one CDP Input.insertText, so controlled inputs (React) see one composed edit. mode='keys' presses one key per character (keydown/keypress/keyup carrying the character, at most 500) for canvas games such as Unity WebGL, which never see insertText. Without selector keys go only to an editable control, a canvas, an element with an explicit tabindex, or a frame - a focused button or link (Enter/Space would activate it) or the bare page is refused; a custom element that hides its focus is unknown and allowed. Keys into a tabindex element are meant for canvas/game surfaces only and come with keys_warning: on an ordinary site with keyboard shortcuts every character may trigger one, so pass selector for a text field there. A focused <canvas> switches to keys automatically (mode_used says which ran).",
        "speed": _HOT_PATH_SPEED,
    },
    "press_keys": {
        "key_action": "tap|hold|release",
        "note": "The dispatcher key is 'action'; the keyboard verb is 'key_action'.",
        "chord": "Several keys in one call are one chord (all down, then all up): send a sequence as separate calls, or type_text mode='keys'. Names: ENTER, TAB, ARROW_LEFT or DOM ArrowLeft/KeyW/Digit1; 'Control+Shift+K' is a chord.",
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
        "action": "web_action screenshot saves the PNG under the download directory and returns saved_to, size_bytes and image_width/height. path=... must end in .png and stay inside that directory; an existing file is refused unless overwrite=true, and the path is checked before the capture. web_info topic=screenshot returns the image itself.",
        "modes": "viewport (default), full_page, region; full_page=true remains an alias for mode='full_page'",
        "viewport": "omit width/height to preserve the actual viewport. An explicit pair resizes Selenium sessions exactly and is refused in current Chrome.",
        "region": "requires x/y/width/height in page CSS pixels, captures without resizing, and works in current Chrome and Selenium",
        "full_page": "captures the whole current layout up to 3840x10000; an oversize page errors instead of returning an unlabelled partial image",
        "background": "Current-Chrome viewport captures use one fresh compositor video frame without activating a tab or changing the window. Full-page/region surface captures may still stall in an obscured window; try viewport or DOM/text. Never call show as automatic recovery. DOM/actions still work; use read-back for labels and selected values.",
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
        "http_session": "Opt-in cookie jar: http_session='login' keeps cookies per (agent_label, name) in this server process (idle TTL WEB_SEARCH_NEO_HTTP_SESSION_TTL, default 1800 s; 32 jars, least recently used dropped). Cookies match by domain/path/secure/expiry, redirect hops feed the jar and a cross-origin hop still strips credentials. The answer's http_session block lists sent_cookies/received_cookies per hop with flags; values only with show_values=true (Set-Cookie values are redacted otherwise). A Cookie header and http_session are mutually exclusive; http_session_clear empties the jar first.",
    },
}


_SERVER_INSTRUCTIONS = (
        "Use web_info for discovery and observation. Start with topic=capabilities when "
        "the compact contract is not already known. Use web_action for one or many "
        "ordered mutations. Read action_schema before an unfamiliar action or topic; "
        "after validation failure fix the call from that schema, never guess aliases. "
        "execute_js takes script (an async function body: return a value, or send a one-line expression), not code; "
        "fill takes fields={CSS_selector: value}. Give each task/agent a unique session_id "
        "and agent_label on open; reuse only that task's session. A new session opens isolated "
        "(profile_mode='current', the user's own Chrome, only when asked). Never close or take over "
        "another agent's tab. After a zero-match click inspect fresh page_elements/find "
        "and frame context before retrying; React portals alone do not explain missing text. In step render "
        "mode an input action applies all mixed keyboard and pointer changes before "
        "advancing exactly one frame."
    )

_ARGUMENT_RECOVERY = {
        "browser_execute_js": " Use params={'script': 'return document.title;', 'session_id': '<your-session>'}. script is an async function body (a one-line expression returns itself). Do not use code.",
        "browser_fill_fields": " Use fields={'<CSS selector from fresh page_elements>': '<value>'}, not selector/value or a list. Read field_values and errors before continuing.",
    }
