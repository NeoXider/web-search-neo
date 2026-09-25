# Changelog

## Unreleased

### 1.21.0

`api_report`: a passive review of how the page talks to its own backend - the map of the calls, and for every own-site API response the CORS, caching, Content-Type and error-body checks, plus transport, where tokens live, the CSRF surface and secrets in URLs. Every finding carries a priority and a fix, like `security_report`; `summary: "min"` keeps `counts`, `priority` and `summary_line`.

Site checks (`web_search_neo/audit/api.py`, `api_parts.py`, `api_checks.py`; wrapper in `audit_actions.py`; `docs/site-checks.md`)
- `api_report {url | session_id}` builds `endpoints[]` in frequency order - method, path template (numeric, UUID and long-hex segments as `{id}`), count, origins and channel (`xhr`, `fetch`, `websocket`, `sse`, `beacon`) - from the session's network journal only. It sends no requests of its own: `requests_made` is always empty and repeating a call stays `replay_request`'s explicit job. `url` loads the page once in a fresh isolated session (closed unless `keep_open`, as `perf_report` does), `session_id` analyses an open page, `hosts` (at most 10, validated with `scope`'s rules) adds your API hosts to "own", `wait_seconds` (default 2) lets the page's calls land in the journal first.
- Per own-site API response: CORS (`Access-Control-Allow-Origin` against credentials, `*` with credentials and `null` refused, specific origins listed, preflight methods/headers/max-age), caching (`Cache-Control`/`Pragma`: `public` on a JSON or cookie-setting response is a finding - it fails when the response also sets a cookie; `no-store`/`private` passes; a missing policy warns), `Content-Type` plus `X-Content-Type-Options: nosniff` on JSON, and error bodies (4xx/5xx read from Chrome's own buffer, never re-requested: stack traces, server file paths and framework versions, snippets and URLs masked; unread bodies are named, not silently skipped). Transport: `ws://` against `wss://` (fail on an https page, warn on http), `http://` from an https page, API calls to other sites' origins.
- Client-side auth without ever printing a value: cookies as name, domain and `Secure`/`HttpOnly`/`SameSite` flags with the value's format; `localStorage`/`sessionStorage` by name and format only; JWT-like values as facts (`alg`, `exp`, `iat`, signature present - an `alg: none`/unsigned token fails high); tokens in JS-readable storage and non-`HttpOnly` token cookies warned. CSRF: `SameSite` on session cookies (`SameSite=None` without `Secure` fails), a visible token in state-changing requests, writes leaving your site without one. Secrets in query strings (tokens, e-mail addresses, session ids) with values masked - parameter names only.
- `save_to` writes the report as JSON, `har_to` the same journal as HAR 1.2 (download folder, `overwrite` to replace). A companion session records no response headers, so the header checks say so (`api-headers-unavailable`) instead of quietly passing.
- Response rows now carry `Cache-Control`, `Pragma`, `Content-Type` and the `Access-Control-Allow-*`/`Access-Control-Max-Age` headers (`diagnostics.SECURITY_HEADERS`).
- The contract stays under 13 500 characters with the new action: the `how` and `discovery` prose was tightened; the details live in `docs/site-checks.md`.
- Fixes found while pinning 1.21 with tests: the storage snapshot arrives as a function body (as written it was an uncalled `() => {...}`, so `api-storage-unavailable` always fired); a cookie with no `SameSite` no longer also fails as `SameSite=None`; values of sensitive-name query parameters (`sid`, `phpsessid`, ...) are masked in evidence URLs, not just detected; the header-less info path imports its constructor.

Docs: README "Check your own site" lists five actions now, and `docs/site-checks.md` gained the `api_report` section.

## 1.20.0
## 1.20.0

Checking your own site: a passive security report graded with Mozilla HTTP Observatory's own tests and modifiers, load metrics, HAR export and regression test runs - plus a safer default for new sessions. The security report is a configuration review for sites the caller owns or may test: a page, a crawl of the site or a list of hosts - only the hosts the caller names, only what an ordinary visit or crawler reads, every request the report sends listed.

Breaking
- **A new session opens `isolated`, not `current`.** `open` without `profile_mode` on a session that does not exist yet now starts a fresh isolated headless browser; `open_many` defaults to `isolated` too. Driving the user's own Chrome - with their logins and open tabs - is something an agent now asks for with `profile_mode: "current"`, not something it gets by leaving a parameter out (the 1.19 known limitation U1). An `open` of an existing session keeps that session's mode as before, and an option only one mode has names that mode: `current_tab_id` means `current`, `debugger_address` means `attach`, `profile_id` means `persistent`; `persist: true` on a new session without a mode is refused (name `current` or `isolated`), while an open or parked session keeps its own mode. Migration: add `"profile_mode": "current"` to the first `open` of every session that needs the user's Chrome. The contract, the playbook, the skill, README and INSTALL (`Migrating to 1.20`) say so.

Site checks (`web_search_neo/audit/`, wrappers in `audit_actions.py`; `docs/site-checks.md`)
- `security_report {url}`: headers (CSP parsed into directives; HSTS `max-age`/`includeSubDomains`/`preload`; `X-Content-Type-Options`; `X-Frame-Options` or `frame-ancestors`; `Referrer-Policy` from the header and `<meta name="referrer">`; COOP, COEP, CORP; `Permissions-Policy`; versions in `Server`/`X-Powered-By`), cookies (`Secure`, `HttpOnly`, `SameSite`, `__Host-`/`__Secure-`, a `Domain` wider than the host or a public suffix; script-set cookies from the browser's jar; values never reported), transport (https, `http://host/` redirecting to https on the same host first, certificate issuer/subject/expiry/protocol from one ordinary verifying handshake - no protocol or cipher scanning), CORS on the page response (`*`, `null`, `*` with credentials; no `Origin` sent, no origins tried), the page (third-party scripts and SRI, active and passive mixed content, forms posting to http, password fields over http / in GET forms / without a password `autocomplete` token, external `target=_blank` without `noopener`, inline scripts and handlers a strict CSP blocks, unsandboxed third-party frames), and `security.txt` (RFC 9116) / `robots.txt`, read only. With `scope: "page"` the page loads once in a fresh isolated session that is closed afterwards (`keep_open` keeps it, except after a failure); `browser: false` reads the served HTML instead, and a snapshot the page's own scripts broke falls back to it.
- Scope, validated before the first request: `scope: "page"` (default; plus `paths`, your own routes - at most 50, GET only), `"site"` (a passive crawl of links in served pages and the sitemap, inside the scope: `max_pages` 10 by default and at most 50, `max_depth` 2, `delay_ms` 500 between pages, `robots.txt` honoured unless `respect_robots: false`; the weakest page decides the grade, every page has its section, and `priority` is deduplicated with the pages each problem is on) and `"hosts"` (at most 10 named origins, each its own section). `hosts` takes exact hosts with optional scheme and port (`example.com`, `localhost:3000`, `http://127.0.0.1:8080`, `[::1]:8443`); wildcards, public suffixes, bare LAN words, paths and credentials are refused; `include_subdomains` widens each domain; a host without a port covers ports 80/443 only. Nothing outside the scope is requested - redirects, links and sitemap entries that leave it are named (`not_crawled`, `redirect-out-of-scope`); a hop over the budget or one the HTTP client refuses (plain http to a public host, public to private) keeps the chain up to it (`redirect-over-budget`, `redirect-refused`); a chain cut at an http:// hop gets at worst -10 (first redirect stays on http, "chain cut at the scope boundary"), never "never reaches https", and plain-http hops are followed to every host in the scope, subdomains under `include_subdomains` included. Every origin of a crawl gets its own redirect probe, TLS handshake, well-known files and robots.txt. Every request goes through one budget (200) and is listed in `requests_made`; every URL anywhere in the answer (routes, errors, evidence) is redacted - no userinfo, sensitive query values masked. When the browser load ends outside the scope, that page is not analysed (`browser_requests.left_scope`).
- The grade reproduces Mozilla HTTP Observatory v1.7.1 (mdn/mdn-http-observatory, tag `v1.7.1`, MPL-2.0: the algorithms reproduce its behaviour - the CSP merge that of `cspParser.js` v1.7.1 - in Python code written anew), pinned case by case in `tests/test_observatory_parity.py`: one result per test, the worst that applies - CSP (merged header and `<meta>` policies, only sources every policy allows; a repeated directive -25, a repeated `report-uri`/`report-to` 0; lowercased sources; an empty `*-src` is `'none'`; `'strict-dynamic'` and nonces edit the `default-src` fallback itself; `http(s)://*.*` and `ftp:` are broad; active and passive insecure sources), cookies (the page request's jar as a browser keeps it - robots.txt is read without the jar, as in Observatory: public-suffix or foreign `Domain` and broken prefixes dropped, the last `Set-Cookie` wins; +5 only when every cookie has `SameSite`; `SameSite=None` without `Secure` -20), COOP and COEP (+10 for a valid token read as one RFC 8941 item, -5 invalid or sent twice), redirection (route[0] against route[1]; plain-http hops on a named host followed), HSTS (15552000 s; a comma is invalid), Referrer-Policy, SRI (on the served HTML; protocol-relative is insecure; the worse result wins), X-Content-Type-Options, X-Frame-Options (+5 for `frame-ancestors` anywhere in the combined CSP), CORP; empty headers as Observatory reads them; only 2xx/3xx/401/403 are graded; bonuses only from a 90+ base; A+ >= 100 ... F < 25; 165 at most. Not awarded, because they need a request or a list the report does not use: the HSTS preload bonus and Observatory's CORS -50, so 160 is the highest grade here.
- An untrusted certificate: the origin is read once more without verification, as Observatory's session does, and every later read of it too (`requests_made` marks them); the report is graded in full with `tls-invalid`, -20 for redirection and -20 for HSTS.
- Local development: localhost, private-network IPs and `.local`/`.lan`/`.internal`/... names are graded in `mode: "local_development"`: https, HSTS, the redirect, the certificate (self-signed is fine), cookie `Secure`, mixed content and http forms are `skip` ("not applicable to a local development server"), the headline grade covers the rest, `as_served` keeps the plain numbers and `production_forecast` the grade the same responses would get over https. Several dev servers in one call with `scope: "hosts"`. Recipe "check a dev server" in `docs/site-checks.md`.
- This report's own penalties for what Observatory does not grade - mixed content, forms posting over http, passwords over http or in GET forms, CORS as served, broken cookie prefixes - are `extra_points`: they never move the grade and feed a second, stricter score under `extended`. All other advice carries 0 points. `score_explanation` lists every modifier, `priority` orders the fixes (fail before warn, then severity), every finding carries `fix` and `evidence`. `save_to` writes the whole answer as JSON.
- `perf_report {url | session_id}`: TTFB, FCP, LCP (buffered observer, with the element), CLS (largest session window, input shifts excluded), DOMContentLoaded/load, resources by type, the largest, third-party share, render-blocking files (`renderBlockingStatus`, or the head's sync scripts/styles on an older Chrome), Web Vitals ratings and recommendations; `buffer_note` when the page filled Chrome's 250-entry resource-timing buffer; the page's answer is type-checked field by field, so a page that replaced the Performance API gives empty metrics, not an error. With `url` alone a cold load in a fresh isolated session. Lab numbers, and the answer says so.
- `har_export {session_id}`: the network journal as a HAR 1.2 file in the download folder (`save_to`, `overwrite`, `third_party_only`, `include_pending`, `inline` under 100 000 characters). Request headers and response bodies are not recorded and the file's comment says so; a request body the journal cut at 4000 characters keeps its real `bodySize` and says so in `postData.comment`, one the browser did not hand over is `bodySize: -1` with a comment; `Set-Cookie` values are redacted; `_dropped` counts what fell out of the 500-request history (1.19 known limitation).
- `network {third_party_only: true}`: only requests to other registrable domains than the page's, with `first_party_site` and `third_party_sites` (1.19 known limitation). `data:`/`blob:` are never third party; the registrable-domain lookup is cached.
- `test_run {steps, url?, session_id}`: a step is an ordinary action plus optional `step_name` and `expect` (`selector` + `selector_state`, `absent`, `text`/`no_text`, `url_contains`, `title_contains`, `script`, `no_console_errors`, `no_failed_requests` since the step began, `action_fails` for negative tests, `timeout_seconds` - a number), or an expect-only step. Every other key belongs to the action (`cookies clear` keeps its `name`). Every check is an ordinary `wait`; the whole plan - every action's arguments against its schema, every expectation, and saved macros whose steps would start another `test_run` - is validated before the first step runs. The answer puts `success`, counts, `summary_line` and `failed_steps` first, then every check per step. `stop_on_failure`, `screenshot_on_failure`, `keep_open`, `include_data` (G9).

Agent ergonomics
- `summary: "min"` on `web_action`, or `"summary": "min"` inside one action: the short form of each result - scalars, small objects and the head of the lists that are the verdict (`priority`, `failed_steps`, ...) - marked `summary_mode: "min"`, with everything left out named in `summary_omitted` and every shortened string (list items included) in `summary_clipped` (U2).
- Recipes `release_check` and `form_regression` in the contract; the `audit` and `testing` playbook sections are rewritten around the new actions. The contract stays under 14 000 characters: recipes and pitfalls were tightened and the details moved to `docs/site-checks.md`.

Sessions, tabs and input
- New tabs and popups of Selenium sessions (G10; `tab_actions.py`, `sessions/windows.py`): an action that makes the page open a window (`click`, `click_text`, `submit`, `pointer`, `run_script`, `input`, `press_keys`, `touch`, `type_text`, `fill`, `scroll`, `wait`) reports it in `new_tabs` (handle, URL, title, opener) while the session stays on its tab; `follow_new_tab: true` on `click`/`click_text` moves to it; the new `tabs` action lists, switches to and closes the session's windows. A popup that closes itself hands the session back to its opener (`tab_closed_by_page`); the last window is never closed; an attached browser loses only the windows its pages opened, when the session closes. The user's own Chrome (`current`) is untouched: `tabs` refuses it by name.
- `persist: true` for `temporary`/`isolated` sessions (`persist_actions.py`, `owned_parking.py`): the whole browser the server launched outlives it - parked at exit, continued by `reattach` or `open` + `persist: true` in a later server. A record is never taken over while another server holds the session live, nothing is killed on a pid whose start stamp changed, and a detached watchdog retires the browser when `WEB_SEARCH_NEO_PARKED_SESSION_TTL` runs out or its server died without parking it. `persist_warning` says when the MCP client's Windows job object will end the browser with the client anyway. Persistent and attach browsers are never parked.
- `click_text` searches open shadow roots and same-origin frames (`perception/text_targets.py`); the answer names `frame` and `shadow_path`, `matched_selector` is a piercing path `click`/`find` accept back, duplicates across documents are refused with the count, and cross-origin frames are counted in `frames_note` instead of silently skipped. Both input paths (Selenium and the companion).
- `CapsLock`, `NumLock` and `ScrollLock` are real key events now (`actions/lock_keys.py`): sent as CDP `Input.dispatchKeyEvent` in Selenium sessions as in the companion, `key`/`code` as a keyboard sends them (keyCode 20/144/145). Chrome keeps no lock state for synthetic input, so the session tracks it: with CapsLock on, letters arrive upper-case (lower-case under Shift); `type_text` types as written. A driver without CDP refuses them before anything is sent (1.19 refused them always).

Fetching
- `http_request` `http_session: "<name>"` (`fetch/sessions.py`): an opt-in cookie jar kept across calls per `(agent_label, name)` in the server process - idle TTL `WEB_SEARCH_NEO_HTTP_SESSION_TTL` (default 1800 s, at least 60), at most 32 jars, the least recently used dropped first. Cookies match by domain, path, `Secure` and expiry; redirect hops feed the jar, a cross-origin hop still strips credentials, a server deletion removes the cookie. The answer's `http_session` block lists `sent_cookies`/`received_cookies` per hop with flags; values, there and in `set_cookies`/`headers`, only with `show_values: true`. `http_session_clear` empties the jar first; a `Cookie` header together with `http_session` is refused. Without a name nothing changes: no cookie carries over. `web_client.request(cookie_jar=...)` is the transport side.

Internals
- `web_client.request(allow_plain_http=True)` admits one public `http://` start URL for that call (the redirect check); hops are still validated with the process-wide rule.
- New leaf package `audit/` (allowed: `web_client`, `fetch`, `actions.cookie_scope`), listed in `pyproject.toml`. The ordered-action loop moved from `main.py` to `dispatch.py` and the `inject_script` operations from `browser_tools.py` to `actions/injected_scripts.py`; the ratchets went down (main.py 2813 -> 2679, browser_tools.py 8359 -> 8305, chrome_bridge.py 2090 -> 2070). `extra_actions.register` receives main's own module so `test_run` replays steps through the one dispatcher, also when main runs as `__main__`.
- The companion's code is unchanged; its manifest and the popup's preview default carry the release version, as every release's do.
- Process trees are read without trusting a stale parent id: `sessions/process_tree.py` drops a listed child that started before its parent (a reused pid on Windows), the parked browser's Chrome is recorded from that list, and a forced stop names exactly the verified pids to `taskkill /F` instead of `/T`. A race in creating the project macro store is gone, and the ledger write retries a blocked replace for up to 10 s.
- Tests: `tests/test_http_session.py`, `tests/test_new_tabs.py` (pages in `tests/fixtures/tabs/`), `tests/test_persist_owned.py`, `tests/test_click_text_shadow.py` (`tests/fixtures/perception/shadow_click*.html`) and `tests/test_lock_keys.py`, all against local servers and temporary headless browsers.
- Tests: `tests/test_release_1_20.py` with a local `http.server` fixture (`tests/security_fixture_site.py`, pages in `tests/fixtures/security/`) serving a weak and a strong configuration; the "third party" is the same server addressed as `localhost`. `tests/test_observatory_parity.py` pins Observatory v1.7.1 case by case (header, cookie or HTML -> result and points, and whole-report scores). `tests/test_security_scope.py` runs scope validation, the crawl, `paths`, `hosts` and the development mode against local route servers (`tests/scope_fixture_site.py`, with a self-signed localhost certificate generated for each test run - no key in the repository), with a second server that must never see a request. No test reaches the internet.

Known limitations
- CORS is judged on the page response as served, without an `Origin` header: an API that reflects arbitrary origins is only visible with a request that sends one, which the report deliberately does not make (Observatory's -50 is therefore never given).
- HSTS preload is judged from the header; membership in the browsers' preload list is not looked up, so Observatory's +5 is never given and 160 is the highest score.
- Known differences from Observatory, all in `docs/site-checks.md`: the redirect check requests `http://host/` without the page's path and query; an `http://` URL of a site that also serves https is graded as given (Observatory grades the https response); redirects are followed at most 5 hops (Observatory: 10); CSP sources are sorted in the collator order of printable ASCII: compared with Observatory's own parser and CSP verdict on 20 000 generated policies each during development (the comparison script is not part of the repository), there is no difference for printable-ASCII sources; sources with non-ASCII characters may merge differently.
- Only the start page of `scope: "page"` is rendered in the browser; crawled pages, `paths` and `hosts` sections read the served HTML, so links and scripts added at runtime are not seen there. A crawl sends no cookies, so it sees what an anonymous visitor sees.
- A bare LAN host name (`nas`) cannot be told apart from a top-level domain and is refused in `hosts`; name it by IP or by its `.local`/`.lan` name.
- The TLS view is the one handshake a default client makes: negotiated protocol, cipher and certificate, no protocol/cipher inventory. `WEB_SEARCH_NEO_PROXY` is not used for that handshake.
- `perf_report` has no network or CPU throttling (G11) and no long-task/TBT metric.
- The HAR has no request headers, response bodies or phase timings, because the journal does not keep them.
- The lock state lives in the session, not in Chrome: a page's `getModifierState('CapsLock')` stays false and key events carry no lock modifier bit, because Chrome drops both for synthetic input. `http_session` jars live in the server process and are gone after a restart.
- Still open from 1.19: extra emulation (G11), `file://`/`serve_dir` (G14), screenshot paths outside the download folder (U4), `upload` relative paths (U5), one Chrome per temporary session (U8), `exclude_data_urls` and `offset` paging for the network window.

## 1.19.0

The functional audit (BUG-1..16, gaps, UX, docs) and the QA orchestrator's ten pains: keys behave like a real keyboard, journals page honestly, nothing is cut without saying so, JS has one semantics and a hard limit, and games get throttling, fresh-frame and aiming tools. Rule for the whole project from now on: **no silent truncation** - every cut carries a flag and a number.

Keyboard
- Enter is Enter. Selenium sent `Keys.ENTER`, which is the numpad key (`code: NumpadEnter`); it now sends `Keys.RETURN`, and `NUMPAD_ENTER` is its own name. The companion sent Enter without `text: "\r"`, so no `keypress` and no `insertLineBreak` fired and a multi-line canvas field never got its new line. Both paths are checked in a real Chrome against a real keyboard for Enter, NumpadEnter, Tab, Backspace, Delete, ArrowLeft, Home, End, Space, a and 1 (`tests/test_release_1_19.py`).
- DOM key names are accepted (`ArrowLeft`, `KeyW`, `Digit1`, `ShiftLeft`, the punctuation codes `Minus`, `Semicolon`, `Quote`, `BracketLeft`, `Backquote`, ..., `NumpadAdd` and the other numpad codes) and `"Control+Shift+K"` chords, `"Shift++"` included; an unknown name lists the accepted ones, and `CapsLock`/`NumLock` say why WebDriver cannot send them. The schema says that several keys in one `press_keys` are one chord.

Journals (network, console)
- `network` lists requests that never finished (a body nobody read, a fire-and-forget POST, a 5xx the page ignored, a long poll) with `done: false`, `state: headers|sent`, "(in flight)" in text output (`include_pending`, default true). Every answer says `returned`, `matched`, `in_flight`, and `omitted_older` + `truncated` when the window hid older rows. Long `data:` URLs are shortened in text with the number of cut characters. Logic in `network_log.py`.
- `console` reads a session history without side effects: `since_seq: N` returns the entries after N (it used to return the newest), filters no longer depend on a hidden cursor (they used to return nothing after the first read), `levels`/`contains`/`kinds`/`since_ms`, `dedupe` (one entry with `count`, `first_seq`, `last_seq`; paged in ascending order a fold counts only the repeats up to the entry that would open one group too many, and `next_seq` is the last entry it took, so `A B A C` with `limit: 1` reads A, B, A, C and never skips B), `order: asc|desc`; answers carry `matched`, `has_more`, `next_seq`, `history_seq`, `history_dropped`. Browser-log copies get their own seq and no longer duplicate hooked entries. `clear` empties the sources in the same read, so nothing logged in between is lost. The session history keeps 2000 entries, the in-page hook of server-launched browsers 2000, the companion's buffer (current Chrome) 500 per stream; what fell out is counted (`history_dropped`, `dropped`). Logic in `console_log.py`.

Fetching
- `fetch_text` decodes like a browser (WHATWG order): a BOM first (and removed from the text), then the header charset, `<meta charset>` (where `utf-16*` means UTF-8, and `iso-8859-1`/`latin1`/`ascii` mean windows-1252 as in browsers), valid UTF-8, then detection (charset-normalizer, already a dependency of requests); the answer names `charset_used`. A UTF-8 page served without a charset is no longer mojibake (`fetch/decoding.py`).
- A page over the byte budget is cut and says so (`body_cut`, `bytes_read`) instead of failing; text windows have `offset`, `total_chars`, `returned_chars`, `truncated`, `next_offset`; `output: "json"` returns that envelope.
- `http_request` lists every `Set-Cookie` separately in `set_cookies` (they used to be merged with ", ", unparseable because of the commas in Expires), reports `total_chars`, and no cookie rides along from one call to the next (the per-thread jar used to leak cookies between unrelated calls and agents).

No silent truncation
- `page_text` takes `offset` and answers `next_offset`, so a page past the 200 000-character cap is read to its end; `max_chars_capped: true` when the request exceeded the cap.
- `run_script`/`execute_js` keep `value_json` within `max_chars` (default 18000): strings by characters, arrays by whole items, other values as a JSON-text slice - each with `truncated`, `total_length`, `offset`, `next_offset`. `save_to` writes the whole value to a JSON file in the download folder (never over an existing file, at most 50 MB) (`script_results.py`). Internal script reads keep a 200 000-character ceiling, flagged the same way. A 3000-object answer used to be 212 KB.
- `fetch_links` ends its list with a line starting with `# ` (never a URL: `# truncated=true: ...` / `# body_cut=true: ...`) when the limit or the byte budget cut it; Bing rows carry `page_cut_at_bytes` when its page was cut; `fetch_text`'s byte budget grows with `offset + max_chars`, so a long page can be read to its end.
- `network_body` after a navigation answers `body_available: false` with an explanation instead of chromedriver's native stack.

JavaScript
- One semantics for `run_script`, `execute_js` and `wait.script`: an async function body - top-level `await` works, `return` returns, a one-line expression returns itself (a `;` or `return` inside quotes does not count; `debugger`/`with` are statements); a SyntaxError fails at once (`syntax_error: true`) instead of being retried until the timeout (`actions/repl.py`) - only a script that did not compile: a SyntaxError thrown while it runs (`JSON.parse("x")`) is an ordinary script error.
- A limit for every script (`timeout_seconds`, default 15, max 600): past it the call answers `timed_out: true` instead of holding the session (and `browser_status`) for 120 s. A slow await or a companion call that ran out of time leaves the browser as it is. Only when chromedriver of a browser the server launched stops answering (the transport timed out: an endless loop froze the tab) is the driver marked, and every close path - `close`, `close_all` (with `idle_for_seconds` too), the idle-TTL sweep at the session cap and process exit - then stops that browser and every process it started outright (`forced`; `taskkill /T` on Windows, the `pgrep -P` tree on POSIX, or `/proc` children / ppids without pgrep) in about a second instead of waiting out chromedriver's 120 s, and still lets go of the tab claim and mocks. A kill that could not be completed (no pgrep and no `/proc`, `taskkill` missing or failing) comes back as `problem`/`warning` and in the log, never as a clean stop; a chromedriver that has already exited is not killed at all (its pid may have been reused) - only its profile is removed. chromedriver's temporary profile is removed only for a temporary or isolated session, only when it is a `scoped_dir*` folder inside the system temp directory - a persistent `profile_id` spelled `scoped_dir_work` keeps its cookies. After any script timeout, `close` gives the page-side teardown one shared 6 s deadline and 2 s per command; the deadline is checked before every step and, inside the injected-state step, before every command (badge, dialogs, headers, each new-document script, stubs), so nothing page-side is sent after it and whatever it skipped is named in the warning (detach, tab close and claim release are never skipped). The user's own Chrome and attached browsers are never killed: `close` runs the ordinary teardown (debugger detached, headers, scripts, mocks and the claim released). The mark clears after the next successful call, and the script limit is restored after every call.

Sessions and pages
- Browsers the server launches download into their own folder (`<download dir>/sessions/<session>-<time>`), not the owner's ~/Downloads; the new `downloads` action lists the files (`in_progress` for partial ones) and a click reports the files it downloaded.
- JS dialogs: `alert`/`confirm`/`prompt` are answered by the session's policy and logged; the new `dialogs` action reads the texts and sets `policy: accept|dismiss` (+ `prompt_text`) for this and later documents. A click reports the dialogs it raised. Automatic in browsers the server owns; in the user's Chrome only after `dialogs` is called, and a tab handed back (`close` of an attached tab, `open` leaving a borrowed one) gets the page's own `alert`/`confirm`/`prompt` back and loses the registration (`page_guards.py`).
- Downloads rerouting that Chrome refuses is reported (`download_routing: failed` in `downloads`) instead of pretending; session download folders older than a week are swept. The click's dialog/download report never fails a click that went through.
- New `navigate` action: a new URL in an open session, keeping its browser, profile and options. `open` without `profile_mode` keeps an existing session's mode (a second `open` of an isolated session used to fail with "different browser/profile options").
- Click verification: `dialog_open` counts only a dialog that is on screen (a hidden `role=dialog` or a closed modal read as open); a dialog that opened, a JS dialog or a download is an effect; a change elsewhere on the page is `effect_detected: true` with `effect_confidence: "low"` and `verified: null` - no longer "no_observable_change".
- Every `web_action` result carries `duration_ms`.
- `close_all` says why each kept session was kept (`kept_because: other_agent|used_recently`); the idle filter no longer calls anonymous sessions "other agents'" and suggests `scope='all'`.
- `set_extra_headers` refuses non-ASCII values (Chrome sent `?` for each character); `context.geolocation` also grants the geolocation permission, so `getCurrentPosition` answers instead of "User denied Geolocation".

Search
- The default engine is `brave`: in the audit `duckduckgo`, `mojeek` and `startpage` (via ddgs) came back empty for every query, so every search paid for a fallback. An engine that keeps answering nothing where a later engine finds hits is tried last and named in `unreliable_engines` (never cooled down). Rows that mention no word of the query are `off_topic`: the next engine is asked, the engine is listed in `engines_off_topic`, and such rows are returned only when nothing better exists (`result_status: "off_topic"`, with a `note`). Bing's `bing.com/ck/a` tracking links are unwrapped to the real URL (`search_quality.py`).

Games and screenshots (orchestrator pains 1-6)
- `game_probe.frame_health`: measured `raf_fps`, `throttled`, `throttle_reason` (`hidden`, `occluded_or_background_window`), `hint`. New `unthrottle` action (focus + lifecycle emulation; the companion allowlist gains `Emulation.setFocusEmulationEnabled` and `Page.setWebLifecycleState`) measures before and after and says honestly when only raising the window helps (`frame_health.py`).
- Screenshots: `wait_frames` renders N frames first; every capture reports `image_width/height`, `css_box`, `viewport_css_width/height`, `device_pixel_ratio`, `scale`, `frame_id`, `captured_at_ms`, `changed_since_last` (and `raf_stalled` when no frame came). `web_info screenshot` returns this JSON next to the image. `pointer` takes `coordinate_space: "image"` to aim in the last capture's pixels (`frame_capture.py`).
- New `look` action: a pointer-locked camera turns by exactly `dx`/`dy` in `steps` relative moves over `duration_ms`. New `wait_frames` action for the focus-then-keys gap. `docs/playing-games.md` gains sections on turning, throttling, focus and aiming from an image.

Contract, docs, tools
- `action_schema` carries a description for the parameters that were opaque (`points`, `key_actions`, `pointer_actions`, `set_cookies`, `keys`, `coordinate_mode`, `delta_x/y`, `script`, ...) (`contract/param_docs.py`). Touch's `end_x/end_y` are documented inside each point; the README latency table says it is step-mode and why normal mode is slower.
- Playbook sections `audit` (site and security review: headers, cookies, console, network, third parties) and `testing` (a scenario with per-step assertions), recipes `fps_game`, `audit`, `test_run`; pitfalls reworded to fit the contract budget.
- `scripts/mcp_cli.py serve|send|repl|stop`: one server kept alive between calls (sessions survive; a call is a round trip instead of a ~12 s cold start). The port is bound first (a busy port is an error, exclusive on Windows), then the token file is created empty, made readable by the user only (`chmod 600`, on Windows `icacls` for the current user alone), only then written and atomically renamed into place, then the server child starts; a failed start leaves neither a token nor a child behind (a failed write or rename removes the temporary file; a `PermissionError` on the rename is retried twice; a missing `icacls` is an error, not a crash), and a token file is removed only while it is still this daemon's. Tokens are compared as bytes. Images are written to files.
- Current Chrome: DOM nodes in script results become `{}` wherever they sit (`{el: node}`, nested arrays), not only at the top. Uploads say how the file got in: `attach_method: "set_file_input_files"` (Chrome read the path itself) or `"streamed"` with `stream_reason`. If Chrome refuses access to the file (a bare "Not allowed"), the server reads the file and hands its bytes to the page itself and reports `attach_method: streamed`; the `input`/`change` events are then synthetic (`isTrusted=false`). Chrome gives the same "Not allowed" when the companion's "Allow access to file URLs" is off and, apparently, when an administrator forbids it - the two cannot be told apart. A refusal whose text names a policy is an error and is not worked around (`cdp/element_handles.py`).
- `local_storage` read and `replay_request` keep a script answer's window flags (`truncated`, `total_length`, `next_offset`; a value windowed as JSON text comes as `value_json_part`/`response_json_part`), so a big value is never cut or turned into `None` silently; `replay_request` also reports `body_chars`, and whenever the body was cut (20 000 characters, `response.truncated`) or the answer windowed, `window_note` gives the same fetch as a `run_script` with `save_to`, which returns all of it.
- The companion allowlist guard scans every module of the package and knows every DevTools domain; methods sent only to server-launched browsers are listed with their reason.
- 1.18.5 leftovers: the companion auto-reload compares versions (only an older running companion is reloaded), re-reads claims right before reloading, sanitises its state file, and the daemon refuses `runtime.reload` while an agent drives a tab; `cookie_scope` uses `tldextract` offline (no network fetch of the suffix list) and falls back on any error.
- New modules instead of raising ratchets: `network_log.py`, `console_log.py`, `page_guards.py`, `extra_actions.py`, `script_results.py`, `search_quality.py`, `frame_capture.py`, `frame_health.py`, `fetch/decoding.py`, `actions/repl.py`, `perception/text_window.py`, `sessions/roster.py`, `contract/param_docs.py`.

Known limitations (deliberately left for a later release)
- No automatic `aim_at`: `look` turns by an exact amount, and the playbook describes the screenshot -> turn -> fresh frame loop; judging "on target" is the game's business.
- `open` still defaults to `profile_mode="current"` for a new session (U1); the `audit`/`testing` sections and recipes open `isolated` explicitly. Changing the default would change every existing caller.
- Task profiles are playbook sections and recipes, not one-call reports: no `security_report`, `perf_report` or `test_run` actions yet (G9).
- Selector clicks in Selenium modes still use chromedriver's click (measured 100-300 ms on a local page; the audit's 2-3 s did not reproduce); every result now carries `duration_ms` to catch it.
- Not done from the gap list: new tabs/popups of Selenium sessions (G10), extra emulation (`device_scale_factor`, `color_scheme`, network/CPU throttling, permissions list - G11), `click_text` through shadow DOM, `persist` for temporary/isolated, `file://`/`serve_dir` (G14), `summary: "min"` (U2), screenshot paths outside the download folder (U4), `upload` relative paths (U5), one Chrome per temporary session (U8), `third_party_only`/`exclude_data_urls` network filters, `offset` paging for the network window.
- `unthrottle` in the user's own Chrome needs the 1.19.0 companion (two new allowlisted methods); an older companion refuses them and the answer says so under `refused`. The server reloads an outdated companion by itself when no agent drives a tab.
- `http_request` still sends a fixed desktop User-Agent string.
- `pointer coordinate_space="image"` is refused together with `frame_selector` (the capture is of the top document); give frame-local CSS pixels there.
- A tab handed back gets its own dialogs back in the top document and same-origin frames; a cross-origin frame - same-site ones included - keeps the answerer until it reloads (the registration itself is removed).
- A page that replaces `alert`/`confirm`/`prompt` itself after the answerer was installed keeps its own versions on hand-back: the saved originals are the ones found at install time.

## 1.18.5

Minor follow-ups to the 1.18.4 audit, and a companion that recovers on its own after a server update.

- Companion after an update: Chrome keeps running the service worker it loaded, so after an update the live companion kept old code (found on the owner's machine: a stale worker) and lacked the newest commands. The server now reloads a connected companion whose manifest version differs from its folder by itself - on `browser_tabs` and before `open`, reported as `companion_refresh` in both answers - only while no agent drives a tab (no daemon claim and no current-Chrome session in this server), at most once per 5 minutes across all MCP server processes of the user (per-user state file under a cross-process lock), and never again for a build whose reload did not take (`self_update: ineffective` with the manual steps). A companion that only differs by code hash (same version, an edited checkout) is reported (`not_attempted`) and never reloaded automatically. `browser_tabs` answers a bridge that is up without a companion with a `companion_note` (`wait_seconds` now up to 90). The logic lives in `companion_refresh.py`.
- `attach_tab` no longer has a `persist` parameter (a claimed tab is the user's and is never parked); its docstring and contract say so.
- `cookies clear` with a domain without a dot or a public suffix (`com`, `co.uk`, `github.io`, `kiev.ua`, `myshopify.com`, `blogspot.*`, ...) needs `confirm_clear_all`, and so does a filter that reaches more than 5 different hosts. A real Public Suffix List is used when `tldextract` or `publicsuffix2` is already installed (no new dependency); otherwise a built-in list. `localhost` and IP addresses are single sites. The clear logic moved to `actions/cookie_scope.py`.
- The click probe is also disarmed when the click happened but its evidence was never collected (the settle or the page summary failed).
- A live persist session's record is not listed under `parked_expired` when its expired record is put back.
- `main.py --bridge --stop` tells a free port ("No bridge daemon") from a listener that never completed the handshake (exit code 1), and waits at most 15 s for the link.
- `type_text mode="keys"` into a `tabindex` element carries a `keys_warning`; the contract says keys without a selector are for canvas/game surfaces, not ordinary sites with keyboard shortcuts.
- `scripts/mcp_cli.py`: a stdio MCP client for agents without an MCP connector (`call` for one tool call, `run` for a JSON list of steps against one server process).
- The two-server daemon convergence test retries its cleanup stop once, so a loaded machine cannot leave the spawned daemon holding the port.

## 1.18.4

Follow-up to the field report (`websearchneo-bugs.md`) plus the gaps found while driving it: sessions follow tabs Chrome itself replaces, survive the MCP client when explicitly asked, and every click, typed text and script result says what really happened. Everything that touches the user's own Chrome refuses rather than guesses.

- Fix (outside the report, serious): `cookies op=clear` with `name`/`domain` passed them to `Storage.clearCookies`, which has no filter and wiped every cookie of the profile - every login of the user in current Chrome. A clear now needs a domain (a name alone exists on every site), reads the jar, deletes exactly the matching cookies with `Network.deleteCookies` (partitioned CHIPS cookies with their `partitionKey`) and reports what a fresh read shows as gone, anything left as `not_deleted`; clearing without a domain is refused unless `confirm_clear_all: true`. `get` uses the same domain rule (the domain and its subdomains, not a substring).
- Recover a companion whose service worker is not running. When Chrome answers the popup with "Receiving end does not exist" (the bridge then sees "No SW" on every command), the switches cannot reach the worker at all and Reconnect used to fail silently. The popup now says the worker is stopped, disables the switches it cannot deliver, and turns Reconnect into "Restart companion", which calls `chrome.runtime.reload()` from the popup - the same as pressing Reload on chrome://extensions, without leaving the browser.
- Tab following: the companion records `chrome.tabs.onReplaced` (`tab-follow.js`), redirects commands aimed at a replaced tab to its successor, reports that on the answer (`tab_followed`, relayed by the daemon, recorded by the bridge client) and answers the new `tabs.resolve`. The server moves the session and its daemon claim to the successor only on that record and only when the successor is alive; ownership is kept (it is the same page), and a successor held by another session or agent drops the session instead. No heuristics: a tab the lost one opened or a tab on the same URL is never taken over. The work happens off the event loop with a bounded wait for the session lock. Read-only topics and `wait`/`screenshot` are repeated once; `execute_js`, `game_probe`, `reload`, `cookies` and every other write fail with `SessionTabFollowed`; a second dead tab after the repeat is translated too.
- Persistent sessions: `open` takes `persist: true` (current Chrome only, explicit `session_id`, never `default`); only tabs the server opened are ever parked, and `attach_tab` refuses `persist`. At process exit such a tab is detached and left open and `sessions/parking.py` records it (per-user file under a cross-process lock; tab id, Chrome run, agent group, redacted URL, `label_tab`; no title). A later client continues it only explicitly with the new `reattach` action (or `open` with `persist: true`, or `attach_tab` on that same tab), and only when the tab is provably the same one: same Chrome run, still in the agent group, same origin, not driven by another client - otherwise it answers `success: false`, the record is dropped and the tab left alone (a server tab that only changed site comes back as `left_open_tab`). A live session keeps its record fresh while used, and expiry and retiring never touch a session or tab that is live in this server or another client. An explicit `close` of a parked session, an `attach_tab` under its name, and TTL expiry (`WEB_SEARCH_NEO_PARKED_SESSION_TTL`, default 24 h, minimum 60 s; unreadable or future timestamps count as expired) close the tab if the server opened it and report it (`retired_parked`, `parked_expired`). `browser_status` lists `parked_sessions` with redacted URLs. `attach` is an alias of `attach_tab`.
- `type_text` gains `mode="keys"`: one key per character (Cyrillic and other non-Latin text unchanged, upper-case Latin under Shift, at most 500) for canvas engines such as Unity WebGL that never receive `Input.insertText`; without a selector keys go only to an editable control, a canvas, an explicit-tabindex element or a frame (never a focused button or link), and a focused `<canvas>` switches to keys automatically. Focus is found through open shadow roots and same-origin frames (typed there through CDP so focus is not moved); a cross-origin frame is "unknown" and not refused; nothing focused or a read-only control is refused. The contract gains a `canvas_text` recipe and `docs/playing-games.md` a section on typing into canvas games.
- `click` verification: a MutationObserver armed right before the click (hidden under a registry Symbol, disarmed if the click never happens) records which nodes changed; `verified`/`effect_detected` count only changes in the clicked element's subtree or ancestors, focus moving somewhere other than the element, the element leaving the document, or a URL/title change. Frame, ref and shadow targets report `verified: null`. `post_state` carries the raw counts, and no answer suggests clicking again.
- `execute_js`/`run_script` add `value_type`, a `value_note` for a script without `return`, a readable error for cyclic/window results, a clear error for a frame that cannot be entered, and `cross_origin_frames` on an empty answer from a framed page.
- `fill` resyncs a React-style controlled input whose value tracker missed the write (`framework_resynced`), so `onChange` runs without `typing=true`.
- `page_elements` rows with a fragile selector (nth-of-type chains, React ids including React 19 `«r1»`/`_r_1_`) carry `stable_selector: false` and `suggested_locator` (role + accessible name for `click`); testing hooks come back as `testid`.
- `cookies get` reports `total`, `limit` and `next_offset`.
- The session-cap error lists every holder (agent, tab, age, idle, busy, redacted url) and how to release orphans; `close_all` takes `idle_for_seconds`.
- `wait` takes `seconds` for a plain delay without selector or session; `web_action` gains a `screenshot` action that saves the PNG (`.png` only, inside the download directory, `overwrite` explicit, checked before the capture).
- Action summaries carry `title_pending: true` while an HTML page's title is still empty (never for about:blank, JSON or images); `fetch_text` flags a title-only page that loads scripts as an SPA shell and names the exact browser calls.
- The companion's code hash now covers every module the worker runs (`code-hash.js`, `chrome_bootstrap.CODE_FILES`), so a changed helper is detected like a changed worker.
- `main.py --bridge --stop` waits for the daemon link to form instead of giving up after the 2 s start timeout, so a cold process no longer reports "No bridge daemon" for a daemon that is running.
- The capabilities contract was tightened to stay well under its size budget (pitfalls reworded, not dropped).
- Pure page scripts moved out of `browser_tools.py` into `perception/challenge.py`, `perception/action_scripts.py` and `sessions/tab_label_source.py` (unchanged, re-exported), which keeps it under its old ratchet, and `service-worker.js` shrank with its code hash moved to `code-hash.js`; the `main.py` and `chrome_bridge.py` ratchets were raised for the new wrappers and the follow report.

## 1.18.3

Detect a stale companion even when its manifest version matches. Chrome keeps running the service worker it loaded until someone presses Reload, so an unpacked extension whose folder gained new commands (such as `capture.viewport`) can report the current version while refusing the method with "Unknown bridge method". The companion now hashes its own service worker at connect and sends the digest in hello; the daemon compares it against the file on disk, flags `stale_code` in browser_status, and setup_current_chrome reloads the companion automatically when only the code is out of date.

- `browser_status` reports `stale_code` alongside `outdated`.
- `setup_current_chrome` self-reloads a same-version-but-stale companion instead of saying "Nothing to do".

## 1.18.2

Bugfix release: the twelve defects from the field report (`websearchneo-bugs.md`), each with regression tests.

- `type_text` without a selector no longer dies with `AttributeError: 'dict' object has no attribute 'send_keys'` on the companion bridge, where `document.activeElement` arrives as a plain dict. It focuses the control and types through CDP `Input.insertText`; a driver with neither a typable element nor a CDP channel gets a clear `ValueError` instead of an internal exception.
- A dead tab no longer reads as `Error: No tab with given id`. When a call lands on a tab that is gone, the session is dropped and the error says the session lost its tab with the exact `open` call to redo it; a tab that is still alive (or a companion that never answered) keeps the original error untouched.
- `execute_js` has one result contract: `value` is always plain JSON (DOM nodes arrive as `{element: tag}` descriptors, never live handles) and `value_json` is the same value as one JSON string. It also accepts `frame_selector` to run inside one same- or cross-origin frame - a top-document script cannot see into cross-origin frames, so framed pages no longer get silently partial answers - and always hands the driver back at the top document.
- `cookies get` pages with `offset` (`count`/`returned`/`truncated` describe the window), so the thousands of cookies past the old hard window are reachable.
- `click` reports `page_changed`, and `no_observable_change` with a `change_note` when the URL and title sit still after a successful click, instead of a bare `success: true`.
- The session-cap error names every holder's idle age (`Idle for:`), and sessions idle past `WEB_SEARCH_NEO_SESSION_IDLE_TTL` (default 30m, `0` disables) are reaped automatically when the cap is hit. Busy sessions are never reaped.
- Fill and every other locator path accept an occurrence suffix - `input.qty[1]` is the second match in document order (0-based); an N past the match count is refused naming how many matched. The `fill` notes document `typing=true` as required for React-controlled inputs (no keystroke stream means `onChange` never runs).
- `fetch_text` flags SPA shells with `spa_suspected=true` plus "SPA: use browser session" instead of silently returning title-only content.
- `page_text mode="main"` falls back to the full body with `fallback_used=true` instead of returning a fractional sliver; `page_elements` prefers `data-testid`/stable ids/`aria-label`/`title` over React-generated ids and `nth-of-type` chains; late titles settle up to 2.5 s and otherwise come back as `null` with `title_pending=true`.
- `icacls` output is decoded from bytes (`cp1251`) instead of text mode, so the bridge handshake no longer breaks under `PYTHONUTF8=1`, and non-ASCII locale summary lines are skipped when pruning foreign ACEs.
- Architecture ratchets raised to the new file sizes after explicit review (browser_tools 8514, main 2724, page_perception 2720, chrome_bridge 2055); `web_info(topic='screenshot')` already covers the standalone-screenshot ask.

## 1.18.1

- Keep ChromeDriver windowless for cached, uncached, and retry launches. Avoid passing Selenium a duplicate startup-info argument that previously forced an unprotected fallback. Test processes now also cover the native Windows multiprocessing spawn path.
- Show the virtual cursor for ordinary selector clicks as well as coordinate input. Capture the target before it disappears, map iframe targets into the main viewport, restore a cursor removed by page updates, and recognize the public pointer-action names for click rings.
- Keep the windowless stdio proxy responsive to small requests and let it exit when its child finishes even if the client still holds stdin open. Preserve module execution and inherited streams when bypassing Windows interpreter launchers.
- Honor per-call script timeouts with the Selenium transport as well as the Chrome bridge, restoring the original timeout after success or failure.
- Save the companion presence preference before changing its active state, so failed storage writes can be retried.
- Capture current-Chrome viewports through a bounded single-frame screencast without activating tabs, restoring windows or changing page geometry. Stop owned captures and remove listeners after success, timeout or detach; refuse overlapping captures.
- Remove foreground recovery advice from screenshot errors. Full-page and region captures retain the surface path and may still time out; use viewport or DOM inspection instead.
- Add interactive element filtering before pagination, and concrete schema recovery guidance for browser agents.

## 1.18.0

The favicon badge stops replacing the site's icon - it is a small corner mark now.

- The presence slime shrinks from a 20 px tile covering the bottom-right quarter of the tab icon to a 12 px mark in the corner (`web_search_neo/agent_presence.py`, `PRESENCE_VERSION = 6`, so live pages reinstall it). The site's own favicon stays fully visible; at tab-strip size the mark reads as a small dot - awake and green while an agent acts, sleepy amber for five minutes after. A page with no icon of its own now gets only that corner dot instead of a full-size slime standing in for the missing icon.
- Console MCP servers can run windowless under any client: `scripts/quiet_stdio.py` starts the server with `CREATE_NO_WINDOW` and proxies stdin/stdout/stderr byte-for-byte, so opencode-style clients no longer pop a console per session. The proxy joins its output pumps before exiting, so the final flush is never lost on slow machines (the exit code stays the server's). Its pumps use `read1()` instead of `read(n)`: with n above the buffer size, `BufferedReader.read` blocks until exactly n bytes arrive, which stalled long-running servers - an MCP initialize request from a live client sat in the pipe forever and every reconnect attempt timed out. Small messages now stream through immediately.
- On Windows the token files' ACLs are now cleaned of leftover explicit entries: newer `icacls` builds (Windows Server 2022 images included) keep `SYSTEM`, `Administrators` and owner-rights entries after `/inheritance:r /grant:r`; every ACE that is not the current account is removed, so bridge tokens really are readable by one user only.

## 1.17.0

Feature release: per-call CDP timeouts for promise-waiting scripts.

- `browser_execute_js` and `browser_run_script` accept an optional `timeout_seconds` (capped at 600) that extends the CDP-layer wait when `await_promise=true`. A promise that outlives the ~15 s script default - waiting on a human solving a captcha, a long network round-trip - no longer dies as a bare `cdp.send timed out`; it now waits as long as you ask.
- The timeout is threaded through every layer (MCP tool -> browser_tools -> scripts -> chrome_bridge) and clamped to [1, 600] at the bridge; plain Selenium backends keep their own defaults when no override is given.

## 1.16.3

Patch release: the windowless MCP entry survives venv launcher redirectors.

- `make_mcp_config.py` on Windows now points the command at the venv's base
  `pythonw.exe` with `__PYVENV_LAUNCHER__` naming the venv, instead of the
  venv's own `pythonw.exe`. Some venv implementations (uv's included) ship the
  launcher as a shim that starts the real interpreter as a child process - a
  windowless shim with a visible console child, which is exactly the window
  1.16.2 set out to remove. The base interpreter starts as one windowless
  process with the venv's packages, the same hand-off the bridge daemon
  already uses.

## 1.16.2

Patch release: nothing the agent runs may pop a console onto your screen.

- The generated MCP configuration points at `pythonw` on Windows: a console
  interpreter under a windowed MCP client owns a visible console for the whole
  session, while `pythonw` runs the same stdio pipes with no console at all.
- `scripts/bridge_autostart.bat` no longer falls back to a console interpreter;
  autostart either runs windowless through `pythonw` or logs why it did not.
- The test suite spawns node and python with `CREATE_NO_WINDOW` on Windows, so
  agent-driven test runs stop flashing consoles too.

## 1.16.1

Patch release: the in-page signals gain a switch in the extension popup.

- **Agent presence** in the popup turns the favicon badge, the action flash and
  the ghost cursor off for the user's own Chrome, without touching the bridge
  connection. On by default. The companion answers the server's paint requests
  with success-shaped stand-ins (`Page.addScriptToEvaluateOnNewDocument` gets a
  placeholder identifier, presence `Runtime.evaluate` gets `true`), so actions
  are unaffected; flipping the switch mid-session restores already-painted tabs
  at once, and enabling needs nothing - the next action ping reinstalls the
  script through the usual fallback. `WEB_SEARCH_NEO_AGENT_PRESENCE=0` still
  turns the signals off everywhere and wins over the popup. Selenium-driven
  sessions never pass through the companion and keep obeying the environment
  variable only.

## 1.16.0

Release focus: a visible virtual pointer, and the paperwork that says who opens
whose Chrome.

Ghost cursor:

- Every coordinate-bearing action now glides a ghost cursor across the tab it
  ran in, with the agent's name riding next to it, and every press lands a
  fading ring where it happened — red when the press was refused
  (`web_search_neo/agent_presence.py`, `PRESENCE_VERSION = 5`, so live pages
  reinstall it). Pointer input was and stays synthetic CDP events: the
  operating system's mouse never moves, agents in different tabs cannot disturb
  each other or the user, and the cursor is one drawing per tab, purely for the
  watcher. The cursor and the rings hide before every screenshot and the cursor
  is shown again right after, so an agent never photographs its own arrow; a
  handed-back tab is cleaned as before. `WEB_SEARCH_NEO_AGENT_PRESENCE=0` turns
  all three signals off together.
- Tests in `tests/test_agent_presence.py`.

Docs:

- README and the bundled skill now say who opens and closes Chrome: `open` with
  `temporary`/`isolated`/`persistent` starts an MCP-owned browser that `close`
  quits again; `attach` only detaches from a Chrome the user started; `current`
  never launches or quits the user's Chrome. New "Starting fresh after a
  reboot" recipe in INSTALL.md: with the logon launcher in place the bridge is
  already listening and opening Chrome reconnects the companion by itself.

## 1.15.0

Release focus: a bridge handshake that never sends the secret, a fetch layer
that cannot be pointed at cloud metadata, paid CAPTCHA solving that only runs
when asked for, and an installable wheel.

Fixed:

- Tests were red on `main`: four stale tests are updated to the current behaviour.

Agent presence:

- The tab favicon keeps the site's own icon and gets a small semi-transparent
  slime in its bottom-right corner — awake and green while an agent acts, sleepy
  and amber for five minutes after (`web_search_neo/agent_presence.py`,
  `PRESENCE_VERSION = 4`, so live pages reinstall it). The "working now" phase
  lasts 30 s, matching the toolbar badge, and the slime is drawn at 20 px of
  the 32 px icon so it stays readable at tab-strip size. Our icon link is
  re-inserted after the page's icons are parked, because Chrome otherwise kept
  showing the page's own icon. A favicon the page cannot read back
  (cross-origin, canvas tainted) is left alone instead of replaced.
  The older green-dot badge in `web_search_neo/sessions/activity.py` stands down
  when the presence script is installed and no longer swaps in a robot icon.
  A borrowed tab that was never marked is handed back without running the
  restore script in it.
- The companion shows a per-tab toolbar badge — green `AI` while an agent acts,
  amber for up to five minutes — whose tooltip names the agent, and the popup
  gets an **Agent tabs** card listing tabs with agent activity in the last five
  minutes (agent, active now / N s or N min ago, claim holder); a click focuses
  the tab.
  New `chrome-extension/agent-activity.js` and `agent-badges.js`. The daemon
  attaches the requesting client's `agent` identity to every relayed command;
  `WEB_SEARCH_NEO_AGENT_NAME` gives the agent a friendly name.
  `scripts/companion-widget-preview.html` accepts `agents=N` for the card.

Bridge security:

- Bridge protocol 2: a mutual HMAC challenge-response in which the raw token
  never crosses the socket and the daemon proves itself first
  (`web_search_neo/bridge_handshake.py`, `chrome-extension/bridge-auth.js`).
  Protocol-1 companions are refused with a close reason that says to update
  and reload the extension.
- Token files are written atomically, locked down before the secret is written,
  and on Windows restricted by ACL to the current user (`icacls`). A new token
  is minted only when the file is missing, never because a read failed.
- The daemon enforces tab claims: a command for a tab claimed by another
  connected client is refused (`tabs.get` stays allowed). `hello` must name a
  `role`, and the `extension` role requires the extension `Origin`. Connections
  are capped at 64 open and 16 still in the handshake. The claim reaper no
  longer broadcasts under the lock and skips claims of connected clients.
  Tab ids are read the way JavaScript `Number()` reads them (so `42.0` cannot
  slip past a claim), and a reconnecting server keeps its own claims.
- An existing token file is re-locked to the current user on every load, not
  only when it is first written.
- The bridge daemon is started with `CREATE_BREAKAWAY_FROM_JOB` on Windows when
  the parent's job allows it, so an MCP client that runs servers in a
  kill-on-close job object does not take the shared daemon down with it.
- Upgrade note: a 1.14 companion cannot reload itself into protocol 2 through a
  1.15 daemon. Press Reload once on `chrome://extensions` after updating, and
  end a still-running 1.14 daemon (its `--bridge` python process): a 1.15
  `--bridge --stop` speaks protocol 2 and cannot talk to it.

HTTP and fetch:

- `http_request` returns the body of 4xx/5xx responses.
- SSRF guard: link-local and cloud-metadata destinations are always blocked,
  hostnames are resolved before the check, and a redirect from a public host to
  a private one is refused. Direct loopback/private URLs remain allowed.
- A cross-origin redirect keeps only non-credential headers.
- `save_to` is confined to `WEB_SEARCH_NEO_DOWNLOAD_DIR` (default `./downloads`)
  and refuses to replace an existing file unless `overwrite=true`.
- Bodies are read under a total deadline, timeouts are capped at 120 s, and
  sensitive query values (`token`, `key`, `sig`, `session`, …) are redacted in
  logs (`web_search_neo/fetch/safety.py`, `web_client.py`).

CAPTCHA:

- Paid solving is opt-in only. `mode='auto'` now waits for a human; a service
  is used with `mode='solve'`, or with `auto` when
  `WEB_SEARCH_NEO_CAPTCHA_AUTO_SOLVE=1`, and always needs
  `WEB_SEARCH_NEO_CAPTCHA_KEY`. A token is applied only if the page URL has not
  changed, success is reported only when it was actually applied, the widget's
  `data-callback` is invoked, and reCAPTCHA Enterprise uses its own task type.

Waits and sessions:

- A plain sleep and challenge waits no longer hold the session lock. Wait,
  captcha and challenge timeouts are capped at 300 s and report `timeout_note`
  when clamped.
- Fix a busy spin in the `chrome_bootstrap` reload poll. The macro one-time-submit
  ledger uses a cross-process file lock. Request mocks are cleared on every
  teardown path. Frame selection reports real errors. Stealth `languages` follow
  the session locale.

Packaging and server:

- `pip install .` works: packages are listed explicitly, the extension ships as
  `web_search_neo/chrome_extension`, and an installed copy is mirrored to
  `%LOCALAPPDATA%\WebSearchNeo\extension` (or `~/.local/share/web-search-neo/extension`)
  where the bridge token can be written (`web_search_neo/extension_path.py`).
  The `wsn` entry point works, and CI builds and installs the wheel.
- `msp_server.log` moved to a per-user state directory:
  `%LOCALAPPDATA%\web-search-neo\logs\msp_server.log` on Windows,
  `$XDG_STATE_HOME/web-search-neo/logs/msp_server.log` (default
  `~/.local/state/...`) elsewhere; `WEB_SEARCH_NEO_LOG_FILE` overrides it, and an
  unwritable location disables the log instead of stopping the server.
- `mcp` pin loosened to `>=1.29,<2`; FastMCP internals are isolated in
  `web_search_neo/mcp_compat.py`. A failed `web_action` batch returns MCP
  `isError=true` with the JSON payload. `make_mcp_config.py` prefers the project
  `.venv` python. Plugins are not loaded in `--bridge` mode.
- `msp_date_time` prints correct `±HH:MM` offsets and DST zone names.

Cleanup:

- Removed the deprecated `driver.py`, `docs/audit-1.11.1.md` and tracked
  `.vscode` settings; `.opencode/` and `.vscode/` are ignored. Unused and
  mid-file imports cleaned up. Legacy module size ratchets were lowered and a
  test now forbids raising them. The cover image is compressed.

## 1.14.0

Release focus: reliable text input for React forms, plain sleeps without polling,
and convenient REST API testing without curl.

- Add `http_request` fetch action: any method, custom headers, query params,
  raw `body` or `body_json`, returning `{status, headers, body}` so 4xx/5xx
  are answers, not exceptions — the model's curl replacement.
- Add `type_text` action: types via a single CDP `insert-text` call. With no
  `selector` it types into the currently focused element, with `selector` it
  focuses the target first; single-call insertion keeps React controlled inputs
  in sync where keystroke-by-keystroke input drifts.
- Allow `wait` with neither `selector` nor `script` as a plain sleep: reports
  `state:sleep`, floors at 0.1s, no polling round trips.
- Add contract notes for `type_text`, `wait.sleep`, and `run_script` body return
  so `action_schema`/`capabilities` document the new behaviour.
- Raise the `capabilities` budget 14_000→15_000 to fit the new notes; skill
  budget stays under 7_000.
- Tests in `tests/test_type_text.py` plus wait-sleep cases in
  `tests/test_bugfix_bundle.py`, and `tests/test_http_request.py` for the API action.

## 1.13.0

Release focus: LMS batch download macros and page_elements filtering for large tables.

- Fix `browser_get_page_elements` truncation on pages with hundreds of controls.
  The LMS group page renders 380 links but the 18k character budget delivered only
  26 (80 of 380 when limited to links) — the 36 lesson rows were lost. New
  server-side filters `href_pattern` and `text_pattern` (case-insensitive substring
  matches on `href`/`text`) run before the budget is applied, so
  `href_pattern="/lesson/view/"` returns exactly the 36 lessons without growing
  `max_chars` or paging. Both filter links and buttons; forms stay unfiltered.
  Wrapper `browser_get_page_elements` in `main.py` exposes the same two parameters.
  Fixes the “26 of 380” failure that blocked the LMS Unity course automation.
- Add project macros for the LMS Unity course (36 lessons, `98781969`):
  `lms-unity-list-lessons` (3 steps: open group, wait `#group-lessons`, run_script to
  return deduplicated `lesson_url/title/note`) and `lms-unity-one-lesson` (4 steps:
  open lesson, wait `#lesson-editor`, run_script for `/storage/` PDFs and for
  `docs.google.com` links). Both use `{{lesson_url}}`/`{{session_id}}` placeholders
  so one macro repeats 36 times with different variables. Verified live: list returns
  36/36, one-lesson returns 4 storage PDFs (methodichka + presentation) for M1U1.
- Add `scripts/lms_unity_batch.py` — one-command batch helper that reuses the same
  browser session (`lms`, `profile_mode=current`): collects 36 lessons, extracts
  storage PDFs per lesson (public, no auth needed for `/storage/`), downloads to
  `data/materials_inbox/lms-unity/<M1U1>/`, and validates extraction via
  `feedback_bot/source_parser`. Prints a summary ready for `courses/unity_36.md`
  (`# Course -> ## Module -> ### Lesson -> ####`).

## 1.12.0

Release focus: visible agent presence — a human watching the browser sees
which tab is driven and what each step touched.

- Show, in the browser itself, that an agent is working there. Every action now
  marks the page it ran in: the tab's favicon carries a green badge while an
  agent is acting and an amber one for five minutes after its last action, and
  the element the action touched flashes for 250 ms — red when the action was
  refused. New module ``web_search_neo/agent_presence.py`` holds the injected
  script and its builders; ``browser_tools.note_agent_activity`` marks one
  action and ``_apply_agent_presence`` arms the script for future documents.
  - Hooked in ``main._execute_actions`` rather than in each handler: a signal
    only as complete as the last handler someone instrumented would leave the
    tab looking idle during whichever actions were forgotten.
  - Everything injected carries ``aria-hidden="true"``, which every reading
    topic already skips, so an overlay can never surface in ``page_text``,
    ``page_outline`` or ``inspect``. No ``<style>`` element is created, so a
    strict ``style-src`` CSP cannot leave the overlay unstyled over the page.
  - Every screenshot clears the overlays first, so a capture taken inside the
    250 ms flash cannot hand an agent a picture of a box its own click drew.
  - Skipped entirely in step and render modes, which freeze the page clock the
    fade and the removal are built on and call ``step`` once per frame, and
    throttled to one mark a second for actions that draw nothing, so a polling
    loop cannot spend a bridge round trip per iteration.
  - Never costs a caller an action: the whole path swallows failures, and a
    handed-back tab has its favicon restored in ``_clear_injected_state``.
  - Off with ``WEB_SEARCH_NEO_AGENT_PRESENCE=0``; headless sessions never show
    either signal.
  - Tests in ``tests/test_agent_presence.py``.

## 1.11.1

- Real Pointer Lock tests require `--run-desktop-input`; ordinary test runs must
  not capture the desktop cursor, including when Chrome is headless.

- Audit correction: raw script execution is single-shot by default; retries require
  explicit opt-in because an exception can follow a completed mutation.
- Fix JavaScript truthiness in condition polling and promise evaluation through CDP.
- Keep locale, navigator language and Accept-Language aligned; preserve the other
  viewport dimension when only one is changed. Do not repeat failed reload navigation.
- Strip credentials on cross-origin HTTP redirects, including session defaults; close
  failed responses. Raw and custom-header fetch remain supported.
- Repair mock navigation persistence, relative URLs, XHR events/response types,
  bodyless responses, cleanup, and companion child-session routing/state restoration.
- Prevent delayed favicon loads from reviving expired/stopped activity badges.
- Advertise live companion CDP methods so status can distinguish shipped and running
  capabilities. Older companions report capability verification as unavailable.
- Extract sessions/actions/perception/cdp/contract/fetch modules behind compatible
  entry points. Enforce 600 soft / 800 hard production line limits, explicit legacy
  ratchets, and import boundaries in CI.
- Correct prior claims: isolated profiles separate storage but do not guarantee an
  unlinkable fingerprint. Debug-banner guidance is a launch workaround, not automatic
  suppression of Chrome's UI.

The audit write-up for this release lives in commit `c254609`.

## 1.11.0

Release focus: the eight defects a live multi-agent session surfaced, plus the
two papercuts around it (agent-held tabs nobody can see, a debugging banner
nobody can dismiss).

- Fix the `reload` version drift. The repo already forwarded `Page.reload`
  while installed companions still refused it with "forwards only the 24
  methods". `browser_status` now publishes the shipped allowlist
  (`allowed_cdp_methods_size/hash`) so the skew is visible before the call,
  and `reload` on a stale companion serves the reload via same-URL navigation
  with `reload_fallback=navigate` plus a note naming the fix (Reload the card
  at chrome://extensions).
- Make `run_script` survive its own navigation race. Transient post-navigation
  evaluation failures (`Uncaught`/detached context) are retried automatically
  (`retry_on_uncaught=true`, `retries=2`, `retry_delay_ms=300`, opt-in
  `wait_ready`), genuine page errors still fail at once, every answer reports
  `attempts`, and bridge errors now carry the exception description with
  line/column instead of a bare `Uncaught`.
- Add a universal `wait`: `script` (a JS expression polled atomically
  server-side until truthy) alongside `selector`, mutually exclusive, for SPA
  hydration and framework flags no selector expresses.
- Stop `fill` from dropping focus silently. `blur_after=true` stays the safe
  default (uncommitted values are lost data, a moved focus is not);
  `blur_after=false` leaves focus in the last control and `typing=true`
  emulates keystroke-by-keystroke input for masked/autocomplete fields.
- Add isolated browser contexts. `profile_mode="isolated"` is a disposable
  owned browser with its own fingerprint (one account = one isolated
  session), with per-session `user_agent`/`timezone`/`locale`/`geolocation`
  overrides and a live `context` action; `current`/`attach` refuse overrides
  with the reason instead of pretending to apply them.
- Teach `fetch_text`/`fetch_many` raw work: `mode="text"|"html"|"raw"`,
  custom `headers`, and `save_to` for downloading bodies (e.g. JS bundles)
  straight to a file.
- Publish real enums in schemas: `local_storage.op/kind` (plus `cookies.op`,
  `inject_script.op`, `wait.state`) are `Literal` so `action_schema` shows
  `enum` without guessing, and `capabilities` accepts `full_schemas=true` to
  embed every action schema in one call.
- Add request stubbing with the `mock` action (`op=add|list|clear`): wildcard
  `url_pattern` stubs answered over the CDP Fetch domain in the companion and
  via an in-page fetch/XHR patch on Selenium drivers, until cleared or the
  session closes. New companion methods: `Fetch.enable/disable/fulfillRequest/
  continueRequest/failRequest` plus `Emulation.setTimezone/Locale/
  GeolocationOverride`.
- Mark agent-held tabs visibly. Driven tabs wear a green activity dot on the
  favicon while an agent acts (fading after 5 quiet minutes, restored
  favicon on teardown); `browser_status` reports `agent_active` per session,
  and shared/claimed sessions warn with the read-only-or-open-your-own
  guidance instead of failing silently mid-run.
- Explain the debugging banner instead of fighting it. `browser_status` and
  `setup_current_chrome` carry `debug_banner`: the strip is Chrome's
  mandatory `chrome.debugger` UI, Cancel only detaches until the next action,
  and the one supported silence is relaunching Chrome with
  `--silent-debugger-extension-api` (per-OS steps included); Selenium modes
  show no banner at all.
- Add periodic cleanup of claims whose owner process has died in the bridge daemon.
  A background thread runs every 30 seconds (configurable via
  ``WEB_SEARCH_NEO_BRIDGE_CLAIM_CLEANUP_INTERVAL``), checks all active claims,
  and releases any whose PID (extracted from the ``program#pid`` label) no longer
  corresponds to a running process.  This prevents tabs from staying permanently
  occupied when a client process is killed without cleanly releasing its tabs
  (e.g. `taskkill /F`, OOM, crash).
  - Labels are parsed for the PID after the ``#`` character; unrecognised formats
    are left untouched (safer than snatching a tab from a live agent).
  - Process existence is checked cross-platform via ``os.kill(pid, 0)``:
    ``ProcessLookupError`` means the process is dead, ``PermissionError`` means
    it is alive but inaccessible.
  - After freeing a claim ``_broadcast_state()`` is called so remaining clients
    see the tab is free.
  - New constant ``PROCESS_CHECK_INTERVAL = 30`` added beside other daemon
    settings.
- Add ``_is_process_alive`` static method using ``os.kill(pid, 0)`` for
  cross-platform process existence checks.
- Add ``_cleanup_dead_claims`` method and ``_cleanup_dead_claims_loop`` background
  thread, started in ``_serve``.
- Add tests in ``tests/test_claim_cleanup.py``:
  * claim with non-existing PID is released,
  * claim with living PID stays,
  * unrecognised label format is not touched.
- Add tests in ``tests/test_bugfix_bundle.py`` (fixes 1-8) and
  ``tests/test_tab_activity.py`` (badge, busy warnings, banner note).

## 1.10.1

...

- The companion reconnects on its own. A periodic one-minute alarm now runs
  alongside the backoff and covers the two states the backoff cannot: a socket
  that died while the service worker was already evicted, so its close handler
  never scheduled a retry, and a browser idle long enough that nothing wakes the
  worker at all. Both used to leave the extension offline until someone clicked
  the toolbar icon. The heartbeat is armed on install, on browser start, when the
  switch is turned on and on every worker load, re-arms itself when it fires, and
  yields to a wait the backoff already owns instead of spending an attempt on top
  of it.

### Earlier in this release

- Label each agent's tab in the tab strip. In `profile_mode="current"`, `open`
  and `attach_tab` install a small script via
  `Page.addScriptToEvaluateOnNewDocument` that prefixes `document.title` with
  `[agent_label] ` (or `[session_id] ` without a label), re-applies the prefix
  when the page rewrites its title (MutationObserver on `<title>` plus a
  `DOMContentLoaded` pass), and hides the prefix from page-side
  `document.title` reads through a shadow accessor - so the strip shows who is
  where while pages and title-comparing macros keep seeing the real title.
  Read topics (`page_outline`, `page_text`, `browser_status`, ...) strip the
  prefix back off as a backup. Headless, persistent, and attach sessions are
  never touched; handing a borrowed tab back removes the prefix. Opt out per
  call with `label_tab=false` or for the whole server with
  `WEB_SEARCH_NEO_LABEL_TABS=0`.
- Add the `reload` page action: reloads the current page in place, keeping the
  session's tab and history, and returns the same page-state envelope (url,
  title, ready_state, ...) as the other page actions. `hard=true` bypasses the
  HTTP cache (`Page.reload` with `ignoreCache=true`); the default revalidates
  like a normal reload. A backend without CDP falls back to WebDriver's own
  refresh. Before this the only reload was `open` on the same URL or
  `run_script` with `location.reload()`.

## 1.10.1

Release focus: the outline stops losing the one thing on the page that can be acted on.

- Describe overlays before the page they cover. A modal is normally appended at the end of
  the body, so a document-order walk spent the whole node budget on the page behind it and
  stopped: the outline came back truncated and missing the login wall that was standing on
  screen, readable by hand through JS and absent from the description of the page. Overlay
  nodes carry `overlay: true` (`overlay` in the text form) and `open_dialogs` counts them.
- Scope the outline to an overlay that declares itself modal, reported through
  `scoped_to_modal` and `modal_dialogs`, with those nodes marked `modal`. Modal means the
  platform's own answer — `:modal`, which matches exactly what `showModal()` and fullscreen
  put in the top layer, or an author's `aria-modal="true"`. Nothing behind those can receive
  a click, so listing hundreds of controls that will not respond is worse than leaving them
  out. A bare `role="dialog"` is deliberately not modal: the web uses it for drawers and
  inline panels, and scoping to one of those would hide a page that works.
- Add `scope="page"` to `browser_page_outline` for a caller that means to look behind an
  open modal; `scope="auto"` is the default described above.
- Move the rule for what counts as an overlay into one place in the shared JS library.
  `page_text` has appended open dialogs to main mode all along and carried its own copy of
  that rule; both readers now decide by the same one, so they cannot disagree about what is
  standing in front of the page.

## 1.10.0

Release focus: the engine becomes a real Python package with an open extension core,
and four browser-slice capabilities land on top of the existing CDP surface.
Chrome Native Messaging is deliberately deferred to 1.11.

- Move the engine into a proper `web_search_neo/` package: fourteen root-level modules
  now live under it, intra-package imports are fully qualified, and thin root shims keep
  `python main.py` and `driver.py` working as before; `pip install -e .` also exposes the
  `wsn` entry point.
- Add a plugin API: set `WEB_SEARCH_NEO_PLUGINS` to an os.pathsep-separated list of `.py`
  files or directories (or publish an entry point in the `web_search_neo.plugins` group)
  and plugins can add compact `web_action` actions, `web_info` topics, and search providers
  without touching the core; a broken plugin fails startup loudly instead of being skipped.
- Add virtual gamepad input: a `gamepad` web action drives engines that read only the
  Gamepad API over `Emulation.sendGamepadEvents`; on Chrome builds without the CDP method it
  reports an honest error and its test skips.
- Add strict CDP virtual time next to the JavaScript frame gate: pause, grant a bounded
  budget in milliseconds, and resume through `Emulation.setVirtualTimePolicy`, covering code
  paths the in-page timer patch cannot reach; the two layers compose.
- Reach into closed shadow roots: perception records each `attachShadow` call per page, so
  the outline can describe and address elements inside closed roots with ordinary refs.
- Resolve element refs and piercing locators inside nested same-origin iframes for actions,
  not only inspection, without switching sessions around.
- Make the shipped MCP config portable: `scripts/make_mcp_config.py` rewrites
  `mcp_servers.json` with the absolute paths of wherever you cloned it (the repository ships
  `"cwd": "."`), and `tests/test_mcp_config.py` pins that no developer path survives.
- Defer Chrome Native Messaging to 1.11: the stdio pipe framing, size limits, and ACL spec
  still need pinning down, and native-first needs a split-brain guard against the loopback
  fallback before it can be trusted; the design is recorded in `docs/native-messaging-design.md`.

## 1.9.1

Release focus: reliable file uploads from forms inside iframes.

- Scope the file-input lookup used by the Chrome companion to the active frame
  execution context. Same-origin iframe uploads now use the frame selector in
  the tab target, while cross-origin uploads keep using the resolved child
  debugger target.
- Add regression coverage for same-origin and cross-origin frame uploads,
  including the debugger session used by `DOM.setFileInputFiles`.
- Document the frame-scoped upload behavior and bump the server, package,
  companion, and preview versions to 1.9.1.

## 1.9.0

Release focus: a dynamic companion widget, a keyboard launch path, and a
documentation pass that moves every remaining domain-specific example out of
the repository. The engine stays universal and domain-neutral; project
workflows keep living under `<project>/.web-search-neo/macros/`.

- Redesign the companion popup as a compact, icon-first widget: inline SVG/CSS
  icons only (no remote assets, no build step), minimal visible words, all of
  them English. Refined motion covers a connection pulse, state transitions,
  hover/press feedback, and an animated capacity meter; every animation and
  transition is disabled under `prefers-reduced-motion`. Accessibility is kept
  strong: semantic controls, visible keyboard focus, `aria-label`/`title` on
  every icon control, readable contrast in both color schemes, and live status
  regions.
- Make the widget genuinely dynamic from live bridge state. It renders
  enabled/disabled/connecting/connected/error from what the service worker
  reports - never a fabricated "connected" - and shows controlled tabs, the
  parallel-session limit against its hard ceiling as an animated meter, the
  bridge endpoint, the next retry countdown, the companion version, and the
  GitHub release/update state. The one-second refresh and safe error handling
  are unchanged, and no secret ever reaches the UI.
- Extend the service-worker status payload with two safe fields: `state`, one
  derived word per connection state so the popup cannot disagree with the
  worker, and `failure_kind` (`transport` when nobody answered the port,
  `auth` when a peer refused this companion's credentials). Loopback and
  authentication restrictions and the version handshake are untouched.
- Add a convenient launch path: the extension action opens the widget, and the
  manifest now suggests **Alt+Shift+N** for it through a `_execute_action`
  command (changeable at `chrome://extensions/shortcuts`). Opening the widget
  never navigates, submits, closes, or otherwise touches user tabs.
- Add two offline harnesses under `scripts/`, both reusing the production
  popup HTML/CSS/JS rather than a mock design:
  `companion-widget-preview.html` (open in any browser; read-only simulated
  states driven by iframe parameters) and `widget_screenshots.py` (headless
  Chrome PNG captures into `docs/assets`). Neither connects to a bridge,
  opens tabs, or needs secrets.
- Documentation is now entirely English and domain-neutral. The 1.8.x
  changelog entries were translated from Russian and generalized; examples
  across README, INSTALL, the bundled skill, and docs no longer reference any
  single business domain, site, macro name, or workflow.
- Tests extend to the widget: consistent versions across server and extension,
  the keyboard command, English-only visible UI, icon and switch
  accessibility, reduced-motion support, no remote assets, dynamic state and
  capacity rendering, release-check wording, and a scan asserting that no
  domain-specific public wording returned.

## 1.8.2

A benchmark that ran browser tasks spent an hour filling the user's working
Chrome with tab groups. The cause was the default: `profile_mode` defaults to
`current`, so an agent that never mentioned a mode opened its pages in exactly
the browser a person was working in.

- New environment variable **`WSN_FORBID_CURRENT_PROFILE`**: set it and every
  request aimed at the working profile is demoted to `temporary` - its own
  disposable Chrome. Demotion, not refusal: the work continues, just somewhere
  else. The swap is not silent either - every answer reports the effective mode
  as `profile_mode`.
- All spellings are covered: `auto` no longer slides into the working profile,
  and `extension` is a second name for the same `current`, so it cannot be used
  to slip past the guard either.
- Why here and not in a prompt: you can ask an agent politely, but a weak model
  ignores the request, and the person who launched the task pays for it.

## 1.8.1

An agent was asked to tidy up browser tabs, and it turned out it could close
exactly one kind - the ones it had opened itself. `close` on a claimed tab
detached and left the page open (right for a borrowed tab, useless for an
unwanted one), and `window.close()` from a page Chrome ignores. The extension
could always do `tabs.remove` on any id; the gap was only on the Python side.

- The **`close_tabs`** action: closes named tabs of the user's Chrome by id
  from `web_info(topic='browser_tabs')`. There is deliberately no
  close-everything switch - closing is irreversible, so each tab is named.
- Two skip categories instead of closing, because that is how it hurts most:
  **pinned** tabs are a set a person keeps on purpose and the last thing a
  cleanup should carry away, and tabs **driven by another agent**, where
  closing one pulls the page out from under a running session. Both can be
  overridden by name via `include_pinned` / `include_claimed`, and every skip
  says which rule it hit.
- The outcome is decided against a **fresh tab list**, not against the
  extension's acknowledgement: `tabs.remove` reports failure whenever it
  throws, and the typical reason is a tab the user had already closed by hand.
  A tab that is already gone lands in `skipped` as `already_gone`, because
  that is the requested outcome.
- A session sitting on a closed tab is **forgotten** and its claim released.
  Otherwise it would have kept answering to its name, and the next action would
  go to an id Chrome had meanwhile reused - failing somewhere else entirely.
- `close` gained an explicit **`close_tab`** parameter: `browser_tools` could
  always close a claimed tab, but the ability was not exposed through the
  action until now.

## 1.8.0

Two days of live use - roughly 130 form submissions driven by parallel agents -
produced three defects, and all three were the same shape: the server answered
confidently and wrongly. Silent where the page had already stalled, loud where
everything had succeeded. Each one cost real work.

- Teach detection to see the **invisible captcha**. Widget traversal used to
  discard everything without a visible box - and an invisible Turnstile is
  exactly that: `div.cf-turnstile` and an `iframe` to challenges.cloudflare.com
  in the DOM, but no picture and no checkbox. A submit handler on such a page
  waits for a token nobody will mint: the button settles into "Submitting...",
  no POST leaves the page, the console stays clean. Now every page summary
  carries `invisible_challenge_pending`, and when true also
  `invisible_challenge` with the vendor, the state (`token_empty` - the hidden
  `cf-turnstile-response` / `g-recaptcha-response` / `h-captcha-response` /
  `smart-token` field is empty; `widget_hidden` - rendered before its field
  existed), evidence, and advice. It gates the form rather than the page, so
  `challenge_detected` stays false: the agent should not park for three
  minutes on a page that reads fine.
- Admit that a minted token ends the question: a hidden container a vendor
  leaves in the DOM after solving says nothing by itself anymore.
- `captcha` with `op=detect` no longer answers `captcha_present=false` on such
  a page, and waiting (`mode='wait'`) no longer calls an invisible challenge
  resolved: an empty token field is the same wait, and "resolved" on top of it
  meant "safe to submit" when it was not.
- Name the **stalled submit** out loud. If a `click` produced no network
  request while an invisible widget is pending, the reply carries
  `submit_blocked_by_challenge=true` and `submit_block_reason`. The server
  already had both halves - the network tap and the DOM walk - nobody joined
  them. The check runs on the shared network-log drain, the same for Selenium
  and the companion, and counts requests in flight: "POST not finished yet" is
  not the same as "no POST".
- Preserve **line breaks in contenteditable**. `Input.insertText` inserts one
  text node, and inside it `\n` renders as a space, so a multi-paragraph
  message arrived as one paragraph - and `fill` honestly reported "The control
  did not take the value" after comparing expected with actual. Text is now
  typed line by line with a soft break between lines (Shift+Enter, never
  Enter: in a chat composer Enter sends what is written so far). Read-back uses
  `innerText`, not `textContent`, which ran the editor's paragraphs together
  and turned a successful write into a refusal.
- If an editor folds the breaks away anyway, say so: the error names the cause
  and the working path - `run_script` with `user_gesture=true` and
  `navigator.clipboard.writeText`, then a real `Ctrl+V` through `input`. Chat
  composers whose state never updates from a DOM write - the send control
  stays inert - always need that paste, and the note lives in the `fill`
  contract.
- Stop treating an empty `input[type=file]` as proof that an **upload**
  failed. Any Dropzone-style widget takes the file off the input and uploads it
  itself: the file is already on its way, the chip with the name is on screen,
  and `upload` still answered `success:false`. The reply now carries
  `upload_state`: `attached` (the input holds the files - exact),
  `taken_by_widget` (the input was emptied, and the page names the file or a
  POST/PUT/PATCH followed) or `unconfirmed` (cannot say). `unconfirmed` is not
  a refusal: `note` explains how to verify (look for the name through
  `page_text`/`elements`, the request through the `network` topic), and
  `success` is false only when the attach itself failed. `fill` with `files`
  reports the same thing in `upload_states`/`upload_notes`.
- Write the contract where behavior changed: notes for `fill`, `upload`,
  `click`, `page_elements`, skill rules and troubleshooting, the general
  pitfalls list. The `capabilities` (14000) and `skill` (7000) budgets were
  not raised - 13401 and 6401.

## 1.7.0

A macro is a JSON file, and now that is all it is. The write half of the `macro` action -
the recorder and the pack transport - is gone, and a checker that runs before the page does
has taken its place.

- Remove `op=record`, `op=save` and `op=cancel`, and with them the whole recording machinery:
  the per-session recording registry, the batch lock that serialised recorded dispatches, the
  interception in the action loop, and the attribution rules for a step that named no session.
  The recorder was never self-sufficient - its own contract told the caller to save the
  recording and then hand-edit the JSON to turn the changing parts into `{{placeholders}}`, so
  the path ended in an editor either way. Over a full day of live use all four
  working macros in the project store were written directly as JSON and
  the recorder was not used once, while it carried a class of defects of its own: races between
  concurrent batches, steps landing in the wrong open recording, a name borrowed by an
  explicit save, steps lost when `record` was called twice.
- Remove `op=delete`, `op=export` and `op=import`. When a macro is a file, deleting one is
  deleting a file and moving a set is copying a directory; a second, weaker API for the same
  thing was one more place for a store to be chosen wrongly. The pack format goes with them.
- Add `op=validate`, which reads a macro file and dispatches nothing. Errors: an action name
  the server does not have, a required parameter missing, a parameter that is not part of that
  action and would be refused at dispatch, a placeholder used but not declared in `variables`,
  and a `{{placeholder}}` inside a `run_script` script. That last one is why this exists - the
  value is pasted into the JavaScript as raw text, so any newline, quote or backslash produces
  a broken program and the step fails with an opaque `Uncaught` from inside the page, several
  steps into a live form. Warnings, which never make a macro invalid: a declared variable no
  step uses, steps drifting between two `session_id` values, and a macro whose last meaningful
  step neither waits nor reads anything back. Every finding carries the step index, what is
  wrong, and how to fix it.
- Rewrite the `macro` recipe, the `macros` skill section and the action's own notes around the
  path that is now the real one: write the JSON, `validate`, `preview` with variables, `run`.
  The old recipe described the recorder and was simply wrong after this change.
- The macro file format is untouched. All sixteen macros in use - fourteen in a project store,
  two in the per-user one - load, resolve and preview exactly as before.


## 1.6.0

A day of real use - about a hundred form submissions filed by five agents through one server -
produced four defects, all of them about several agents sharing one MCP server.

- Raise the default session cap from 4 to 8. Four was chosen when a session meant a Chrome
  process; in `profile_mode="current"`, which is what agents actually use, a session is one tab
  of a Chrome that is already running and costs tens of megabytes, not hundreds. In the run
  above four agents took every slot and the fifth could not open a single page, so it filed
  nothing at all - a far worse outcome than the memory the low number was protecting. Eight
  covers an ordinary fan-out with room to spare and still stops a leaking model early. The
  ceiling stays 64: it exists to catch a typo, and a desktop runs out of memory long before it.
- Make the cap settable from the companion extension's popup, under Settings next to the bridge
  port. The number rides in the hello the extension already sends, which the daemon already
  relays to every connected MCP server, so no new channel was needed. `WEB_SEARCH_NEO_MAX_SESSIONS`
  in the server's own environment still wins - a number deployed there was said about that
  server - and the popup hint says so. `browser_status` and `capabilities` report which of the
  three sources the cap in force came from.
- Give `close_all` an owner. It used to close every session in the process, which inside one
  server is every *agent's* session: one subagent tidying up ended four others' work mid-form,
  and the only defence was a line in every brief telling agents never to call it. It now
  defaults to `scope="mine"` and closes the sessions carrying the caller's `agent_label` (with
  no label, the unlabelled ones), always reporting `kept_sessions` and who owns them.
  `scope="all"` (or `include_foreign=true`) is the old behaviour, kept and explicit. The
  shutdown hook still closes everything, because at process exit nobody is left to own a tab.
- Bound perception answers by size, not only by count. `page_elements` had `limit` and `offset`
  but nothing measuring the answer: 200 controls on a large live page came back as 83,616
  characters, which the model that asked could not receive at all. `page_elements`, `page_outline`
  and `find` now take `max_chars` (default 18,000), trim to a prefix, restate `returned` and
  `range[*].next_offset` so the continuation offset points at the first entry that was not sent,
  and say what was dropped in `budget_note`. The budget is shared round-robin across the
  categories, so a page whose buttons matter is not handed an answer made entirely of links.
  `page_text` and `element_text` already had honest budgets and are unchanged.
- Let a session say who opened it. `open` and `attach_tab` take an optional `agent_label`;
  omitting it is not an error. `browser_status` now carries the whole roster - per session the
  owner label, tab id and group, profile mode, the page it was last seen on, when it was created
  and last used, idle seconds, and whether another thread is inside it - plus `N of M` occupancy
  and where the cap came from. It is answered entirely from memory: asking each tab for its URL
  would mean waiting on the lock its own agent holds, and status is what a stuck run reads first.
  `capabilities` reports the same occupancy under `limits`, because "8" tells a blocked agent
  nothing that failing would not have told it, while "0 free" does.

## 1.5.0

- Key macro recordings by `session_id`. A single shared recording collected every dispatched
  action, so with two agents in one server an agent recording a task captured the other's
  actions and replayed them. Recordings are now per session and independent.
- Infer the recording for `save` and `cancel` when one is open, and refuse to guess when
  several are, naming the sessions. An action with no session of its own joins the only open
  recording, and when several are open it is reported as `unattributed_steps` instead of
  attributed by luck.
- Attribute an action to the session its schema defaults to, rather than treating an unset
  `session_id` as no session at all.
- Serialise only the batches that touch a recorded session; everything else stays concurrent.
- Accept `session_id` on `macro op=run` and `op=preview` to point a recorded macro at another
  tab, refusing to collapse a macro that already drives two sessions.
- Refuse any DevTools method outside an explicit allowlist inside the companion, so an
  authenticated local peer holds the contract's capabilities rather than the whole protocol.
  A test compares the allowlist against the server's call sites so they cannot drift apart.
- Make the companion's bridge port a stored setting in the popup, replacing the documented
  edit of `BRIDGE_URL` in an installed extension. It validates the range, reconnects at once,
  and survives a browser restart.
- Show the next reconnect attempt in the popup, so a deliberate backoff no longer reads as a
  broken bridge.
- Keep the Windows daemon-spawn branch importable on other platforms, and wait for the
  compositor before reading a container's scroll position, fixing both Linux CI failures.
- Check that the companion manifest version matches the server's, and that popup.js, popup.html
  and popup.css describe the same page.

## 1.4.1

- Declare a macro's placeholders from its steps on every read, not only when `save` wrote the
  file, so a hand-written macro reports what it wants through `op=list` and `op=show` instead
  of appearing to want nothing until a run fails.
- Check every resolved step against its published action schema during `op=preview`, reporting
  `steps_valid` and a `problems` list, so a mistyped parameter in a hand-edited file is found
  before any step dispatches rather than midway through a replay.

## 1.4.0

- Resolve `project_root: "auto"` for every macro operation: `WEB_SEARCH_NEO_PROJECT_ROOT`, then
  the nearest ancestor with `.web-search-neo`, then the nearest repository root.
- Let `WEB_SEARCH_NEO_PROJECT_ROOT` supply the default project for calls that pass no
  `project_root`, so an MCP client can be configured once per project.
- Report `scope`, `project_root`, `storage`, and `other_store` on every macro answer.
- Accept a bare step list as a macro file, and take the macro's identity from its file name.
- Add `macro op=export` and `op=import` for whole macro sets, with all-or-nothing validation and
  a refusal to overwrite an existing name unless asked.
- Write a `README.md` into every macro store describing the file format.
- Stop listing the guarded-operation ledger as a macro, and report a file that cannot be read
  as a macro as broken instead of summarising it as empty.
- Fix a recording started with a project that resolves to none: it no longer derives a project
  directory from the per-user store's path.
- Add `web_info(topic="actions")`: the action index alone, narrowable with `params.group`.
- Add detailed skill sections behind `web_info(topic="skill", params={"section": "<name>"})`
  covering start, loop, locators, forms, macros, guarded, parallel, search, diagnostics, games,
  and troubleshooting.

## 1.3.11

- Make `guard.resource_sha256` mandatory for `guarded_stage`.
- Compute SHA-256 from the current `guard.resource_path` bytes and fail closed on a missing,
  malformed, or mismatched digest.
- Normalize a verified digest to lowercase, return it from guarded stage/commit, and persist it
  in the project-local one-time checkpoint ledger.
- Document that guarded commit attempts its terminal action once and that confirmation proof
  must be collected separately without automatically retrying that action.
