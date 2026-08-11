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
- a real rendered Chrome browser that can inspect pages, fill forms, upload files, click buttons, submit forms, and return screenshots.

DuckDuckGo is the default route. Brave, Mojeek, Yahoo, Bing, and Startpage are available as fallbacks. No paid search API or provider API key is required.

## Why Web Search Neo

| Strength | What it means |
| --- | --- |
| Free search | Uses public search routes through the maintained [DDGS](https://github.com/deedy5/ddgs) library; no paid search plan or API key. |
| Resilient fallback | Provider health, cooldowns, bounded retries, caching, and an overall deadline prevent one challenged engine from stalling the agent. |
| Visible automation | Run Chrome with `headless=false` and watch every navigation, field fill, upload, click, and submit. |
| Reusable authorization | Use a persistent MCP-owned profile or attach to a dedicated Chrome window where you are already signed in. |
| Agent-friendly tools | Structured page elements, reusable CSS selectors, explicit session IDs, screenshots, status, and clear validation/error results. |
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

### Three Chrome profile modes

| Mode | Authorization and lifetime | Best for |
| --- | --- | --- |
| `temporary` | Clean disposable profile; cookies disappear when the session closes. | Search, scraping, isolated tests. |
| `persistent` | MCP owns a durable profile under `%LOCALAPPDATA%\WebSearchNeo\profiles\<profile_id>`. | Repeated automation with a separate signed-in profile. |
| `attach` | MCP connects to a Chrome process that you started with a DevTools port and does not close it on detach. | Watching the agent work in an already authorized managed Chrome window. |

Start a durable visible Chrome for attach mode:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId authorized -Port 9222
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

## MCP tools

| Group | Tools |
| --- | --- |
| Search | `get_search_engines_status`, `search_web`, `search_duckduckgo`, `search_bing` |
| Fast HTTP fetch | `fetch_url_text`, `fetch_page_links`, `fetch_urls_text` |
| Open and inspect | `browser_open_page`, `browser_open_pages`, `browser_get_page_elements`, `browser_get_status` |
| Wait and interact | `browser_wait_for`, `browser_wait_for_challenge`, `browser_fill_fields`, `browser_upload_file`, `browser_click`, `browser_submit_form` |
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

The deterministic suite uses a local test site and verifies search routing, fallback/cooldown/cache, HTTP fetches, a real MCP stdio handshake, multipart file upload, form inspection/fill/click/submission, native validation, exact PNG viewport size, concurrent sessions, manual challenge resolution/timeout, persistent storage, and detach/reattach behavior.

Public search engines may rate-limit an IP or region, so live internet smoke checks are kept separate from deterministic tests.

## Safety notes

- Visible or attached sessions may contain authenticated accounts. The MCP client can act with the permissions of those accounts.
- File upload tools can upload local paths supplied to the tool. Review agent actions and scope filesystem access appropriately.
- Browser automation may be restricted by a site's terms of service. Use it only where you are authorized.
- Manual challenge mode hands control to you; it does not attempt to bypass CAPTCHA protections.

## Contributing

Issues and focused pull requests are welcome. A new search engine only needs a `SearchProvider` implementation plus `register_search_provider(provider)`; status, cooldown, cache, and fallback routing update automatically.

See [TODO.md](TODO.md) for the current roadmap.
