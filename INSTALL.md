# Installing Web Search Neo

This guide installs the MCP server from source and connects it to LM Studio or another stdio-compatible MCP client.

## 1. Requirements

- Python 3.10, 3.11, 3.12, or 3.13 available as `python` on `PATH`.
- Git.
- Google Chrome for rendered browser automation. Search and plain HTTP fetch tools do not require Chrome.
- Windows, Linux, or macOS supported by Selenium Manager.

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

## 6. Choose a browser mode

### Visible disposable browser

Use `headless=false` with `profile_mode="temporary"`. A Chrome window opens and you can watch the agent. Cookies are discarded when the session closes.

### Visible persistent browser

Use `headless=false`, `profile_mode="persistent"`, and a stable `profile_id`. Log in once in that MCP-owned window; later sessions with the same profile ID reuse its cookies and local storage.

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

For MCP-owned `temporary` and `persistent` modes, set `headless=false` for a visible window or `headless=true` for background operation. If `headless` is omitted, owned profiles default to background operation and `attach` defaults to visible.

Chrome 136+ requires remote debugging to use a non-default data directory. You cannot safely retrofit attach mode onto an arbitrary normal Chrome window that was started without a DevTools port. The launcher handles both requirements with a separate durable profile.

## 7. Verify the installation

Install development requirements and run the deterministic suite:

```text
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests use a local web server and do not rely on search-engine availability.

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

Restart or toggle the MCP server after updating.

## Troubleshooting

### `python` is not found

Install a supported Python version and enable the installer's “Add Python to PATH” option, then reopen the terminal and MCP client.

### MCP starts and immediately disconnects

Run `python main.py` from the clone directory and inspect `msp_server.log`. Confirm all packages were installed into the same Python environment that the MCP client resolves through `PATH`.

### Chrome does not start

Confirm Google Chrome is installed and can launch normally. The first Selenium Manager run may need network access to resolve a compatible driver.

### Attach mode cannot connect

Open `http://127.0.0.1:9222/json/version` locally. If it is unavailable, restart the managed Chrome launcher and make sure another process is not using the port.

### A canvas/WebGL game loads but cannot be controlled

Call `web_info(topic="game_probe")` first. If it reports a game iframe, reuse its selector as `frame_selector` in the `input` and `render` actions. Call `web_info(topic="screenshot")` after input to verify the visible state change.

To slow a typical canvas/WebGL loop continuously, send `{"action":"render","mode":"throttled","target_fps":10}` through `web_action`. For exact input states use `mode="step"`; each `input` action advances one frame, while the `step` action advances explicitly. Always restore `mode="normal"` when finished. Closing the MCP session also releases held input and restores normal rendering.

Use one `input` action when different inputs must land in the same step-mode frame: each key entry has its own `tap`, `hold`, or `release`, while pointer entries support `hover`, `move`, button actions, absolute coordinates, or `coordinate_mode="delta"`. Send `release_inputs` as an emergency release for every held key and mouse button. If the contract is not in context, call `web_info(topic="action_schema", params={"action":"input"})` first.

### Search provider is challenged

Keep `challenge_mode="fallback"` for fast automatic routing. Use `challenge_mode="manual"` only when you want a visible three-minute opportunity to complete the challenge yourself.

### PowerShell blocks virtual-environment activation

You can install and run without activation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

For MCP configuration, keep `command` as `python` when the environment is already available on the client's `PATH`; otherwise see the LM Studio environment note above.
