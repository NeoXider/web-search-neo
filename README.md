<p align="center">
  <img src="docs/assets/web-search-neo-hero.jpg" alt="Web Search Neo — free MCP web search and visible browser automation" width="100%">
</p>

<h1 align="center">Web Search Neo</h1>

<p align="center">
  Free, API-keyless web search and visible Chrome automation for AI agents through MCP.
</p>

<p align="center">
  <a href="https://github.com/modelcontextprotocol/python-sdk"><img alt="MCP" src="https://img.shields.io/badge/MCP-stdio-10b981"></a>
  <img alt="Python 3.10–3.13" src="https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776AB?logo=python&logoColor=white">
  <img alt="No API key required" src="https://img.shields.io/badge/Search_API_key-not_required-14b8a6">
  <img alt="Chrome automation" src="https://img.shields.io/badge/Chrome-visible_automation-06b6d4?logo=googlechrome&logoColor=white">
</p>

Web Search Neo gives LM Studio and other MCP clients four complementary capabilities:

- fast text search with automatic fallback across independent search engines;
- the user's already-open, signed-in Chrome through a local companion extension, with full page/form/game automation and screenshots;
- perception and diagnostics for a rendered page: an accessibility outline, readable text, semantic element lookup, the page console, and its HTTP traffic;
- isolated Selenium profiles when a clean or headless browser is preferable.

DuckDuckGo is the default route. Brave, Mojeek, Yahoo, Bing, and Startpage are available as fallbacks. No paid search API or provider API key is required.

## Why Web Search Neo

| Strength | What it means |
| --- | --- |
| Free search | Uses public search routes through the maintained [DDGS](https://github.com/deedy5/ddgs) library; no paid search plan or API key. |
| Resilient fallback | Provider health, cooldowns, bounded retries, caching, and an overall deadline prevent one challenged engine from stalling the agent. |
| Your current Chrome by default | New tabs open in the `AI` tab group of the Chrome you already use, so existing logins remain available and every action is visible. |
| Reusable authorization | List and claim existing tabs, use the current signed-in Chrome, a persistent MCP-owned profile, or a DevTools attach window. |
| Two-tool MCP surface | Models see only `web_info` and `web_action`; detailed action schemas are discovered only when needed. |
| Self-describing contract | `web_info()` with no arguments returns the actions, recipes, pitfalls, limits, and examples, so an external skill file is optional. |
| Semantic page reading | `page_outline`, `page_text`, and `find` return roles, accessible names, states, boxes, and reusable `ref:N` handles across open Shadow DOM and same-origin iframes. |
| Visible failures | The `console` and `network` topics surface console output, uncaught exceptions with stack frames, and every HTTP request with status, type, duration, and size. |
| Deterministic frames | Gated render modes freeze `performance.now()`/`Date.now()` and queue page timers, so a released frame is a fixed delta instead of the agent's thinking time. |
| Concurrent work | Search, HTTP fetches, and independent browser sessions run outside the MCP event loop; up to four browser sessions can work in parallel. |

## Quick start

Requirements: Python 3.10–3.13 on `PATH` and Google Chrome 116+ for rendered browser tools.

```powershell
git clone https://github.com/NeoXider/web-search-neo.git
cd web-search-neo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

The server uses MCP over stdio. Keep stdout reserved for MCP messages; rotating diagnostic logs are written to `msp_server.log`.

For Linux/macOS activation and detailed setup/troubleshooting, see [INSTALL.md](INSTALL.md).

## Connect to LM Studio

Open LM Studio's MCP configuration and add:

```json
{
  "mcpServers": {
    "web-search-neo": {
      "command": "python",
      "args": ["main.py"],
      "cwd": "C:/path/to/web-search-neo"
    }
  }
}
```

The Python executable is intentionally resolved through `PATH`, not pinned to a machine-specific absolute interpreter path. Restart or toggle the MCP server after changing the configuration. A ready-to-edit example is included in [mcp_servers.json](mcp_servers.json).

### Connect your already-open Chrome

The default browser mode is `current`. It fails with a clear setup error when the
companion isn't connected; it does not silently open a different browser.

An agent can prepare the bundled companion through the compact MCP contract. If
it is not already connected, the first call changes no Chrome setting and returns
the exact requested browser change:

```json
{"actions":[{"action":"setup_current_chrome"}]}
```

After the user explicitly approves installing the extension, repeat the call with
`"confirm_install": true`. On Windows this opens `chrome://extensions`, enables
Developer mode, loads this repository's fixed `chrome-extension` folder, and waits
for the loopback connection. Chrome deliberately shows the extension and its
permissions; this isn't a silent extension install.

Manual installation remains available:

1. Start or restart the MCP server so its loopback bridge is listening on `127.0.0.1:8765`.
2. Open `chrome://extensions`, enable **Developer mode**, and choose **Load unpacked**.
3. Select this repository's `chrome-extension` folder.
4. Keep **Web Search Neo Companion** enabled. Its toolbar badge reads `ON` while the MCP is connected.

The bundled companion is version 1.2.0. Chrome does not refresh an unpacked extension by itself, so after pulling a new revision press **Reload** on the companion card at `chrome://extensions`; otherwise the older service worker keeps running and the console, network, and tab-close commands are rejected as unknown methods.

The bridge accepts only the fixed bundled extension ID, binds only to loopback, and never reads Chrome profile files, cookies, or saved passwords directly. The extension uses Chrome's standard [tabs](https://developer.chrome.com/docs/extensions/reference/api/tabs), [tab groups](https://developer.chrome.com/docs/extensions/reference/api/tabGroups), and [debugger](https://developer.chrome.com/docs/extensions/reference/api/debugger) APIs, plus `storage` for its own session state, with the permissions shown by Chrome.

Keep the companion enabled in one Chrome profile at a time. The MCP accepts one
companion connection and rejects later profiles instead of silently switching the
agent to a different signed-in account.

## Optional agent skill

`web_info()` called with no arguments returns the entire agent-facing contract: every action with its required and optional parameters, the observation topics, ready-made recipes, the common mistakes, the hard limits, and runnable examples. An agent that reads it needs no external instructions, so the bundled skill is a convenience, not a requirement.

The repository still includes a short [Web Search Neo skill](skills/web-search-neo/SKILL.md) for clients that prefer a resident description of when to reach for the server at all.

Install it locally by copying `skills/web-search-neo` into your Codex skills directory, then restart Codex:

```powershell
Copy-Item -Recurse -Force skills\web-search-neo "$env:USERPROFILE\.codex\skills\web-search-neo"
```

Invoke it explicitly as `$web-search-neo`, or let its task description trigger it for web search, visible Chrome automation, authorized attach sessions, form work, and browser-game testing.

## Search behavior

The normal agent call is:

```json
{
  "actions": [{
    "action": "search",
    "query": "best local-first MCP tools",
    "num": 5,
    "engine": "duckduckgo",
    "fallback": true,
    "challenge_mode": "fallback"
  }]
}
```

Send that object to `web_action`. `web_info(topic="search_status", params={"check_live": true})` reports configured engines, current live availability, latency, cooldown state, and detected challenges. A live probe is a diagnostic: a provider that fails during the probe is not pushed into the cooldown used by real searches, so checking status can no longer degrade the next search. Status checks are cached for five minutes; search results are cached for two minutes.

### CAPTCHA and challenge modes

- `challenge_mode="fallback"` is the default. A challenged provider is skipped immediately and the search continues through another route.
- `challenge_mode="manual"` opens visible Chrome and waits up to `manual_timeout_seconds=180`. If you complete the challenge, the agent receives the open browser session; otherwise the window closes and fallback continues.

Detection looks for a rendered provider widget — reCAPTCHA, hCaptcha, Cloudflare Turnstile, Yandex SmartCaptcha, PerimeterX, or any element carrying `data-sitekey` — at least 20x20 pixels in size, and only treats challenge wording as evidence in a page heading or in the body of a short interstitial. A normal article that happens to mention captchas is no longer reported as challenged. Every page summary carries `challenge_detected`, `challenge_type`, and `challenge_evidence`.

Automatic CAPTCHA bypass is intentionally not implemented. The roadmap tracks a future provider-supported, legal integration in [TODO.md](TODO.md).

## Current Chrome automation

Yes — the agent can work in your normal already-open Chrome while you watch it. New pages go into a visible purple tab group named `AI` by default:

```json
{
  "actions": [{
    "action": "open",
    "url": "https://example.com",
    "session_id": "demo"
  }]
}
```

The agent can inspect with `web_info(topic="page_outline", params={"session_id": "demo"})` or `page_elements`, then send ordered `fill`, `upload`, `click`, and `submit` actions through `web_action` using the same `session_id`. Screenshots are returned by the `screenshot` info topic.

Inspect existing Chrome tabs without opening anything:

```json
{"topic":"browser_tabs"}
```

Then claim one returned `tab_id` without navigating or moving it:

```json
{"actions":[{"action":"attach_tab","tab_id":123,"session_id":"existing"}]}
```

A session tracks whether it owns its tab. `close` removes a tab that the agent opened itself, and leaves a tab claimed through `attach_tab` open and detached, so the `AI` group no longer accumulates abandoned pages. The MCP doesn't read cookies or passwords; the page simply continues using the authorization already present in Chrome.

If the companion isn't ready, read `web_info(topic="browser_status")` and run the
confirmation-gated `setup_current_chrome` action. Use `profile_mode="auto"` only
when opening a separate visible Selenium window is an acceptable fallback.

For isolated/background work, opt into Selenium explicitly:

- `profile_mode="temporary", headless=false`: clean visible disposable Chrome;
- `profile_mode="temporary", headless=true`: clean headless Chrome;
- `profile_mode="persistent"`: separate durable MCP-owned profile;
- for `attach`, the launcher determines whether the already-running Chrome is visible or headless.

### Chrome profile modes

| Mode | Authorization and lifetime | Best for |
| --- | --- | --- |
| `current` (default) | Companion extension controls the user's open Chrome. New tabs enter the `AI` group; claimed tabs stay where they are. | Authorized sites, visible automation, existing tabs. |
| `auto` | Prefer `current`; fall back to a visible temporary Selenium profile if the companion is unavailable. | Portable clients that accept a fallback window. |
| `temporary` | Clean disposable profile; cookies disappear when the session closes. | Search, scraping, isolated tests. |
| `persistent` | MCP owns a durable profile under `%LOCALAPPDATA%\WebSearchNeo\profiles\<profile_id>`. | Repeated automation with a separate signed-in profile. |
| `attach` | MCP connects to a Chrome process that you started with a DevTools port and does not close it on detach. | Watching the agent work in an already authorized managed Chrome window. |

Start a durable visible Chrome for attach mode (visible is the launcher default):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId authorized -Port 9222 -WindowMode visible
```

Sign in to the sites you need in that window, keep it open, then let the agent attach:

```json
{
  "actions": [{
    "action": "open",
    "url": "https://hh.ru/",
    "session_id": "hh-authorized",
    "headless": false,
    "profile_mode": "attach",
    "debugger_address": "127.0.0.1:9222"
  }]
}
```

Chrome 136+ does not allow remote debugging against its normal default data directory. The included launcher therefore uses a separate durable profile. It feels like a normal visible Chrome window, keeps its logins, and remains open after MCP disconnects. See the [Chrome remote debugging security change](https://developer.chrome.com/blog/remote-debugging-port).

An ordinary Chrome window cannot accept a DevTools-port attach retroactively, which is why Web Search Neo includes its own companion extension. Codex's private extension/runtime isn't copied or required.

Attach mode can also connect to a managed Chrome running without a window:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId automation -Port 9223 -WindowMode headless
```

The `headless` argument cannot hide or reveal a Chrome process that is already running; for `attach`, `-WindowMode` on the launcher controls the actual window.

## Reading a page

Four observation topics describe an open session, from semantic structure down to raw selectors:

| Topic | Returns |
| --- | --- |
| `page_outline` | An indented tree of roles, accessible names, states, `ref:N` handles, and boxes. `limit=200` nodes, `output="text"` by default; `output="json"` returns one object per node with `rect`, `page_rect`, `center`, `visible`, `in_viewport`, and `occluded`. |
| `page_text` | The rendered text, with headings, list items, and table cells preserved. `mode="main"` drops navigation, header, footer, aside, and form chrome; `mode="full"` keeps it. `max_chars=20000`, `include_links=false`. |
| `find` | Ranked matches for a plain-language `query` such as `"submit application"`, each with a `ref`, role, name, score, and box. `limit=5`, `role` is a soft preference. Sets `low_confidence` when nothing scores well. |
| `page_elements` | The original flat lists of links, forms, fields with `<select>` options, and buttons, addressed by CSS selector. |

```json
{"topic":"page_outline","params":{"session_id":"demo","output":"json","limit":80}}
```

The outline and `find` walk open shadow roots and same-origin iframes, including nested ones, and translate every box into top-document coordinates so a reported `center` can be clicked or pointed at directly. A cross-origin frame appears as one node with `same_origin: false`; read it by passing its selector as `frame_selector`. Closed shadow roots are counted in `closed_shadow_roots` rather than entered.

### Locators

| Form | Example | Notes |
| --- | --- | --- |
| CSS selector | `input[name='q']` | Unchanged, and still the only form accepted everywhere. |
| Ref handle | `ref:12` | Issued by `page_outline` and `find`. Stable while the document lives; both topics also report `dom_epoch`, which changes after a navigation. |
| Piercing path | `#host >>> .inner` | The separator is a space-padded ` >>> `. Each step enters the element's open shadow root, or its document when the element is a same-origin iframe. Any number of segments may be chained. |

`ref:N` and piercing paths are accepted by `fill` (both its field and its file keys), `upload`, and the `form_selector` of `submit`. `click`, the `submit_selector`, `wait`, and every `frame_selector` still expect a plain CSS selector. Both new forms need a live element handle, so they resolve in the Selenium-backed modes (`temporary`, `persistent`, `attach`); in companion `current` mode they are refused with an explicit message and CSS selectors remain the way to address elements.

## Console and network diagnostics

The page's own console and HTTP traffic are readable without leaving the MCP contract:

| Topic | Returns |
| --- | --- |
| `console` | `console.log/info/warn/error`, uncaught exceptions and rejections with stack frames, and browser log entries. Filter with `levels`, `kinds`, `contains`; page through with `since_seq` and `limit=50`. |
| `network` | One compact line per request in the documented `method status type ms size url` order. Filter with `url_pattern` (a case-insensitive regex), `types`, `status_min`/`status_max`, or `only_errors=true`. |
| `network_body` | One response body, by `request_id`, capped at `max_chars=20000`. |

```json
{"topic":"network","params":{"session_id":"demo","only_errors":true}}
```

Use `output="json"` on the `network` topic when you need the per-request `id` that `network_body` expects; the default text lines omit it.

Console capture starts when the session attaches to the tab, and network capture starts on the first `network` read, so traffic and logs produced before that are not in the buffer — reload the page to observe a full page load. Each stream keeps up to 500 entries within a shared 512 KB budget and reports how many older records were `dropped`. Both topics work in companion `current` mode and in the Selenium-backed modes; the Selenium path reads Chrome's browser and performance logs instead of the extension's buffer, so field coverage is close but not identical.

## Testing canvas and WebGL games

Browser automation is not limited to DOM forms. The compact contract covers common HTML5 game controls:

- `web_info(topic="game_probe")` reports canvases, 2D/WebGL context, iframe surfaces, document focus, sampled animation FPS, loading time, console issues, and held input;
- the `input` action mixes per-key `tap/hold/release` with pointer `click`, `double_click`, `hover`, `move`, `drag`, `press`, `release`, and `wheel`, using absolute coordinates, deltas, or unbounded relative motion, up to 16 entries of each kind;
- `touch` sends `tap`, `press`, `move`, `release`, `swipe`, or `cancel` with up to ten simultaneous points, and `touch_emulation` makes the page report `navigator.maxTouchPoints` and `ontouchstart` so a game's mobile code path actually runs;
- `pointer_lock` acquires, releases, or reports pointer lock for first-person controls; while locked, `coordinate_mode="relative"` moves without clamping to the viewport, which is what feeds `movementX`/`movementY`;
- `render`, `step`, and `release_inputs` actions control animation and safely reset held input;
- `frame_selector` targets a cross-origin game iframe such as the one used by Yandex Games.

Keyboard coverage includes `F1`-`F12`, `NUMPAD0`-`NUMPAD9` with the numeric keypad location, `MULTIPLY`/`ADD`/`SUBTRACT`/`DECIMAL`/`DIVIDE`, `META` (also `WIN`, `CMD`, `COMMAND`), the arrow, navigation, and editing keys, and any single printable character. A key held as `W` is released by `w` as well, and the release dispatches exactly the character that was pressed. A modifier held with `hold` is carried into subsequent mouse and touch events, so `Shift`-click and `Ctrl`-click behave as a user's would. Giving a canvas keyboard focus no longer costs a synthetic click, which used to reach the game as a shot or a jump.

Example for a game hosted inside an iframe:

```json
{
  "actions": [
    {"action": "open", "url": "https://yandex.ru/games/app/geometry-dash-ufo-2d-371298", "session_id": "ufo", "headless": false},
    {"action": "render", "mode": "step", "session_id": "ufo", "frame_selector": "#game-frame"},
    {
      "action": "input",
      "session_id": "ufo",
      "frame_selector": "#game-frame",
      "key_actions": [
        {"key": "W", "action": "hold"},
        {"key": "S", "action": "release"},
        {"key": "SPACE", "action": "tap"},
        {"key": "E", "action": "tap"}
      ],
      "pointer_actions": [
        {"action": "hover", "x": 640, "y": 360},
        {"action": "wheel", "x": 640, "y": 360, "delta_y": -240},
        {"action": "move", "x": 15, "y": -5, "coordinate_mode": "delta"}
      ]
    },
    {"action": "step", "frames": 5, "session_id": "ufo"},
    {"action": "release_inputs", "session_id": "ufo"},
    {"action": "render", "mode": "normal", "session_id": "ufo"}
  ]
}
```

Send the batch to `web_action`; use `web_info(topic="game_probe", params={"session_id": "ufo", "frame_selector": "#game-frame"})` or the `screenshot` topic to observe it.

For a top-level canvas application such as `https://redoschool.ru/demo/?auto=true`, omit `frame_selector`. Pointer coordinates are relative to the selected top-level viewport or iframe.

### Render modes

| Mode | Behavior |
| --- | --- |
| `normal` | Removes the render gate and returns to the page's normal `requestAnimationFrame` loop and real clocks. |
| `throttled` | Continuously releases animation callbacks at no more than `target_fps`, for example 10 FPS. `target_fps` defaults to 10 and is clamped to 1-60. |
| `step` | Holds queued animation callbacks. The `step` action releases 1-120 frames explicitly; every `input` action also releases exactly one frame. |

While the gate is engaged — in `throttled` as well as in `step` — page time is frozen. `performance.now()` and `Date.now()` advance by exactly one frame delta of 16.667 ms per released frame, and `setTimeout`, `setInterval`, and `requestIdleCallback` are queued against that same virtual clock and run immediately before the frame's animation callbacks. Without this a game reads the agent's thinking time as its `deltaTime`, and a batch of frames released back to back arrives with a delta near zero. Promises and `queueMicrotask` are not gated, and `new Date()` still reports wall-clock time. The MCP `render` action always uses these defaults; the Python API `browser_tools.set_render_control` exposes `frame_delta_ms`, `freeze_time`, and `gate_timers` for callers that need to change them. Queued timers are handed back to the real scheduler when the mode returns to `normal`.

One `input` action can hold one key, release another, tap two more, turn the wheel, and move the pointer by a delta before releasing exactly one frame. A tapped key is pressed together with the rest of the batch, stays down for the whole released frame, and is lifted afterwards, so an engine that polls key state once per frame — Phaser, Godot, a hand-written canvas loop — actually observes the press. In `step` mode the game never observes a partially applied intermediate input state. Outside step mode the actions are still serialized, but the page continues rendering normally.

The gate is installed into every new document of a session, so it survives a page reload or a game iframe that reloads itself; a `step` that lands on a fresh document re-applies step mode once and reports `gate_reinstalled`. `render` reports `frame_delta_ms`, `time_frozen`, `timers_gated`, `pending_callbacks`, and `input_advances_frame`; `step` reports `frames`, `callbacks`, `pending_timers`, and `virtual_now`.

The render controller gates JavaScript `requestAnimationFrame`, which covers typical canvas/WebGL and Unity WebGL loops. It does not change video decoding, CSS compositor animations, the monitor refresh rate, or guarantee an exact GPU hardware frame rate on every engine.

`scripts/live_smoke.py` is the only committed check that touches the public internet. It runs a search, opens two pages concurrently, fills and submits a public Selenium test form, uploads a file, and verifies exact screenshot dimensions; it does not cover games. Public game sites change without notice, so the frame gate, input atomicity, and held-input recovery are verified by the deterministic local suite instead.

## Two-tool MCP contract

| Tool | Responsibility |
| --- | --- |
| `web_info` | Return the whole contract, or one action schema on demand; read search, current Chrome tabs, browser, page outline/text/find, console, network, game, screenshot, or time state. |
| `web_action` | Execute one or up to 32 ordered setup, search, fetch, tab attach/open, form, input, render, and close actions. Supports fail-fast or `continue_on_error=true`. |

Start with `web_info()`. With no arguments it returns `actions` with each action's required and optional parameters, `action_groups`, `info_topics`, `recipes`, `pitfalls`, `limits`, and worked `examples`. Request only the needed, generated JSON Schema with `web_info(topic="action_schema", params={"action": "input"})`, then invoke it through `web_action`. This follows the on-demand Tool Search principle used by [official Unreal MCP](https://dev.epicgames.com/documentation/unreal-engine/unreal-mcp-in-unreal-editor): keep the eager tool list small, disclose schemas only when needed, and dispatch actions through a meta-tool. Web Search Neo combines Unreal's list/describe discovery tools into one `web_info`, so only two tools are advertised.

Measured on the current build, summing each advertised tool's `name`, `description`, and serialized `inputSchema`: the compact surface is 1,029 characters across two tools, against 19,944 characters across the 40 tools of legacy mode.

Every action is declared once in a single registry that also generates its published schema, and arguments are validated against that same model before the handler runs. An unknown or malformed field returns the offending names and the list of allowed parameters instead of an internal `TypeError`:

```text
ValueError: action 'input': unknown parameter(s) ['frames']. Allowed: ['key_actions', 'pointer_actions',
'session_id', 'target_selector', 'frame_selector', 'wait_seconds']. Call
web_info(topic='action_schema', params={'action': '<name>'}) for the full schema.
```

Existing direct Python imports remain available. For temporary MCP-client migration only, set `WEB_SEARCH_NEO_LEGACY_TOOLS=1` before starting the server to advertise the former narrow tool list instead of the compact default.

Browser state is keyed by `session_id`. The `open_many` action can create up to four independent sessions concurrently. In isolated Selenium modes, non-full-page screenshots match the requested viewport dimensions exactly. In `current` mode the MCP intentionally preserves the user's existing Chrome viewport and captures it at its actual size.

## Optional configuration

```powershell
$env:WEB_SEARCH_NEO_REGION = "us-en"
$env:WEB_SEARCH_NEO_PROXY = "socks5://127.0.0.1:9050"
$env:WEB_SEARCH_NEO_BROWSER_USER_AGENT = "..."
$env:WEB_SEARCH_NEO_PROFILE_ROOT = "D:\BrowserProfiles"
$env:WEB_SEARCH_NEO_DEBUGGER_ADDRESS = "127.0.0.1:9222"
$env:WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP = "1"
python main.py
```

Only use a proxy you are authorized to use. HTTP sessions use desktop browser headers, connection pooling, bounded response sizes, and conservative retry/backoff. Rendered pages use the installed Chrome's native matching User-Agent unless explicitly overridden.

### Transport policy

Unencrypted `http://` to a public host is refused, for plain fetches and for browser `open` alike, with an error that names the host and the override. Loopback, private, link-local, and unspecified addresses stay reachable over plain HTTP, as do `localhost` and any host ending in `.local`, `.localhost`, `.internal`, or `.home.arpa` — a local ComfyUI, Ollama, or dev server keeps working unchanged. Set `WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP=1` to accept plain HTTP everywhere.

Redirects are followed one hop at a time, at most five, and every hop is validated again, so a public HTTPS URL cannot quietly land on plain HTTP.

## Tests

[![Tests](https://github.com/NeoXider/web-search-neo/actions/workflows/tests.yml/badge.svg)](https://github.com/NeoXider/web-search-neo/actions/workflows/tests.yml)

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python -m pytest --cov=. --cov-report=term-missing
```

The deterministic suite verifies that MCP advertises exactly two tools, performs on-demand action discovery, runs ordered multi-action calls, and retains coverage of search routing, fallback/cooldown/cache, live status probes that leave the real cooldown untouched, accurate Bing challenge detection, HTTP fetches, the plain-HTTP policy and per-hop redirect checks, the accessibility outline including open shadow roots and same-origin frames, page text extraction, semantic `find` ranking, the three locator forms and their escaping, the companion's console and network buffers with their ring-buffer eviction, companion bridge origin checks and tab grouping, confirmation-gated Chrome setup, multipart upload, forms, validation, exact PNG viewport size, canvas probing, normal/throttled/step rendering, atomic mixed input, held modifiers across a batch, gate and held-input reset on navigation, concurrent sessions, manual challenges, persistent storage, and a real managed-Chrome attach/detach that leaves Chrome running.

Public search engines may rate-limit an IP or region, so live internet smoke checks are kept separate from deterministic tests.

### End-to-end check with a local model

`scripts/live_agent_game.py` exercises the whole stack the way a real client does: it starts `main.py` as an MCP stdio subprocess, hands the two advertised tools to a model served by [LM Studio](https://lmstudio.ai/) on `127.0.0.1:1234`, and lets that model play the bundled platformer fixture while every tool call is timed.

```powershell
lms load qwen3.5-4b-mtp --context-length 16384 --parallel 1
python scripts/live_agent_game.py --model qwen3.5-4b-mtp
```

It reports the eager tool-schema size and the median and p95 latency of both MCP tool calls and model turns, so a regression in either is visible immediately. Thinking is disabled through `reasoning_effort: "none"`, because a mechanical control loop pays for it without gaining anything. The script needs Chrome and a running LM Studio server and is not part of `pytest`.

## Safety notes

- Visible or attached sessions may contain authenticated accounts. The MCP client can act with the permissions of those accounts.
- The companion declares four permissions: `debugger`, `storage`, `tabs`, and `tabGroups`. It ships no content scripts and asks for no `host_permissions`, but `debugger` is the broad one: it lets the extension attach the Chrome DevTools Protocol to a tab and from there read and modify that page, its console, and its network traffic. Chrome shows a "started debugging this browser" banner whenever it is attached. Install the companion only from this repository and keep the local MCP bridge trusted.
- `setup_current_chrome` requires explicit user approval before it changes Chrome's extension state.
- Plain `http://` to public hosts is refused unless `WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP=1` is set; loopback and private-network addresses are always reachable.
- File upload tools can upload local paths supplied to the tool. Review agent actions and scope filesystem access appropriately.
- Browser automation may be restricted by a site's terms of service. Use it only where you are authorized.
- Manual challenge mode hands control to you; it does not attempt to bypass CAPTCHA protections.

## Contributing

Issues and focused pull requests are welcome. A new search engine only needs a `SearchProvider` implementation plus `register_search_provider(provider)`; status, cooldown, cache, and fallback routing update automatically.

See [TODO.md](TODO.md) for the current roadmap.
