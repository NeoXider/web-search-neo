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

Web Search Neo gives LM Studio and other MCP clients three complementary capabilities:

- fast text search with automatic fallback across independent search engines;
- the user's already-open, signed-in Chrome through a local companion extension, with full page/form/game automation and screenshots;
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

The bridge accepts only the fixed bundled extension ID, binds only to loopback, and never reads Chrome profile files, cookies, or saved passwords directly. The extension uses Chrome's standard [tabs](https://developer.chrome.com/docs/extensions/reference/api/tabs), [tab groups](https://developer.chrome.com/docs/extensions/reference/api/tabGroups), and [debugger](https://developer.chrome.com/docs/extensions/reference/api/debugger) APIs with the permissions shown by Chrome.

Keep the companion enabled in one Chrome profile at a time. The MCP accepts one
companion connection and rejects later profiles instead of silently switching the
agent to a different signed-in account.

## Optional agent skill

The repository includes a concise [Web Search Neo skill](skills/web-search-neo/SKILL.md) that teaches Codex-compatible agents the two-tool workflow, browser profile selection, safe form handling, and atomic game input without loading every action schema.

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

Send that object to `web_action`. `web_info(topic="search_status", params={"check_live": true})` reports configured engines, current live availability, latency, cooldown state, and detected challenges. Status checks are cached for five minutes; search results are cached for two minutes.

### CAPTCHA and challenge modes

- `challenge_mode="fallback"` is the default. A challenged provider is skipped immediately and the search continues through another route.
- `challenge_mode="manual"` opens visible Chrome and waits up to `manual_timeout_seconds=180`. If you complete the challenge, the agent receives the open browser session; otherwise the window closes and fallback continues.

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

The agent can inspect with `web_info(topic="page_elements", params={"session_id": "demo"})`, then send ordered `fill`, `upload`, `click`, and `submit` actions through `web_action` using the same `session_id`. Screenshots are returned by the `screenshot` info topic.

Inspect existing Chrome tabs without opening anything:

```json
{"topic":"browser_tabs"}
```

Then claim one returned `tab_id` without navigating or moving it:

```json
{"actions":[{"action":"attach_tab","tab_id":123,"session_id":"existing"}]}
```

Closing a `current` MCP session detaches automation and leaves the user's tab open. The MCP doesn't read cookies or passwords; the page simply continues using the authorization already present in Chrome.

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

## Testing canvas and WebGL games

Browser automation is not limited to DOM forms. The compact contract covers common HTML5 game controls:

- `web_info(topic="game_probe")` reports canvases, 2D/WebGL context, iframe surfaces, document focus, sampled animation FPS, loading time, console issues, and held input;
- the `input` action mixes per-key `tap/hold/release` with pointer click, hover, move, drag, press, or release using absolute coordinates or deltas;
- `render`, `step`, and `release_inputs` actions control animation and safely reset held input;
- `frame_selector` targets a cross-origin game iframe such as the one used by Yandex Games.

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
| `normal` | Removes the render gate and returns to the page's normal `requestAnimationFrame` loop. |
| `throttled` | Continuously releases animation callbacks at no more than `target_fps`, for example 10 FPS. |
| `step` | Holds queued animation callbacks. The `step` action releases frames explicitly; every `input` action also releases exactly one frame. |

One `input` action can hold one key, release another, tap two more, hover, and move the pointer by a delta before releasing exactly one frame. In `step` mode the game never observes a partially applied intermediate input state. Outside step mode the actions are still serialized, but the page continues rendering normally.

The render controller gates JavaScript `requestAnimationFrame`, which covers typical canvas/WebGL and Unity WebGL loops. It does not change video decoding, CSS compositor animations, the monitor refresh rate, or guarantee an exact GPU hardware frame rate on every engine.

Live MCP stdio smoke results on 2026-08-11:

| Public example | Detected surface | Verified behavior |
| --- | --- | --- |
| [Geometry Dash: UFO 2D](https://yandex.ru/games/app/geometry-dash-ufo-2d-371298) | Cross-origin `#game-frame`, WebGL2 canvas | Launch, probe, mixed `W=hold / S=release / SPACE+E=tap`, absolute hover plus pointer delta, one automatic and three explicit step frames, emergency release. |
| [RedoSchool demo](https://redoschool.ru/demo/?auto=true) | Top-level WebGL2 canvas | Launch, probe, console collection, normal 75.8 FPS sample, continuous 10 FPS target measured at 9.7 FPS. |

Live values depend on the machine and page version; the deterministic local suite verifies the control semantics independently of public-site availability.

## Two-tool MCP contract

| Tool | Responsibility |
| --- | --- |
| `web_info` | Discover action groups and one action schema on demand; read search, current Chrome tabs, browser, page, game, screenshot, or time state. |
| `web_action` | Execute one or up to 32 ordered setup, search, fetch, tab attach/open, form, input, render, and close actions. Supports fail-fast or `continue_on_error=true`. |

Start with `web_info()` for a compact list. Request only the needed, generated JSON Schema with `web_info(topic="action_schema", params={"action": "input"})`, then invoke it through `web_action`. This follows the on-demand Tool Search principle used by [official Unreal MCP](https://dev.epicgames.com/documentation/unreal-engine/unreal-mcp-in-unreal-editor): keep the eager tool list small, disclose schemas only when needed, and dispatch actions through a meta-tool. Web Search Neo combines Unreal's list/describe discovery tools into one `web_info`, so only two tools are advertised. In the current build this reduces the eager model-facing tool schema from about 14.9k to 1.0k characters.

Existing direct Python imports remain available. For temporary MCP-client migration only, set `WEB_SEARCH_NEO_LEGACY_TOOLS=1` before starting the server to advertise the former narrow tool list instead of the compact default.

Browser state is keyed by `session_id`. The `open_many` action can create up to four independent sessions concurrently. In isolated Selenium modes, non-full-page screenshots match the requested viewport dimensions exactly. In `current` mode the MCP intentionally preserves the user's existing Chrome viewport and captures it at its actual size.

## Optional configuration

```powershell
$env:WEB_SEARCH_NEO_REGION = "us-en"
$env:WEB_SEARCH_NEO_PROXY = "socks5://127.0.0.1:9050"
$env:WEB_SEARCH_NEO_BROWSER_USER_AGENT = "..."
$env:WEB_SEARCH_NEO_PROFILE_ROOT = "D:\BrowserProfiles"
$env:WEB_SEARCH_NEO_DEBUGGER_ADDRESS = "127.0.0.1:9222"
python main.py
```

Only use a proxy you are authorized to use. HTTP sessions use desktop browser headers, connection pooling, bounded response sizes, and conservative retry/backoff. Rendered pages use the installed Chrome's native matching User-Agent unless explicitly overridden.

## Tests

[![Tests](https://github.com/NeoXider/web-search-neo/actions/workflows/tests.yml/badge.svg)](https://github.com/NeoXider/web-search-neo/actions/workflows/tests.yml)

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python -m pytest --cov=. --cov-report=term-missing
```

The deterministic suite verifies that MCP advertises exactly two tools, performs on-demand action discovery, runs ordered multi-action calls, and retains coverage of search routing, fallback/cooldown/cache, accurate Bing challenge detection, HTTP fetches, companion bridge origin checks and tab grouping, confirmation-gated Chrome setup, multipart upload, forms, validation, exact PNG viewport size, canvas probing, normal/throttled/step rendering, atomic mixed input, concurrent sessions, manual challenges, persistent storage, and a real managed-Chrome attach/detach that leaves Chrome running.

Public search engines may rate-limit an IP or region, so live internet smoke checks are kept separate from deterministic tests.

## Safety notes

- Visible or attached sessions may contain authenticated accounts. The MCP client can act with the permissions of those accounts.
- The companion requests access to HTTP/HTTPS pages and Chrome's debugger API so it can inspect, click, type, upload, capture, and test rendered pages. Install it only from this repository and keep the local MCP bridge trusted.
- `setup_current_chrome` requires explicit user approval before it changes Chrome's extension state.
- File upload tools can upload local paths supplied to the tool. Review agent actions and scope filesystem access appropriately.
- Browser automation may be restricted by a site's terms of service. Use it only where you are authorized.
- Manual challenge mode hands control to you; it does not attempt to bypass CAPTCHA protections.

## Contributing

Issues and focused pull requests are welcome. A new search engine only needs a `SearchProvider` implementation plus `register_search_provider(provider)`; status, cooldown, cache, and fallback routing update automatically.

See [TODO.md](TODO.md) for the current roadmap.
