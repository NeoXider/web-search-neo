// The popup's Computers tab: this computer's network identity, the switch that
// shares it on the LAN, and every other computer this MCP can drive with node=.
//
// Loaded after popup.js as a classic script, so it reuses that file's send(),
// messageNode, panelNode and PREVIEW. All data arrives through the service
// worker from the local bridge daemon (web_search_neo/node_link.py); every
// string a remote machine reports goes into the page through textContent only.

const selfSummaryButton = document.querySelector("#self-summary");
const selfSummaryText = document.querySelector("#self-summary-text");
const selfNameNode = document.querySelector("#self-name");
const selfLanNode = document.querySelector("#self-lan");
const selfPublicNode = document.querySelector("#self-public");
const selfPlaceNode = document.querySelector("#self-place");
const selfIspNode = document.querySelector("#self-isp");
const shareInput = document.querySelector("#share");
const shareDetailsNode = document.querySelector("#share-details");
const shareAddressNode = document.querySelector("#share-address");
const pairKeyNode = document.querySelector("#pair-key");
const revealPairKeyButton = document.querySelector("#reveal-pair-key");
const rotatePairKeyButton = document.querySelector("#rotate-pair-key");
const scanNodesButton = document.querySelector("#scan-nodes");
const nodeListNode = document.querySelector("#node-list");
const nodesEmptyNode = document.querySelector("#nodes-empty");
const nodesCountNode = document.querySelector("#nodes-count");
const addNodePanel = document.querySelector("#add-node-panel");
const nodeNameInput = document.querySelector("#node-name");
const nodeAddressInput = document.querySelector("#node-address");
const nodeKeyInput = document.querySelector("#node-key");
const addNodeButton = document.querySelector("#add-node");

const NODES_REFRESH_MS = 15000;
const CONFIRM_WINDOW_MS = 4000;
let pairKey = "";
let pairKeyRevealed = false;
let loadingNodes = null;
let armed = {action: "", until: 0};

const STATE_WORDS = {online: "online", unpaired: "needs its pairing key", offline: "offline"};

function countryChip(code) {
  const chip = document.createElement("span");
  chip.className = "cc";
  chip.textContent = String(code || "").toUpperCase().slice(0, 2);
  return chip;
}

function placeText(publicInfo) {
  if (!publicInfo || typeof publicInfo !== "object") return "";
  return [publicInfo.city, publicInfo.region, publicInfo.country]
    .map(value => String(value || "").trim())
    .filter((value, index, all) => value && all.indexOf(value) === index)
    .join(", ");
}

function mapLink(publicInfo) {
  const lat = Number(publicInfo?.latitude);
  const lon = Number(publicInfo?.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return "";
  return `https://www.openstreetmap.org/?mlat=${lat.toFixed(4)}&mlon=${lon.toFixed(4)}#map=10/${lat.toFixed(4)}/${lon.toFixed(4)}`;
}

function publicLine(publicInfo) {
  if (!publicInfo) return "-";
  if (publicInfo.disabled) return "lookup off";
  if (publicInfo.error) return "unavailable";
  return String(publicInfo.ip || "-");
}

// Hidden, but the last group stays readable so two screens can be compared at a glance.
function maskedKey(value) {
  const groups = value.split("-");
  return groups.length > 1 ? `••••-••••-…-${groups[groups.length - 1]}` : "••••••••";
}

function renderPairKey() {
  pairKeyNode.textContent = pairKey ? (pairKeyRevealed ? pairKey : maskedKey(pairKey)) : "-";
  pairKeyNode.dataset.revealed = String(pairKeyRevealed);
}

function renderSelf(me) {
  const publicInfo = me?.public || null;
  const lan = Array.isArray(me?.lan_ips) ? me.lan_ips : [];
  selfNameNode.textContent = String(me?.name || "-");
  selfLanNode.textContent = String(me?.primary_ip || lan[0] || "-");
  selfLanNode.title = lan.length > 1 ? `All addresses: ${lan.join(", ")}` : "";
  selfPublicNode.textContent = publicLine(publicInfo);
  selfPublicNode.title = publicInfo?.error ? String(publicInfo.error) : "";
  const place = placeText(publicInfo);
  selfPlaceNode.replaceChildren?.();
  if (publicInfo?.country_code) selfPlaceNode.append?.(countryChip(publicInfo.country_code));
  selfPlaceNode.append?.(document.createTextNode(place || "-"));
  const link = mapLink(publicInfo);
  if (link) selfPlaceNode.href = link;
  else selfPlaceNode.removeAttribute?.("href");
  selfPlaceNode.title = link ? "Approximate location of this public IP - open the map" : "";
  selfIspNode.textContent = String(publicInfo?.isp || (publicInfo?.disabled ? "WEB_SEARCH_NEO_GEOIP=0" : "-"));
  const pieces = [me?.primary_ip, publicInfo?.ip, place].filter(Boolean);
  selfSummaryText.textContent = pieces.length
    ? `${publicInfo?.country_code ? `${String(publicInfo.country_code).toUpperCase()} · ` : ""}${pieces.join(" · ")}`
    : "This computer's address is not known yet";
}

function renderSharing(sharing) {
  const running = Boolean(sharing?.running);
  shareInput.checked = Boolean(sharing?.enabled || running);
  shareInput.disabled = false;
  shareDetailsNode.hidden = !running;
  const reach = Array.isArray(sharing?.reach_at) ? sharing.reach_at : [];
  shareAddressNode.textContent = String(reach[0] || "-");
  shareAddressNode.title = reach.length > 1 ? `Also: ${reach.slice(1).join(", ")}` : "";
  if (sharing?.pair_key) pairKey = String(sharing.pair_key);
  renderPairKey();
}

function nodeButton(label, title, path, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-btn mini";
  button.setAttribute("aria-label", label);
  button.title = title;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "12");
  svg.setAttribute("height", "12");
  svg.setAttribute("aria-hidden", "true");
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", path);
  shape.setAttribute("fill", "currentColor");
  svg.append(shape);
  button.append(svg);
  button.addEventListener("click", handler);
  return button;
}

const ICON_SEND = "M1 8 15 1l-3 14-4-5-4 3 1-5 7-5-8 4Z";
const ICON_PAIR = "M6 9a3 3 0 1 1 2.8-4H15v2h-1v2h-2V7H8.8A3 3 0 0 1 6 9Zm0-2a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z";
const ICON_REMOVE = "M4 4l8 8M12 4l-8 8";

const seenNodes = new Set();

function nodeRow(row) {
  const item = document.createElement("li");
  item.className = "node-row";
  if (!seenNodes.has(row.address)) item.classList.add("fresh");
  seenNodes.add(row.address);
  const state = ["online", "unpaired", "offline"].includes(row.state) ? row.state : "offline";
  item.dataset.state = state;
  const dot = document.createElement("span");
  dot.className = "node-dot";
  dot.setAttribute("aria-hidden", "true");
  const name = document.createElement("span");
  name.className = "node-name";
  const reported = row.reported_name && row.reported_name !== row.name ? ` (${row.reported_name})` : "";
  name.textContent = `${row.name}${reported}`;
  const meta = document.createElement("span");
  meta.className = "node-meta";
  if (row.public?.country_code) meta.append(countryChip(row.public.country_code));
  const facts = [row.primary_ip || String(row.address || "").split(":")[0]];
  if (state === "online") {
    const place = placeText(row.public);
    if (place) facts.push(place);
    if (Number.isFinite(row.latency_ms)) facts.push(`${row.latency_ms} ms`);
    const browser = row.browser || {};
    facts.push(browser.companion_connected ? "Chrome linked" : "no Chrome link");
  } else {
    facts.push(STATE_WORDS[state]);
  }
  meta.append(document.createTextNode(facts.filter(Boolean).join(" · ")));
  meta.title = row.error ? String(row.error) : `${row.address} (${row.source})`;
  const actions = document.createElement("span");
  actions.className = "node-actions";
  if (state === "online") {
    actions.append(nodeButton(`Open the current tab on ${row.name}`, "Send this tab there", ICON_SEND,
      () => sendTab(row.name)));
  }
  if (state === "unpaired") {
    actions.append(nodeButton(`Pair with ${row.name}: enter its key`, "Pair", ICON_PAIR,
      () => startPairing(row)));
  }
  if (row.source === "saved") {
    const remove = nodeButton(`Forget ${row.name}`, "Forget (click twice)", ICON_REMOVE,
      () => removeNode(row.name, remove));
    remove.querySelector("path")?.setAttribute("fill", "none");
    remove.querySelector("path")?.setAttribute("stroke", "currentColor");
    remove.querySelector("path")?.setAttribute("stroke-width", "2");
    actions.append(remove);
  }
  item.append(dot, name, actions, meta);
  return item;
}

function renderNodes(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const online = list.filter(row => row.state === "online").length;
  nodesCountNode.textContent = String(online);
  nodesCountNode.dataset.online = String(online > 0);
  nodesCountNode.title = `${online} of ${list.length} other computer(s) online`;
  nodesEmptyNode.hidden = list.length > 0;
  nodeListNode.replaceChildren(...list.map(nodeRow));
}

function renderOverview(result) {
  renderSelf(result?.self);
  renderSharing(result?.sharing);
  renderNodes(result?.nodes);
}

async function loadNodes(options = {}) {
  if (loadingNodes && !options.discover) return loadingNodes;
  loadingNodes = (async () => {
    try {
      const result = await send("companion.nodes", options);
      renderOverview(result);
      return result;
    } catch (error) {
      selfSummaryText.textContent = "Network details need the bridge (start an agent here)";
      shareInput.disabled = true;
      if (options.loud) messageNode.textContent = error.message;
      return null;
    } finally {
      loadingNodes = null;
    }
  })();
  return loadingNodes;
}

// A destructive click needs a second click within a few seconds - no modal dialogs.
function confirmTwice(action, button, prompt) {
  const now = Date.now();
  if (armed.action === action && now < armed.until) {
    armed = {action: "", until: 0};
    return true;
  }
  armed = {action, until: now + CONFIRM_WINDOW_MS};
  messageNode.textContent = prompt;
  button?.focus?.();
  return false;
}

async function busy(button, work) {
  button.disabled = true;
  button.setAttribute?.("aria-busy", "true");
  try {
    return await work();
  } finally {
    button.disabled = false;
    button.removeAttribute?.("aria-busy");
  }
}

async function sendTab(name) {
  messageNode.textContent = `Opening this tab on ${name}...`;
  try {
    const answer = await send("companion.nodeSendTab", {node: name});
    messageNode.textContent = answer?.sent
      ? `Opened on ${name}; the tab stays there for the person or agent at that computer.`
      : `${name} could not open it: ${answer?.error || "unknown error"}`;
  } catch (error) {
    messageNode.textContent = error.message;
  }
}

function startPairing(row) {
  addNodePanel.open = true;
  nodeNameInput.value = row.reported_name || row.name || "";
  nodeAddressInput.value = row.address || "";
  nodeKeyInput.value = "";
  nodeKeyInput.focus?.();
  messageNode.textContent = `Enter the pairing key shown on ${row.name} (its Computers tab, or wsn --node --key).`;
}

async function removeNode(name, button) {
  if (!confirmTwice(`remove:${name}`, button, `Click again to forget ${name}.`)) return;
  try {
    await send("companion.nodeRemove", {name});
    messageNode.textContent = `Forgot ${name}.`;
    await loadNodes();
  } catch (error) {
    messageNode.textContent = error.message;
  }
}

async function copyFrom(button) {
  const source = document.querySelector(`#${button.dataset.copy}`);
  const text = button.dataset.copy === "pair-key" ? pairKey : String(source?.textContent || "");
  if (!text || text === "-") return;
  try {
    await navigator.clipboard.writeText(text);
    button.dataset.copied = "true";
    setTimeout(() => { button.dataset.copied = "false"; }, 1200);
    messageNode.textContent = "Copied.";
  } catch (error) {
    messageNode.textContent = `Copy failed: ${error.message}`;
  }
}

function bindComputers() {
  for (const button of document.querySelectorAll?.("[data-copy]") || []) {
    button.addEventListener("click", () => copyFrom(button));
  }
  selfSummaryButton.addEventListener("click", () => window.__wsn?.selectTab?.("tab-computers", true));
  revealPairKeyButton.addEventListener("click", () => {
    pairKeyRevealed = !pairKeyRevealed;
    renderPairKey();
  });
  rotatePairKeyButton.addEventListener("click", async () => {
    if (!confirmTwice("rotate", rotatePairKeyButton,
      "Click again for a new pairing key; every paired computer must be given the new one.")) return;
    try {
      renderSharing(await busy(rotatePairKeyButton, () => send("companion.nodeRotateKey")));
      messageNode.textContent = "New pairing key made; the node restarted with it.";
    } catch (error) {
      messageNode.textContent = error.message;
    }
  });
  shareInput.addEventListener("change", async () => {
    const enabled = shareInput.checked;
    shareInput.disabled = true;
    messageNode.textContent = enabled ? "Starting the node on this computer..." : "Stopping the node...";
    try {
      renderSharing(await send("companion.nodeShare", {enabled}));
      messageNode.textContent = enabled
        ? "Shared. Other computers add this one with the address and pairing key below."
        : "This computer is no longer shared.";
    } catch (error) {
      shareInput.checked = !enabled;
      shareInput.disabled = false;
      messageNode.textContent = error.message;
    }
  });
  scanNodesButton.addEventListener("click", async () => {
    messageNode.textContent = "Scanning this network...";
    const result = await busy(scanNodesButton, () => loadNodes({discover: true, loud: true}));
    if (result) {
      const found = Number(result.discovered) || 0;
      messageNode.textContent = found
        ? `Scan done: ${found} answer(s) on this network.`
        : "Scan done: no other computer answered. Is wsn --node running there, UDP 8767 open?";
    }
  });
  addNodeButton.addEventListener("click", async () => {
    const address = nodeAddressInput.value.trim();
    if (!address) {
      messageNode.textContent = "Enter the other computer's address, e.g. 192.168.1.115.";
      nodeAddressInput.focus?.();
      return;
    }
    messageNode.textContent = `Adding ${address}...`;
    try {
      const answer = await busy(addNodeButton, () => send("companion.nodeAdd", {
        name: nodeNameInput.value.trim(), address, key: nodeKeyInput.value.trim(),
      }));
      const status = answer?.status || {};
      nodeKeyInput.value = "";
      messageNode.textContent = status.state === "online"
        ? `${answer.saved?.name} is paired and online.`
        : `${answer.saved?.name} saved, ${STATE_WORDS[status.state] || "not reachable"}${status.error ? `: ${status.error}` : "."}`;
      if (status.state === "online") addNodePanel.open = false;
      await loadNodes();
    } catch (error) {
      messageNode.textContent = error.message;
    }
  });
  for (const input of [nodeNameInput, nodeAddressInput, nodeKeyInput]) {
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") addNodeButton.click();
    });
  }
  window.addEventListener?.("wsn-tab", event => {
    if (event.detail === "tab-computers") loadNodes();
  });
}

/* Read-only preview data for scripts/companion-widget-preview.html. */
function previewComputers(type, extra) {
  const me = {
    name: "studio-pc", primary_ip: "192.168.1.154", lan_ips: ["192.168.1.154", "10.0.236.10"],
    public: {ip: "203.0.113.24", country: "Example Land", country_code: "EX", city: "Sample City",
      region: "North", isp: "Example Telecom", latitude: 56.84, longitude: 60.6},
  };
  const nodes = [
    {name: "office-pc", address: "192.168.1.115:8766", source: "saved", state: "online",
      primary_ip: "192.168.1.115", latency_ms: 4, browser: {companion_connected: true},
      public: me.public},
    {name: "laptop", address: "192.168.1.120:8766", source: "discovered", state: "unpaired"},
    {name: "home-server", address: "10.8.0.5:8766", source: "saved", state: "offline"},
  ];
  const sharing = {enabled: true, running: true, port: 8766, pair_key: "1f2e-3d4c-5b6a-7980-a1b2-c3d4-e5f6-0718",
    reach_at: ["192.168.1.154:8766", "10.0.236.10:8766"]};
  if (type === "companion.nodes") {
    return Promise.resolve({self: me, sharing, nodes, discovered: extra.discover ? 2 : null});
  }
  if (type === "companion.nodeShare") return Promise.resolve({...sharing, running: Boolean(extra.enabled)});
  if (type === "companion.nodeRotateKey") return Promise.resolve(sharing);
  if (type === "companion.nodeSendTab") return Promise.resolve({sent: true, node: extra.node});
  if (type === "companion.nodeAdd") {
    return Promise.resolve({saved: {name: extra.name || extra.address}, status: {state: "online"}});
  }
  return Promise.resolve({removed: true});
}

if (typeof window !== "undefined" && PREVIEW) window.__wsnPreviewNodes = previewComputers;
bindComputers();
loadNodes();
setInterval(() => {
  if (!document.hidden) loadNodes();
}, NODES_REFRESH_MS);
