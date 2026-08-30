# Changelog

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
