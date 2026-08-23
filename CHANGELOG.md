# Changelog

## 1.6.0

A day of real use - about a hundred job applications filed by five agents through one server -
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
  but nothing measuring the answer: 200 controls on a live job board came back as 83,616
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
