const panelNode = document.querySelector("#panel");
const enabledInput = document.querySelector("#enabled");
const presenceInput = document.querySelector("#presence");
const reconnectButton = document.querySelector("#reconnect");
const releaseButton = document.querySelector("#release-tabs");
const statusNode = document.querySelector("#status");
const tabsNode = document.querySelector("#tabs");
const sessionsValueNode = document.querySelector("#max-sessions-value");
const sessionsUsedNode = document.querySelector("#sessions-used");
const meterFillNode = document.querySelector("#meter-fill");
const bridgeNode = document.querySelector("#bridge");
const versionNode = document.querySelector("#version");
const releaseNode = document.querySelector("#release-status");
const releaseChipNode = document.querySelector("#release-chip");
const messageNode = document.querySelector("#message");
const checkReleaseButton = document.querySelector("#check-release");
const openGitHubButton = document.querySelector("#open-github");
const updateButton = document.querySelector("#update-companion");
const updateHintNode = document.querySelector("#update-hint");
const portInput = document.querySelector("#port");
const savePortButton = document.querySelector("#save-port");
const resetPortButton = document.querySelector("#reset-port");
const portDefaultNode = document.querySelector("#port-default");
const maxSessionsInput = document.querySelector("#max-sessions");
const saveMaxSessionsButton = document.querySelector("#save-max-sessions");
const resetMaxSessionsButton = document.querySelector("#reset-max-sessions");
const maxSessionsDefaultNode = document.querySelector("#max-sessions-default");
const nextAttemptNode = document.querySelector("#next-attempt");
const agentListNode = document.querySelector("#agent-tab-list");
const agentEmptyNode = document.querySelector("#agent-tabs-empty");
const agentCountNode = document.querySelector("#agent-tabs-count");
const takeOverButton = document.querySelector("#take-over");
const takeOverLabelNode = document.querySelector("#take-over-label");
// Overview / Computers / Settings: one panel visible at a time, remembered per browser.
const TAB_IDS = ["tab-overview", "tab-computers", "tab-settings"];
const tabButtons = TAB_IDS.map(id => document.querySelector(`#${id}`));
const tabPanels = {
  "tab-overview": document.querySelector("#panel-overview"),
  "tab-computers": document.querySelector("#panel-computers"),
  "tab-settings": document.querySelector("#panel-settings"),
};
const TAB_STORAGE = "wsn-popup-tab";
const AFTERGLOW_MS = 5 * 60 * 1000; // agent-activity.js RECENT_MS

const GITHUB_URL = "https://github.com/NeoXider/web-search-neo";
const RELEASES_API = "https://api.github.com/repos/NeoXider/web-search-neo/releases?per_page=1";
// Where the newest published release lives, once the check has read it: the
// Update button opens that page rather than a guess at it.
let latestReleaseUrl = GITHUB_URL;

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
  // Take over outranks the link state: whatever the socket does, agents are on hold.
  if (state.paused && state.enabled !== false) return "paused";
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
  paused: "Paused - you have control",
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
  paused: () => "agents wait until you press Resume",
  loading: () => "...",
};

function render(state) {
  const view = deriveState(state);
  panelNode.dataset.state = view;
  enabledInput.checked = Boolean(state.enabled);
  enabledInput.disabled = false;
  // An older worker sends no presence flag; the switch has always meant on.
  presenceInput.checked = state.presence !== false;
  presenceInput.disabled = false;
  reconnectButton.disabled = !state.enabled;
  // A worker that answered is alive again: undo the restart-only mode.
  reconnectButton.textContent = "Reconnect";
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
  const ceiling = Number(state.max_sessions_ceiling) || 999;
  // 0 is "no limit"; a worker that sent nothing shows a dash.
  const known = state.max_sessions !== undefined && state.max_sessions !== null;
  const limit = known ? Number(state.max_sessions) || 0 : null;
  const used = Number(state.controlled_tabs) || 0;
  if (sessionsUsedNode) sessionsUsedNode.textContent = String(used);
  sessionsValueNode.textContent = limit === null ? "-" : limit === 0 ? "∞" : String(limit);
  maxSessionsInput.max = String(ceiling);
  if (document.activeElement !== maxSessionsInput && limit !== null) {
    maxSessionsInput.value = String(limit);
  }
  // How full the allowance is: tabs held against the limit, so a cap about to
  // bite is seen before an agent is refused. No limit leaves the bar empty.
  const fill = limit ? Math.max(0, Math.min(100, (used / limit) * 100)) : 0;
  meterFillNode.style.width = `${fill.toFixed(1)}%`;
  meterFillNode.dataset.level = fill >= 90 ? "full" : fill >= 70 ? "high" : "ok";
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
  renderTakeOver(state);
  nextAttemptNode.title = view === "error"
    ? "The bridge refused this companion's credentials; run setup_current_chrome"
    : "Next connection attempt";
}

function renderTakeOver(state) {
  if (!takeOverButton || !takeOverLabelNode) return; // a host page without the button
  const paused = Boolean(state.paused);
  takeOverButton.dataset.paused = String(paused);
  takeOverButton.disabled = state.enabled === false;
  takeOverLabelNode.textContent = paused ? "Resume" : "Take over";
  takeOverButton.title = paused
    ? "Resume: hand the browser back to the agents (Alt+Shift+P)"
    : "Take over: pause every agent in this browser (Alt+Shift+P)";
}

function selectTab(id, focus = false) {
  const chosen = TAB_IDS.includes(id) ? id : TAB_IDS[0];
  for (const button of tabButtons) {
    const selected = button?.id === chosen;
    button?.setAttribute?.("aria-selected", String(selected));
    button?.setAttribute?.("tabindex", selected ? "0" : "-1");
    if (selected && focus) button?.focus?.();
  }
  for (const [tabId, panel] of Object.entries(tabPanels)) {
    if (panel) panel.hidden = tabId !== chosen;
  }
  panelNode.dataset.tab = chosen;
  try {
    localStorage.setItem(TAB_STORAGE, chosen);
  } catch (error) {
    // No storage (preview harness, tests): the choice simply is not remembered.
  }
  if (typeof window !== "undefined") window.dispatchEvent?.(new CustomEvent("wsn-tab", {detail: chosen}));
}

function savedTab() {
  try {
    return localStorage.getItem(TAB_STORAGE) || TAB_IDS[0];
  } catch (error) {
    return TAB_IDS[0];
  }
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
    latestReleaseUrl = String(latest.html_url || GITHUB_URL);
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

// Chrome's answer when the extension's service worker is not running at all:
// the popup is alive, the worker behind it is not, and every message is lost.
// Nothing inside the worker can fix that - it is not there to run - but the
// popup can restart the whole extension, which is exactly what pressing
// Reload on chrome://extensions does.
const WORKER_GONE = /Receiving end does not exist|Could not establish connection|No SW/i;
let workerGone = false;

function markWorkerGone(error) {
  workerGone = true;
  panelNode.dataset.state = "error";
  statusNode.dataset.state = "error";
  statusNode.textContent = "Companion stopped";
  // The switches cannot reach a dead worker; Reconnect is the one control
  // that still works, so it is the one left enabled.
  enabledInput.disabled = true;
  presenceInput.disabled = true;
  reconnectButton.disabled = false;
  reconnectButton.textContent = "Restart companion";
  messageNode.textContent =
    `The companion's background worker is not running (${error.message}). ` +
    "Press Restart companion to reload it.";
}

function restartCompanion() {
  messageNode.textContent = "Restarting the companion...";
  // Closes this popup; the extension comes back with a fresh worker, which
  // reconnects to the bridge on its own.
  chrome.runtime.reload();
}

async function refresh() {
  try {
    render(await send("companion.status"));
    workerGone = false;
  } catch (error) {
    if (!PREVIEW && WORKER_GONE.test(error.message || "")) {
      markWorkerGone(error);
      return;
    }
    panelNode.dataset.state = "error";
    statusNode.dataset.state = "error";
    statusNode.textContent = "Companion error";
    messageNode.textContent = error.message;
  }
}

// "Agent tabs": every tab an agent acted on in the last five minutes, as the
// service worker tracks it (chrome-extension/agent-badges.js). A row focuses
// its tab. Text only goes through textContent: tab titles are page-controlled.
// Rows are rebuilt every second; only a tab not listed before slides in.
const seenAgentTabs = new Set();

function agentRow(row) {
  const item = document.createElement("li");
  if (!seenAgentTabs.has(row.tab_id)) item.className = "fresh";
  seenAgentTabs.add(row.tab_id);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-row";
  button.dataset.phase = row.phase === "active" ? "active" : "recent";
  button.title = row.url || row.title || "";
  const claim = row.claim ? ` · claimed by ${row.claim}` : "";
  const meta = `${row.agent} · ${row.status_text}${claim}`;
  button.setAttribute("aria-label", `Focus ${row.title}: ${meta}`);
  const dot = document.createElement("span");
  dot.className = "agent-dot";
  dot.setAttribute("aria-hidden", "true");
  const title = document.createElement("span");
  title.className = "agent-title";
  title.textContent = String(row.title ?? "");
  const details = document.createElement("span");
  details.className = "agent-meta";
  details.textContent = meta;
  // How much of the five-minute afterglow is left: full while working, draining after.
  const life = document.createElement("span");
  life.className = "agent-life";
  life.setAttribute("aria-hidden", "true");
  const left = Math.max(0, Math.min(1, 1 - (Number(row.age_ms) || 0) / AFTERGLOW_MS));
  life.style.width = `${(row.phase === "active" ? 1 : left) * 100}%`;
  life.style.animationDelay = `-${Date.now() % 1400}ms`; // the sweep survives the rebuild
  button.append(dot, title, details, life);
  button.addEventListener("click", () => focusAgentTab(row.tab_id));
  item.append(button);
  return item;
}

function renderAgentTabs(rows) {
  const list = Array.isArray(rows) ? rows : [];
  // The whole widget breathes while any agent is working right now.
  panelNode.dataset.busy = String(list.some(row => row.phase === "active"));
  agentCountNode.textContent = String(list.length);
  agentEmptyNode.hidden = list.length > 0;
  if (typeof document.createElement !== "function" || !agentListNode.replaceChildren) return;
  agentListNode.replaceChildren(...list.map(agentRow));
}

async function refreshAgentTabs() {
  try {
    renderAgentTabs((await send("companion.agentTabs"))?.tabs);
  } catch (error) {
    // An older worker, or one restarting: the status line already reports it.
  }
}

async function focusAgentTab(tabId) {
  try {
    await send("companion.focusAgentTab", {tab_id: tabId});
  } catch (error) {
    messageNode.textContent = error.message;
    refreshAgentTabs();
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

presenceInput.addEventListener("change", async () => {
  presenceInput.disabled = true;
  messageNode.textContent = presenceInput.checked ? "Showing presence..." : "Hiding presence...";
  try {
    const state = await send("companion.setPresence", {presence: presenceInput.checked});
    render(state);
    messageNode.textContent = state.presence !== false
      ? "Agent presence shown in driven tabs."
      : "Agent presence hidden; tabs stay clean.";
  } catch (error) {
    messageNode.textContent = error.message;
    await refresh();
  }
});

reconnectButton.addEventListener("click", async () => {
  if (workerGone) {
    restartCompanion();
    return;
  }
  messageNode.textContent = "Reconnecting...";
  try {
    render(await send("companion.reconnect"));
    setTimeout(refresh, 350);
  } catch (error) {
    // The worker died between the last status read and this click.
    if (!PREVIEW && WORKER_GONE.test(error.message || "")) {
      markWorkerGone(error);
      return;
    }
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
    const shown = Number(state.max_sessions) === 0 ? "no limit" : state.max_sessions;
    messageNode.textContent =
      `Parallel sessions set to ${shown}; the MCP server picks it up on reconnect.`;
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

tabButtons.forEach((button, index) => {
  button?.addEventListener("click", () => selectTab(button.id));
  // Arrow keys move along the tab strip, as the tablist role promises.
  button?.addEventListener("keydown", event => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault?.();
    selectTab(TAB_IDS[(index + step + TAB_IDS.length) % TAB_IDS.length], true);
  });
});
selectTab(savedTab());

takeOverButton?.addEventListener("click", async () => {
  const pause = takeOverButton.dataset.paused !== "true";
  takeOverButton.disabled = true;
  messageNode.textContent = pause ? "Taking over..." : "Resuming...";
  try {
    await send("companion.setPaused", {paused: pause});
    await refresh();
    messageNode.textContent = pause
      ? "You have control: agents are paused and told to wait. Resume hands it back."
      : "Agents resumed.";
  } catch (error) {
    messageNode.textContent = error.message;
    takeOverButton.disabled = false;
  }
});

checkReleaseButton.addEventListener("click", checkRelease);
openGitHubButton.addEventListener("click", () => {
  if (PREVIEW) return;
  chrome.tabs.create({url: GITHUB_URL});
});

// Update: pulling and reinstalling is a shell step this page cannot run, but the
// half people actually forget is pressing Reload on the extension card - that is
// what loads the pulled files into the running companion. So the button names the
// command it cannot run, opens the release that explains it, and does the half it
// can: reload the companion onto whatever is on disk now.
updateButton.addEventListener("click", async () => {
  updateButton.disabled = true;
  messageNode.textContent = "Updating...";
  try {
    await checkRelease();
    const remote = releaseNode.textContent.replace(/^update v/, "");
    const behind = releaseNode.dataset.state === "update";
    if (behind) {
      updateHintNode.hidden = false;
      updateHintNode.textContent =
        `v${remote} is published. In the folder this extension was loaded from, run ` +
        `"git pull --ff-only" and reinstall the package; this button then reloads the ` +
        `companion onto those files. An agent on this machine can do that pull itself: ` +
        `the update_companion action.`;
      if (!PREVIEW) chrome.tabs.create({url: latestReleaseUrl});
    }
    await send("companion.reloadExtension");
    messageNode.textContent = behind
      ? `Reloading the companion for v${remote}...`
      : "Companion reloaded onto the files in this checkout.";
  } catch (error) {
    messageNode.textContent = error.message;
    updateButton.disabled = false;
  }
});

// Test and preview hook: the same render pipeline the popup uses, reachable
// without Chrome. Production popups never read this property.
if (typeof window !== "undefined") {
  window.__wsn = {render, deriveState, countdown, compareVersions, renderAgentTabs, selectTab};
}

/* Read-only preview driver: deterministic fake state, no browser APIs.
   Declared before the startup branch below so the preview boot can never
   touch a not-yet-initialized binding (temporal dead zone). */

let previewTabs = 2;
let previewCap = 30;
let previewCeiling = 999;
let previewPort = 8765;
let previewVersion = "1.27.0";
let previewUpdate = null;
let previewEnabled = true;
let previewPresence = true;
let previewAgents = 3;
let previewPaused = false;

// Sample rows in the exact shape the worker's companion.agentTabs returns.
function previewAgentTabs() {
  const rows = [
    {tab_id: 101, title: "Pull request #42 - review", agent: "claude#4120", phase: "active",
     status_text: "active now", claim: "main.py#4120", url: "https://example.com/pr/42", age_ms: 4000},
    {tab_id: 102, title: "chrome://extensions", agent: "codex#5230", phase: "recent",
     status_text: "2 min ago", claim: null, url: "chrome://extensions", age_ms: 120000},
    {tab_id: 103, title: "Quarterly report.pdf", agent: "main.py#6008", phase: "recent",
     status_text: "4 min ago", claim: "main.py#6008", url: "file:///report.pdf", age_ms: 240000},
  ];
  return {tabs: previewEnabled ? rows.slice(0, previewAgents) : [], now: Date.now()};
}

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
    default_max_sessions: 30,
    max_sessions_ceiling: previewCeiling,
    presence: previewPresence,
    next_attempt_at: 0,
    version: previewVersion,
    failure_kind: null,
    paused: previewPaused,
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
  if (type === "companion.setPresence") {
    previewPresence = extra.presence !== false;
    return Promise.resolve(currentPreviewStatus());
  }
  if (type === "companion.reconnect") return Promise.resolve(currentPreviewStatus());
  if (type === "companion.setPaused") {
    previewPaused = Boolean(extra.paused);
    return Promise.resolve({paused: previewPaused, paused_since: previewPaused ? Date.now() : 0});
  }
  // The Computers tab's fake network lives beside its renderer (popup-computers.js).
  if (String(type).startsWith("companion.node") && typeof window.__wsnPreviewNodes === "function") {
    return window.__wsnPreviewNodes(type, extra);
  }
  if (type === "companion.agentTabs") return Promise.resolve(previewAgentTabs());
  if (type === "companion.focusAgentTab") return Promise.resolve({focused: extra.tab_id});
  if (type === "companion.releaseTabs") {
    const detached = previewTabs;
    previewTabs = 0;
    return Promise.resolve({...currentPreviewStatus(), detached_tabs: detached});
  }
  if (type === "companion.reloadExtension") {
    // The preview harness has no extension to reload, so it records the intent.
    return Promise.resolve({reloading: true, version: previewVersion});
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
    if (!Number.isFinite(count) || count < 0 || count > previewCeiling) {
      return Promise.reject(
        new Error(`Parallel sessions must be a whole number from 0 (no limit) to ${previewCeiling}.`),
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
  const cap = Number.parseInt(params.get("cap"), 10);
  previewCap = Number.isFinite(cap) ? cap : 30;
  previewCeiling = Number.parseInt(params.get("ceiling"), 10) || 999;
  previewPort = Number.parseInt(params.get("port"), 10) || 8765;
  previewVersion = params.get("ver") || "1.27.0";
  const agents = Number.parseInt(params.get("agents"), 10);
  previewAgents = Number.isFinite(agents) ? Math.max(0, agents) : 3;
  previewPaused = params.get("paused") === "1";
  refresh().then(checkRelease).then(refreshAgentTabs);
  setInterval(() => refresh().then(refreshAgentTabs), 1000);
}

if (PREVIEW) {
  startPreview(String(PREVIEW));
} else {
  // A real action popup without a preview query renders only the live
  // service-worker state; nothing here is simulated.
  refresh().then(checkRelease).then(refreshAgentTabs);
  setInterval(() => refresh().then(refreshAgentTabs), 1000); // the documented one-second live refresh
}
