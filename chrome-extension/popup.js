const panelNode = document.querySelector("#panel");
const enabledInput = document.querySelector("#enabled");
const reconnectButton = document.querySelector("#reconnect");
const releaseButton = document.querySelector("#release-tabs");
const statusNode = document.querySelector("#status");
const tabsNode = document.querySelector("#tabs");
const sessionsValueNode = document.querySelector("#max-sessions-value");
const sessionsCeilingNode = document.querySelector("#max-sessions-ceiling");
const meterFillNode = document.querySelector("#meter-fill");
const bridgeNode = document.querySelector("#bridge");
const versionNode = document.querySelector("#version");
const releaseNode = document.querySelector("#release-status");
const releaseChipNode = document.querySelector("#release-chip");
const messageNode = document.querySelector("#message");
const checkReleaseButton = document.querySelector("#check-release");
const openGitHubButton = document.querySelector("#open-github");
const portInput = document.querySelector("#port");
const savePortButton = document.querySelector("#save-port");
const resetPortButton = document.querySelector("#reset-port");
const portDefaultNode = document.querySelector("#port-default");
const maxSessionsInput = document.querySelector("#max-sessions");
const saveMaxSessionsButton = document.querySelector("#save-max-sessions");
const resetMaxSessionsButton = document.querySelector("#reset-max-sessions");
const maxSessionsDefaultNode = document.querySelector("#max-sessions-default");
const nextAttemptNode = document.querySelector("#next-attempt");

const GITHUB_URL = "https://github.com/NeoXider/web-search-neo";
const RELEASES_API = "https://api.github.com/repos/NeoXider/web-search-neo/releases?per_page=1";

// Read-only preview mode for scripts/companion-widget-preview.html: the same
// production markup and code render simulated state, touching no chrome.* API,
// no network, and no secret. See that harness file for the parameters.
const PREVIEW_QUERY =
  typeof location !== "undefined" &&
  new URLSearchParams(location.search).get("wsn-preview");
const PREVIEW =
  PREVIEW_QUERY &&
  (location.protocol === "file:" ||
    (location.protocol === "http:" && ["127.0.0.1", "localhost"].includes(location.hostname)))
    ? PREVIEW_QUERY
    : null;

async function send(type, extra = {}) {
  if (PREVIEW) return previewSend(type, extra);
  const response = await chrome.runtime.sendMessage({type, ...extra});
  if (response?.error) throw new Error(response.error);
  return response;
}

function countdown(timestamp) {
  const remaining = Number(timestamp) - Date.now();
  if (!Number.isFinite(remaining) || remaining <= 0) return "now";
  if (remaining < 60000) return `in ${Math.ceil(remaining / 1000)}s`;
  return `in ${Math.ceil(remaining / 60000)}m`;
}

// One word per connection state, preferring what the service worker derived
// from its own live socket and falling back to the reported flags with an
// older worker. A closed port is "waiting", never "connected": the widget
// only repeats a verified handshake, it does not guess one.
function deriveState(state) {
  if (typeof state.state === "string") {
    return state.enabled === false ? "disabled" : state.state;
  }
  if (!state.enabled) return "disabled";
  if (state.connected) return "connected";
  if (state.connecting) return "connecting";
  if (state.failure_kind === "auth") return "error";
  return "waiting";
}

const STATE_TEXT = {
  connected: "Connected",
  connecting: "Connecting",
  waiting: "Waiting",
  error: "Blocked",
  disabled: "Disabled",
  loading: "Starting...",
};

// The sub-line that tells a deliberate backoff from a stalled bridge: a
// waiting connection says when it will retry.
const STATE_SUBTEXT = {
  connected: () => "",
  connecting: () => "",
  waiting: state => (state.next_attempt_at ? `retry ${countdown(state.next_attempt_at)}` : "now"),
  error: () => "setup_current_chrome",
  disabled: () => "switch on to connect",
  loading: () => "...",
};

function render(state) {
  const view = deriveState(state);
  panelNode.dataset.state = view;
  enabledInput.checked = Boolean(state.enabled);
  enabledInput.disabled = false;
  reconnectButton.disabled = !state.enabled;
  releaseButton.disabled = !state.controlled_tabs;
  tabsNode.textContent = String(state.controlled_tabs ?? 0);
  bridgeNode.textContent = String(state.bridge_url || "ws://127.0.0.1:8765")
    .replace(/^ws:\/\//, "");
  versionNode.textContent = state.version || "-";
  if (state.default_bridge_port) portDefaultNode.textContent = String(state.default_bridge_port);
  // Never overwrite a port the user is in the middle of typing.
  if (document.activeElement !== portInput && state.bridge_port) {
    portInput.value = String(state.bridge_port);
  }
  if (state.default_max_sessions) {
    maxSessionsDefaultNode.textContent = String(state.default_max_sessions);
  }
  const ceiling = Number(state.max_sessions_ceiling) || 64;
  const limit = Number(state.max_sessions) || 0;
  sessionsCeilingNode.textContent = String(ceiling);
  sessionsValueNode.textContent = limit ? String(limit) : "-";
  maxSessionsInput.max = String(ceiling);
  if (document.activeElement !== maxSessionsInput && limit) {
    maxSessionsInput.value = String(limit);
  }
  // Capacity animation: the configured parallel-session limit against its
  // hard ceiling, so the number is felt as well as read.
  const fill = ceiling ? Math.max(0, Math.min(100, (limit / ceiling) * 100)) : 0;
  meterFillNode.style.width = `${fill.toFixed(1)}%`;
  panelNode.dataset.state = view;
  statusNode.dataset.state = view;
  statusNode.textContent = STATE_TEXT[view] || STATE_TEXT.loading;
  // A waiting connection says when it will try again - that is what tells a
  // deliberate backoff from a stalled bridge. A connected one has nothing to
  // count down to and echoes where the bridge is instead.
  nextAttemptNode.textContent =
    view === "connected" ? bridgeNode.textContent
    : view === "connecting" || view === "disabled" ? "-"
    : STATE_SUBTEXT[view](state);
  nextAttemptNode.title = view === "error"
    ? "The bridge refused this companion's credentials; run setup_current_chrome"
    : "Next connection attempt";
}

function versionParts(value) {
  return String(value || "")
    .replace(/^v/i, "")
    .split(/[.+-]/, 3)
    .map(part => Number.parseInt(part, 10) || 0);
}

function compareVersions(left, right) {
  const a = versionParts(left);
  const b = versionParts(right);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    if ((a[index] || 0) !== (b[index] || 0)) return (a[index] || 0) - (b[index] || 0);
  }
  return 0;
}

async function checkRelease() {
  releaseNode.textContent = "checking";
  releaseNode.dataset.state = "";
  releaseChipNode.dataset.state = "";
  checkReleaseButton.disabled = true;
  try {
    let releases;
    if (PREVIEW) {
      releases = previewReleases();
    } else {
      const response = await fetch(RELEASES_API, {
        headers: {Accept: "application/vnd.github+json"},
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`GitHub HTTP ${response.status}`);
      releases = await response.json();
    }
    if (!Array.isArray(releases) || !releases.length) {
      markRelease("none published", "error", `${GITHUB_URL}/releases`);
      return;
    }
    const latest = releases[0];
    const remote = String(latest.tag_name || latest.name || "").replace(/^v/i, "");
    const local = versionNode.textContent;
    const comparison = compareVersions(local, remote);
    if (comparison < 0) {
      markRelease(`update v${remote}`, "update", latest.html_url);
    } else if (comparison === 0) {
      markRelease("latest", "current", latest.html_url);
    } else {
      markRelease(`ahead of v${remote}`, "current", latest.html_url);
    }
  } catch (error) {
    markRelease("check failed", "error", error.message);
  } finally {
    checkReleaseButton.disabled = false;
  }
}

function markRelease(text, state, title) {
  releaseNode.textContent = text;
  releaseNode.dataset.state = state;
  releaseChipNode.dataset.state = state;
  releaseChipNode.title = title || "Latest GitHub release vs this build";
}

async function refresh() {
  try {
    render(await send("companion.status"));
  } catch (error) {
    panelNode.dataset.state = "error";
    statusNode.dataset.state = "error";
    statusNode.textContent = "Companion error";
    messageNode.textContent = error.message;
  }
}

enabledInput.addEventListener("change", async () => {
  enabledInput.disabled = true;
  messageNode.textContent = enabledInput.checked ? "Enabling..." : "Disabling...";
  try {
    const state = await send("companion.setEnabled", {enabled: enabledInput.checked});
    render(state);
    messageNode.textContent = state.enabled
      ? "Companion enabled."
      : `Companion disabled; released ${state.detached_tabs || 0} tab(s).`;
  } catch (error) {
    messageNode.textContent = error.message;
    await refresh();
  }
});

reconnectButton.addEventListener("click", async () => {
  messageNode.textContent = "Reconnecting...";
  try {
    render(await send("companion.reconnect"));
    setTimeout(refresh, 350);
  } catch (error) {
    messageNode.textContent = error.message;
  }
});

releaseButton.addEventListener("click", async () => {
  messageNode.textContent = "Releasing tabs...";
  try {
    const state = await send("companion.releaseTabs");
    render(state);
    messageNode.textContent = `Released ${state.detached_tabs || 0} tab(s).`;
  } catch (error) {
    messageNode.textContent = error.message;
  }
});

async function applyPort(value) {
  savePortButton.disabled = true;
  resetPortButton.disabled = true;
  messageNode.textContent = "Applying port...";
  try {
    const state = await send("companion.setBridgePort", {port: value});
    render(state);
    messageNode.textContent = `Bridge port set to ${state.bridge_port}; reconnecting.`;
  } catch (error) {
    messageNode.textContent = error.message;
    await refresh();
  } finally {
    savePortButton.disabled = false;
    resetPortButton.disabled = false;
  }
}

async function applyMaxSessions(value) {
  saveMaxSessionsButton.disabled = true;
  resetMaxSessionsButton.disabled = true;
  messageNode.textContent = "Applying session limit...";
  try {
    const state = await send("companion.setMaxSessions", {max_sessions: value});
    render(state);
    messageNode.textContent =
      `Parallel sessions set to ${state.max_sessions}; the MCP server picks it up on reconnect.`;
  } catch (error) {
    messageNode.textContent = error.message;
    await refresh();
  } finally {
    saveMaxSessionsButton.disabled = false;
    resetMaxSessionsButton.disabled = false;
  }
}

saveMaxSessionsButton.addEventListener("click", () => applyMaxSessions(maxSessionsInput.value));
resetMaxSessionsButton.addEventListener("click", () => {
  maxSessionsInput.value = maxSessionsDefaultNode.textContent;
  return applyMaxSessions(maxSessionsDefaultNode.textContent);
});
maxSessionsInput.addEventListener("keydown", event => {
  if (event.key === "Enter") applyMaxSessions(maxSessionsInput.value);
});

savePortButton.addEventListener("click", () => applyPort(portInput.value));
resetPortButton.addEventListener("click", () => {
  portInput.value = portDefaultNode.textContent;
  return applyPort(portDefaultNode.textContent);
});
portInput.addEventListener("keydown", event => {
  if (event.key === "Enter") applyPort(portInput.value);
});

checkReleaseButton.addEventListener("click", checkRelease);
openGitHubButton.addEventListener("click", () => {
  if (PREVIEW) return;
  chrome.tabs.create({url: GITHUB_URL});
});

// Test and preview hook: the same render pipeline the popup uses, reachable
// without Chrome. Production popups never read this property.
if (typeof window !== "undefined") {
  window.__wsn = {render, deriveState, countdown, compareVersions};
}

/* Read-only preview driver: deterministic fake state, no browser APIs.
   Declared before the startup branch below so the preview boot can never
   touch a not-yet-initialized binding (temporal dead zone). */

let previewTabs = 2;
let previewCap = 8;
let previewCeiling = 64;
let previewPort = 8765;
let previewVersion = "1.9.1";
let previewUpdate = null;
let previewEnabled = true;

function baseState() {
  return {
    enabled: previewEnabled,
    connected: false,
    connecting: false,
    controlled_tabs: previewEnabled ? previewTabs : 0,
    bridge_url: `ws://127.0.0.1:${previewPort}`,
    bridge_port: previewPort,
    default_bridge_port: 8765,
    max_sessions: previewCap,
    default_max_sessions: 8,
    max_sessions_ceiling: previewCeiling,
    next_attempt_at: 0,
    version: previewVersion,
    failure_kind: null,
  };
}

function currentPreviewStatus() {
  const named = String(PREVIEW);
  const state = baseState();
  if (previewEnabled && named !== "disabled") {
    if (named === "waiting") {
      state.next_attempt_at = Date.now() + 23000;
    } else if (named === "error") {
      state.failure_kind = "auth";
    } else if (named === "connecting") {
      state.connecting = true;
    } else {
      state.connected = true;
    }
  }
  return state;
}

function previewSend(type, extra = {}) {
  if (type === "companion.status") return Promise.resolve(currentPreviewStatus());
  if (type === "companion.setEnabled") {
    previewEnabled = Boolean(extra.enabled);
    return Promise.resolve({
      ...currentPreviewStatus(),
      detached_tabs: previewEnabled ? 0 : previewTabs,
    });
  }
  if (type === "companion.reconnect") return Promise.resolve(currentPreviewStatus());
  if (type === "companion.releaseTabs") {
    const detached = previewTabs;
    previewTabs = 0;
    return Promise.resolve({...currentPreviewStatus(), detached_tabs: detached});
  }
  if (type === "companion.setBridgePort") {
    const port = Number.parseInt(extra.port, 10);
    if (!Number.isFinite(port) || port < 1024 || port > 65535) {
      return Promise.reject(new Error("Bridge port must be between 1024 and 65535."));
    }
    previewPort = port;
    return Promise.resolve(currentPreviewStatus());
  }
  if (type === "companion.setMaxSessions") {
    const count = Number.parseInt(extra.max_sessions, 10);
    if (!Number.isFinite(count) || count < 1 || count > previewCeiling) {
      return Promise.reject(
        new Error(`Parallel sessions must be a whole number between 1 and ${previewCeiling}.`),
      );
    }
    previewCap = count;
    return Promise.resolve(currentPreviewStatus());
  }
  return Promise.resolve(currentPreviewStatus());
}

function previewReleases() {
  if (previewUpdate === null) {
    const wantsUpdate = new URLSearchParams(location.search).get("update") === "1";
    previewUpdate = wantsUpdate
      ? [{tag_name: "v999.0.0", html_url: `${GITHUB_URL}/releases`}]
      : [{tag_name: `v${previewVersion}`, html_url: `${GITHUB_URL}/releases`}];
  }
  return previewUpdate;
}

function startPreview() {
  const params = new URLSearchParams(location.search);
  previewTabs = Number.parseInt(params.get("tabs"), 10) || 2;
  previewCap = Number.parseInt(params.get("cap"), 10) || 8;
  previewCeiling = Number.parseInt(params.get("ceiling"), 10) || 64;
  previewPort = Number.parseInt(params.get("port"), 10) || 8765;
  previewVersion = params.get("ver") || "1.9.1";
  refresh().then(checkRelease);
  setInterval(refresh, 1000);
}

if (PREVIEW) {
  startPreview(String(PREVIEW));
} else {
  // A real action popup without a preview query renders only the live
  // service-worker state; nothing here is simulated.
  refresh().then(checkRelease);
  setInterval(refresh, 1000); // the documented one-second live refresh
}
