# Architecture invariants

Web Search Neo is a universal browser/search MCP. Its core stays domain-neutral: public APIs,
storage, validation, guards, and documentation primitives must not encode recruitment, job
sites, commerce vendors, or any other domain-specific workflow or host policy.

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
be the only distinction between two resources or requisitions on one shared path. URL
fragments are excluded because they are client-side navigation state. Core never guesses
which query parameters are tracking noise; the calling project supplies an already-canonical URL.

These are mechanisms, not policies. A concrete project decides which hosts, identities,
resources, assertions, and tokens are appropriate and stores those decisions with the project.
