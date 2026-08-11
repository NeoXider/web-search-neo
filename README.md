# Web Search Neo MCP

Fast, API-key-free web search, page fetching, and stateful browser interaction for MCP clients such as LM Studio.

DuckDuckGo is the default search engine. If it is unavailable, the server can continue through Brave, Mojeek, Yahoo, Bing, and Startpage. Search uses the maintained [DDGS metasearch library](https://github.com/deedy5/ddgs), provider cooldowns, a short result cache, and bounded retries. No paid search API or API key is required.

## Requirements

- Python 3.10-3.13 on `PATH`
- Google Chrome for rendered browser tools
- Windows, Linux, or macOS supported by Selenium Manager

```powershell
python -m pip install -r requirements.txt
python main.py
```

The MCP transport is stdio. Logs rotate in `msp_server.log` and never use stdout.

## LM Studio / MCP configuration

Use `python`, not an absolute interpreter path. `cwd` may be adapted to the clone location:

```json
{
  "mcpServers": {
    "web-search-neo": {
      "command": "python",
      "args": ["main.py"],
      "cwd": "C:/Git/PythonUrlFeatch"
    }
  }
}
```

## Tool calls

### Search and fetch

- `get_search_engines_status(check_live=true, force_refresh=false)` — configured engines, live availability, latency, challenge/cooldown state. Live results are cached for five minutes.
- `search_web(query, num=5, engine="duckduckgo", fallback=true, fresh=false)` — normalized results and structured per-provider errors. Its timeout is one overall fallback deadline, with each provider attempt capped at four seconds. Repeated searches are cached for two minutes.
- `search_duckduckgo(...)`, `search_bing(...)` — direct compatibility tools.
- `fetch_url_text(...)`, `fetch_page_links(...)` — bounded HTTP fetch and absolute links.
- `fetch_urls_text(urls)` — up to 16 URLs concurrently.

Adding an engine requires one implementation of `SearchProvider` (or a `FunctionSearchProvider`) plus `register_search_provider(provider)`. Status, fallback, cooldown, and cache routing update automatically.

### Rendered browser

Browser state is keyed by `session_id`; up to four independent sessions can run in parallel.

1. `browser_open_page(url, session_id, width, height, headless=true, profile_mode="temporary")`
2. `browser_get_page_elements(session_id)` returns rendered links, global fields, forms, buttons, iframe metadata, and reusable CSS selectors.
3. `browser_wait_for(selector, session_id, state="visible")` waits up to a bounded timeout for dynamic forms or controls.
4. `browser_wait_for_challenge(session_id, timeout_seconds=180)` waits for manual CAPTCHA completion while leaving the visible session open.
5. `browser_fill_fields(fields, files, session_id)` fills text, textarea, select, checkbox/radio, and optional file inputs.
6. `browser_upload_file(selector, file_paths, session_id)` is the dedicated single/multiple file upload call.
7. `browser_click(selector, session_id)` clicks a control.
8. `browser_submit_form(form_selector, submit_selector, session_id)` validates and reports whether a submit event or navigation was actually observed.
9. `browser_screenshot(session_id, width, height, full_page=false)` returns an MCP PNG image. Non-full-page PNG dimensions exactly match the requested viewport.
10. `browser_close(session_id)` or `browser_close_all()` releases Chrome processes owned by MCP. Attached Chrome stays open.

`browser_open_pages(urls, session_ids)` opens up to four pages concurrently. All blocking HTTP and Selenium work runs outside the MCP event loop, so independent tool calls can also execute in parallel.

## CAPTCHA and anti-bot behavior

`search_web(..., challenge_mode="fallback")` is the default: a challenged provider is put on cooldown and fallback continues immediately. `challenge_mode="manual"` opens a visible Chrome window and waits up to `manual_timeout_seconds=180`. If the challenge clears, the response returns an open `session_id` and suggested browser tools. On timeout, that window is closed and provider fallback continues.

Automatic CAPTCHA bypass is intentionally not implemented. See [TODO.md](TODO.md).

## Browser profiles and existing authorization

`browser_open_page` supports three profile modes:

- `temporary` (default) creates an isolated disposable Chrome profile.
- `persistent` stores cookies under `%LOCALAPPDATA%\WebSearchNeo\profiles\<profile_id>`. Open it visibly once and log in manually; later MCP sessions can reuse the authorization.
- `attach` connects to an already running local Chrome through `debugger_address`, for example `127.0.0.1:9222`. MCP does not own or close that Chrome process.

[Chrome 136 and later](https://developer.chrome.com/blog/remote-debugging-port) do not permit remote debugging of the normal default Chrome data directory. Use the included launcher to create a dedicated, durable authorized Chrome profile:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId authorized -Port 9222
```

After logging in to the required sites in that window:

```text
browser_open_page(
  url="https://hh.ru/",
  session_id="hh-authorized",
  headless=false,
  profile_mode="attach",
  debugger_address="127.0.0.1:9222"
)
```

Alternatively, let MCP own the persistent browser directly with `profile_mode="persistent", profile_id="hh", headless=false`. A persistent profile or debugger address can be used by only one active MCP session at a time.

Optional environment configuration:

```powershell
$env:WEB_SEARCH_NEO_REGION = "us-en"
$env:WEB_SEARCH_NEO_PROXY = "socks5://127.0.0.1:9050" # only a proxy you are authorized to use
$env:WEB_SEARCH_NEO_BROWSER_USER_AGENT = "..." # optional; native Chrome UA is safer by default
$env:WEB_SEARCH_NEO_PROFILE_ROOT = "D:\BrowserProfiles" # optional persistent-profile root
python main.py
```

HTTP sessions use a consistent desktop User-Agent per session, connection pooling, size limits, and conservative retry/backoff. Rendered pages use the installed Chrome's native matching User-Agent unless explicitly overridden. Search uses DDGS browser impersonation and multiple independent providers rather than repeatedly hammering one endpoint.

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python -m pytest --cov=. --cov-report=term-missing
```

The deterministic suite starts a local HTTP form server and verifies text/links, multipart upload, field fill, checkbox/select/radio behavior, click, native validation, submission, exact-size PNG rendering, two parallel Chrome sessions, persistent browser storage, manual challenge resolution/timeout, provider fallback/cooldown/cache, and a real MCP stdio handshake. A live smoke also verifies attaching to a managed Chrome and detaching without closing it.

Live checks should use harmless public pages and are run separately from deterministic tests because search engines can legitimately rate-limit a machine or region.
