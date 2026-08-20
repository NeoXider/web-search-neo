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

Web Search Neo is an MCP server that gives a model two tools — `web_info` to
look, `web_action` to act — and behind them four capabilities:

| Capability | What it is |
| --- | --- |
| **Search** | Text search with automatic fallback across independent engines. No paid API, no provider key. |
| **Your own Chrome** | The already-open, signed-in browser you are looking at, driven through a local companion extension: tabs, forms, uploads, games, screenshots. |
| **Perception** | An accessibility outline, readable text, semantic element lookup, the page console, and its HTTP traffic. |
| **Isolation** | Separate Selenium profiles — temporary, persistent, or attached — when a clean or headless browser is preferable. |

DuckDuckGo is the default search route. Brave, Mojeek, Yahoo, Bing, and
Startpage are available as fallbacks.

## Contents

- [Quick start](#quick-start) · [Connect to LM Studio](#connect-to-lm-studio)
- [**Examples**](#examples) — copy-paste calls for the six things people do most
- [Deep dives: playing games](docs/playing-games.md) · [complex forms](docs/complex-forms.md)
- [Why Web Search Neo](#why-web-search-neo) — the capability table
- [Connect your already-open Chrome](#connect-your-already-open-chrome) · [The bridge daemon](#the-bridge-daemon) · [Bridge authentication](#bridge-authentication)
- [Search behavior](#search-behavior) · [CAPTCHA and challenge modes](#captcha-and-challenge-modes)
- [Current Chrome automation](#current-chrome-automation) · [Chrome profile modes](#chrome-profile-modes)
- [Reading a page](#reading-a-page) · [Locators](#locators)
- [Console and network diagnostics](#console-and-network-diagnostics)
- [Forms and multi-step flows](#forms-and-multi-step-flows) · [Reviewable macros](#reviewable-macros)
- [Architecture invariants](ARCHITECTURE.md) · [Project-local and guarded macros](#project-local-and-guarded-macros)
- [Canvas and WebGL games](#canvas-and-webgl-games) · [Render modes](#render-modes) · [Input latency](#input-latency)
- [Two-tool MCP contract](#two-tool-mcp-contract) · [Optional agent skill](#optional-agent-skill)
- [Optional configuration](#optional-configuration) · [Transport policy](#transport-policy)
- [Tests](#tests) · [Safety notes](#safety-notes) · [Contributing](#contributing)

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

The server uses MCP over stdio. Keep stdout reserved for MCP messages; rotating diagnostic logs are written to `msp_server.log`. The companion bridge runs as [its own process](#the-bridge-daemon) and keeps a separate rotating log at `%LOCALAPPDATA%\WebSearchNeo\bridge-daemon.log`, or `$XDG_DATA_HOME/WebSearchNeo/bridge-daemon.log` — by default `~/.local/share/WebSearchNeo/bridge-daemon.log` — because two processes rotating one file collide on Windows.

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

## Examples

Every example below is the argument object of one of the two tools:
`web_action` takes `{"actions": [...]}` and performs 1–32 ordered actions;
`web_info` takes `{"topic": ..., "params": {...}}` and reads state without
changing anything. One `session_id` is one page — reuse it across calls.

### 1. Search and read a result

```json
{
  "actions": [
    {"action": "search", "query": "model context protocol servers", "num": 5},
    {"action": "fetch_text", "url": "https://modelcontextprotocol.io/", "max_chars": 4000}
  ]
}
```

Search first, then read the page you picked. `fetch_text` needs no browser at
all; `fetch_many` reads up to 16 URLs concurrently.

### 2. Open a page and read it the way the agent does

```json
{"actions":[{"action":"open","url":"https://example.com","session_id":"demo"}]}
```

Opens a tab in your own Chrome, in the visible `🟢 AI` tab group.

```json
{"topic":"page_outline","params":{"session_id":"demo","limit":60}}
```

Returns an indented tree of roles, accessible names, states, `ref:<epoch>:N`
handles, and on-screen boxes — the page as a screen reader sees it, not as HTML.

```json
{"topic":"find","params":{"session_id":"demo","query":"more information link","role":"link"}}
```

Ranked matches for a plain-language query when a CSS selector would be a guess.

```json
{"actions":[{"action":"click","selector":"a[href*='iana.org']","session_id":"demo"}]}
```

Clicks it. `click` and `wait` also accept a `ref:` handle or a piercing path in
the Selenium-backed profile modes.

### 3. Fill in a form and submit it

```json
{
  "actions": [
    {"action": "fill", "session_id": "apply",
     "fields": {"#candidate-name": "Neo Candidate", "#role": "unity", "#remote": true},
     "files": {"#resume": "C:/docs/resume.pdf"}},
    {"action": "submit", "session_id": "apply",
     "form_selector": "#application", "submit_selector": "#submit-button"}
  ]
}
```

Text fields, `<select>` options, checkboxes, and file inputs in one call, then a
submit that runs native browser validation. `fill` reports `filled` and a
per-selector `errors` map, so a partial failure is visible rather than silent.
→ [Complex forms: the long version](docs/complex-forms.md)

### 4. Find out why a page broke

```json
{"topic":"console","params":{"session_id":"demo","levels":["error"],"limit":20}}
```

```json
{"topic":"network","params":{"session_id":"demo","only_errors":true,"limit":20}}
```

The page's own console — including uncaught exceptions with stack frames — and
every failed or 4xx/5xx request as `method status type ms size url`, back to and
including the navigation that opened the session: capture is armed before the
first request, so nothing has to be reloaded to be seen. Use `output="json"` on
`network` to get the request `id`, then read one body with the `network_body`
topic.

### 5. Play a game frame by frame

With the game already open in session `game`:

```json
{
  "actions": [
    {"action": "render", "mode": "step", "session_id": "game"},
    {"action": "input", "session_id": "game", "target_selector": "#game",
     "key_actions": [{"key": "ARROW_RIGHT", "action": "hold"}]},
    {"action": "step", "frames": 3, "session_id": "game"},
    {"action": "input", "session_id": "game",
     "key_actions": [{"key": "SPACE", "action": "tap"}]},
    {"action": "release_inputs", "session_id": "game"},
    {"action": "render", "mode": "normal", "session_id": "game"}
  ]
}
```

In `step` mode the page is frozen: page time only advances when a frame is
released, so the game measures a fixed delta instead of the model's thinking
time, and a tapped key stays down for the whole frame an engine polls.
→ [Playing games: the long version](docs/playing-games.md)

### 6. Work in a tab you already have open

```json
{"topic":"browser_tabs"}
```

```json
{"actions":[{"action":"attach_tab","tab_id":123,"session_id":"existing"}]}
```

Lists the tabs in your Chrome with their ids and group names, then claims one
without navigating or moving it. `close` later releases the session and leaves
that tab exactly where it was.

### Where to go next

```json
{"topic":"capabilities"}
```

`web_info()` with no arguments returns the whole agent-facing contract: every
action with its required parameters, the observation topics, recipes, pitfalls,
limits, and worked examples. `web_info(topic="action_schema", params={"action":
"input"})` returns one full JSON Schema on demand.

## Why Web Search Neo

| Strength | What it means |
| --- | --- |
| Free search | Uses public search routes through the maintained [DDGS](https://github.com/deedy5/ddgs) library; no paid search plan or API key. |
| Resilient fallback | Provider health, cooldowns, bounded retries, caching, and an overall deadline prevent one challenged engine from stalling the agent. |
| Your current Chrome by default | New tabs open in the `🟢 AI` tab group of the Chrome you already use, so existing logins remain available while automation stays in the background unless `show` is explicitly requested. |
| Reusable authorization | List and claim existing tabs, use the current signed-in Chrome, a persistent MCP-owned profile, or a DevTools attach window. |
| Authenticated companion | Server and extension prove knowledge of a machine-local secret to each other before a single command crosses the loopback bridge. |
| One bridge, many clients | The companion port belongs to a standalone bridge process, not to whichever agent happens to be running, so Claude Code and LM Studio can drive the same Chrome at the same time and the badge stays `ON` between calls. |
| Two-tool MCP surface | Models see only `web_info` and `web_action`; detailed action schemas are discovered only when needed. |
| Self-describing contract | `web_info()` with no arguments returns the actions, recipes, pitfalls, limits, and examples, so an external skill file is optional. |
| Semantic page reading | `page_outline`, `page_text`, `element_text`, and `find` return roles, accessible names, states, boxes, and `ref:<epoch>:N` handles across open Shadow DOM and same-origin iframes — and `element_text` hands over the whole content of one element, overflow-clipped tails included. |
| Visible failures | The `console` and `network` topics surface console output, uncaught exceptions with stack frames, and every HTTP request with status, type, duration, and size — recorded from the session's first navigation, not from the first time someone asks. |
| Deterministic frames | Gated render modes freeze `performance.now()`/`Date.now()` and queue page timers, so a released frame is a fixed delta instead of the agent's thinking time. |
| Concurrent work | Search, HTTP fetches, and independent browser sessions run outside the MCP event loop; up to four browser sessions can work in parallel. |

## Connect your already-open Chrome

The default browser mode is `current`. It fails with a clear setup error when the
companion isn't connected; it does not silently open a different browser.

An agent prepares the bundled companion through the compact MCP contract:

```json
{"actions":[{"action":"setup_current_chrome"}]}
```

That call opens no page and touches no browsing data. It writes the shared secret
into `chrome-extension/bridge-token.js`, checks the bundled build against
whatever is connected, and returns `manual_steps` — the clicks that are left,
with the absolute path of the folder to pick — plus `token_ready` and
`token_file`. The one thing it can change is the companion itself: a connected
build older than the bundled one is asked to reload itself, reported as
`self_update` (`done`, `unsupported`, or `timeout`) with `replaced_version`.

Chrome does not let a program add an unpacked extension to a browser that is
already open: the installed set is signed inside `Secure Preferences`, policy
installs need a packed CRX behind an update URL, and a Chrome started without a
DevTools port cannot be given one later. So three steps stay with the user:

1. Open `chrome://extensions`.
2. Switch on **Developer mode**, then choose **Load unpacked**.
3. Select this repository's `chrome-extension` folder.

Keep **Web Search Neo Companion** enabled. Click its toolbar icon to open the
companion panel: it shows the bridge state and controlled-tab count, and offers
**Reconnect**, **Release tabs**, a GitHub release/version check, a repository link,
and a persistent on/off switch. Turning the switch
off closes the bridge and detaches every controlled tab. Its toolbar badge reads
`ON` while the companion is connected and authenticated to the [bridge daemon](#the-bridge-daemon),
which is between agent calls too, not only while one is running. Nobody has to
create or copy a token: it is written whenever the bridge comes up and on every
`setup_current_chrome` call.

Earlier revisions tried to perform those clicks through Windows UI Automation.
That code is gone: it depended on the interface language, on which window had
focus, and on a folder picker the automation backend does not enumerate. There
is no automatic substitute. If you would rather not install an extension at all,
`profile_mode="temporary"` and `profile_mode="persistent"` drive a Selenium
browser that needs no companion.

The bundled companion is version 1.3.8. Chrome does not refresh an unpacked
extension by itself, but from 1.3.1 the server does it instead: the worker
understands a `runtime.reload` command, and `setup_current_chrome` sends it
whenever the connected build is older than the bundled one. Upgrading *onto*
1.3.1 is the last one that costs a click, because the build being replaced is the
build that has to understand the command; the very first install still needs the
three steps above, which no program can perform.

So press **Reload** on the companion card at `chrome://extensions` once, after the
pull that crosses 1.3.1. Skipping it on a 1.2.0-or-older install is the loudest
case, because that build cannot even authenticate:

- a service worker from 1.2.0 or older sends no token, so the bridge closes it with code 1008 and the reason `Companion token mismatch; reload the extension on chrome://extensions`;
- the badge then stays `OFF` and the worker retries on a slow ladder, from about ten seconds up to two minutes;
- the bridge daemon records the rejection in `%LOCALAPPDATA%\WebSearchNeo\bridge-daemon.log`, not in `msp_server.log`;
- a 1.3.0 worker also prints the close code and reason in its own service-worker console; an older one does not, so on a stale install the badge and that daemon log are the signal.

The bridge accepts only the fixed bundled extension ID, binds only to loopback, and never reads Chrome profile files, cookies, or saved passwords directly. The extension uses Chrome's standard [tabs](https://developer.chrome.com/docs/extensions/reference/api/tabs), [tab groups](https://developer.chrome.com/docs/extensions/reference/api/tabGroups), and [debugger](https://developer.chrome.com/docs/extensions/reference/api/debugger) APIs, plus `storage` for its own session state and [alarms](https://developer.chrome.com/docs/extensions/reference/api/alarms) for the long waits in its reconnect backoff, with the permissions shown by Chrome.

A badge reading `OFF` means this companion is not connected and authenticated, which is usually — but not only — because nothing is listening on the bridge port: no daemon has been started since the machine booted, the last one exited after its idle window, or it was stopped with `--bridge --stop`. That is the normal state of a machine on which no MCP server has run yet, not a fault — but it is no longer the state between two agent calls, which is what it was when the port lived inside the MCP server. The badge also reads `OFF` in two cases where the port is very much held: when the daemon refused this companion's token, and when a companion in a second Chrome profile authenticated later and displaced it. So `OFF` is not by itself a reason to start a daemon — read `web_info(topic="browser_status")` and `bridge-daemon.log` before concluding the port is free. While nothing listens, the worker retries with an exponential backoff — about 1.5 seconds after the first failure, doubling to a ceiling of one minute, and dropping back to the floor the moment a handshake verifies. Chrome suspends an idle service worker after roughly thirty seconds, which is why the longer waits are `chrome.alarms` rather than timers: a timer that long would die with the worker. Chrome also logs every refused attempt as an extension error of ours and an extension cannot suppress that, so a red **Errors** button on the card of an idle machine is expected; before 1.3.1 the worker retried every two seconds and could fill that page with hundreds of identical `ERR_CONNECTION_REFUSED` lines. Starting the browser or reloading the extension resets the schedule, and the popup's **Reconnect** button retries immediately.

Keep the companion enabled in one Chrome profile at a time. The bridge holds exactly one companion connection and the most recent authenticated one wins, so a second profile with the companion enabled quietly takes the agent's tabs with it.

### The bridge daemon

The listener the companion dials is not inside the MCP server any more. It is a
standalone process — `bridge_daemon.py` — that owns `127.0.0.1:8765`, holds the
single connection to the extension, and relays commands for any number of local
MCP clients. `ChromeBridge` is now a client of it. Its public surface did not
change and neither did the frames the extension exchanges, so nothing an agent
calls looks any different.

Three things follow, and they are why the listener moved out:

- **The badge stays `ON` between calls.** The port used to exist only while an
  agent was running, so the companion spent most of the day disconnected and
  reconnected on its own backoff schedule whenever a server appeared.
- **Two MCP clients can share one Chrome.** Claude Code and LM Studio can now run
  at the same time; the second server to start used to fail to bind the port with
  `OSError: [Errno 10048]` and silently lose `profile_mode: "current"`.
- **The first call after a quiet stretch is not spent waiting.** A freshly started
  server no longer has to sit out whatever reconnect delay the extension had
  climbed to.

An MCP server starts the daemon as it comes up, if nothing is listening yet, so
the companion is connected before the first action rather than during it. It is
started detached — with no console window on Windows, in its own session
elsewhere, and never inheriting the stdio that carries MCP — and it outlives the
server that started it. Nothing is registered for autostart: no service, no
scheduled task, no login item. It exists once an MCP server has run, or once you
have run one yourself, and it goes away again on its own.

On Windows the launcher deliberately bypasses a uv virtual-environment
`python.exe`/`pythonw.exe` redirector and starts the base `pythonw.exe` with
`CREATE_NO_WINDOW`, while preserving the venv through `__PYVENV_LAUNCHER__`.
This matters because a redirector can start the real console interpreter after
the original creation flags are gone; combining `DETACHED_PROCESS` with
`CREATE_NO_WINDOW` does not fix it, because Windows ignores the latter flag in
that combination.

Two entry points are yours:

```powershell
python main.py --bridge         # run the daemon in the foreground
python main.py --bridge --stop  # ask a running daemon to exit
```

`--bridge` exits quietly if another daemon already owns the port, because that one
serves just as well. It prints nothing while it runs either: the daemon logs to
`%LOCALAPPDATA%\WebSearchNeo\bridge-daemon.log`, foreground or not, and never to
`msp_server.log`, because two processes rotating one file collide on Windows.
`--bridge --stop` is the one that talks back — it says whether it stopped a daemon
or found none — and it never starts one.

Left with neither a companion nor a client attached, the daemon exits after
fifteen minutes. A connected companion on its own keeps it alive indefinitely —
that is the case where the badge stays on all day. Set
`WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS` to change the window, or to `0` to disable it;
a daemon inherits the environment of the server that spawned it, so setting it in
the MCP client configuration is enough.

The daemon reports its version during the client handshake. A client whose
`__version__` differs asks it to exit and starts one of its own, so a daemon
started before a `git pull` cannot keep serving the old code. It will do that
twice; if a third daemon still reports the wrong version, it stops instead of
replacing daemons forever and reports an error naming both versions. That error
is latched — later calls repeat it rather than restarting the tug-of-war — and it
means another checkout of Web Search Neo is running against the same port. Stop
that one, then restart this server.

`web_info(topic="browser_status")` reports the link under `current_chrome.daemon`:
`linked` says whether this server currently holds one, and `version` and `pid`
come from the daemon's own handshake, so they identify the process that would be
replaced. `current_chrome.connected` still means what it always did — the
companion itself is attached — and a `daemon.linked: true` with
`connected: false` is exactly the case of a running bridge with Chrome closed.

### Bridge authentication

Loopback is not a trust boundary: every process running as the same user can open
`127.0.0.1:8765`, an `Origin` header stops web pages but not local programs, and the
extension ID is derived from the public `key` in the manifest, so it is not a secret
either. Before this release, a local process that reached the port first received the
full companion protocol, including `cdp.send` on any signed-in tab. Both sides now prove
knowledge of a machine-local secret before a single command is executed:

- a 32-byte random token — 64 hex characters — is minted on first use, by the daemon as it
  starts or by a server as it connects, and kept in
  `%LOCALAPPDATA%\WebSearchNeo\bridge-token` on Windows, or in
  `$XDG_DATA_HOME/WebSearchNeo/bridge-token` — by default
  `~/.local/share/WebSearchNeo/bridge-token` — created `0600` on POSIX;
- the same secret is copied into `chrome-extension/bridge-token.js` before Chrome is
  asked to load the folder, so setup stays hands-free. That file is listed in
  `.gitignore` and is never committed or shared between machines;
- the companion sends the token and a fresh 16-byte nonce in its `hello`. The daemon
  compares the token in constant time and answers `hello_ack` with
  `HMAC-SHA256(token, nonce)`;
- the companion verifies that proof with WebCrypto and runs nothing until it matches. A
  peer that sends anything other than a valid ack first — a command above all — is closed
  immediately;
- an MCP server authenticates to the daemon with the same hello, marked `role: "client"`,
  and checks the daemon's proof before relaying anything. Being a relay is not a way
  around the token: a local process without it is closed on both roles alike.

A newly authenticated connection replaces the previous one, so a companion whose service
worker Chrome had suspended reclaims the bridge on reconnect instead of finding it held by
a stale socket.

The honest limit: the secret is a file owned by the user account, so any process running
as that same user can read it and impersonate either side. This closes the "whoever binds
the port first owns the browser" hole; it is not protection against malware already
running as you. The real fix is Chrome Native Messaging, where Chrome launches the server
itself and no port is listened on at all — it is tracked in [TODO.md](TODO.md). An
authenticated peer is also unrestricted: `cdp.send` forwards any DevTools method to a tab,
with no method allowlist.

The daemon did not move that boundary, but it did change how long the door stands open.
The listener used to exist only while an agent was running — minutes a day on a normal
machine, which is the assumption the paragraph above was written under. Now it is held
by a process that outlives every agent call and stays up for as long as Chrome keeps the
companion attached, which in practice means the whole working day. Nothing about the
authentication moved: the bind is still loopback-only, both roles still have to present
the token, and a process running as you could always read the token file — same-user
processes were never excluded by any of it. What is larger is simply the share of the day
during which such a process finds something listening. Close it deliberately with
`python main.py --bridge --stop`, or quit Chrome and let it close itself: a daemon with
neither a companion nor a client attached exits after fifteen minutes
(`WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS`, `0` disables the timer).

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

Send that object to `web_action`. Search state is readable separately:

```json
{"topic":"search_status","params":{"check_live":true}}
```

It reports configured engines, current live availability, latency, cooldown
state, and detected challenges. A live probe is a diagnostic: a provider that
fails during the probe is not pushed into the cooldown used by real searches, so
checking status can no longer degrade the next search. Status checks are cached
for five minutes; search results are cached for two minutes.

### CAPTCHA and challenge modes

- `challenge_mode="fallback"` is the default. A challenged provider is skipped immediately and the search continues through another route.
- `challenge_mode="manual"` opens visible Chrome and waits up to `manual_timeout_seconds=180`. If you complete the challenge, the agent receives the open browser session; otherwise the window closes and fallback continues.

Detection answers "is a challenge in the way", not "does this page contain a captcha". It looks for a rendered provider widget — reCAPTCHA, hCaptcha, Cloudflare Turnstile, Yandex SmartCaptcha, PerimeterX, DataDome or AWS WAF — at least 20x20 pixels, walking open shadow roots and same-origin frames rather than only the top document, which is where the last three and anything one frame down were being missed. A bare `data-sitekey` counts only when the element or a nested frame says captcha, so a chat widget carrying one is not a challenge. Wording counts only in a page heading or in a short interstitial. Every page summary carries `challenge_detected`, `challenge_type`, `challenge_evidence` and `captcha_widgets` — the last lists every captcha seen, blocking or not, so an article with an hCaptcha in its comment form is reported without being called challenged.

Automatic CAPTCHA bypass is intentionally not implemented. The roadmap tracks a future provider-supported, legal integration in [TODO.md](TODO.md).

## Current Chrome automation

Yes — the agent can work in your normal already-open Chrome while you watch it. New pages go into a visible tab group named `🟢 AI` by default — the mascot is there so agent tabs are obvious in a crowded window, and the companion colours the group purple when it is the one creating it:

```json
{
  "actions": [{
    "action": "open",
    "url": "https://example.com",
    "session_id": "demo"
  }]
}
```

From there, inspect with `page_outline`, `page_text`, `find`, or `page_elements`,
then send ordered `fill`, `upload`, `click`, `scroll`, and `submit` actions through
`web_action` using the same `session_id`. `scroll` defaults to the viewport centre,
uses positive `delta_y` for down and negative for up, and returns before/after page
scroll metrics. Screenshots are returned by the `screenshot` info topic: its default
is the actual current viewport, `mode="full_page"` captures the document, and
`mode="region"` captures an exact `x`/`y`/`width`/`height` CSS-pixel rectangle
without resizing Chrome.

A session tracks whether it owns its tab, and never navigates one it borrowed.
An `open` on a session that claimed a tab through `attach_tab` opens the agent's
own tab in the `🟢 AI` group and hands the borrowed one back — the debugger
detaches, and the page the user was reading is neither closed nor navigated. The
freed tab id comes back as `left_claimed_tab`. `close` removes a tab that the
agent opened itself, and leaves a claimed tab open and detached, so the group no
longer accumulates abandoned pages. Both `close` and `close_all` report what they
could not release — a tab that would not close, a debugger that would not detach
— instead of answering `closed: true` over the top of it; closing a session that
was never open stays a no-op and says so in `note`. A tab the user closed by hand
frees its session slot the next time the four-session cap is reached, so the
server does not refuse to open a page because of four tabs that no longer exist. `close_all` — and the
shutdown hook that runs it — applies the same rule instead of leaving every
self-opened tab behind. The MCP doesn't read cookies or passwords; the page
simply continues using the authorization already present in Chrome.

Teardown is not unconditional, and the exception is the point of it. Tab ids
restart with Chrome, so a session that outlived a restart holds a number that now
names somebody else's tab. Every teardown path — `close`, `close_all`, the
shutdown hook, the cap sweep — asks first whether the session's browser run is
still the one it was opened in, and if it is not, sends nothing at all and forgets
the session. `close` reports that as `browser_gone: true` with a note;
`close_all` lists the affected ids under `browser_gone` and still answers
`closed_all: true`, because nothing of ours was left to leak. Both keys are absent
on the ordinary path. The claim is not released either: the daemon dropped its
whole registry when the run changed, so a release aimed at the old id could only
hit a claim made since. Before this, closing such a session sent `tabs.remove` for
the stale id to the new browser, closed a tab of the user's, and reported success.

`browser_status` answers the same way rather than describing a stranger's tab: a
session whose Chrome is gone is dropped and reported as `session_open: false`,
`browser_gone: true`, with a `next` saying to open the page again. It used to read
a page summary off whatever tab had inherited the id and answer `session_open:
true` — the same identity bug, in the topic an agent uses to check for it.

If the companion isn't ready, read `web_info(topic="browser_status")` and run the
`setup_current_chrome` action for the exact steps. `profile_mode="auto"` falls
back to a separate headless Selenium session, so it does not raise another window.

### Chrome profile modes

| Mode | Authorization and lifetime | Best for |
| --- | --- | --- |
| `current` (default) | Companion extension controls the user's open Chrome. New tabs enter the `🟢 AI` group in the background; claimed tabs stay where they are. | Authorized sites, work alongside the user, existing tabs. |
| `auto` | Prefer `current`; fall back to a headless temporary Selenium profile if the companion is unavailable. | Portable background clients. |
| `temporary` | Clean disposable profile, headless by default; cookies disappear when the session closes. | Search, scraping, isolated tests. |
| `persistent` | MCP owns a durable profile under `%LOCALAPPDATA%\WebSearchNeo\profiles\<profile_id>`, headless by default. | Repeated automation with a separate signed-in profile. |
| `attach` | MCP connects to a Chrome process that you started with a DevTools port and does not close it on detach. | Watching the agent work in an already authorized managed Chrome window. |

#### Working in the same Chrome as the user

In `current` mode the agent takes no focus. Its tabs open in the background, in
an existing window — the group's own window when there is one, otherwise any
window that is neither focused nor minimized. It does not open a window of its
own, because on Windows a new window raises itself into the taskbar; the one
exception is a Chrome left running with no window at all, where a tab has nowhere
else to go. Navigation and keyboard input do not activate a tab or focus its OS
window either. Only `web_action` with the explicit `show` action requests the
foreground; it never minimizes, maximizes, restores, or resizes a window.

Chrome starves a tab nobody is looking at, which would make that useless: a
hidden tab gets no `requestAnimationFrame` callbacks at all, timers are clamped
to a second and then to a minute, and — measured on Chrome 151 — input dispatched
into a tab that has never been shown is *silently dropped*. So the companion
turns on focus emulation for every tab it drives, which restores all three
(49 fps, 4.5 ms timers, input delivered). The page believes it is focused and
visible while it is not, and stops believing it the moment the debugger detaches.

Two consequences worth knowing. A targeted keyboard action can change DOM focus
inside the controlled background page, but it does not take OS focus or change the
active user tab. A screenshot of a tab in a window that another window covers can take tens of
seconds — Chrome has no fresh pixels to hand over — so `screenshot` waits up to
45 s there and, if it gives up, says that the window is obscured and that reading
and typing are unaffected.

Several agents can drive one Chrome at once, so tab ownership lives in the bridge
daemon rather than in any one server process: every tab an agent opens or claims
is registered there, an `attach_tab` for a tab another agent already drives is
refused with who holds it and for how long, and a claim is dropped the moment its
client's socket does — including an abrupt one, so a crashed agent cannot strand
a tab. A client that reconnects re-asserts its claims, and gives up any it lost.
When there is no daemon to ask, nothing is guarding the browser and the claim is
skipped rather than failing closed.

Sessions are pinned to the browser run they were opened in. Tab ids restart with
Chrome, so a session that outlived a restart would address whatever tab inherited
its number — quite possibly one of the user's. Such a session is dropped, with an
error that says to open the page again, and nothing is sent to the new browser on
the way out. The companion updating itself counts as a new run, since its reload
drops every debugger attachment anyway.

For isolated work, opt into Selenium explicitly. `profile_mode="temporary"` and
`profile_mode="persistent"` are headless when `headless` is omitted; pass
`headless=false` only when a visible MCP-owned Chrome is intentional. Persistent
mode keeps a durable MCP-owned profile. For `attach`, the
launcher — not the `headless` argument — determines whether the already-running
Chrome is visible or headless.

`headless=true` cannot be combined with `current`: that mode drives a Chrome the
user is looking at, so the request is refused outright rather than quietly
answered by some other browser. `auto` with `headless=true` resolves straight to
`temporary` without even probing for the companion — worth knowing, because it is
the one way `auto` reaches a Selenium profile on a machine where `current` works
perfectly.

Start a durable visible Chrome for attach mode (visible is the launcher default):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId authorized -Port 9222 -WindowMode visible
```

Sign in to the sites you need in that window, keep it open, then let the agent attach:

```json
{
  "actions": [{
    "action": "open",
    "url": "https://example.com/",
    "session_id": "authorized",
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
| `page_outline` | An indented tree of roles, accessible names, states, `ref:<epoch>:N` handles, and boxes. `limit=200` nodes, `output="text"` by default; `output="json"` returns one object per node with `rect`, `page_rect`, `center`, `visible`, `in_viewport`, and `occluded`. |
| `page_text` | The rendered text, with headings, list items, and table cells preserved. `mode="full"` is the whole `<body>`, same-origin frames and open dialogs included; `mode="main"` narrows to the main-content sub-tree and drops navigation, header, footer, aside, and form chrome. `max_chars=20000`, `include_links=false`. |
| `element_text` | The whole content of one element — not a clipped slice of the page. `selector` takes CSS, a fresh `ref:<epoch>:N`, or an `a >>> b` piercing path. `full_text=true` reads `textContent`, which no rendering filter may drop, so the tail of a scrolled-out code block or a collapsed panel is returned whole. `mode="text"|"html"|"outer"|"both"` returns the rendered text, `innerHTML`, `outerHTML`, or all three; `max_chars` clips at a paragraph boundary and says so with `truncated`. |
| `find` | Ranked matches for a plain-language `query` such as `"submit application"`, each with a `ref`, role, name, box, a `match_score` (query against the element alone) and the ranking `score` that adds context. `limit=5`, capped at 25; `visible_only=true` by default, so a control the page has not revealed yet is not a candidate; `role` filters rather than nudges. `low_confidence` means nothing on the page answers the query — the closest few still come back, as the guesses they are — and `ambiguous` means the top two matched *and* ranked equally, so document order picked the winner. `candidates`/`scored`/`matched`/`returned`, `truncated` and `aria_hidden_skipped` account for what was examined, cut, and skipped as hidden from assistive technology. |
| `page_elements` | The flat lists of links, forms, fields with `<select>` options, and buttons, addressed by CSS selector — or by a piercing path when they live in an open shadow root or a same-origin frame, or by an empty string when nothing addresses them uniquely, which is the honest answer and not a bug to work around. It covers the whole existing DOM, not only the viewport, so a rendered button below the fold is returned before any scroll. Each entry carries `visible` and, when it is not, a `hidden_reason`; visible entries come first. `limit` is capped at 1,000 per category; continue with `offset` and `range.<category>.next_offset` until it is `null`. `found`, `returned`, `truncated`, and `collector_truncated` account for the result and the 20,000-element safety cap. Lazy, infinite, and virtualized controls do not exist until the page creates them: `scroll`, wait, then reread from `offset=0`. Alone among the four it takes no `frame_selector`: it always walks the whole page, open shadow roots, and same-origin frames. |

```json
{"topic":"page_outline","params":{"session_id":"demo","output":"json","limit":80}}
```

The outline and `find` walk open shadow roots and same-origin iframes, including nested ones, and map every box into top-document coordinates so a reported `center` can be clicked or pointed at directly. A frame is mapped through the full transform of itself and its ancestors — `transform`, the individual `rotate`/`scale`/`translate` properties, and CSS `zoom` — because a frame scaled to fit its container is ordinary on a checkout page, and translating by its origin alone put the reported centre tens of pixels from the control. `center` is the point to aim at; `page_rect` is the smallest rectangle containing the mapped corners, which for a rotated frame is larger than the element itself and is not something to click. A chain that is not affine — a 3D `perspective` — can only be approximated flat, and those nodes say so with `page_rect_approximate` rather than presenting the guess as an ordinary number. A cross-origin frame appears as one node with `same_origin: false`; read it by passing its selector as `frame_selector`. Closed shadow roots are counted in `closed_shadow_roots` rather than entered. A node inside a frame reports the `frame` path that reaches it, verified to match exactly one element and written as a `#host >>> #inner` piercing path when the frame is itself nested or inside a shadow root; a frame with no such path is marked `frame_addressable: false` instead of being given a selector that would land in the wrong one.

`page_text` never answers with a blank page, and never hands a fragment over as if it were the page:

- `mode="main"` on a document that is one large form — a sign-up, login, or checkout page — used to drop everything as chrome. An empty result is now retried without the noise filter and reported as `fallback_used: true` with `mode_used: "full"`.
- A landmark is only trusted when it holds at least half of what the body renders, so an app shell whose real content is mounted outside its `<main>` no longer reads as `Loading...`.
- `excluded_chars` counts how many of the `body_chars` the body renders are missing from the answer, and `excluded` says why: chrome dropped by `mode="main"`, cross-origin frames, frames nested deeper than the shared frame-depth limit, a frame whose document has not parsed yet, or a clip at `max_chars`.
- Same-origin frames are read in place, `<dialog open>` and `role="dialog"` overlays that sit outside the chosen root are appended and counted in `dialogs_appended`, and `frames` reports what was entered, skipped as cross-origin, or too deeply nested.
- With `include_links=true` the link index is paid for out of the same budget, so the response stays inside the requested `max_chars` instead of overshooting it by the size of the listing.

### What `find` returns

Each match carries two numbers, because one number cannot say both things:

| Field | Meaning |
| --- | --- |
| `match_score` | 0–100. How well the query matched *that element alone*, before any ranking bonus: 100 the whole field, 62 a prefix, 45 a substring, 34 every query token present — times the field's weight. `matched_field` names the field it came from. |
| `score` | The ranking score: `match_score` plus context — in the viewport +18, an action-shaped query on an interactive element +12, the requested role +8, enabled +6, a child of `<main>` or `<form>` +4, disabled −15, a generic non-interactive node on a page that has interactive ones −12, a role other than the one asked for −20, occluded −25. It orders the results and says nothing about whether the page holds an answer. |

Field weights, and the values `matched_field` can take: `name` 1.0, `text` — the
element's own visible words, scored separately when an accessible name overrode
them — 0.9, `placeholder` 0.8, `title` 0.65, `testid` 0.6, `name_id` (the `name`
attribute and the `id`) 0.5, `role` and `value` 0.4, `href` 0.3.

Two flags sit on top of them, and they mean different things:

- `low_confidence` — nothing cleared `match_threshold` (25) on `match_score`, so
  the matches are the closest things on the page, offered as guesses. It is
  derived from `match_score` and never from `score`, which is the fix for a real
  failure: context alone is worth 36 points, so on any action-shaped query every
  visible enabled control cleared a bar of 25 and unrelated elements came back
  with `low_confidence: false`. A `role` filter is a filter too — if nothing of
  that role clears the bar, the result is `low_confidence` rather than a
  confidently wrong element of another role.
- `ambiguous` — the top two matched the query equally well *and* ranked equally
  (within five points on both), so document order alone decided which came first.
  Both are good matches; the choice between them is not, which is why it is a
  separate flag. It is computed before `limit` is applied, so asking for one
  answer still tells you the second was just as good.

The counts account for everything the page offered: `candidates` were examined,
`scored` resembled the query at all — weak role-word brushes included — `matched`
cleared `match_threshold`, `returned` fit inside `limit` (capped at 25), and
`truncated` says `matched` did not. `aria_hidden_skipped` counts elements skipped
because an ancestor is `aria-hidden` or `[hidden]`, by the same rule `page_outline`
uses, and `frames_too_deep` counts frames no topic enters. Under `low_confidence`
nothing cleared the bar, so `matched` is `0` while `returned` counts the guesses —
the one case where `returned` exceeds `matched`.

`low_confidence: true` is an instruction, not a footnote: re-query with different
words or a `role` filter. Clicking `matches[0]` because it is the only thing on
offer is how an agent ends up pressing *Cancel* when it meant *Submit order*.

### Locators

| Form | Example | Notes |
| --- | --- | --- |
| CSS selector | `input[name='q']` | Unchanged, and still the only form accepted everywhere. |
| Ref handle | `ref:3f9a1c04b7e25d18:12` | Issued by `page_outline` and `find`. The first field is the document epoch — 16 hex characters, also reported separately as `dom_epoch`; the second is the element number. |
| Piercing path | `#host >>> .inner` | The separator is a space-padded ` >>> `. Each step enters the element's open shadow root, or its document when the element is a same-origin iframe. Any number of segments may be chained. |

Ref handles carry the document they were read from — which, for a node inside an iframe, is that frame and not the top page:

- element numbers restart at 1 in every document, so the earlier `ref:N` form silently resolved to an unrelated element after a navigation — a `ref:1` saved from a search page addressed whatever happened to be the first node of the next page;
- a node inside a frame is numbered in that frame's own registry, so its epoch differs from the page's `dom_epoch`, and resolving it enters the frame that issued it. Until 1.3.2 every ref was minted in the top document, which made the natural `page_outline` → `click` sequence fail on a payment or consent frame — after a ten-second poll, blaming staleness for what was really the wrong browsing context;
- a handle whose epoch no longer matches now resolves to nothing, and the action fails with an explicit "read the page again with `page_outline`" message. So does a handle whose element has been detached from the DOM. Both fail immediately rather than polling: a replaced document does not come back, and the two cases are reported apart, since a frame that is still open but no longer holds the element can still be reached by the `center` coordinates the outline reported;
- a bare `ref:N` without an epoch is refused outright, with an error that says to read `page_outline` again: it carries no document identity, and answering it from whatever document happens to be loaded is the very mistake the epoch exists to prevent.

Ref handles and piercing paths are accepted by `fill` (both its field and its file keys),
`upload`, `click`, `wait`, and the `form_selector` of `submit`. `click` and `wait` took
plain CSS only until 1.3.0, which made the natural `find` → `click` sequence fail with
`InvalidSelectorException`; they now poll the resolved handle for the requested
`present`/`visible`/`clickable` state. The `submit_selector` still expects a plain CSS
selector. Both non-CSS forms need a live element handle, so they
resolve in the Selenium-backed modes (`temporary`, `persistent`, `attach`); in companion
`current` mode they are refused with an explicit message and CSS selectors remain the way
to address elements.

A CSS selector that contains ` >>> ` inside quotes or brackets — `div[data-op='a >>> b']`
— is no longer mistaken for a piercing path; the separator is only recognised outside
quoted strings and attribute brackets.

### `frame_selector` accepts less than the locators do

A `frame_selector` always names exactly one frame. A CSS selector matching more than one is
refused with the count, everywhere, rather than answered with whichever came first.

What differs is which *forms* are accepted. `render` and the reading topics —
`page_outline`, `page_text`, `find` — take all three, so a nested frame can be named by the
`#host >>> #frame` path the outline reports for it, as can `fill`, `click`, `wait`, `submit`
and `upload`.

The input actions take plain CSS and nothing else: `press_keys`, `pointer`, `touch`, the
pointer entries of an `input` batch, `pointer_lock`, and `game_probe`. They aim by
coordinate, which needs the frame's own box measured in the top-level page, and neither a
`ref:` handle nor a piercing path yields one. Both are refused before a single event is sent
— not half-way through a batch, which is what an `input` mixing keys and pointer entries
used to do — with an error that says to pass a CSS selector matching this frame and nothing
else. `pointer_lock` refuses them on `release` and `status` too, though only `acquire`
dispatches a click: one call cannot read the same string two ways.

`game_probe` sits on that side too, though it dispatches nothing. It reports canvas
rectangles that are then aimed at with the same string, so a probe that read one of four
matching frames while the input went to another was the same defect wearing a different hat.

So the sequence the games section leads to has one step in it: the outline names a nested
game frame `#host >>> #frame`, and that path cannot be handed to `game_probe` or `input`.
Give the input actions a CSS selector that is unique by itself, and keep the piercing path
for reading.

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

One line per request, for example a form post the server rejected:

```text
POST 422 Document       4ms 0.5KB https://example.com/submit
```

`only_errors=true` keeps exactly these — failed requests and everything from 400 up — so a
post the server accepted is filtered out and an empty list is itself an answer.

Use `output="json"` on the `network` topic when you need the per-request `id` that `network_body` expects; the default text lines omit it.

Capture is armed when the session takes its tab, not when a topic is first read. In `current` mode the subscription is the last step of opening the tab, while it is still `about:blank`, so the requests and logs of the very first navigation are in the buffer — a single `open` reports the document, its subresources, and a 404 favicon without anyone reloading anything. This is a fix, not a nicety: network capture used to be armed by the first `network` read, so an agent that opened a page, saw it fail, and then asked which requests failed was told there were none.

`attach_tab` is necessarily different. Capture starts at the claim, and whatever the tab did before it was claimed was recorded by nobody and cannot be recovered — reload the claimed page if you need its load traffic.

Each stream keeps up to 500 entries and reports how many older records were `dropped`. Two further ceilings belong to companion `current` mode alone, because there the buffers live inside the extension: a 512 KB budget shared between console and network, and in-flight requests capped at 1000. Those limits bite on a chatty page: one that fires 700 requests keeps the newest 500 and reports a little over 200 `dropped` — a few more than the arithmetic, because the shared byte budget evicts as well — and eviction is oldest-first, so the very first navigation is the first thing to go. Read `network` before the page has had time to bury it. An empty list next to a non-zero `dropped` means "evicted", not "quiet".

Both topics work in companion `current` mode and in the Selenium-backed modes; the Selenium path reads Chrome's browser and performance logs instead of the extension's buffer, so field coverage is close but not identical. It caps in-flight requests at 500 and keeps no byte budget. Its performance log is switched on when the session starts, and its page console hook is registered with `Page.addScriptToEvaluateOnNewDocument` before the session navigates anywhere, so it runs ahead of each document's own first script and the first page's boot output is covered too. That hook used to be installed the first time the `console` topic was read, which lost exactly those lines and made a reload the price of seeing them.

## Forms and multi-step flows

The short version: open the page, read it, `fill` everything in one call, attach
files through `files` or the `upload` action, reread every consequential live
choice, submit exactly once, then prove the outcome with fresh DOM/text.

```json
{
  "actions": [
    {"action": "fill", "session_id": "apply",
     "fields": {"#candidate-name": "Neo Candidate", "#role": "unity", "#remote": true}},
    {"action": "submit", "session_id": "apply", "form_selector": "#application"}
  ]
}
```

- `fill` applies what it can and returns `filled`, `field_values`, `files_uploaded` and a per-selector `errors` map; `success` is `false` whenever `errors` is non-empty. Every write is read back off the control, so a `maxlength` truncation and an `oninput` handler that rewrote the value are errors naming both values instead of successes. `field_values` answers for every selector you sent, failures included — a refused control shows what it still holds, and `null` means nothing could be read back, because the selector matched nothing or the control has left the page.
- Sanitisation is not refusal. Whitespace trimmed off a `type=email` value, `\r\n` normalised to `\n`, a handler that lower-cases the input — the control kept what you gave it, in its own terms, and all three read back as filled. A byte-for-byte comparison used to call them failures.
- A `<select multiple>` is the one control that takes a list of values and reads back as one. A scalar sent to it *replaces* the whole selection rather than extending it, and a list sent to anything else is refused.
- Date, time, datetime-local, month, week, range and colour inputs are set rather than typed, since typing into them depends on the browser's locale. The value is rehearsed on a throwaway input first, so an unparseable one is refused **without touching the control** — no more valid-looking wrong date, no more slider dropped to its midpoint — and the error names the format the control wants.
- `submit` runs native validation first and reports `validation_passed` with the offending field ids, then `submit_triggered` from the fired `submit` event or from the document being replaced — including a reload of the same URL, which a check on url and title alone reported as a failed submit. `submit_default_prevented` marks the SPA case where a handler cancelled the navigation on purpose. `submit_evidence` states in a sentence what the verdict rests on, and `new_tab_opened` warns that a `target="_blank"` result landed in a tab this session does not own — so the `url` and `title` beside it are still this page's, not the answer you are looking for.
- A checkbox takes `1`/`yes`/`y`/`on`/`check`/`checked` or `0`/`no`/`n`/`off`/`uncheck`/`unchecked`/`""` and refuses anything else, a `<select>` takes an option `value` or its visible text, and a file input must go through `files`/`upload`.
- Every control `fill` writes is blurred afterwards, because that is the only way the last field of a fill ever fires its `change` event. Three consequences follow and the third bites: focus ends on `document.body`, an autocomplete list the fill opened is dismissed, and a following `press_keys(["ENTER"])` goes to the body rather than the field — pass `target_selector`, or `focus_mode="click"`, when you mean to submit by keyboard. The `files` entries are the exception: nothing is typed into them, so nothing is blurred.
- `fill`, `click`, `submit`, `upload` and `wait` take `frame_selector`, so a form inside an iframe is addressable by name and not only through a ref.
- `wait` takes `present`, `visible`, or `clickable`, and honours its `timeout_seconds` as passed (it defaults to 10). A timeout names the seconds actually waited in its message, so you can ask for as long as the target needs and read back what really happened.
- After any navigation or step change, read `page_outline` again: old ref handles are stale by definition. `dom_epoch` only tests one direction of that. A different epoch proves your refs are dead; the same epoch proves nothing, because the epoch belongs to the document and a wizard step that swaps its own markup in place keeps it while every ref it issued goes with the old nodes. A ref read inside a frame carries that frame's epoch rather than the page's, so a mismatch there is normal and not staleness at all.

For an application, payment, message, deletion, or other consequential terminal
action, keep `submit_attempted=false`. From a fresh `page_elements` read match the
exact target (`href` where available) and verify each critical selected value in
the live control; remembered defaults are not evidence. Set the flag as the
terminal button is clicked once. After any response or timeout, never retry that
button before inspecting URL, text, elements, console, and network — the first
click may already have succeeded. Stop immediately on terminal success text.

**[→ Complex forms](docs/complex-forms.md)** covers the rest with worked calls:
choosing between the three locator forms, finding a field by meaning when
selectors are generated, partial-failure recovery, multi-step wizards and SPAs,
fields inside Shadow DOM and same-origin iframes, what to do when
`challenge_detected` turns true, and how to trace a submit that silently failed
through `network` and `network_body`.

## Reviewable macros

Use the `macro` action to record and replay a repeated browser flow. Before a
consequential replay, preview the exact resolved steps with the same variables
that will be passed to `run`:

```json
{"actions":[{"action":"macro","op":"preview","name":"document-submit","variables":{"target_url":"https://forms.example/requests/42","resource_path":"C:/docs/request-42.pdf"},"project_root":"C:/work/my-project"}]}
```

`preview` loads the saved macro, validates that every placeholder was supplied,
and returns the resolved `steps` with `executed: false`. It never dispatches an
action or changes browser state. Review the canonical URL, form values, upload
path, and whether a terminal submit is present before sending the corresponding
`op: "run"` call.

Preview is a review boundary, not proof that replay will succeed. After `run`,
check the batch result and freshly inspect the page. For applications, payments,
messages, or other consequential actions, keep terminal Submit in a separate
macro or direct action so it is attempted only once after live-state validation.

### Project-local and guarded macros

The engine is universal and domain-neutral. Domain rules belong in a saved macro or its
calling project, never in Web Search Neo core. Pass an existing absolute `project_root` to
`save`, `list`, `show`, `preview`, `run`, `guarded_stage`, `guarded_commit`, or `delete` to
use that project's independent macro set under `.web-search-neo/macros/`. Without it, the
existing per-user macro store remains the default. Macro names are already traversal-safe;
the resolved project store is additionally required to remain beneath `project_root`.

Consequential flows use a generic two-phase path. A guarded macro must end in exactly one
explicit `submit` action. `guarded_stage` resolves the macro, then fails closed unless
`guard` provides:

- equal `target_url` and `canonical_url`, plus an optional domain-defined `identity_key`;
- an explicit `allowed_hosts` policy and optional `denied_hosts` policy;
- an existing absolute `resource_path` that the resolved macro uploads exactly;
- a stable 16-128 character `idempotency_token` unique to this target;
- one or more live `assertions` over staged action results.

Host policy is data. Core contains no built-in site categories or denylist. A project can
deny platforms, production hosts, vendors, or any other domain-specific routes in its own
macro/configuration. The resolved macro must open the exact canonical target URL.

This neutral document-request example assumes result 2 is a fresh `page_text` checkpoint:

```json
{
  "actions": [{
    "action": "macro",
    "op": "guarded_stage",
    "name": "document-request",
    "project_root": "C:/work/my-project",
    "variables": {
      "target_url": "https://forms.example.com/requests/42",
      "resource_path": "C:/work/my-project/artifacts/request-42.pdf"
    },
    "guard": {
      "target_url": "https://forms.example.com/requests/42",
      "canonical_url": "https://forms.example.com/requests/42",
      "identity_key": "request-42",
      "allowed_hosts": ["example.com"],
      "denied_hosts": ["staging.example.com"],
      "resource_path": "C:/work/my-project/artifacts/request-42.pdf",
      "idempotency_token": "request-42-20260820",
      "assertions": [
        {"result_index": 2, "path": "data.text", "contains": "Request 42"}
      ]
    }
  }]
}
```

`guarded_stage` dispatches every step except terminal Submit. Only after all staged actions
succeed and every semantic assertion passes does it reserve a `checkpoint`. Review the
result and commit once from the same project store:

```json
{"actions":[{"action":"macro","op":"guarded_commit","checkpoint":"guard-request-42-20260820","project_root":"C:/work/my-project"}]}
```

The project-local ledger is marked `submit_attempted` *before* Submit dispatch. A timeout,
lost response, second call, new token for the same target identity, or resource reused for
another target refuses replay. The guard proves one guarded attempt, not server acceptance;
inspect durable confirmation separately. Concrete domain macros should live with their
projects and are not bundled in this repository.

## Canvas and WebGL games

Browser automation is not limited to DOM forms. The compact contract covers common HTML5 game controls:

| Call | What it does |
| --- | --- |
| `web_info(topic="game_probe")` | Reports canvases, 2D/WebGL context, iframe surfaces, document focus, sampled FPS under `animation`, loading time, the console warnings and errors new since the previous probe, and held input. |
| `input` | Mixes per-key `tap`/`hold`/`release` with pointer `click`, `double_click`, `hover`, `move`, `drag`, `press`, `release`, and `wheel`, using absolute coordinates, deltas, or unbounded relative motion, up to 16 entries of each kind. |
| `press_keys` | Keyboard-only shortcut: 1-8 `keys` plus `key_action` (`tap`, `hold`, `release`), `repeat` (1-50), `hold_seconds`, `focus_mode` (`focus`, `click`, `none`), and `hold_frames` (1-30), which keeps a tap down across N released frames in `step` mode. |
| `touch`, `touch_emulation` | `tap`, `press`, `move`, `release`, `swipe`, or `cancel` with up to ten simultaneous points; the emulation makes the page report `navigator.maxTouchPoints` and `ontouchstart` so a game's mobile code path actually runs. |
| `pointer_lock` | Acquires, releases, or reports pointer lock for first-person controls; while locked, `coordinate_mode="relative"` moves without clamping to the viewport, which is what feeds `movementX`/`movementY`. |
| `render`, `step`, `release_inputs` | Control the animation gate and safely reset held input. |
| `frame_selector` | Targets a cross-origin game iframe such as the one used by Yandex Games. |

The verb of each action is namespaced because the dispatcher already owns
`action`: `key_action`, `pointer_action`, `touch_action`, and `pointer_lock`'s
`operation`.

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

A batch like that one ends with `release_inputs` and `render mode=normal` for a reason, and `web_action` stops at the first action that fails unless `continue_on_error=true`. So the failure of a single `input` in the middle is the case where the cleanup at the end never runs and the page is left frozen with keys held. Either send the cleanup as its own call, or pass `continue_on_error=true` on any batch whose tail is cleanup.

For a top-level canvas application such as `https://redoschool.ru/demo/?auto=true`, omit `frame_selector`. Pointer coordinates are relative to the selected top-level viewport or iframe.

**[→ Playing games](docs/playing-games.md)** is the full walkthrough: a complete
run from `open` to the win condition, why a tapped key is held across the frame,
the auto-repeat that keeps a held key alive after a respawn, iframe coordinate
handling, pointer lock, touch games, and the cleanup that must happen at the end.

### Render modes

| Mode | Behavior |
| --- | --- |
| `normal` | Removes the render gate and returns to the page's normal `requestAnimationFrame` loop and real clocks. |
| `throttled` | Continuously releases animation callbacks at no more than `target_fps`, for example 10 FPS. `target_fps` defaults to 10 and is clamped to 1-60. |
| `step` | Holds queued animation callbacks. The `step` action releases 1-120 frames explicitly; every `input` action also releases exactly one frame. |

While the gate is engaged — in `throttled` as well as in `step` — page time is frozen:

- `performance.now()` and `Date.now()` advance by exactly one frame delta of 16.667 ms per released frame;
- `setTimeout`, `setInterval`, and `requestIdleCallback` are queued against that same virtual clock and run immediately before the frame's animation callbacks. Without this a game reads the agent's thinking time as its `deltaTime`, and a batch of frames released back to back arrives with a delta near zero;
- promises and `queueMicrotask` are not gated, and `new Date()` still reports wall-clock time;
- queued timers are handed back to the real scheduler, with their remaining delay intact, when the mode returns to `normal`;
- page time never runs backwards. Stepping carries the clock *ahead* of wall time — sixty frames released back to back advance it by a second in barely a moment — and returning to `normal` keeps that gap instead of dropping it. Handing the native clock back used to lose the whole gain at once, measured at −5.9 s, which a game reads as a negative frame delta. A session that has stepped keeps the clock wrapper for the rest of its life, since only the wrapper can go on applying the offset; one that never stepped gets the untouched native clock back.

`render` also accepts `frame_delta_ms`, `freeze_time`, and `gate_timers` when an
engine needs something other than those defaults. One knob stays Python-only:
`browser_tools.set_render_control(..., key_repeat=False)` disables the auto-repeat
that keeps a held key alive for games which latch input on `keydown`.

One `input` action can hold one key, release another, tap two more, turn the wheel, and move the pointer by a delta before releasing exactly one frame. A tapped key is pressed together with the rest of the batch, stays down for the whole released frame, and is lifted afterwards, so an engine that polls key state once per frame — Phaser, Godot, a hand-written canvas loop — actually observes the press. In `step` mode the game never observes a partially applied intermediate input state. Outside step mode the actions are still serialized, but the page continues rendering normally.

The gate is installed into every new document of a session, so it survives a page reload or a game iframe that reloads itself; a `step` that lands on a fresh document re-applies step mode once and reports `gate_reinstalled`. `render` reports `frame_delta_ms`, `time_frozen`, `timers_gated`, `pending_callbacks`, and `input_advances_frame`; `step` reports `frames`, `callbacks`, `pending_timers`, and `virtual_now`.

The render controller gates JavaScript `requestAnimationFrame`, which covers typical canvas/WebGL and Unity WebGL loops. It does not change video decoding, CSS compositor animations, the monitor refresh rate, or guarantee an exact GPU hardware frame rate on every engine.

While a gate is engaged, `game_probe` does not try to sample FPS. Frames are released by
hand, so the measurement could only expire against its own script timeout and then report
a fabricated zero; the probe returns immediately with an `animation` object holding
`fps: null`, `animation_suspended: true`, and a `reason` naming the active render mode and
the gated frame. Every frame-rate field lives in that nested object — `animation.fps`,
`animation.frames`, `animation.elapsed_ms`, `animation.available` — never at the top level
of the result.

`console_messages` holds the warnings and errors that appeared **since the previous
`game_probe` call**, which is what the probe's `console_scope` field says in the result. A
polling loop therefore reports a problem when it happens instead of re-reading everything
the session has ever logged, and its output does not grow with the length of the run. The
flip side is that each entry is delivered exactly once: a probe result that is thrown away
takes its console entries with it. Read `console_messages` on every probe you make, or use
the `console` topic, which keeps its own place in the same buffers — polling the probe never
hides entries from `console`, and reading `console` never hides them from the probe.

The probe reads the in-page hook as well as Chrome's browser log, so `console.warn`,
`console.error`, and uncaught exceptions are covered on both backends. In companion
`current` mode that browser log is not available at all, and earlier builds, which read only
it, reported nothing there.

### Input latency

Game control is only useful if a round trip is cheap, so each action reads the page once
and never sleeps by default. Measured through `web_action` against the bundled platformer
fixture in step mode on a headless Chrome session, 30 iterations each, median and p95:

| Action | Median | p95 | Same call at the former `wait_seconds=0.2` |
| --- | --- | --- | --- |
| `input` with two keys and a pointer entry | 34 ms | 47 ms | 233 ms |
| `press_keys` single key tap | 25 ms | 26 ms | 230 ms |
| `pointer` hover | 27 ms | 27 ms | 220 ms |
| `step` of one frame | 11 ms | 12 ms | — |

The published `wait_seconds` default of every input action dropped from 0.2 to 0.0, which
is the whole of the last column: the action itself was never the cost. `include_summary`
is now part of the MCP contract as well as the Python API, and setting it to `false` skips
the post-action page read: `input` 34 ms → 29 ms, `press_keys` 25 ms → 21 ms, `step`
11 ms → 8 ms. It saves nothing measurable on `pointer`, whose summary is already the
cheapest of the four. Absolute numbers depend on the machine; the ratios do not.

## Two-tool MCP contract

| Tool | Responsibility |
| --- | --- |
| `web_info` | Return the whole contract, the built-in automation skill, or one action schema on demand; read search, current Chrome tabs, browser, page outline/text/find, console, network, game, screenshot, or time state. |
| `web_action` | Execute one or up to 32 ordered setup, search, fetch, tab attach/open, form, input, render, and close actions. Supports fail-fast or `continue_on_error=true`. |

Start with `web_info()`. With no arguments it returns `actions` with each action's summary and its required parameter names, `action_groups`, `info_topics`, `recipes`, `pitfalls`, `limits`, and worked `examples`. Optional names, types, and defaults are deliberately left out of it. Request only the needed, generated JSON Schema with `web_info(topic="action_schema", params={"action": "input"})`, then invoke it through `web_action`. The same call describes an observation topic — `params={"action": "find"}` returns `find`'s parameters — which matters because a topic refuses any argument it does not list, and that list appears nowhere else. This follows the on-demand Tool Search principle used by [official Unreal MCP](https://dev.epicgames.com/documentation/unreal-engine/unreal-mcp-in-unreal-editor): keep the eager tool list small, disclose schemas only when needed, and dispatch actions through a meta-tool. Web Search Neo combines Unreal's list/describe discovery tools into one `web_info`, so only two tools are advertised.

Measured on the current build, summing each advertised tool's `name`, `description`, and serialized `inputSchema`: the compact surface is 1,112 characters across two tools, against 24,453 characters across the 43 tools of legacy mode. The self-describing contract behind `web_info()` is 9,095 characters, and it is fetched only when an agent asks for it. Each action in it lists its required parameter names; optional names, types, and defaults stay in `action_schema`, where they cost nothing until needed — and where an observation topic's parameters live too, since a topic accepts exactly the list it publishes and nothing else.

Every action is declared once in a single registry that also generates its published schema, and arguments are validated against that same model before the handler runs. An unknown or malformed field returns the offending names and the list of allowed parameters instead of an internal `TypeError`:

```text
ValueError: action 'input': unknown parameter(s) ['frames']. Allowed: ['key_actions', 'pointer_actions',
'session_id', 'target_selector', 'frame_selector', 'wait_seconds', 'include_summary']. Call
web_info(topic='action_schema', params={'action': '<name>'}) for the full schema.
```

Existing direct Python imports remain available. For temporary MCP-client migration only, set `WEB_SEARCH_NEO_LEGACY_TOOLS=1` before starting the server to advertise the former narrow tool list instead of the compact default.

Browser state is keyed by `session_id`. The `open_many` action can create up to four independent sessions concurrently. A viewport screenshot with no dimensions preserves the current viewport in every mode. An explicit viewport `width`/`height` pair is exact in isolated Selenium modes and is refused in `current` mode, where the MCP never resizes the user's Chrome; use `mode="region"` there for an exact-size image. Full-page captures above `3840x10000` fail explicitly instead of returning an unlabelled partial image.

Image-guided clicking is available through `pointer` with
`pointer_action="click"` and viewport CSS `x`/`y`. Take a fresh viewport screenshot;
if its PNG dimensions differ from the reported viewport dimensions, scale both
axes proportionally. Full-page and region pixels do not map directly to the
viewport: scroll the target into view and recapture. Any scroll, zoom, resize,
navigation, animation, or rerender invalidates the old image coordinates.

## Optional agent skill

`web_info(topic="skill")` returns a compact built-in playbook intended even for
small local models: inspect → act → verify, schema discovery before guessing
optional parameters, element pagination and lazy-page scrolling, the three
screenshot modes, safe visual-coordinate clicks, current-Chrome CSS-only action
locators, exact href/value matching, and a one-shot final-submit guard. It is
about 5.5 KB and can be fetched once at the start of an automation task.

`web_info()` called with no arguments still returns the entire agent-facing contract: every action with its summary and required parameter names, the observation topics, ready-made recipes, the common mistakes, the hard limits, and runnable examples. Optional parameters — for an action or for a topic — come from `action_schema`, one at a time. An agent that reads either contract needs no external instructions, so the bundled filesystem skill is a convenience, not a requirement.

The repository still includes a short [Web Search Neo skill](skills/web-search-neo/SKILL.md) for clients that prefer a resident description of when to reach for the server at all.

Install it locally by copying `skills/web-search-neo` into your Codex skills directory, then restart Codex:

```powershell
Copy-Item -Recurse -Force skills\web-search-neo "$env:USERPROFILE\.codex\skills\web-search-neo"
```

Invoke it explicitly as `$web-search-neo`, or let its task description trigger it for web search, visible Chrome automation, authorized attach sessions, form work, and browser-game testing.

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

The [bridge daemon](#the-bridge-daemon) has five switches of its own. Set them for the MCP
server process: a daemon inherits the environment of whichever server spawns it.

| Variable | Effect |
| --- | --- |
| `WEB_SEARCH_NEO_BRIDGE_PORT` | Loopback port shared by the daemon, the servers, and the extension, default `8765`. Changing it also means editing `BRIDGE_URL` in `chrome-extension/service-worker.js` and reloading the unpacked extension. |
| `WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS` | How long a daemon with neither a companion nor a client stays up, default `900`. `0` disables the timer and keeps the port held. |
| `WEB_SEARCH_NEO_BRIDGE_AUTOSPAWN` | `0`, `false`, or `no` stops a server from starting a daemon; it then uses one only if something else already runs it. |
| `WEB_SEARCH_NEO_BRIDGE_CONNECT_TIMEOUT` | How long a server keeps trying to reach a daemon, including one it started itself, default `12` seconds. |
| `WEB_SEARCH_NEO_BRIDGE_START_TIMEOUT` | How long server startup waits for that first attempt before continuing in the background, default `2` seconds. |

Only use a proxy you are authorized to use. HTTP sessions use desktop browser headers, connection pooling, bounded response sizes, and conservative retry/backoff. Rendered pages use the installed Chrome's native matching User-Agent unless explicitly overridden.

### Transport policy

Unencrypted `http://` to a public host is refused, for plain fetches and for browser `open` alike, with an error that names the host and the override. Loopback, private, link-local, and unspecified addresses stay reachable over plain HTTP, as do `localhost` and any host ending in `.local`, `.localhost`, `.internal`, `.home.arpa`, `.lan`, `.home`, `.intranet`, `.private`, or `.corp` — a local ComfyUI, Ollama, or dev server keeps working unchanged. So does any single-label host, `http://nas/` or `http://raspberrypi:8080/`: a name with no dot cannot be published on the public DNS, and deciding it by resolution would cost a lookup on every URL and every redirect hop. Set `WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP=1` to accept plain HTTP everywhere.

Redirects are followed one hop at a time, at most five, and every hop is validated again, so a public HTTPS URL cannot quietly land on plain HTTP.

## Tests

[![Tests](https://github.com/NeoXider/web-search-neo/actions/workflows/tests.yml/badge.svg)](https://github.com/NeoXider/web-search-neo/actions/workflows/tests.yml)

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python -m pytest --cov=. --cov-report=term-missing
```

The deterministic suite is 576 tests, grouped by what they protect:

| Area | Covered |
| --- | --- |
| MCP contract | Exactly two advertised tools, on-demand action discovery, ordered multi-action calls. |
| Search | Routing, fallback, cooldown and cache, live status probes that leave the real cooldown untouched, accurate Bing challenge detection, manual challenges. |
| HTTP | Fetches, the plain-HTTP policy, and per-hop redirect checks. |
| Perception | The accessibility outline including open shadow roots and same-origin frames, refs minted in the document that owns them and resolved back into it, selectors verified unique before they are handed over, page text extraction with its non-empty `main` fallback, its link budget and its account of what it left out, and `find` separating how well a query matched from how a result ranks. |
| Locators | The three forms and their escaping, refs that refuse to resolve in another document or after their element was removed, valid CSS that merely contains ` >>> `. |
| Diagnostics | The companion's console and network buffers with their ring-buffer eviction, capture armed at open so the very first navigation is reported, a claimed tab recorded only from the claim, and a subscription that failed at open repaired on the next read. |
| Companion bridge | The handshake — a client without the token, a hello without a nonce, a newer companion evicting an older one, and WebCrypto HMAC agreeing with the Python signature — origin checks, tab grouping, and confirmation-gated Chrome setup that publishes the token before touching Chrome. |
| Bridge daemon | Two MCP servers with commands in flight at once, two servers starting together converging on one daemon, a client never mistaken for the browser, an in-flight command failing instead of hanging when the daemon dies, a daemon of the same version left alone while an outdated one is replaced, a second outdated replacement reported instead of looped over, the idle exit once neither browser nor client is left, and `--bridge --stop` against a running daemon and against none. |
| Forms | Multipart upload, form filling, native validation, values read back off the control so a refused write is not a success, exact PNG viewport size. |
| Challenges | A captcha that blocks told apart from one merely present on the page, and the providers a top-level query walked past — DataDome, AWS WAF, a challenge one frame down, and one in a shadow root. |
| Games and input | Canvas probing, normal/throttled/step rendering, atomic mixed input, held modifiers across a batch, gate and held-input reset on navigation, coordinates that follow a frame's CSS transforms, key spellings that release each other, and a virtual clock whose intervals keep their period. |
| Sessions | Sessions that close the tabs they own, concurrent sessions, persistent storage, a session dropped when the browser it was opened in is gone, a borrowed tab handed back instead of navigated, and a real managed-Chrome attach/detach that leaves Chrome running. |

Public search engines may rate-limit an IP or region, so live internet smoke checks are kept separate from deterministic tests. `scripts/live_smoke.py` runs a search, opens two pages concurrently, fills and submits a public Selenium test form, uploads a file, and verifies exact screenshot dimensions; it does not cover games. `scripts/companion_live_smoke.py` reaches the network too, though its subject is the extension: it starts a disposable Chromium with the companion loaded and drives whatever `--url` names, which defaults to `https://example.com`. Public game sites change without notice, so the frame gate, input atomicity, and held-input recovery are verified by the deterministic local suite instead.

### End-to-end check with a local model

`scripts/live_agent_game.py` exercises the whole stack the way a real client does: it starts `main.py` as an MCP stdio subprocess, hands the two advertised tools to a model served by [LM Studio](https://lmstudio.ai/) on `127.0.0.1:1234`, and lets that model play the bundled platformer fixture while every tool call is timed.

```powershell
lms load qwen3.5-4b-mtp --context-length 16384 --parallel 1
python scripts/live_agent_game.py --model qwen3.5-4b-mtp
```

It reports the eager tool-schema size and the median and p95 latency of both MCP tool calls and model turns, so a regression in either is visible immediately. Thinking is disabled through `reasoning_effort: "none"`, because a mechanical control loop pays for it without gaining anything. The script needs Chrome and a running LM Studio server and is not part of `pytest`. Whether the model finishes the level is a property of the model, not of the server: a 4B does it, but not on every attempt.

## Safety notes

- Visible or attached sessions may contain authenticated accounts. The MCP client can act with the permissions of those accounts.
- The companion declares five permissions: `alarms`, `debugger`, `storage`, `tabs`, and `tabGroups`. It ships no content scripts and asks for no `host_permissions`, but `debugger` is the broad one: it lets the extension attach the Chrome DevTools Protocol to a tab and from there read and modify that page, its console, and its network traffic. Chrome shows a "started debugging this browser" banner whenever it is attached. `alarms` is the narrow one — it only wakes a suspended service worker to retry the bridge, and Chrome shows no extra warning for it. Install the companion only from this repository.
- The loopback bridge is authenticated in both directions. The extension proves it holds the machine-local token before the bridge accepts a command, and the bridge proves the same by returning `HMAC-SHA256(token, nonce)` before the extension executes one; an MCP server presents the same token before it may relay anything. Until 1.3.0 the port accepted any local client that spoke the protocol, and the extension trusted whatever answered on it.
- That secret is a file readable by the user account that owns it, so it does not defend against a malicious process already running as you: such a process can read the token and impersonate either side. It removes the race in which any local program that binds `127.0.0.1:8765` before the server inherits DevTools access to every signed-in tab. Chrome Native Messaging, which needs no listening port at all, is the actual fix and is tracked in [TODO.md](TODO.md).
- The port is now held by a [daemon](#the-bridge-daemon) that outlives each agent call, so it is reachable for as long as Chrome keeps the companion attached rather than only while an agent runs. The authentication is unchanged and same-user processes were never excluded by it, but the window in which one could use the token is wider. Stop the daemon with `python main.py --bridge --stop`, or let its idle exit close the port fifteen minutes after the last companion and client are gone.
- Authentication is not authorization. An authenticated peer may call `cdp.send` with any DevTools method on any tab the session drives; there is no method allowlist yet.
- `setup_current_chrome` opens no page, navigates nothing, and reads no browsing data. It publishes the shared secret and returns the steps. The single exception, since 1.3.1, is that it may tell an already-installed companion older than the bundled build to reload itself; *installing* the companion stays a deliberate user action in Chrome's own UI.
- Plain `http://` to public hosts is refused unless `WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP=1` is set; loopback and private-network addresses are always reachable.
- File upload tools can upload local paths supplied to the tool. Review agent actions and scope filesystem access appropriately.
- Browser automation may be restricted by a site's terms of service. Use it only where you are authorized.
- Manual challenge mode hands control to you; it does not attempt to bypass CAPTCHA protections.

## Contributing

Issues and focused pull requests are welcome. A new search engine only needs a `SearchProvider` implementation plus `register_search_provider(provider)`; status, cooldown, cache, and fallback routing update automatically.

See [TODO.md](TODO.md) for the current roadmap.
