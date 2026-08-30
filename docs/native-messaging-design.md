# Native Messaging transport design

## Current transport

`web_search_neo.bridge_daemon.BridgeDaemon` owns a WebSocket listener on
`127.0.0.1:8765` (`WEB_SEARCH_NEO_BRIDGE_PORT` may override the port). It is a detached
`python main.py --bridge` process, normally spawned by `ChromeBridge`. It accepts the fixed
extension origin and origin-less local Python clients, holds one extension connection and
many MCP-client connections, routes commands/results, and owns tab claims.

The extension service worker opens `ws://127.0.0.1:<port>`. Its first frame is:

```json
{"type":"hello","protocol":1,"token":"<64 hex>","nonce":"<32 hex>","browser":{"name":"Chrome","extension_version":"...","browser_run":"...","max_sessions":8}}
```

An MCP client sends the same hello with `role: "client"`, its version, PID, and program.
The daemon validates the raw token and replies with `hello_ack` containing
`HMAC-SHA256(token, nonce)`. Both peers verify that proof before accepting traffic. Commands
are `{type:"command", id, method, params}` and extension replies are
`{type:"result", id, result|error}`. Local clients also use control messages; the daemon
broadcasts extension and claim state.

`web_search_neo/bridge_auth.py` creates `%LOCALAPPDATA%\WebSearchNeo\bridge-token`, then mirrors it into
`chrome-extension/bridge-token.js`. `setup_current_chrome()`, the daemon, and the default
client ensure that copy exists. The worker fetches it without caching on every attempt.
This protects against a random loopback peer, but not another process running as the same
user. A process that binds 8765 first also sees the extension's raw token before proving
anything.

The worker keeps a verified socket alive with 20-second pings. Reconnect uses persisted,
jittered exponential backoff: 1.5--60 seconds for transport failure and 10--120 seconds for
authentication failure. Long waits use `chrome.alarms` so MV3 worker eviction does not stop
recovery.

## Target architecture

Use `com.neoxider.web_search_neo` as the stable native host name. Chrome starts one native
broker for `chrome.runtime.connectNative()`. The extension-to-broker leg is Chrome's framed
stdio channel. The broker exposes a per-user Windows named pipe, for example
`\\.\pipe\web-search-neo.bridge.v2.<sid-hash>`, to the independently launched MCP processes.
It must not open a TCP port in native mode.

Keep the existing broker responsibilities: one current extension, multiple MCP clients,
request routing, browser-run state, tab claims, status broadcasts, and reclaim after a
reconnect. Split those responsibilities from WebSocket-specific `send`, receive, and close
operations. Use the same JSON command/result/control shapes on both transports.

Native mode still starts with a transport-neutral hello so version, `browser_run`, and
`max_sessions` reach the broker:

```json
{"type":"hello","protocol":2,"transport":"native","browser":{"name":"Chrome","extension_version":"...","browser_run":"...","max_sessions":8}}
```

The broker answers with `hello_ack`, protocol, transport, broker version, PID, and instance.
There is no token, nonce, or HMAC on this leg: Chrome has selected the registered host and
enforced its `allowed_origins`. Protocol 1 and its authentication remain unchanged for the
WebSocket fallback.

The named pipe needs a current-user-only ACL. MCP clients should retain their existing
client hello during rollout, including the token, so mixed versions fail closed. This is
compatibility and defence against other accounts, not protection from malicious code under
the same account.

## Required changes

### Native host and installer

- Add a host entry point, preferably `web_search_neo/native_host.py` behind a packaged
  `web-search-neo-native-host.exe`. Set stdin/stdout to binary mode on Windows. Read and
  write one UTF-8 JSON object as a native-endian 32-bit length followed by exactly that many
  bytes; flush every reply. Send logs only to stderr or a file.
- Make that process the broker. Its Chrome side reads/writes native frames; its local side
  accepts length-prefixed JSON on the named pipe. On stdin EOF it closes the pipe, fails
  in-flight routes, and exits. A successor waits for the old per-user mutex/pipe owner to
  leave before accepting clients.
- Refactor `web_search_neo/bridge_daemon.py` so routing, claims, browser state, and client
  control are transport-independent. Keep its WebSocket server as the legacy adapter.
- Refactor `web_search_neo/chrome_bridge.py` to prefer the named-pipe client, then use the
  existing WebSocket client when native mode is unavailable. Preserve the public
  `ChromeBridge` API and status fields; add `transport: "native"|"websocket"` and native-host
  diagnostics. Never have both transports active for one client epoch.
- Add `scripts/install_native_host.py` and an uninstall/check path. Install the executable
  and its dependencies in a stable per-user directory, not a checkout venv that may move.
  Write the host manifest exactly here:

  `%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\com.neoxider.web_search_neo.json`

  On Windows that directory alone is not discovery. Also set the default value of
  `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.neoxider.web_search_neo` to the
  manifest's absolute path. No elevation is required. The host JSON is:

  ```json
  {
    "name": "com.neoxider.web_search_neo",
    "description": "Web Search Neo native bridge",
    "path": "C:\\Users\\<user>\\AppData\\Local\\WebSearchNeo\\native-host\\web-search-neo-native-host.exe",
    "type": "stdio",
    "allowed_origins": [
      "chrome-extension://ndbmcjhbdjpefojkoljacjhammmcigao/"
    ]
  }
  ```

  Host names may contain only lowercase letters, digits, underscores, and dots; no leading,
  trailing, or consecutive dots. `path` must resolve after installation, and Windows
  backslashes must be JSON-escaped. The existing extension `key` must remain unchanged,
  because it fixes the extension ID used by `allowed_origins`. The host should also reject
  an unexpected caller-origin argument as defence in depth.
- Change `web_search_neo/chrome_bootstrap.py` and setup output to install/check the host,
  registry value, executable, and allowed extension ID. Token publication remains only while
  WebSocket fallback is supported.

Chrome's framing, manifest, registration, and lifecycle rules are documented in
[Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging).

### Extension

- Add `"nativeMessaging"` to `chrome-extension/manifest.json` permissions. Keep the current
  `key` verbatim.
- In `service-worker.js`, call
  `chrome.runtime.connectNative("com.neoxider.web_search_neo")`. Use
  `port.postMessage(message)`, `port.onMessage.addListener(...)`, and
  `port.onDisconnect.addListener(...)`; inspect `chrome.runtime.lastError` in the disconnect
  handler for installation or launch failures.
- Put WebSocket and `chrome.runtime.Port` behind one small transport adapter. Liveness must
  use adapter identity/epoch rather than `readyState === WebSocket.OPEN`; results must still
  return only through the connection that received the command.
- Send the protocol-2 metadata hello immediately after `connectNative`; accept commands only
  after its acknowledgement. Remove token loading, WebCrypto HMAC, WebSocket pings, port
  setting, and alarm-backed retry only from the native path. Keep the existing logic intact
  in the fallback adapter.
- Prefer native once per reconnect cycle. Fall back only for host-not-found, launch failure,
  or a bounded native handshake timeout. If a native connection later becomes available,
  finish/fail the current WebSocket epoch before switching; do not duplicate commands.
- Update popup status to show `Native Messaging`, `WebSocket fallback`, or a host-install
  error. Keep the port control only when fallback is selected.

## Test impact

- `tests/test_chrome_bridge.py` is the main break. Its real WebSocket listener/dial/spawn,
  origin and token handshake, HMAC, port, daemon replacement, reconnect, worker `FakeSocket`,
  routing, in-flight failure, browser-run, and claim tests need native counterparts. Keep the
  transport-neutral command/CDP cases. Add `tests/test_native_messaging.py` for binary framing,
  partial reads, malformed/oversize frames, stdout purity, origin checks, native hello,
  named-pipe reconnect, multi-client routing, claims, and `chrome.runtime.Port` mocks. Retain
  the old cases as fallback coverage.
- `tests/test_chrome_bootstrap.py` currently asserts both token files and host/port status.
  Add `tests/test_native_host_install.py` for the exact JSON, escaping, stable extension ID,
  LocalAppData path, HKCU value, idempotent install, stale executable, and uninstall. Keep
  token tests only for fallback.
- `tests/test_popup_widget.py` assumes `bridge_url`, `bridge_port`, and 8765, and its worker
  harness assumes WebSocket. Add native/fallback/install-error states and keep the no-secret
  assertion.
- `tests/test_lifecycle_defects.py` uses an unused TCP port to prove the unavailable error.
  Add missing-host, host-exit, named-pipe-drop, and in-flight native-disconnect cases.
- `tests/test_parallel_agent_defects.py` extracts `max_sessions` and `browser_run` from the
  WebSocket hello. Run the same assertions through the protocol-2 native hello.
- `tests/test_close_tabs.py` and `tests/test_without_chrome.py` contain host/port status
  fixtures. Update them if the compatibility schema changes. `tests/test_session_identity.py`
  may keep its transport-neutral fake if `browser` and broker status remain stable.

## Rollout

1. Extract the transport-neutral broker and add framing/installer tests. Ship no behavior
   change; WebSocket remains the only transport.
2. Ship the native host, named-pipe client, extension permission, and service-worker adapter
   behind an opt-in setting/environment flag. Native failure falls back to protocol-1
   WebSocket. Report the selected transport and exact native failure in setup/status.
3. Make native preferred after setup installs and verifies the host. Keep WebSocket fallback
   for at least one release and cover mixed old/new extension, host, daemon, and MCP versions.
4. After adoption, disable fallback by default. Keep an explicit emergency flag for one more
   release.
5. Remove port UI/configuration, `bridge-token.js`, extension token publication, HMAC code,
   WebSocket daemon startup, and the `websockets` dependency only after supported upgrades no
   longer require fallback.

## Risks

- Native Messaging authenticates the extension-to-host edge, not arbitrary local MCP clients.
  A same-user process that can use the named pipe remains inside the browser-control boundary.
  A current-user ACL and token stop other accounts and accidents, but stronger same-user
  isolation would require a separately enforceable client identity or a different product
  boundary.
- Keeping fallback preserves the original token theft and port-squatting risk and creates a
  downgrade path. Status must make fallback visible; automatic fallback must be narrow and
  bounded.
- Chrome permits only 1 MiB per host-to-extension message and 64 MiB per
  extension-to-host message. The current WebSocket cap is 64 MiB in both directions. Reject or
  chunk any host-to-extension command that can exceed 1 MiB.
- One stray byte on stdout corrupts framing. Windows text-mode newline conversion also
  corrupts it. Tests must exercise binary mode, partial I/O, EOF, and malformed lengths.
- MV3 worker suspension, `Port` disconnect, Chrome shutdown, duplicate host launches, and
  host upgrades can lose broker memory. Browser-run identity and MCP-side claim reassertion
  must remain authoritative.
- A moved interpreter, venv, checkout, or executable leaves a valid registry entry pointing
  nowhere. Installation must use a stable path and diagnostics must distinguish missing
  manifest, bad registry value, launch failure, crash, and protocol mismatch.
- Changing the extension `key` changes its ID and silently invalidates `allowed_origins`.
  Enterprise policy or non-Chrome browsers may also require separate registration paths.
