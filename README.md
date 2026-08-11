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

Web Search Neo gives LM Studio and other MCP clients two complementary ways to use the web:

- fast text search with automatic fallback across independent search engines;
- a real rendered Chrome browser that can inspect pages, fill forms, upload files, click buttons, submit forms, control canvas/WebGL games, and return screenshots.

DuckDuckGo is the default route. Brave, Mojeek, Yahoo, Bing, and Startpage are available as fallbacks. No paid search API or provider API key is required.

## Why Web Search Neo

| Strength | What it means |
| --- | --- |
| Free search | Uses public search routes through the maintained [DDGS](https://github.com/deedy5/ddgs) library; no paid search plan or API key. |
| Resilient fallback | Provider health, cooldowns, bounded retries, caching, and an overall deadline prevent one challenged engine from stalling the agent. |
| Visible automation | Run Chrome with `headless=false` and watch every navigation, field fill, upload, click, and submit. |
| Reusable authorization | Use a persistent MCP-owned profile or attach to a dedicated Chrome window where you are already signed in. |
| Agent-friendly tools | Structured page elements, canvas/iframe probes, keyboard and pointer input, reusable CSS selectors, screenshots, status, and clear validation/error results. |
| Concurrent work | Search, HTTP fetches, and independent browser sessions run outside the MCP event loop; up to four browser sessions can work in parallel. |

## Quick start

Requirements: Python 3.10–3.13 on `PATH` and Google Chrome for rendered browser tools.

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

## Search behavior

The normal agent call is:

```text
search_web(
  query="best local-first MCP tools",
  num=5,
  engine="duckduckgo",
  fallback=true,
  challenge_mode="fallback"
)
```

`get_search_engines_status(check_live=true)` reports configured engines, current live availability, latency, cooldown state, and detected challenges. Status checks are cached for five minutes; search results are cached for two minutes.

### CAPTCHA and challenge modes

- `challenge_mode="fallback"` is the default. A challenged provider is skipped immediately and the search continues through another route.
- `challenge_mode="manual"` opens visible Chrome and waits up to `manual_timeout_seconds=180`. If you complete the challenge, the agent receives the open browser session; otherwise the window closes and fallback continues.

Automatic CAPTCHA bypass is intentionally not implemented. The roadmap tracks a future provider-supported, legal integration in [TODO.md](TODO.md).

## Visible browser automation

Yes — the agent can work in an open browser while you watch it.

For a fresh visible session:

```text
browser_open_page(
  url="https://example.com",
  session_id="demo",
  headless=false,
  profile_mode="temporary"
)
```

The agent can then call `browser_get_page_elements`, `browser_fill_fields`, `browser_upload_file`, `browser_click`, `browser_submit_form`, and `browser_screenshot` using the same `session_id`.

`headless` is an automatic three-state option:

- omit it or send `null`: temporary/persistent sessions default to headless, while `attach` defaults to visible;
- `headless=false`: open an owned temporary or persistent Chrome visibly;
- `headless=true`: run an owned temporary or persistent Chrome without a visible window.

### Three Chrome profile modes

| Mode | Authorization and lifetime | Best for |
| --- | --- | --- |
| `temporary` | Clean disposable profile; cookies disappear when the session closes. | Search, scraping, isolated tests. |
| `persistent` | MCP owns a durable profile under `%LOCALAPPDATA%\WebSearchNeo\profiles\<profile_id>`. | Repeated automation with a separate signed-in profile. |
| `attach` | MCP connects to a Chrome process that you started with a DevTools port and does not close it on detach. | Watching the agent work in an already authorized managed Chrome window. |

Start a durable visible Chrome for attach mode (visible is the launcher default):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId authorized -Port 9222 -WindowMode visible
```

Sign in to the sites you need in that window, keep it open, then let the agent attach:

```text
browser_open_page(
  url="https://hh.ru/",
  session_id="hh-authorized",
  headless=false,
  profile_mode="attach",
  debugger_address="127.0.0.1:9222"
)
```

Chrome 136+ does not allow remote debugging against its normal default data directory. The included launcher therefore uses a separate durable profile. It feels like a normal visible Chrome window, keeps its logins, and remains open after MCP disconnects. See the [Chrome remote debugging security change](https://developer.chrome.com/blog/remote-debugging-port).

An ordinary Chrome window that was started without a DevTools port cannot be attached retroactively. Browser-extension connections owned by Codex or another client are also not shared with LM Studio or this standalone MCP server.

Attach mode can also connect to a managed Chrome running without a window:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId automation -Port 9223 -WindowMode headless
```

The `headless` argument cannot hide or reveal a Chrome process that is already running; for `attach`, `-WindowMode` on the launcher controls the actual window.

## Testing canvas and WebGL games

Browser automation is not limited to DOM forms. These tools cover common HTML5 game controls:

- `browser_game_probe` reports canvases, 2D/WebGL context, iframe surfaces, document focus, sampled animation FPS, loading time, and browser console warnings/errors;
- `browser_pointer` clicks, double-clicks, hovers, moves, drags, or keeps a mouse button pressed across calls, using absolute coordinates or deltas from the current pointer position;
- `browser_press_keys` sends keys such as `SPACE`, `ARROW_LEFT`, `W`, or combinations with `tap`, `hold`, and `release` actions;
- `browser_input_batch` mixes different per-key actions and pointer actions into one input transaction;
- `browser_release_inputs` safely releases every key and mouse button held by a session;
- `browser_render_control` switches between normal rendering, continuous target-FPS throttling, and input-driven frame stepping;
- `browser_render_step` releases an exact number of queued animation frames in step mode;
- `frame_selector` targets a cross-origin game iframe such as the one used by Yandex Games.

Example for a game hosted inside an iframe:

```text
browser_open_page(url="https://yandex.ru/games/app/geometry-dash-ufo-2d-371298",
                  session_id="ufo", headless=false)
browser_game_probe(session_id="ufo", frame_selector="iframe", sample_seconds=1)
browser_render_control(mode="step", session_id="ufo", frame_selector="iframe")
browser_input_batch(
  key_actions=[
    {"key": "W", "action": "hold"},
    {"key": "S", "action": "release"},
    {"key": "SPACE", "action": "tap"},
    {"key": "E", "action": "tap"}
  ],
  pointer_actions=[
    {"action": "hover", "x": 640, "y": 360},
    {"action": "move", "x": 15, "y": -5, "coordinate_mode": "delta"}
  ],
  session_id="ufo",
  frame_selector="iframe"
)
browser_render_step(frames=5, session_id="ufo")
browser_release_inputs(session_id="ufo")
browser_render_control(mode="normal", session_id="ufo", frame_selector="iframe")
browser_screenshot(session_id="ufo")
```

For a top-level canvas application such as `https://redoschool.ru/demo/?auto=true`, omit `frame_selector`. Pointer coordinates are relative to the selected top-level viewport or iframe.

### Render modes

| Mode | Behavior |
| --- | --- |
| `normal` | Removes the render gate and returns to the page's normal `requestAnimationFrame` loop. |
| `throttled` | Continuously releases animation callbacks at no more than `target_fps`, for example 10 FPS. |
| `step` | Holds queued animation callbacks. `browser_render_step` releases frames explicitly; every keyboard or pointer batch also releases exactly one frame. |

`browser_press_keys(keys=["W", "SHIFT", "SPACE"], action="release")` releases the complete combination as one batch. For mixed actions, `browser_input_batch` can hold one key, release another, tap two more, hover, and move the pointer by a delta before releasing exactly one frame. In `step` mode the game never observes a partially applied intermediate input state. Outside step mode the actions are still serialized as one MCP call, but the page continues rendering normally.

The render controller gates JavaScript `requestAnimationFrame`, which covers typical canvas/WebGL and Unity WebGL loops. It does not change video decoding, CSS compositor animations, the monitor refresh rate, or guarantee an exact GPU hardware frame rate on every engine.

Live MCP stdio smoke results on 2026-08-11:

| Public example | Detected surface | Verified behavior |
| --- | --- | --- |
| [Geometry Dash: UFO 2D](https://yandex.ru/games/app/geometry-dash-ufo-2d-371298) | Cross-origin `#game-frame`, WebGL2 canvas | Launch, probe, mixed `W=hold / S=release / SPACE+E=tap`, absolute hover plus pointer delta, one automatic and three explicit step frames, emergency release. |
| [RedoSchool demo](https://redoschool.ru/demo/?auto=true) | Top-level WebGL2 canvas | Launch, probe, console collection, normal 75.8 FPS sample, continuous 10 FPS target measured at 9.7 FPS. |

Live values depend on the machine and page version; the deterministic local suite verifies the control semantics independently of public-site availability.

## MCP tools

| Group | Tools |
| --- | --- |
| Search | `get_search_engines_status`, `search_web`, `search_duckduckgo`, `search_bing` |
| Fast HTTP fetch | `fetch_url_text`, `fetch_page_links`, `fetch_urls_text` |
| Open and inspect | `browser_open_page`, `browser_open_pages`, `browser_get_page_elements`, `browser_get_status` |
| Wait and interact | `browser_wait_for`, `browser_wait_for_challenge`, `browser_fill_fields`, `browser_upload_file`, `browser_click`, `browser_submit_form` |
| Game testing | `browser_game_probe`, `browser_pointer`, `browser_press_keys`, `browser_input_batch`, `browser_release_inputs`, `browser_render_control`, `browser_render_step` |
| Observe and close | `browser_screenshot`, `browser_close`, `browser_close_all` |

Browser state is keyed by `session_id`. `browser_open_pages` can create up to four independent sessions concurrently. Non-full-page screenshots match the requested viewport dimensions exactly.

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

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python -m pytest --cov=. --cov-report=term-missing
```

The deterministic suite uses a local test site and verifies search routing, fallback/cooldown/cache, HTTP fetches, a real MCP stdio handshake, multipart file upload, form inspection/fill/click/submission, native validation, exact PNG viewport size, canvas probing, sampled animation, normal/throttled/step rendering, atomic mixed per-key actions, absolute/delta pointer hover, persistent input, concurrent sessions, manual challenge resolution/timeout, persistent storage, and detach/reattach behavior.

Public search engines may rate-limit an IP or region, so live internet smoke checks are kept separate from deterministic tests.

## Safety notes

- Visible or attached sessions may contain authenticated accounts. The MCP client can act with the permissions of those accounts.
- File upload tools can upload local paths supplied to the tool. Review agent actions and scope filesystem access appropriately.
- Browser automation may be restricted by a site's terms of service. Use it only where you are authorized.
- Manual challenge mode hands control to you; it does not attempt to bypass CAPTCHA protections.

## Contributing

Issues and focused pull requests are welcome. A new search engine only needs a `SearchProvider` implementation plus `register_search_provider(provider)`; status, cooldown, cache, and fallback routing update automatically.

See [TODO.md](TODO.md) for the current roadmap.
