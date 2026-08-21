# Changelog

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
