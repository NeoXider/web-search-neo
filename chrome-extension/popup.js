const enabledInput = document.querySelector("#enabled");
const reconnectButton = document.querySelector("#reconnect");
const releaseButton = document.querySelector("#release-tabs");
const statusNode = document.querySelector("#status");
const tabsNode = document.querySelector("#tabs");
const bridgeNode = document.querySelector("#bridge");
const versionNode = document.querySelector("#version");
const releaseNode = document.querySelector("#release-status");
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

async function send(type, extra = {}) {
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

function render(state) {
  enabledInput.checked = Boolean(state.enabled);
  enabledInput.disabled = false;
  reconnectButton.disabled = !state.enabled;
  releaseButton.disabled = !state.controlled_tabs;
  tabsNode.textContent = String(state.controlled_tabs ?? 0);
  bridgeNode.textContent = String(state.bridge_url || "ws://127.0.0.1:8765")
    .replace(/^ws:\/\//, "");
  versionNode.textContent = state.version || "—";
  if (state.default_bridge_port) portDefaultNode.textContent = String(state.default_bridge_port);
  // Never overwrite a port the user is in the middle of typing.
  if (document.activeElement !== portInput && state.bridge_port) {
    portInput.value = String(state.bridge_port);
  }
  if (state.default_max_sessions) {
    maxSessionsDefaultNode.textContent = String(state.default_max_sessions);
  }
  if (state.max_sessions_ceiling) {
    maxSessionsInput.max = String(state.max_sessions_ceiling);
  }
  if (document.activeElement !== maxSessionsInput && state.max_sessions) {
    maxSessionsInput.value = String(state.max_sessions);
  }
  // "Waiting" with no end in sight reads as broken. The backoff is deliberate
  // and the popup is the only place that can say so.
  if (!state.enabled) {
    nextAttemptNode.textContent = "—";
  } else if (state.connected) {
    nextAttemptNode.textContent = "connected";
  } else {
    nextAttemptNode.textContent = state.next_attempt_at
      ? countdown(state.next_attempt_at)
      : "now";
  }
  if (!state.enabled) {
    statusNode.textContent = "Disabled";
    statusNode.dataset.state = "disabled";
  } else if (state.connected) {
    statusNode.textContent = "Connected to MCP";
    statusNode.dataset.state = "connected";
  } else if (state.connecting) {
    statusNode.textContent = "Connecting…";
    statusNode.dataset.state = "waiting";
  } else {
    statusNode.textContent = "Waiting for MCP";
    statusNode.dataset.state = "waiting";
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
  releaseNode.textContent = "Checking…";
  releaseNode.dataset.state = "";
  checkReleaseButton.disabled = true;
  try {
    const response = await fetch(RELEASES_API, {
      headers: {Accept: "application/vnd.github+json"},
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`GitHub HTTP ${response.status}`);
    const releases = await response.json();
    if (!Array.isArray(releases) || !releases.length) {
      releaseNode.textContent = "None published";
      releaseNode.dataset.state = "error";
      return;
    }
    const latest = releases[0];
    const remote = String(latest.tag_name || latest.name || "").replace(/^v/i, "");
    const local = versionNode.textContent;
    const comparison = compareVersions(local, remote);
    if (comparison < 0) {
      releaseNode.textContent = `Update v${remote}`;
      releaseNode.dataset.state = "update";
    } else if (comparison === 0) {
      releaseNode.textContent = `Up to date (v${remote})`;
      releaseNode.dataset.state = "current";
    } else {
      releaseNode.textContent = `Local ahead of v${remote}`;
      releaseNode.dataset.state = "current";
    }
    releaseNode.title = latest.html_url || `${GITHUB_URL}/releases`;
  } catch (error) {
    releaseNode.textContent = "Check failed";
    releaseNode.dataset.state = "error";
    releaseNode.title = error.message;
  } finally {
    checkReleaseButton.disabled = false;
  }
}

async function refresh() {
  try {
    render(await send("companion.status"));
  } catch (error) {
    statusNode.textContent = "Companion error";
    statusNode.dataset.state = "error";
    messageNode.textContent = error.message;
  }
}

enabledInput.addEventListener("change", async () => {
  enabledInput.disabled = true;
  messageNode.textContent = enabledInput.checked ? "Enabling…" : "Disabling…";
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
  messageNode.textContent = "Reconnecting…";
  try {
    render(await send("companion.reconnect"));
    setTimeout(refresh, 350);
  } catch (error) {
    messageNode.textContent = error.message;
  }
});

releaseButton.addEventListener("click", async () => {
  messageNode.textContent = "Releasing tabs…";
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
  messageNode.textContent = "Applying port…";
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
  messageNode.textContent = "Applying session limit…";
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
openGitHubButton.addEventListener("click", () => chrome.tabs.create({url: GITHUB_URL}));

refresh().then(checkRelease);
setInterval(refresh, 1000);
