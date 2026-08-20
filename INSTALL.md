# Installing Web Search Neo

This guide installs the MCP server from source and connects it to LM Studio or another stdio-compatible MCP client.

It describes version 1.3.6. The Python package, the server, and the bundled Chrome
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

Diagnostic logs are written to `msp_server.log`. The log file is ignored by Git. The
companion bridge is a separate process and logs separately, to
`%LOCALAPPDATA%\WebSearchNeo\bridge-daemon.log` on Windows or
`$XDG_DATA_HOME/WebSearchNeo/bridge-daemon.log` — by default
`~/.local/share/WebSearchNeo/bridge-daemon.log` — because two processes rotating one file
collide on Windows. See [The bridge daemon](#the-bridge-daemon).

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
summary and its required parameter names, the observation topics, recipes, pitfalls, limits,
and examples. Optional parameters are not in that document — `web_info(topic="action_schema",
params={"action": "<action or topic>"})` generates the full schema for one of them on demand.
Installing the skill is therefore optional.

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
use, preserves the page's existing login, and opens new tabs into a visible group named
`🟢 AI` — purple, when the companion is the one creating it. It cleans up after itself: a
tab the agent opened is closed when its session closes, and so is any still open when the
server exits, because otherwise every run would leave another tab behind in your browser. A
tab you had open and handed over with `attach_tab` is the one that survives — it was never
the agent's to close, and `close` only detaches from it.

With one deliberate exception, and it is the one that protects your tabs. Tab ids restart
with Chrome, so a session that outlived a Chrome restart names a tab number that now belongs
to somebody else — quite possibly a tab of yours. Nothing at all is sent to the browser for
such a session: not by `close`, not by `close_all`, not by the shutdown hook, not by the
sweep that reclaims session slots. The session is dropped in silence and `close` says so
with `browser_gone: true` and a note, instead of reporting a clean close. Until this landed,
closing one of those sessions sent `tabs.remove` for the old id to the *new* browser, closed
whichever of your tabs had inherited the number, and reported success.

From 1.3.2 it stays out of your way while it does so: agent tabs open in the background, in
a window that is already there, and navigation no longer brings a tab to the front, so you
can keep working in the same Chrome. Chrome throttles a tab nobody is watching almost to a
standstill — and silently drops input into one that has never been shown — so the companion
turns on DevTools focus emulation for the tabs it drives, which restores normal speed and
delivery for as long as it is attached to them.

Ordinary `open`, `attach_tab`, navigation, keyboard input, and screenshots never request
OS/browser foreground focus and never minimize, maximize, restore, or resize a window. The
only foreground opt-in is `web_action` with `{"action":"show","session_id":"..."}`;
use it only when the user explicitly asks to see the controlled tab.

Start/restart the MCP first, then ask it for setup state:

```json
{
  "actions": [{"action": "setup_current_chrome"}]
}
```

That call opens no page and touches no browsing data. It writes the shared bridge secret
into `chrome-extension/bridge-token.js`, compares the bundled build with whatever is
connected, and returns `manual_steps`: the clicks that are left, with the absolute path of
the folder to pick. The result also carries `token_ready` and the `token_file` it wrote.
The one thing it can change is the companion itself: when the connected build is older than
the bundled one it asks that companion to reload itself, and reports the outcome as
`self_update` (`done`, `unsupported`, or `timeout`) with the `replaced_version` it evicted.

Chrome does not let any program add an unpacked extension to a browser you already have
open. The installed set is signed inside `Secure Preferences`, policy installs need a packed
CRX behind an update URL, and a Chrome that started without a DevTools port cannot be given
one afterwards. So these three steps are yours:

1. Open `chrome://extensions`.
2. Switch on **Developer mode** (top-right of that page).
3. Choose **Load unpacked** and select the repository's `chrome-extension` folder.

Leave **Web Search Neo Companion** enabled. Its toolbar popup shows live status, the
controlled-tab count, a GitHub release/version check and repository link, a persistent
on/off switch, **Reconnect**, and **Release tabs**.
Switching it off also detaches every controlled tab. Its toolbar badge reads `ON` once it has
connected and authenticated to the [bridge daemon](#the-bridge-daemon). The daemon, not the
MCP server — it outlives every server process, so the badge says nothing about whether an
agent is running, and stays `ON` when none is. You never create or copy a token by hand —
the server writes it on every start.

Earlier revisions tried to perform those clicks for you through Windows UI Automation. That
code is gone. It depended on the interface language, on which window happened to have focus,
and on a folder picker that the automation backend does not even enumerate.

The bundled companion is version 1.3.6 and declares five permissions: `alarms`, `debugger`,
`storage`, `tabs`, and `tabGroups`. There are no content scripts and no `host_permissions`;
page access comes from `debugger`, which attaches the Chrome DevTools Protocol to the tabs
the agent drives. `alarms` exists because Chrome suspends an idle MV3 service worker after
about thirty seconds: the longer waits in the reconnect backoff have to be handed to
`chrome.alarms` or they would die with the worker and leave the bridge offline until
someone clicked the toolbar icon. Chrome shows no additional user-facing warning for it.

If the companion was already installed from an earlier revision, Chrome keeps running the
old service worker until it is reloaded. From 1.3.1 the server does that for you: a
`setup_current_chrome` call that finds an older connected build asks it to re-read its own
folder from disk. The one upgrade that still costs a click is the one *onto* 1.3.1, because
the build being replaced is the build that has to understand the command — see
[Updating](#updating).

There is no automatic alternative to those three steps, on any platform. If you do not want
an extension at all, use the Selenium modes in section 7 — `profile_mode="temporary"` and
`profile_mode="persistent"` drive their own browser and need no companion.

Verify it with `web_info(topic="browser_status")`; `current_chrome.connected`
should be `true`. List tabs with `web_info(topic="browser_tabs")`, then claim an
existing returned ID with an `attach_tab` action. A normal `open` creates a new
tab in group `🟢 AI` unless another `tab_group` is supplied — including an `open`
on a session that claimed one of your tabs, which takes a tab of its own rather
than navigating yours away, and hands the claimed one back untouched.

The bridge listens only on `127.0.0.1:8765` and accepts the fixed bundled extension
ID. If that port conflicts, change `WEB_SEARCH_NEO_BRIDGE_PORT` for the MCP and the
`BRIDGE_URL` constant in `chrome-extension/service-worker.js`, then reload the
unpacked extension.

Enable the companion in one Chrome profile at a time. The bridge keeps one companion
connection, and the newest authenticated one replaces the previous one, so a second profile
running the companion takes the agent's tabs over from the first.

### The bridge daemon

That listener is not part of the MCP server process. It is a standalone daemon —
`bridge_daemon.py` — which owns the port, holds the one connection to the companion, and
relays commands for any number of local MCP clients. You do not install or configure it: an
MCP server starts it detached as it comes up, if nothing is listening yet, and it then
outlives that server. Nothing is registered for autostart: no service, no scheduled task, no
login item.

Two consequences are worth knowing before you troubleshoot anything:

- The badge stays `ON` between agent calls, and while no agent runs at all. It goes `OFF`
  only when no daemon is listening.
- Two MCP clients can use the same Chrome at once — Claude Code and LM Studio together, for
  instance. Both relay through the one daemon, which also keeps them off each other's tabs:
  it registers every tab an agent opens or claims, refuses an `attach_tab` for a tab another
  agent is already driving, and frees a tab the moment that agent's connection ends, however
  it ends.

You can drive it directly from the clone:

```text
python main.py --bridge
python main.py --bridge --stop
```

The first runs the daemon in the foreground, tied to that terminal and stopped with
`Ctrl+C`; it exits quietly if another daemon already owns the port, because that one serves
just as well. It prints nothing — its output goes to `bridge-daemon.log` either way, so read
that file to follow it. The second asks a running daemon to exit and says whether it found
one; it never starts one.

Left alone with neither a companion nor a client attached, the daemon exits after fifteen
minutes. A connected companion by itself keeps it up indefinitely, which is the ordinary
state of a machine with Chrome open. Set `WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS` to change
that window, or to `0` to keep the port held; the daemon inherits the environment of the
server that spawned it, so setting it in your MCP client configuration is enough.

After `git pull`, a daemon started by the previous revision does not keep serving the old
code: it reports its version during the client handshake, and a server of a different
version stops it and starts a current one. It does that at most twice; if a third daemon
still reports the wrong version, the server gives up and repeats an error naming both
versions instead of restarting the fight. That means another checkout is running against the
same port — stop it, then restart this server.

`web_info(topic="browser_status")` shows the link under `current_chrome.daemon`: `linked`
says whether this server currently holds one, while `version` and `pid` identify the daemon
process from its handshake. `current_chrome.connected` is about the companion itself, so
`linked: true` with `connected: false` means the bridge is up and Chrome is not.

### The shared bridge secret

Loopback is reachable by every process running under your account, so the server and the
companion authenticate each other before any command is executed. Nothing here needs manual
work; it is documented because the file exists on your disk.

- A random 32-byte token is minted on first use — by the daemon as it starts, or by an MCP
  server as it connects — and stored at
  `%LOCALAPPDATA%\WebSearchNeo\bridge-token` on Windows, or at
  `$XDG_DATA_HOME/WebSearchNeo/bridge-token` — by default
  `~/.local/share/WebSearchNeo/bridge-token` — created with `0600` permissions on POSIX.
- The token is machine-local and per-user. Never copy it to another machine or into a
  repository. If it leaks: delete the file, restart the MCP server, and reload the companion.
  Stopping the daemon used to be a required step, because it kept the token it had read at
  startup and would refuse a secret it had never seen; it now re-reads the file before
  calling any token a mismatch, so rotation survives a running daemon.
- A copy is written into `chrome-extension/bridge-token.js` whenever the bridge comes up and
  on every `setup_current_chrome` call. That file is in `.gitignore`; a fresh clone does not
  contain it, and the companion simply retries until it has been written.
- The companion sends the token with a nonce; the daemon answers with
  `HMAC-SHA256(token, nonce)`; each side stops if the other cannot prove it holds the same
  secret. An MCP server proves itself to the daemon the same way before it may relay
  anything, so the relay is not a way around the token.
- A file-based secret does not protect against a malicious process running as the same user,
  which can read it. It closes the case where any local process that grabs the bridge port
  before the server gains DevTools access to your signed-in tabs.
- Since the daemon holds the port between agent calls, the bridge is reachable for as long
  as Chrome keeps the companion attached, rather than only while an agent runs. The
  authentication and the loopback-only bind are unchanged, and a process running as you
  could always read the token file, but the reachable window is longer than it was.
  `python main.py --bridge --stop` closes it at once, and the idle exit closes it fifteen
  minutes after the last companion and client are gone.

## 7. Choose an isolated or managed browser mode

### Disposable browser

Use `profile_mode="temporary"`, which is headless by default. Cookies are discarded when the session closes. Set `headless=false` only when you intentionally want a visible Chrome window.

### Persistent browser

Use `profile_mode="persistent"` and a stable `profile_id`. It is headless by default; set `headless=false` for the one-time visible login. Later sessions with the same profile ID reuse its cookies and local storage.

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

All newly created MCP-owned `temporary` and `persistent` sessions are headless when `headless` is omitted. Set `headless=false` only for an intentionally visible session. For `attach`, the launcher's `-WindowMode` controls the already-running Chrome; attach cannot change its visibility afterward.

Use `profile_mode="auto"` to prefer the current Chrome but fall back to a separate
headless temporary session. The default `current` mode does not fall back silently.

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

Restart or toggle the MCP server after updating. The [bridge daemon](#the-bridge-daemon)
started by the previous revision needs nothing from you: it announces its version to the
restarted server, which stops it and starts a current one, so pulled code cannot be shadowed
by a process that predates it. `python main.py --bridge --stop` is the manual equivalent, and
is worth remembering when a change refuses to take effect.

Chrome never refreshes an unpacked extension by itself, but from 1.3.1 the server no longer
needs you to: a
`setup_current_chrome` call that finds a connected companion older than the bundled one
sends it a `runtime.reload` command, waits for the worker to come back, and reports
`self_update: "done"` with the `replaced_version`. Reloading is also what makes the worker
re-read `chrome-extension/bridge-token.js`, which is how a rotated secret reaches it.

Two cases still need the human:

- **Upgrading from 1.3.0 or older.** The build being replaced is the one that has to
  understand the command, and it does not. From 1.3.0 the server sees the stale companion,
  answers `self_update: "unsupported"`, and returns the Reload steps as `manual_steps`. From
  1.2.0 or older it sees nothing at all — that worker never completes the handshake, so the
  badge stays `OFF`, every `current`-mode action fails with a setup error, and the setup
  call reports the companion as simply not connected. Either way it is the same single fix:
  open `chrome://extensions` and press **Reload** on **Web Search Neo Companion** once.
  Every update after that one applies by itself.
- **A first install.** Nothing has changed there: no program can add an unpacked extension
  to a Chrome that is already open, so the three **Load unpacked** steps in section 6 remain
  yours.

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
| `WEB_SEARCH_NEO_BRIDGE_PORT` | Loopback port of the companion bridge, default `8765`. Shared by the daemon, every MCP server, and the extension. |
| `WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS` | How long the bridge daemon stays up with neither a companion nor a client attached, default `900`. `0` disables the timer. |
| `WEB_SEARCH_NEO_BRIDGE_AUTOSPAWN` | `0`, `false`, or `no` stops a server from starting a daemon; it then works only if one is already running. |
| `WEB_SEARCH_NEO_BRIDGE_CONNECT_TIMEOUT` | How long a server keeps trying to reach a daemon, including one it just started, default `12` seconds. |
| `WEB_SEARCH_NEO_BRIDGE_START_TIMEOUT` | How long startup waits for that first attempt before it continues in the background, default `2` seconds. |
| `WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP` | Accept unencrypted `http://` to public hosts. |
| `WEB_SEARCH_NEO_LEGACY_TOOLS` | Advertise the former wide tool list instead of the two compact tools. |

The `WEB_SEARCH_NEO_BRIDGE_*` variables reach the daemon as well: a spawned one inherits the
environment of the server that started it, and one you run yourself with
`python main.py --bridge` reads them from that shell. A daemon that is already running keeps
the values it started with, so stop it with `--bridge --stop` after changing them.

`WEB_SEARCH_NEO_ALLOW_PLAIN_HTTP` accepts `1`, `true`, `yes`, or `on`. Without it, plain
`http://` to a public host is refused for both page fetches and browser `open`, and each
redirect hop is validated the same way. Loopback, private, and link-local addresses, plus
`localhost` and hosts ending in `.local`, `.localhost`, `.internal`, `.home.arpa`, `.lan`,
`.home`, `.intranet`, `.private`, or `.corp`, are always allowed over plain HTTP — and so is
any single-label host such as `http://nas/`, because a name with no dot cannot exist on the
public DNS and testing it by resolution would cost a lookup per URL and per redirect hop. So
local services such as ComfyUI or a dev server need no configuration change.

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

Call `web_info(topic="browser_status")`. Confirm that **Web Search Neo Companion** is
enabled at `chrome://extensions`, that the toolbar badge shows `ON`, and that
`current_chrome.daemon.linked` is `true` — that last one separates "the server cannot reach
the bridge" from "the bridge cannot reach Chrome". If port 8765 is occupied by something
that is not our daemon, stop that process, or set the same free `WEB_SEARCH_NEO_BRIDGE_PORT`
for the MCP and the extension source before loading it. To take the bridge under your own
control while you reproduce the problem, stop the running one and start it yourself, then
follow `%LOCALAPPDATA%\WebSearchNeo\bridge-daemon.log` — that is where it writes, foreground
or not:

```text
python main.py --bridge --stop
python main.py --bridge
```

A badge that reads `OFF` means this companion is not connected and authenticated. Usually
that is because no daemon is listening — none has been started since the machine booted, the
last one exited after its idle window, or it was stopped. That is not a fault, but it is no
longer what you see between two agent calls.

Two other cases read `OFF` while the port is held by a perfectly healthy daemon, which is
why "start a daemon" is not the automatic answer: the daemon refused this companion's token
(the case above, and the daemon log names it), or a companion running in a *second* Chrome
profile authenticated later and displaced this one — the bridge holds exactly one companion
connection and the newest wins. Keep the companion enabled in one profile at a time.

While it is `OFF`, the worker retries with an
exponential backoff — about 1.5 seconds after the first
failure, doubling to a ceiling of one minute, and resetting to the floor as soon as a
handshake verifies. Starting the browser and reloading the extension reset that schedule;
the popup's **Reconnect** button retries at once, which is how you say "the bridge is up now"
without waiting out the current delay.

### The companion card shows a red “Errors” button

Expected on an idle machine, and harmless. Chrome records every refused connection attempt
to `127.0.0.1:8765` as an extension runtime error, so with no daemon listening the card
collects identical `ERR_CONNECTION_REFUSED` lines. Before 1.3.1 the worker retried every two
seconds and could bury the page under hundreds of them; the backoff above turns that into
roughly one line a minute. An extension cannot suppress the entries — Chrome writes them
itself — so clear them with **Clear all** if they are in your way, and judge the connection
by the badge and by `web_info(topic="browser_status")` instead.

### The companion stopped connecting after an update

This is the expected symptom of an unreloaded extension. From 1.3.1 the server reloads a
stale companion itself, so the check matters for a `git pull` that crosses 1.3.0 or 1.3.1,
or when `setup_current_chrome` answered `self_update: "unsupported"` or `"timeout"`.

1. Open `chrome://extensions` and press **Reload** on **Web Search Neo Companion**. The
   badge should turn `ON` within a few seconds.
2. If it stays `OFF`, open the card's **service worker** link and read its console. `no
   companion token yet, run setup_current_chrome` means `chrome-extension/bridge-token.js`
   is missing: start the MCP server from the clone, which writes it, or send a
   `setup_current_chrome` action. `handshake refused (1008) Companion token mismatch` means
   `chrome-extension/bridge-token.js` holds a different secret than the daemon does, and
   there is essentially one cause: that file belongs to a *clone*, while the secret it is
   compared against is per-user and singular. Chrome is loading one checkout and a server is
   refreshing another, so the copy Chrome reads never catches up. Start the server from the
   very directory **Load unpacked** points at, then reload the extension. Restarting the
   daemon does not help and never did after the daemon learned to re-read the token file:
   it already fetches the current secret from disk before calling anything a mismatch.
3. Check the card's version. It must read 1.3.6; anything older than 1.3.0 cannot
   authenticate at all, and Chrome only picks up the new manifest on reload.
4. `%LOCALAPPDATA%\WebSearchNeo\bridge-daemon.log` records the bridge's side: `Rejected a
   bridge client that did not present the companion token` confirms that something did reach
   the port but could not prove the secret.

The companion keeps retrying a refused handshake on its own — starting at about ten seconds
and slowing to at most two minutes — so once the cause is fixed it reconnects without help.
Clicking the toolbar icon resets that schedule and retries immediately.

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

Capture is armed when the session takes its tab, before the first navigation, so an empty
result is usually a genuinely quiet page rather than a missed one. Two cases are not: a tab
claimed with `attach_tab` is recorded only from the claim onwards, and a busy page evicts its
own history — each stream keeps the newest 500 entries within a 512 KB budget shared with the
other, and `dropped` says how many went that way. Re-open or reload the page and read again.
If the companion was updated in place, reload it at `chrome://extensions` first.

### A session stops working after Chrome was restarted

Expected, and deliberate. Chrome hands out tab ids from a counter that starts again with the
browser, so a session opened before the restart would address whatever tab inherited its
number — possibly one of yours. Such a session is dropped instead, with an error that says
to open the page again; nothing is sent to the new browser on the way out. A companion that
updates itself counts as a new run for the same reason: its reload drops every debugger
attachment those sessions stood on. Open the page again under the same `session_id`.

### Search provider is challenged

Keep `challenge_mode="fallback"` for fast automatic routing. Use `challenge_mode="manual"` only when you want a visible three-minute opportunity to complete the challenge yourself.

### PowerShell blocks virtual-environment activation

You can install and run without activation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

For MCP configuration, keep `command` as `python` when the environment is already available on the client's `PATH`; otherwise see the LM Studio environment note above.
