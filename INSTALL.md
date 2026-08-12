# Installing Web Search Neo

This guide installs the MCP server from source and connects it to LM Studio or another stdio-compatible MCP client.

It describes version 1.3.0. The Python package, the server, and the bundled Chrome
companion carry that same version, and the bridge only accepts a companion able to complete
the 1.3.0 handshake — see [Updating](#updating) if an older one is already installed.

## 1. Requirements

- Python 3.10, 3.11, 3.12, or 3.13 available as `python` on `PATH`.
- Git.
- Google Chrome 116+ for rendered browser automation. Search and plain HTTP fetch tools do not require Chrome.
- Windows, Linux, or macOS supported by Selenium Manager. The companion is installed by
  three clicks in your own Chrome, which works anywhere Chrome supports extensions.

Check the commands before continuing:

```text
python --version
git --version
```

Selenium Manager resolves the matching Chrome driver automatically on the first rendered-browser run.

## 2. Clone and install

```text
git clone https://github.com/NeoXider/web-search-neo.git
cd web-search-neo
python -m venv .venv
```

Activate the environment on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Activate it on Linux or macOS:

```bash
source .venv/bin/activate
```

Install runtime dependencies:

```text
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 3. Start the MCP server directly

```text
python main.py
```

The process waits for MCP messages on stdin and writes responses to stdout, so an apparently idle terminal is expected. Stop it with `Ctrl+C`.

Diagnostic logs are written to `msp_server.log`. The log file is ignored by Git.

## 4. LM Studio configuration

Open LM Studio, go to the MCP integrations/configuration screen, and merge this entry into its MCP JSON:

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

Replace `cwd` with the directory you cloned. Use forward slashes in JSON on Windows, or escape each backslash as `\\`.

The configuration deliberately uses `"command": "python"` instead of an absolute interpreter path. If LM Studio cannot find the virtual environment, either launch LM Studio with that environment on `PATH` or use the system Python where you installed `requirements.txt`.

Toggle the MCP integration off and on, or restart LM Studio. The server should expose exactly two tools under `web-search-neo`: `web_info` and `web_action`.

If an older prompt or client still requires the previous individual tool names, set `WEB_SEARCH_NEO_LEGACY_TOOLS=1` for that server process. Compact mode remains the recommended default.

### Optional Codex-compatible skill

The server describes itself: `web_info()` with no arguments returns every action with its
parameters, the observation topics, recipes, pitfalls, limits, and examples. Installing the
skill is therefore optional.

Copy the bundled skill to the local Codex skill directory and restart Codex:

```powershell
Copy-Item -Recurse -Force skills\web-search-neo "$env:USERPROFILE\.codex\skills\web-search-neo"
```

The skill can then be invoked as `$web-search-neo`. It keeps agent context small by discovering one action schema at a time through `web_info` and dispatching work through `web_action`.

## 5. Other MCP clients

Use the same stdio command and working directory:

```text
command: python
args: main.py
working directory: /absolute/path/to/web-search-neo
```

Some clients do not support a separate working-directory field. In that case keep `python` as the command and pass the absolute path to `main.py` as the argument:

```json
{
  "command": "python",
  "args": ["C:/path/to/web-search-neo/main.py"]
}
```

## 6. Connect the current signed-in Chrome (default)

`profile_mode="current"` is the default. It controls tabs in the Chrome you already
use, preserves the page's existing login, opens new tabs into a visible purple
group named `AI`, and leaves tabs open when MCP sessions close.

Start/restart the MCP first, then ask it for setup state:

```json
{
  "actions": [{"action": "setup_current_chrome"}]
}
```

That call changes nothing in any browser. It writes the shared bridge secret into
`chrome-extension/bridge-token.js`, compares the bundled build with whatever is connected,
and returns `manual_steps`: the clicks that are left, with the absolute path of the folder
to pick. The result also carries `token_ready` and the `token_file` it wrote.

Chrome does not let any program add an unpacked extension to a browser you already have
open. The installed set is signed inside `Secure Preferences`, policy installs need a packed
CRX behind an update URL, and a Chrome that started without a DevTools port cannot be given
one afterwards. So these three steps are yours:

1. Open `chrome://extensions`.
2. Switch on **Developer mode** (top-right of that page).
3. Choose **Load unpacked** and select the repository's `chrome-extension` folder.

Leave **Web Search Neo Companion** enabled; its toolbar badge reads `ON` once it reaches the
server. You never create or copy a token by hand — the server writes it on every start.

Earlier revisions tried to perform those clicks for you through Windows UI Automation. That
code is gone. It depended on the interface language, on which window happened to have focus,
and on a folder picker that the automation backend does not even enumerate.

The bundled companion is version 1.3.0 and declares four permissions: `debugger`, `storage`,
`tabs`, and `tabGroups`. There are no content scripts and no `host_permissions`; page access
comes from `debugger`, which attaches the Chrome DevTools Protocol to the tabs the agent
drives.

If the companion was already installed from an earlier revision, Chrome keeps running the
old service worker until you press **Reload** on its card at `chrome://extensions`. Do that
after every `git pull`. From 1.3.0 onwards it is not optional: an older worker knows nothing
about the handshake below, so the bridge rejects it and the badge never turns `ON`.

There is no automatic alternative to those three steps, on any platform. If you do not want
an extension at all, use the Selenium modes in section 7 — `profile_mode="temporary"` and
`profile_mode="persistent"` drive their own browser and need no companion.

Verify it with `web_info(topic="browser_status")`; `current_chrome.connected`
should be `true`. List tabs with `web_info(topic="browser_tabs")`, then claim an
existing returned ID with an `attach_tab` action. A normal `open` creates a new
tab in group `AI` unless another `tab_group` is supplied.

The bridge listens only on `127.0.0.1:8765` and accepts the fixed bundled extension
ID. If that port conflicts, change `WEB_SEARCH_NEO_BRIDGE_PORT` for the MCP and the
`BRIDGE_URL` constant in `chrome-extension/service-worker.js`, then reload the
unpacked extension.

Enable the companion in one Chrome profile at a time. The bridge keeps one companion
connection, and the newest authenticated one replaces the previous one, so a second profile
running the companion takes the agent's tabs over from the first.

### The shared bridge secret

Loopback is reachable by every process running under your account, so the server and the
companion authenticate each other before any command is executed. Nothing here needs manual
work; it is documented because the file exists on your disk.

- The MCP server mints a random 32-byte token the first time it starts and stores it at
  `%LOCALAPPDATA%\WebSearchNeo\bridge-token` on Windows, or at
  `$XDG_DATA_HOME/WebSearchNeo/bridge-token` — by default
  `~/.local/share/WebSearchNeo/bridge-token` — created with `0600` permissions on POSIX.
- The token is machine-local and per-user. Never copy it to another machine or into a
  repository; if it leaks, delete the file, restart the MCP server, and reload the
  companion, which mints and publishes a new one.
- The server writes a copy into `chrome-extension/bridge-token.js` whenever it starts and on
  every `setup_current_chrome` call. That file is in `.gitignore`; a fresh clone does not
  contain it, and the companion simply retries until the server has written it.
- The companion sends the token with a nonce; the server answers with
  `HMAC-SHA256(token, nonce)`; each side stops if the other cannot prove it holds the same
  secret.
- A file-based secret does not protect against a malicious process running as the same user,
  which can read it. It closes the case where any local process that grabs the bridge port
  before the server gains DevTools access to your signed-in tabs.

## 7. Choose an isolated or managed browser mode

### Visible disposable browser

Use `profile_mode="temporary"`, which opens visibly by default. A Chrome window opens and you can watch the agent. Cookies are discarded when the session closes. Set `headless=true` only for background operation.

### Visible persistent browser

Use `profile_mode="persistent"` and a stable `profile_id`. It opens visibly by default. Log in once in that MCP-owned window; later sessions with the same profile ID reuse its cookies and local storage.

```json
{
  "actions": [{
    "action": "open",
    "url": "https://example.com",
    "session_id": "authorized-work",
    "headless": false,
    "profile_mode": "persistent",
    "profile_id": "authorized-work"
  }]
}
```

### Attach to an open authorized Chrome

On Windows, start the included managed Chrome launcher:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId authorized -Port 9222 -WindowMode visible
```

Log in manually, leave that Chrome window open, and attach:

```json
{
  "actions": [{
    "action": "open",
    "url": "https://example.com",
    "session_id": "attached-work",
    "headless": false,
    "profile_mode": "attach",
    "debugger_address": "127.0.0.1:9222"
  }]
}
```

MCP detaches without closing the managed Chrome. The next attach reuses the same browser state.

Visible mode is the attach launcher default. To run the same managed profile without a visible window, use a different free port and `-WindowMode headless`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_managed_chrome.ps1 -ProfileId automation -Port 9223 -WindowMode headless
```

All newly created MCP-owned `temporary` and `persistent` sessions are visible when `headless` is omitted. Set `headless=true` only for background operation. For `attach`, the launcher's `-WindowMode` controls the already-running Chrome; attach cannot change its visibility afterward.

Use `profile_mode="auto"` to prefer the current Chrome but fall back to a separate
visible temporary window. The default `current` mode does not fall back silently.

Chrome 136+ requires remote debugging to use a non-default data directory. You cannot safely retrofit attach mode onto an arbitrary normal Chrome window that was started without a DevTools port. The launcher handles both requirements with a separate durable profile.

## 8. Verify the installation

Install development requirements and run the deterministic suite:

```text
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests use a local web server and do not rely on search-engine availability.

Maintainers can smoke-test the real unpacked extension in a disposable Chromium
profile without touching their normal Chrome:

```powershell
python scripts\companion_live_smoke.py --chromium-binary C:\path\to\chromium.exe
```

Check providers from an MCP client with:

```json
{
  "topic": "search_status",
  "params": {"check_live": true}
}
```

A provider may report a regional challenge while the overall service remains healthy through fallback.

## Updating

```text
git pull --ff-only
python -m pip install -r requirements.txt
```

Restart or toggle the MCP server after updating. If you use the companion extension, you
must then open `chrome://extensions` and press **Reload** on **Web Search Neo Companion**.
Chrome keeps running the service worker it loaded earlier, and a worker older than 1.3.0
does not authenticate against the bridge, so without the reload the companion stays
disconnected — the badge shows `OFF` and every `current`-mode action fails with a setup
error. Reloading also makes the worker re-read `chrome-extension/bridge-token.js`, which is
how a rotated secret reaches it.

Confirm with `web_info(topic="browser_status")` that `current_chrome.connected` is `true`
before continuing.

## Optional environment variables

Set these for the MCP server process before it starts.

| Variable | Effect |
| --- | --- |
| `WEB_SEARCH_NEO_REGION` | DDGS region for search, default `us-en`. |
| `WEB_SEARCH_NEO_PROXY` | Proxy for HTTP fetches, search, and browser sessions. |
| `WEB_SEARCH_NEO_BROWSER_USER_AGENT` | Override the User-Agent of rendered sessions. |
| `WEB_SEARCH_NEO_PROFILE_ROOT` | Root directory for `persistent` Chrome profiles. |
| `WEB_SEARCH_NEO_DEBUGGER_ADDRESS` | Default DevTools address for `attach` mode. |
| `WEB_SEARCH_NEO_BRIDGE_PORT` | Loopback port of the companion bridge, default `8765`. |
| `WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP` | Accept unencrypted `http://` to public hosts. |
| `WEB_SEARCH_NEO_LEGACY_TOOLS` | Advertise the former wide tool list instead of the two compact tools. |

`WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP` accepts `1`, `true`, `yes`, or `on`. Without it, plain
`http://` to a public host is refused for both page fetches and browser `open`, and each
redirect hop is validated the same way. Loopback, private, and link-local addresses, plus
`localhost` and hosts ending in `.local`, `.localhost`, `.internal`, or `.home.arpa`, are
always allowed over plain HTTP, so local services such as ComfyUI or a dev server need no
configuration change.

## Troubleshooting

### `python` is not found

Install a supported Python version and enable the installer's “Add Python to PATH” option, then reopen the terminal and MCP client.

### MCP starts and immediately disconnects

Run `python main.py` from the clone directory and inspect `msp_server.log`. Confirm all packages were installed into the same Python environment that the MCP client resolves through `PATH`.

### Chrome does not start

Confirm Google Chrome is installed and can launch normally. The first Selenium Manager run may need network access to resolve a compatible driver.

### Attach mode cannot connect

Open `http://127.0.0.1:9222/json/version` locally. If it is unavailable, restart the managed Chrome launcher and make sure another process is not using the port.

### Current Chrome companion cannot connect

Call `web_info(topic="browser_status")`. Confirm that the MCP server is still
running, **Web Search Neo Companion** is enabled at `chrome://extensions`, and the
toolbar badge shows `ON`. If port 8765 is occupied, stop the other process or set
the same free `WEB_SEARCH_NEO_BRIDGE_PORT` for the MCP and extension source before
loading it.

### The companion stopped connecting after an update

This is the expected symptom of an unreloaded extension, and it is the first thing to check
after any `git pull` that crosses 1.3.0.

1. Open `chrome://extensions` and press **Reload** on **Web Search Neo Companion**. The
   badge should turn `ON` within a few seconds.
2. If it stays `OFF`, open the card's **service worker** link and read its console. `no
   companion token yet, run setup_current_chrome` means `chrome-extension/bridge-token.js`
   is missing: start the MCP server from the clone, which writes it, or send a
   `setup_current_chrome` action. `handshake refused (1008) Companion token mismatch` means
   the file holds a different secret than the running server — the usual cause is two
   clones, or a copied checkout, so start the server from the same directory Chrome loaded
   and reload the extension again.
3. Check the card's version. It must read 1.3.0; anything older cannot authenticate at all,
   and Chrome only picks up the new manifest on reload.
4. `msp_server.log` records the server's side: `Rejected a bridge client that did not
   present the companion token` confirms that something did reach the port but could not
   prove the secret.

The companion retries roughly every ten seconds after a refused handshake, so once the
cause is fixed it reconnects on its own.

### A canvas/WebGL game loads but cannot be controlled

Call `web_info(topic="game_probe")` first. If it reports a game iframe, reuse its selector as `frame_selector` in the `input` and `render` actions. Call `web_info(topic="screenshot")` after input to verify the visible state change.

To slow a typical canvas/WebGL loop continuously, send `{"action":"render","mode":"throttled","target_fps":10}` through `web_action`. For exact input states use `mode="step"`; each `input` action advances one frame, while the `step` action advances explicitly. Always restore `mode="normal"` when finished. Closing the MCP session also releases held input and restores normal rendering.

Both gated modes also freeze page time: `performance.now()` and `Date.now()` move by one
fixed frame delta per released frame, and `setTimeout`, `setInterval`, and
`requestIdleCallback` are queued against the same virtual clock. A game therefore sees a
constant `deltaTime` instead of the time the agent spent thinking. The gate is reinstalled
automatically when the page or the game iframe reloads.

Use one `input` action when different inputs must land in the same step-mode frame: each key entry has its own `tap`, `hold`, or `release`, while pointer entries support `hover`, `move`, `wheel`, button actions, absolute coordinates, `coordinate_mode="delta"`, or `coordinate_mode="relative"` under pointer lock. A tapped key stays down for the whole released frame, so engines that poll key state once per frame observe it. Send `release_inputs` as an emergency release for every held key and mouse button. If the contract is not in context, call `web_info(topic="action_schema", params={"action":"input"})` first.

### A page or a local service cannot be opened over http

Plain `http://` to a public host is refused; the error names the host and the override. Use
`https://`, or set `WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP=1` for the server process. Loopback and
private addresses are unaffected and never need the override.

### The console or network topic returns nothing

Console capture begins when the session attaches to the tab and network capture begins on
the first `network` read, so anything that happened earlier is not buffered. Re-open or
reload the page and read again. If the companion was updated in place, reload it at
`chrome://extensions` first.

### Search provider is challenged

Keep `challenge_mode="fallback"` for fast automatic routing. Use `challenge_mode="manual"` only when you want a visible three-minute opportunity to complete the challenge yourself.

### PowerShell blocks virtual-environment activation

You can install and run without activation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

For MCP configuration, keep `command` as `python` when the environment is already available on the client's `PATH`; otherwise see the LM Studio environment note above.
