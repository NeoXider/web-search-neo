# Architecture invariants

Web Search Neo is a universal browser/search MCP. Its core stays domain-neutral: public APIs,
storage, validation, guards, and documentation primitives must not encode recruitment, job
sites, commerce vendors, or any other domain-specific workflow or host policy.

Domain behavior belongs in saved macros, templates, and configuration owned by the calling
project. Only neutral, broadly useful examples may be bundled in this repository. In
particular, the Unity application macro belongs outside this repository at
`D:\AI\outputs\unity-job-agent\.web-search-neo\macros\`.

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
binding, assertions against live staged results, and a persistent one-time token. The
terminal Submit is held back during staging and its checkpoint is consumed before dispatch,
so retry after an ambiguous result fails closed.

These are mechanisms, not policies. A concrete project decides which hosts, identities,
resources, assertions, and tokens are appropriate and stores those decisions with the project.
