# Architecture invariants

Web Search Neo is a universal browser/search MCP. Its core stays domain-neutral: public APIs,
storage, validation, guards, and documentation primitives must not encode recruitment, job
sites, commerce vendors, or any other domain-specific workflow or host policy.

Domain behavior belongs in saved macros, templates, and configuration owned by the calling
project. Only neutral, broadly useful examples may be bundled in this repository. Concrete
project macros belong under that project's `.web-search-neo/macros/` directory and must not
be copied into the MCP repository.

## Macro storage

- With no `project_root`, macro operations use the backward-compatible per-user store.
- With an explicit existing absolute `project_root`, every operation uses
  `<project_root>/.web-search-neo/macros/`.
- Macro names cannot contain path separators or traversal tokens.
- The resolved storage directory must remain beneath the resolved project root; symlink or
  junction escape is refused.
- Each project has an independent macro set and guarded-operation ledger.

## Guarded consequential actions

The generic `guarded_stage` / `guarded_commit` protocol provides canonical target identity,
optional domain identity, caller-supplied host allow/deny policy, exact uploaded-resource
binding, assertions against live staged results, and a persistent one-time token. Exactly one
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
