# Changelog

## Unreleased

- Keep ChromeDriver windowless for cached, uncached, and retry launches. Avoid passing Selenium a duplicate startup-info argument that previously forced an unprotected fallback. Test processes now also cover the native Windows multiprocessing spawn path.

- Show the virtual cursor for ordinary selector clicks as well as coordinate input. Capture the target before it disappears, map iframe targets into the main viewport, restore a cursor removed by page updates, and recognize the public pointer-action names for click rings.
- Keep the windowless stdio proxy responsive to small requests and let it exit when its child finishes even if the client still holds stdin open. Preserve module execution and inherited streams when bypassing Windows interpreter launchers.
- Honor per-call script timeouts with the Selenium transport as well as the Chrome bridge, restoring the original timeout after success or failure.
- Save the companion presence preference before changing its active state, so failed storage writes can be retried.

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
