---
name: web-search-neo
description: Use the Web Search Neo MCP server for free web search without API keys, resilient multi-engine fallback, HTTP page fetching, rendered Chrome automation, authorized persistent or attached browser sessions, form inspection/filling/upload/submission, screenshots, and deterministic canvas/WebGL game testing. Trigger when a task needs current web results, visible browser work, an already signed-in managed Chrome profile, DOM controls, file upload, or atomic keyboard/mouse input and render stepping through the two-tool web_info/web_action contract.
---

# Web Search Neo

Use the compact two-tool contract. Read state with `web_info`; execute one or more ordered operations with `web_action`.

## Discover only what is needed

1. Call `web_info` with `topic="capabilities"` if the contract is unknown.
2. Call `web_info` with `topic="action_schema"` and `params={"action":"<name>"}` before using an unfamiliar action.
3. Avoid loading every action schema. Keep context focused on the current task.

Use these observation topics directly:

- `search_status`: configured and live search providers, latency, cooldowns, and challenges.
- `browser_status`: Chrome capability and session state.
- `page_elements`: links, forms, fields, and buttons with CSS selectors.
- `game_probe`: canvas/WebGL/iframe surfaces, focus, FPS, console issues, and held input.
- `screenshot`: current rendered image.
- `time`: local date, time, and UTC offset.

## Search and fetch

Send a `search` action through `web_action`. Keep `engine="duckduckgo"` unless the user requests another provider. Keep `fallback=true` and `challenge_mode="fallback"` for normal fast search.

Use `challenge_mode="manual"` only when a visible three-minute human challenge handoff is useful. If unresolved, allow the server to close that session and continue fallback. Never claim that the MCP automatically solves CAPTCHA.

Use `fetch_text`, `fetch_links`, or `fetch_many` when exact URLs are already known. Prefer `fetch_many` for independent pages that can be downloaded concurrently.

## Choose a browser profile

- Use `temporary` for clean disposable work. It opens visibly by default; set `headless=true` only when background operation is explicitly wanted.
- Use `persistent` with a stable `profile_id` for a separate MCP-owned profile that retains logins. It also opens visibly by default.
- Use `attach` with `debugger_address="127.0.0.1:<port>"` for a dedicated Chrome launched with remote debugging. It defaults to visible and remains open after MCP detaches.

Do not imply that an arbitrary normal Chrome window can be attached retroactively. Reuse one `session_id` for all operations on the same page and close owned sessions when finished.

## Automate forms safely

1. Open the page.
2. Read `page_elements` and use the returned selectors.
3. Use ordered `fill`, `upload`, `click`, and `submit` actions.
4. Inspect each action result. Treat `success=false`, validation errors, or a detected challenge as incomplete work.
5. Read page state or a screenshot after consequential actions before reporting success.

Use the `upload` action for one or more local file paths. Do not confuse selecting a file with final form submission.

## Test canvas and WebGL games

Read `game_probe` first. Pass its `frame_selector` to input and render actions when the game is inside an iframe.

Use one `input` action for changes that must occur in the same game frame. Mix keyboard and pointer entries in the same contract:

```json
{
  "actions": [{
    "action": "input",
    "session_id": "game",
    "key_actions": [
      {"key": "W", "action": "hold"},
      {"key": "S", "action": "release"},
      {"key": "SPACE", "action": "tap"},
      {"key": "E", "action": "tap"}
    ],
    "pointer_actions": [
      {"action": "hover", "x": 640, "y": 360},
      {"action": "move", "x": 15, "y": -5, "coordinate_mode": "delta"},
      {"action": "click", "x": 700, "y": 400}
    ]
  }]
}
```

Choose one render mode:

- `normal`: native page timing.
- `throttled`: continuous slow motion at `target_fps`, such as 10.
- `step`: freeze animation callbacks; each mixed `input` action advances exactly one frame and `step` advances an explicit count.

Always send `release_inputs` after held input and restore `render` to `normal` before handoff or close. Use screenshots or `game_probe` between meaningful action batches; do not continue a long blind sequence.

## Handle batch results

`web_action` executes up to 32 ordered actions. Check top-level `success`, `failure_count`, and every item in `results`. Use `continue_on_error=true` only when later actions are independent or cleanup must still run.
