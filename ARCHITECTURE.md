# Architecture invariants

## Module boundaries and size gates

The first extraction stage keeps `browser_tools.py` and `main.py` as compatibility
facades. Existing callers and session locks remain there; leaf modules accept a driver
and explicit callbacks instead of importing either facade or accessing its globals.

- `sessions/`: session models, activity badge lifecycle, owned-profile context overrides, the tab-label page source, and the parked (`persist=true`) session registry.
- `actions/`: script execution, condition polling, render bootstrap source, click/typing verification and text-as-keys input.
- `perception/`: element collection, the challenge probe and action page-script sources, and response budgets.
- `cdp/`: request mock installation, navigation persistence, teardown, and reading the replacement Chrome recorded for a session's lost tab.
- `contract/`: domain-neutral action notes, examples, and built-in playbook.
- `fetch/`: bounded HTTP source/text/link extraction.
- `audit/`: passive site checks - CSP/header/cookie/transport/page analysis, Mozilla HTTP
  Observatory's grade plus the extended score, the scope (`scope.py`: validated hosts, the
  request budget) and the page/site/hosts runner (`site.py`), HAR building, Performance API
  shaping and the test_run plan. Pure analysis over dicts plus plain GETs through `web_client`; the
  session-handling MCP wrappers live in the top-level `audit_actions.py`, which main hands its
  own module to (`extra_actions.register`).

Top-level helpers extracted from the facades: `dispatch.py` is the ordered-action loop behind
`web_action`, macro replays and test_run (main keeps a thin `_execute_actions` that passes
itself as the facade, so the loop reads main's registry and validator at call time), and
`actions/injected_scripts.py` holds the `inject_script` operations.

`scripts/architecture_policy.py` is the executable dependency allowlist. The AST checker
resolves absolute and relative imports; a leaf may not import `main` or `browser_tools`.
Third-party dependencies are named explicitly. Do not bypass boundaries with dynamic
imports, injected module globals, or wildcard re-exports.

Production Python and JavaScript files have a 600-line soft limit and 800-line hard limit,
counting physical lines including comments. Existing oversized files have exact, reviewed
maxima in the same policy. Those are ratchets: lower them after extraction; do not raise
them to accommodate unrelated features. New modules never inherit legacy exemptions.

CI runs `python scripts/check_module_size.py` before tests; `tests/test_architecture.py`
also exercises prohibited imports, new oversized files, and legacy growth. The remaining
legacy files are deliberately not claimed to be fully decomposed. Continue extracting
coherent implementations while preserving the public contract and regression coverage.

Web Search Neo is a universal browser/search MCP. Its core stays domain-neutral: public APIs,
storage, validation, guards, and documentation primitives must not encode any business-domain
workflow, vocabulary, or host policy.

Domain behavior belongs in saved macros, templates, and configuration owned by the calling
project. Only neutral, broadly useful examples may be bundled in this repository. Concrete
project macros belong under that project's `.web-search-neo/macros/` directory and must not
be copied into the MCP repository.

## Macro storage

- With no `project_root`, macro operations use the backward-compatible per-user store,
  unless `WEB_SEARCH_NEO_PROJECT_ROOT` names a project for the whole server process.
- With an explicit existing absolute `project_root`, every operation uses
  `<project_root>/.web-search-neo/macros/`.
- `project_root: "auto"` resolves `WEB_SEARCH_NEO_PROJECT_ROOT`, then the nearest ancestor of
  the working directory holding `.web-search-neo`, then the nearest holding `.git`. Nearest
  wins, so a package with its own store inside a repository is its own project. Discovery that
  finds nothing falls back to the per-user store rather than inventing a location.
- Macro names cannot contain path separators or traversal tokens.
- The resolved storage directory must remain beneath the resolved project root; symlink or
  junction escape is refused.
- Each project has an independent macro set and guarded-operation ledger.
- Every macro operation reports the store it used as `scope`, `project_root`, and `storage`,
  because the failure mode of two stores is a macro saved into one and read from the other.
- A macro file is either a full record or the bare step list; the file name is the macro's
  identity, and a file that does not parse as a macro is reported as broken rather than hidden.
- Packs are transport, not storage: `export` serialises the active store into one file and
  `import` validates every entry before writing any of it.

## Session ownership and answer size

- One `session_id` is one tab and the only boundary between agents sharing a server process.
  An MCP call carries no caller identity, so an optional `agent_label` on `open` and
  `attach_tab` is how a session records who opened it. A missing label is never an error.
- Destructive session operations are owner-scoped by default. `close_all` closes the sessions
  matching the caller's `agent_label` (with no label, the unlabelled ones) and reports what it
  left standing; ending every agent's work requires the explicit `scope="all"`. Process exit
  is the one place that closes everything unasked, because no owner outlives it. The one
  exception is a session opened with `persist=true`: its tab is detached and parked, and
  only an explicit `reattach` that re-proves the tab's identity continues it.
- The session cap is per process and shared by every agent in it. It is a setting in three
  places, in this order: the server's own environment, then the companion popup's value
  relayed through the bridge hello, then the built-in default. The refusal at the cap names
  the setting and the labels holding the slots.
- Perception answers are bounded by size as well as by count. A `limit` counts things and
  cannot bound an answer, so every perception topic also takes `max_chars`, reports
  `budget_truncated`, and restates its own paging so the continuation offset points at the
  first entry that was not sent.

## Guarded consequential actions

The generic `guarded_stage` / `guarded_commit` protocol provides canonical target identity,
optional domain identity, caller-supplied host allow/deny policy, exact uploaded-resource
binding by absolute path and required SHA-256, assertions against live staged results, and a
persistent one-time token. The guard computes the current file hash during staging, refuses a
missing, malformed, or mismatched digest, normalizes it to lowercase, and records it in the
one-time checkpoint ledger. Exactly one
terminal consequential action is held back during staging: either an explicit Submit or a safe
Click. Guarded Click accepts only a plain CSS selector with a live unique-match check at
dispatch, or exact rendered text plus an explicit role through the ambiguity-refusing semantic
dispatcher. Coordinates, trusted centre clicks, substring text, ref handles, piercing paths,
and embedded Submit steps are refused. The checkpoint is consumed before dispatch, so retry
after an ambiguous result fails closed.

Canonical target identity preserves the complete query string because a query parameter can
be the only distinction between two records on one shared path. URL
fragments are excluded because they are client-side navigation state. Core never guesses
which query parameters are tracking noise; the calling project supplies an already-canonical URL.

These are mechanisms, not policies. A concrete project decides which hosts, identities,
resources, assertions, and tokens are appropriate and stores those decisions with the project.
