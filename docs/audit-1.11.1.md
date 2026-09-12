# Audit of v1.11.0

The statement that every issue was fully closed was too strong. Canned-driver tests
passed while missing browser behavior and unsafe retries. This patch adds behavioral
Chrome, JavaScript, and local HTTP coverage and the first architecture extraction stage.

| Area | Finding and result |
| --- | --- |
| Reload/version drift | Fallback existed, but a failed navigation could be repeated. Fixed; live CDP method inventory is now separate from the shipped inventory. |
| Script retries | Ordinary JavaScript exceptions could repeat a completed mutation three times. Single-shot default now reaches the public MCP schema too. |
| SPA waits | Python treated empty JS arrays/objects as false. Corrected; promise evaluation explicitly uses CDP awaitPromise. |
| Fill | Live Chrome confirms typing input and retained focus. Typing means per-character input, not a promise of full physical keyboard event emulation. |
| Isolation/context | Separate owned browser profiles work. Locale and partial viewport fixes added. Hardware/IP unlinkability is not provided. |
| Fetch | Raw source, headers and download exist. Cross-origin redirects leaked credentials; fixed and tested between local origins. |
| Schemas | Enums and opt-in full action schemas exist; defaults now agree with single-shot script execution. |
| Request mocks | Navigation lost Selenium rules, XHR omitted listener/loadend handling, relative URLs and null-body statuses broke. Fixed with live navigation/XHR tests. Selenium fallback covers page fetch/XHR, not workers or all subresources. Companion uses CDP Fetch. |
| Activity/ownership | Existing claims are refused and concurrency is reported. Late favicon loads could resurrect a stopped badge; fixed with executed JS regression tests. |
| Debug banner | No automatic suppression implemented or claimed. The browser launch flag is documented; ordinary MCP actions cannot change flags of a running Chrome. |

## Architecture

The six package boundaries now hold real implementations and contract data. Public
entry points retain session ownership and locks; extracted modules do not import the
legacy facades. CI checks imports and line counts. Existing large modules remain under
explicit shrinking maxima; this is an incremental extraction, not a claim that every
legacy module is already below 800 lines. Details are in `ARCHITECTURE.md`.

## Verification

Regression suites cover real headless Chrome focus/locale/promises, mock navigation and
XHR, executed companion/badge JavaScript, local cross-origin HTTP redirects, MCP schemas,
ownership cleanup, and architecture gate failure paths. The original full baseline
also exposed two Windows cleanup tests patching the obsolete os.kill seam; those tests
now patch the platform-neutral liveness method, with real platform coverage retained.

Final local verification: one completed full-suite shard had 306 passed / 1 skipped;
the final focused regression run had 96 passed / 2 explicitly skipped desktop-input
tests. Other full-suite shards were interrupted after the reported desktop interference,
so a complete final all-tests pass is not claimed. Architecture checks, compilation and
skill validation passed. Real Pointer Lock tests now require `--run-desktop-input`.
