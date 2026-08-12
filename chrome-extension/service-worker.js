const BRIDGE_URL = "ws://127.0.0.1:8765";
const PROTOCOL_VERSION = 1;
const RECONNECT_MS = 2000;
const KEEPALIVE_MS = 20000;

let socket = null;
let reconnectTimer = null;
let keepaliveTimer = null;
const attachedTabs = new Set();
const consoleMessages = new Map();
const childSessions = new Map();
let groupQueue = Promise.resolve();

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

async function ensureDebugger(tabId) {
  if (attachedTabs.has(tabId)) return;
  await chrome.debugger.attach({tabId}, "1.3");
  attachedTabs.add(tabId);
  await chrome.debugger.sendCommand({tabId}, "Runtime.enable");
  await chrome.debugger.sendCommand({tabId}, "Page.enable");
  await chrome.debugger.sendCommand({tabId}, "Log.enable").catch(() => {});
}

async function cdpSend({tabId, sessionId, method, params = {}}) {
  await ensureDebugger(tabId);
  const target = sessionId ? {tabId, sessionId} : {tabId};
  return chrome.debugger.sendCommand(target, method, params);
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

  const cacheKey = `${tabId}:${info.src}`;
  if (childSessions.has(cacheKey)) {
    return {sameOrigin: false, sessionId: childSessions.get(cacheKey), url: info.src};
  }
  const targets = await cdpSend({tabId, method: "Target.getTargets"});
  const target = (targets.targetInfos || []).find(item =>
    item.type === "iframe" && item.url === info.src
  );
  if (!target) throw new Error(`Cross-origin iframe target is unavailable: ${info.src}`);
  const attached = await cdpSend({
    tabId,
    method: "Target.attachToTarget",
    params: {targetId: target.targetId, flatten: true},
  });
  childSessions.set(cacheKey, attached.sessionId);
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
    for (const key of childSessions.keys()) {
      if (key.startsWith(`${Number(tabId)}:`)) childSessions.delete(key);
    }
    await chrome.tabs.update(Number(tabId), {url, active: true});
    return serializeTab(await waitForTab(Number(tabId)), await groupMap());
  },

  async "tabs.activate"({tabId}) {
    const tab = await chrome.tabs.update(Number(tabId), {active: true});
    await chrome.windows.update(tab.windowId, {focused: true});
    return serializeTab(tab, await groupMap());
  },

  async "frames.resolve"({tabId, selector}) {
    return resolveFrame(Number(tabId), String(selector));
  },

  async "cdp.send"(params) {
    return cdpSend({...params, tabId: Number(params.tabId)});
  },

  async "console.get"({tabId}) {
    return consoleMessages.get(Number(tabId)) || [];
  },

  async "debugger.detach"({tabId}) {
    tabId = Number(tabId);
    if (!attachedTabs.has(tabId)) return {detached: false};
    await chrome.debugger.detach({tabId}).catch(() => {});
    attachedTabs.delete(tabId);
    for (const key of childSessions.keys()) {
      if (key.startsWith(`${tabId}:`)) childSessions.delete(key);
    }
    return {detached: true};
  },
};

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source.tabId) return;
  if (method === "Log.entryAdded") {
    const items = consoleMessages.get(source.tabId) || [];
    items.push({
      level: String(params.entry.level || "INFO").toUpperCase(),
      message: params.entry.text || "",
      timestamp: Date.now(),
    });
    consoleMessages.set(source.tabId, items.slice(-100));
  }
});

chrome.debugger.onDetach.addListener(source => {
  if (source.tabId) attachedTabs.delete(source.tabId);
});

async function handleCommand(message) {
  const {id, method, params = {}} = message;
  try {
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
    const message = JSON.parse(event.data);
    if (message.type === "command") handleCommand(message);
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
connect();
