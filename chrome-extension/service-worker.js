import {
  applyFailed,
  applyFinished,
  applyRedirect,
  applyResponse,
  browserEntry,
  clearBuffer,
  collectEvents,
  consoleEntry,
  createBuffer,
  exceptionEntry,
  isTextualMime,
  legacyConsole,
  navigationEntry,
  networkRow,
  nextSeq,
  pushEntry,
  sameFrameUrl,
  trackPending,
} from "./events.js";

const BRIDGE_URL = "ws://127.0.0.1:8765";
const PROTOCOL_VERSION = 1;
const RECONNECT_MS = 2000;
const KEEPALIVE_MS = 20000;
const DEBUGGER_VERSION = "1.3";
const STATE_KEY = "bridge_state";
const BODY_LIMIT = 512 * 1024;
const NETWORK_BUFFER_LIMITS = {
  maxTotalBufferSize: 10485760,
  maxResourceBufferSize: 5242880,
  maxPostDataSize: 65536,
};

let socket = null;
let reconnectTimer = null;
let keepaliveTimer = null;
const attachedTabs = new Set();
// `${tabId}:${url}` -> {tabId, url, sessionId, targetId}
const childSessions = new Map();
// tabId -> {domains: Set, startedAt, includeHeaders}
const capture = new Map();
// tabId -> ring buffer from events.js
const buffers = new Map();
let groupQueue = Promise.resolve();
let restorePromise = null;
let persistQueue = Promise.resolve();

function serializeTab(tab, groupsById = new Map()) {
  const group = groupsById.get(tab.groupId);
  return {
    id: tab.id,
    window_id: tab.windowId,
    index: tab.index,
    active: Boolean(tab.active),
    pinned: Boolean(tab.pinned),
    audible: Boolean(tab.audible),
    status: tab.status || null,
    title: tab.title || "",
    url: tab.url || tab.pendingUrl || "",
    group_id: tab.groupId,
    group: group?.title || null,
  };
}

async function groupMap() {
  const groups = await chrome.tabGroups.query({});
  return new Map(groups.map(group => [group.id, group]));
}

async function waitForTab(tabId, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tab;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return chrome.tabs.get(tabId);
}

async function ensureGroupUnlocked(tabId, title) {
  if (!title) return null;
  const tab = await chrome.tabs.get(tabId);
  const groups = await chrome.tabGroups.query({windowId: tab.windowId});
  let group = groups.find(item => item.title === title);
  let groupId;
  if (group) {
    groupId = await chrome.tabs.group({groupId: group.id, tabIds: [tabId]});
  } else {
    groupId = await chrome.tabs.group({tabIds: [tabId]});
    group = await chrome.tabGroups.update(groupId, {
      title,
      color: "purple",
      collapsed: false,
    });
  }
  if (group?.collapsed) await chrome.tabGroups.update(groupId, {collapsed: false});
  return groupId;
}

async function ensureGroup(tabId, title) {
  const operation = groupQueue.catch(() => {}).then(() => ensureGroupUnlocked(tabId, title));
  groupQueue = operation;
  return operation;
}

// MV3 evicts the worker and takes module state with it, so attachments and
// child sessions are mirrored into chrome.storage.session.
async function restoreState() {
  if (!restorePromise) {
    restorePromise = (async () => {
      const stored = (await chrome.storage.session.get(STATE_KEY))?.[STATE_KEY] || {};
      for (const tabId of stored.attachedTabs || []) attachedTabs.add(Number(tabId));
      for (const entry of stored.childSessions || []) {
        childSessions.set(`${entry.tabId}:${entry.url}`, {...entry, tabId: Number(entry.tabId)});
      }
      for (const item of stored.capture || []) {
        capture.set(Number(item.tabId), {
          domains: new Set(item.domains || []),
          startedAt: Number(item.startedAt) || 0,
          includeHeaders: Boolean(item.includeHeaders),
        });
      }
    })().catch(error => console.warn("bridge: state restore failed", error));
  }
  return restorePromise;
}

function persistState() {
  persistQueue = persistQueue
    .catch(() => {})
    .then(() =>
      chrome.storage.session.set({
        [STATE_KEY]: {
          attachedTabs: [...attachedTabs],
          childSessions: [...childSessions.values()],
          capture: [...capture.entries()].map(([tabId, state]) => ({
            tabId,
            domains: [...state.domains],
            startedAt: state.startedAt,
            includeHeaders: state.includeHeaders,
          })),
        },
      }),
    )
    .catch(error => console.warn("bridge: state persist failed", error));
  return persistQueue;
}

function bufferFor(tabId) {
  let buffer = buffers.get(tabId);
  if (!buffer) {
    buffer = createBuffer(Date.now());
    buffers.set(tabId, buffer);
  }
  return buffer;
}

function captureFor(tabId) {
  let state = capture.get(tabId);
  if (!state) {
    state = {domains: new Set(), startedAt: 0, includeHeaders: false};
    capture.set(tabId, state);
  }
  return state;
}

function capturesNetwork(tabId) {
  return Boolean(capture.get(tabId)?.domains.has("network"));
}

function forgetTab(tabId, dropBuffer = true) {
  attachedTabs.delete(tabId);
  if (dropBuffer) buffers.delete(tabId);
  capture.delete(tabId);
  for (const [key, entry] of childSessions) {
    if (entry.tabId === tabId) childSessions.delete(key);
  }
  persistState();
}

async function sendSafe(tabId, method, params = {}) {
  try {
    return await chrome.debugger.sendCommand({tabId}, method, params);
  } catch (error) {
    console.warn(`bridge: ${method} failed on tab ${tabId}`, error);
    return null;
  }
}

async function ensureDebugger(tabId) {
  await restoreState();
  bufferFor(tabId);
  if (attachedTabs.has(tabId)) return;
  try {
    await chrome.debugger.attach({tabId}, DEBUGGER_VERSION);
  } catch (error) {
    // A restarted worker forgets the attachment while Chrome keeps it alive.
    if (!/already attached/i.test(String(error?.message || error))) throw error;
  }
  attachedTabs.add(tabId);
  await chrome.debugger.sendCommand({tabId}, "Runtime.enable");
  await sendSafe(tabId, "Page.enable");
  await sendSafe(tabId, "Log.enable");
  await sendSafe(tabId, "Target.setAutoAttach", {
    autoAttach: true,
    waitForDebuggerOnStart: false,
    flatten: true,
  });
  persistState();
}

async function cdpSend({tabId, sessionId, method, params = {}}) {
  await ensureDebugger(tabId);
  const target = sessionId ? {tabId, sessionId} : {tabId};
  return chrome.debugger.sendCommand(target, method, params);
}

function findChildSession(tabId, url) {
  for (const entry of childSessions.values()) {
    if (entry.tabId === tabId && sameFrameUrl(entry.url, url)) return entry;
  }
  return null;
}

async function resolveFrame(tabId, selector) {
  await ensureDebugger(tabId);
  const evaluated = await cdpSend({
    tabId,
    method: "Runtime.evaluate",
    params: {
      expression: `(()=>{const f=document.querySelector(${JSON.stringify(selector)});return f?{src:f.src||'',sameOrigin:(()=>{try{return !!f.contentDocument}catch(_){return false}})()}:null})()`,
      returnByValue: true,
    },
  });
  const info = evaluated?.result?.value;
  if (!info) throw new Error(`No iframe matches selector: ${selector}`);
  if (info.sameOrigin) return {sameOrigin: true, sessionId: null, url: info.src};

  const cached = findChildSession(tabId, info.src);
  if (cached) return {sameOrigin: false, sessionId: cached.sessionId, url: info.src};

  const targets = await cdpSend({tabId, method: "Target.getTargets"});
  const target = (targets.targetInfos || []).find(item =>
    item.type === "iframe" && sameFrameUrl(item.url, info.src)
  );
  if (!target) throw new Error(`Cross-origin iframe target is unavailable: ${info.src}`);
  const attached = await cdpSend({
    tabId,
    method: "Target.attachToTarget",
    params: {targetId: target.targetId, flatten: true},
  });
  childSessions.set(`${tabId}:${info.src}`, {
    tabId,
    url: info.src,
    sessionId: attached.sessionId,
    targetId: target.targetId,
  });
  persistState();
  return {sameOrigin: false, sessionId: attached.sessionId, url: info.src};
}

const commands = {
  async "tabs.list"() {
    const [tabs, groups] = await Promise.all([chrome.tabs.query({}), groupMap()]);
    return tabs
      .filter(tab => /^https?:|^file:|^about:blank/.test(tab.url || tab.pendingUrl || ""))
      .map(tab => serializeTab(tab, groups));
  },

  async "tabs.get"({tabId}) {
    return serializeTab(await chrome.tabs.get(Number(tabId)), await groupMap());
  },

  async "tabs.create"({url = "about:blank", group = "AI"}) {
    const windows = await chrome.windows.getAll({windowTypes: ["normal"]});
    const focused = windows.find(window => window.focused) || windows[0];
    const tab = await chrome.tabs.create({
      url,
      active: true,
      ...(focused ? {windowId: focused.id} : {}),
    });
    await ensureGroup(tab.id, group);
    return serializeTab(await waitForTab(tab.id, 10000), await groupMap());
  },

  async "tabs.navigate"({tabId, url}) {
    const numericId = Number(tabId);
    for (const [key, entry] of childSessions) {
      if (entry.tabId === numericId) childSessions.delete(key);
    }
    persistState();
    await chrome.tabs.update(numericId, {url, active: true});
    return serializeTab(await waitForTab(numericId), await groupMap());
  },

  async "tabs.activate"({tabId}) {
    const tab = await chrome.tabs.update(Number(tabId), {active: true});
    await chrome.windows.update(tab.windowId, {focused: true});
    return serializeTab(tab, await groupMap());
  },

  async "tabs.remove"({tabId}) {
    const numericId = Number(tabId);
    await restoreState();
    if (attachedTabs.has(numericId)) {
      await chrome.debugger.detach({tabId: numericId}).catch(() => {});
    }
    forgetTab(numericId);
    try {
      await chrome.tabs.remove(numericId);
    } catch (error) {
      console.warn(`bridge: tabs.remove failed for ${numericId}`, error);
      return {removed: false, id: numericId};
    }
    return {removed: true, id: numericId};
  },

  async "frames.resolve"({tabId, selector}) {
    return resolveFrame(Number(tabId), String(selector));
  },

  async "cdp.send"(params) {
    return cdpSend({...params, tabId: Number(params.tabId)});
  },

  async "console.get"({tabId}) {
    const buffer = buffers.get(Number(tabId));
    return buffer ? legacyConsole(buffer) : [];
  },

  async "events.subscribe"({tabId, domains = ["console"], limits, include_headers = false}) {
    const numericId = Number(tabId);
    await ensureDebugger(numericId);
    const state = captureFor(numericId);
    const buffer = bufferFor(numericId);
    state.includeHeaders = Boolean(include_headers);
    const wanted = (Array.isArray(domains) ? domains : [domains]).map(item =>
      String(item).toLowerCase()
    );
    for (const domain of wanted) {
      if (domain === "console") {
        await sendSafe(numericId, "Runtime.enable");
        await sendSafe(numericId, "Log.enable");
      } else if (domain === "network") {
        const merged = {...NETWORK_BUFFER_LIMITS};
        for (const key of Object.keys(NETWORK_BUFFER_LIMITS)) {
          const value = Number(limits?.[key]);
          if (Number.isFinite(value) && value > 0) merged[key] = Math.round(value);
        }
        await chrome.debugger.sendCommand({tabId: numericId}, "Network.enable", merged);
      } else {
        throw new Error(`Unknown capture domain: ${domain}`);
      }
      state.domains.add(domain);
    }
    if (!state.startedAt) state.startedAt = Date.now();
    buffer.startedAt = state.startedAt;
    persistState();
    return {
      started_at: state.startedAt,
      seq: buffer.seq,
      domains: [...state.domains],
      include_headers: state.includeHeaders,
    };
  },

  async "events.unsubscribe"({tabId, domains}) {
    const numericId = Number(tabId);
    await restoreState();
    const state = capture.get(numericId);
    if (!state) return {domains: []};
    const wanted = domains
      ? (Array.isArray(domains) ? domains : [domains]).map(item => String(item).toLowerCase())
      : [...state.domains];
    for (const domain of wanted) {
      if (domain === "network") await sendSafe(numericId, "Network.disable");
      if (domain === "console") await sendSafe(numericId, "Log.disable");
      state.domains.delete(domain);
    }
    persistState();
    return {domains: [...state.domains]};
  },

  async "events.get"({tabId, ...options}) {
    const buffer = buffers.get(Number(tabId));
    if (!buffer) {
      return {
        entries: [],
        next_seq: Math.max(0, Number(options.since_seq) || 0),
        dropped: {console: 0, network: 0},
        started_at: 0,
        truncated: false,
        reset: false,
        pending: 0,
      };
    }
    return collectEvents(buffer, options);
  },

  async "events.clear"({tabId, kinds}) {
    const buffer = buffers.get(Number(tabId));
    if (!buffer) return {cleared: [], seq: 0};
    return {cleared: clearBuffer(buffer, kinds), seq: buffer.seq};
  },

  async "network.body"({tabId, requestId}) {
    const numericId = Number(tabId);
    const id = String(requestId);
    const buffer = buffers.get(numericId);
    const row =
      buffer?.pending.get(id) || buffer?.network.find(item => item.id === id) || null;
    const response = await cdpSend({
      tabId: numericId,
      method: "Network.getResponseBody",
      params: {requestId: id},
    });
    const body = String(response?.body || "");
    const mime = row?.mime || "";
    if (response?.base64Encoded && !isTextualMime(mime)) {
      return {
        request_id: id,
        mime,
        binary: true,
        size: row?.size ?? Math.floor((body.length * 3) / 4),
      };
    }
    return {
      request_id: id,
      mime,
      base64: Boolean(response?.base64Encoded),
      truncated: body.length > BODY_LIMIT,
      size: row?.size ?? body.length,
      body: body.slice(0, BODY_LIMIT),
    };
  },

  async "debugger.detach"({tabId}) {
    const numericId = Number(tabId);
    await restoreState();
    if (!attachedTabs.has(numericId)) return {detached: false};
    await chrome.debugger.detach({tabId: numericId}).catch(() => {});
    forgetTab(numericId);
    return {detached: true};
  },
};

function cdpTimestamp(value) {
  const timestamp = Number(value);
  // Runtime/Log timestamps are epoch milliseconds; anything smaller is monotonic.
  return Number.isFinite(timestamp) && timestamp > 1e11 ? Math.round(timestamp) : Date.now();
}

function contextFor(buffer, source, timestamp) {
  return {
    seq: nextSeq(buffer),
    ts: cdpTimestamp(timestamp),
    frame: source.sessionId || null,
  };
}

function dropChildSessions(predicate) {
  for (const [key, entry] of childSessions) {
    if (predicate(entry)) childSessions.delete(key);
  }
  persistState();
}

const EVENT_ROUTES = {
  "Runtime.consoleAPICalled"(tabId, params, source) {
    const buffer = bufferFor(tabId);
    pushEntry(buffer, "console", consoleEntry(params, contextFor(buffer, source, params.timestamp)));
  },

  "Runtime.exceptionThrown"(tabId, params, source) {
    const buffer = bufferFor(tabId);
    pushEntry(
      buffer,
      "console",
      exceptionEntry(params, contextFor(buffer, source, params.timestamp)),
    );
  },

  "Log.entryAdded"(tabId, params, source) {
    const buffer = bufferFor(tabId);
    pushEntry(
      buffer,
      "console",
      browserEntry(params, contextFor(buffer, source, params.entry?.timestamp)),
    );
  },

  "Page.frameNavigated"(tabId, params, source) {
    if (params.frame?.parentId) return;
    const buffer = bufferFor(tabId);
    // A reload marker instead of a buffer reset: the agent needs the boundary.
    pushEntry(
      buffer,
      "console",
      navigationEntry(params.frame?.url || "", contextFor(buffer, source, 0)),
    );
  },

  "Network.requestWillBeSent"(tabId, params, source) {
    if (!capturesNetwork(tabId)) return;
    const buffer = bufferFor(tabId);
    const previous = buffer.pending.get(String(params.requestId));
    if (previous && params.redirectResponse) {
      buffer.pending.delete(String(params.requestId));
      pushEntry(buffer, "network", applyRedirect(previous, params));
    }
    const ts = Number(params.wallTime) ? Math.round(params.wallTime * 1000) : Date.now();
    trackPending(buffer, networkRow(params, {ts, frame: source.sessionId || null}));
  },

  "Network.responseReceived"(tabId, params) {
    const buffer = buffers.get(tabId);
    const row = buffer?.pending.get(String(params.requestId));
    if (!row) return;
    applyResponse(row, params, Boolean(capture.get(tabId)?.includeHeaders));
  },

  "Network.loadingFinished"(tabId, params) {
    const buffer = buffers.get(tabId);
    const row = buffer?.pending.get(String(params.requestId));
    if (!row) return;
    buffer.pending.delete(String(params.requestId));
    pushEntry(buffer, "network", applyFinished(row, params));
  },

  "Network.loadingFailed"(tabId, params) {
    const buffer = buffers.get(tabId);
    const row = buffer?.pending.get(String(params.requestId));
    if (!row) return;
    buffer.pending.delete(String(params.requestId));
    pushEntry(buffer, "network", applyFailed(row, params));
  },

  "Target.detachedFromTarget"(tabId, params) {
    const sessionId = params.sessionId;
    if (sessionId) dropChildSessions(entry => entry.sessionId === sessionId);
  },

  "Target.targetDestroyed"(tabId, params) {
    const targetId = params.targetId;
    if (targetId) dropChildSessions(entry => entry.targetId === targetId);
  },
};

chrome.debugger.onEvent.addListener((source, method, params) => {
  const route = EVENT_ROUTES[method];
  if (!route || !source.tabId) return;
  try {
    route(source.tabId, params || {}, source);
  } catch (error) {
    console.warn(`bridge: event ${method} failed`, error);
  }
});

chrome.debugger.onDetach.addListener(source => {
  // Keep the buffer: whatever the page logged before the detach is still useful.
  if (source.tabId) forgetTab(source.tabId, false);
});

chrome.tabs.onRemoved.addListener(tabId => forgetTab(tabId));

async function handleCommand(message) {
  const {id, method} = message;
  const params = message.params && typeof message.params === "object" ? message.params : {};
  try {
    await restoreState();
    const handler = commands[method];
    if (!handler) throw new Error(`Unknown bridge method: ${method}`);
    const result = await handler(params);
    socket?.send(JSON.stringify({type: "result", id, result}));
  } catch (error) {
    socket?.send(JSON.stringify({
      type: "result",
      id,
      error: `${error?.name || "Error"}: ${error?.message || error}`,
    }));
  }
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  socket = new WebSocket(BRIDGE_URL);
  socket.onopen = () => {
    chrome.action.setBadgeBackgroundColor({color: "#16a34a"});
    chrome.action.setBadgeText({text: "ON"});
    socket.send(JSON.stringify({
      type: "hello",
      protocol: PROTOCOL_VERSION,
      browser: {name: "Chrome", extension_version: chrome.runtime.getManifest().version},
    }));
    clearInterval(keepaliveTimer);
    keepaliveTimer = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({type: "ping", at: Date.now()}));
      }
    }, KEEPALIVE_MS);
  };
  socket.onmessage = event => {
    let message = null;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      console.warn("bridge: dropped a frame that is not JSON", error);
      return;
    }
    if (!message || message.type !== "command") return;
    const {id, method} = message;
    if (typeof id !== "string" && typeof id !== "number") return;
    if (typeof method !== "string") {
      socket?.send(JSON.stringify({type: "result", id, error: "Error: method must be a string"}));
      return;
    }
    handleCommand(message);
  };
  socket.onclose = () => {
    chrome.action.setBadgeBackgroundColor({color: "#dc2626"});
    chrome.action.setBadgeText({text: "OFF"});
    clearInterval(keepaliveTimer);
    socket = null;
    scheduleReconnect();
  };
  socket.onerror = () => socket?.close();
}

chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.action.onClicked.addListener(connect);
restoreState();
connect();
