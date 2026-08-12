---
name: web-search-neo
description: Use the Web Search Neo MCP server for free web search without API keys, resilient multi-engine fallback, HTTP page fetching, the user's current signed-in Chrome with AI-group tabs, isolated browser profiles, accessibility-level page inspection, form filling and upload, page console and network diagnostics, screenshots, and deterministic canvas/WebGL game testing. Trigger when a task needs current web results, visible work in existing Chrome tabs, authorized browser state, reading or driving a rendered page, diagnosing a broken page, or frame-exact keyboard, mouse, and touch input through the two-tool web_info/web_action contract.
---

# Web Search Neo

This file is optional. The server carries its own contract: `web_info()` with no arguments
returns every action with its required and optional parameters, the observation topics,
recipes, pitfalls, limits, and examples. Read that first, and use
`web_info(topic="action_schema", params={"action": "<name>"})` for one full schema.

Two tools only. `web_info` reads state; `web_action` performs 1-32 ordered operations.
One `session_id` is one page — reuse it.

## Observation topics

- `page_outline`: roles, accessible names, states, `ref:<epoch>:N` handles, and boxes. Start
  here.
- `page_text`: readable rendered text; `mode="main"` drops navigation and chrome. On a page
  that is one big form, `main` would be empty, so the reader falls back to the full page and
  says so with `fallback_used` and `mode_used`.
- `find`: `params.query="submit application"` returns ranked refs instead of a whole page.
- `page_elements`: flat CSS selectors, `<select>` options, and form metadata.
- `console`: console output and uncaught errors with stack frames. Pages through history
  with `since_seq` and `limit`, and keeps its own place, so it never competes with
  `game_probe` for entries.
- `network`: requests with status, type, ms, and size; `only_errors=true` to triage. Use
  `output="json"` to get the `id` that `network_body` needs.
- `screenshot`, `game_probe`, `browser_status`, `browser_tabs`, `search_status`, `time`.

The outline and `find` cross open shadow roots and same-origin iframes; a cross-origin frame
is a stub, so pass its selector as `frame_selector` to read it.

## Locators

Plain CSS works everywhere and stays the default. `fill`, `upload`, `click`, `wait`, and the
`form_selector` of `submit` also accept a ref handle such as `ref:3f9a1c04b7e25d18:12` from the
outline or `find`, and a piercing path such as `#host >>> .inner` that steps through an open
shadow root or a same-origin iframe per segment. `submit_selector` and every
`frame_selector` still need plain CSS. Those two forms need a live element handle, so they
resolve in `temporary`, `persistent`, and `attach` sessions, not in companion `current`
mode.

Pass a ref back exactly as it was returned: the first field is the document it came from, so
a handle from a page you have since navigated away from resolves to nothing and the action
tells you to read `page_outline` again, instead of silently acting on a different element.
Re-read after any navigation or re-render.

## Search and fetch

Send a `search` action. Keep `engine="duckduckgo"`, `fallback=true`, and
`challenge_mode="fallback"` unless the user asks otherwise. Use `challenge_mode="manual"`
only when a visible three-minute human handoff is useful, and never claim the server solves
CAPTCHA. Use `fetch_text`, `fetch_links`, or `fetch_many` when the URLs are already known.

Plain `http://` to public hosts is refused; use `https://`. Loopback and private addresses
stay reachable, so local services work unchanged.

## Browser profiles

- `current` (default) drives the user's signed-in Chrome through the companion extension.
  New tabs enter group `AI`; `attach_tab` claims an existing `tab_id` without moving it.
  `close` removes a tab the agent opened and leaves a claimed tab open; `close_all` follows
  the same rule for every session at once.
- If the companion is disconnected, read `browser_status` and call `setup_current_chrome`.
  It opens no page and touches no browsing data; a connected companion older than the
  bundled build is reloaded automatically, reported as `self_update`. When it does return
  `manual_steps`, show them to the user word for word and wait: nothing can install the
  extension, or reload a build older than 1.3.1, on their behalf.
- `auto` falls back to a visible temporary window; `temporary` and `persistent` are
  MCP-owned and visible unless `headless=true`; `attach` uses a Chrome you started with a
  DevTools port and stays open afterwards.

## Forms

Open the page, read `page_outline` or `page_elements`, then send ordered `fill`, `upload`,
`click`, and `submit`. Inspect every result: `success=false`, a validation error, or
`challenge_detected` means the work is not done. Confirm consequential actions with a fresh
read or a screenshot. If a submit silently fails, check `console` and `network`.

## Input and games

Read `game_probe` first and reuse its `frame_selector` for input and render actions.

Use one `input` action for everything that must land in the same frame. Key entries take
`tap`, `hold`, or `release`; pointer entries take `click`, `double_click`, `hover`, `move`,
`drag`, `press`, `release`, or `wheel` with `delta_x`/`delta_y`. Coordinates are viewport- or
frame-local; `coordinate_mode="delta"` moves relatively and `"relative"` is the unclamped
mode for pointer lock. A held modifier reaches later mouse and touch events. Keys include
`F1`-`F12`, `NUMPAD0`-`NUMPAD9`, `META`, arrows, and any printable character; releasing `w`
lifts a key held as `W`.

```json
{
  "actions": [{
    "action": "input",
    "session_id": "game",
    "key_actions": [
      {"key": "W", "action": "hold"},
      {"key": "S", "action": "release"},
      {"key": "SPACE", "action": "tap"}
    ],
    "pointer_actions": [
      {"action": "hover", "x": 640, "y": 360},
      {"action": "wheel", "x": 640, "y": 360, "delta_y": -240},
      {"action": "move", "x": 15, "y": -5, "coordinate_mode": "delta"}
    ]
  }]
}
```

Other input actions: `press_keys` for keyboard-only work — `keys` plus `key_action`
(`tap|hold|release`), with `repeat`, `hold_seconds`, `focus_mode`, and `hold_frames`, which
keeps a tap down across N released frames in step mode; `touch` for tap, swipe, and
multi-finger press/move/release; `touch_emulation` so a game's mobile code path runs at all;
`pointer_lock` for first-person controls. The keyboard verb is `key_action`, the pointer
verb `pointer_action`, the touch verb `touch_action`, and pointer lock's is `operation`,
because the dispatcher itself owns `action`.

`input`, `press_keys`, `pointer`, `touch`, and `step` all accept `include_summary=false`,
which skips the page read and returns only the action result; `wait_seconds` already
defaults to `0`. Use both while driving a game frame by frame, and read the page explicitly
when you actually want to look at it.

Render modes:

- `normal`: native page timing.
- `throttled`: continuous slow motion at `target_fps`, such as 10.
- `step`: nothing advances until `step {frames}` or an `input` action releases a frame.

Both gated modes freeze page time: `performance.now()` and `Date.now()` advance one fixed
frame delta per released frame and page timers are queued against that clock, so the game
never measures your thinking time as its `deltaTime`. A tapped key is held for the whole
released frame, which is what engines that poll key state per frame need.

Always `release_inputs` after holding input and restore `render` to `normal` before handoff
or close. Take a screenshot or read `game_probe` between batches; in step mode a screenshot
taken before the first frame still shows the old frame, and `game_probe` reports
`animation.animation_suspended` with `animation.fps: null` instead of measuring a gate you
are driving by hand.

A probe's `console_messages` holds only the warnings and errors new since the previous
`game_probe` call — the result says so in `console_scope` — so polling it in a loop is cheap
and its output never grows with the run. Each entry is delivered once: read
`console_messages` on every probe you make, because a result you drop takes them with it.
Use the `console` topic when you need history, `log`/`info` levels, or stack frames.

## Batch results

`web_action` runs up to 32 ordered actions. Check top-level `success`, `failure_count`, and
every entry in `results`. Use `continue_on_error=true` only when later actions are
independent or cleanup must still run.
