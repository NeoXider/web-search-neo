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
// The bridge only listens while the MCP server runs, which on a desktop is a
// small slice of the day. Chrome logs every refused attempt as a runtime error
// of ours, so a flat retry buries chrome://extensions under hundreds of
// identical lines and the extension reads as broken while it is merely idle.
// Backing off turns thirty lines a minute into roughly one; the explicit
// triggers further down buy back the promptness it costs.
const RECONNECT_BASE_MS = 1500;
const RECONNECT_MAX_MS = 60000;
const RECONNECT_FACTOR = 2;
// Jitter only ever shortens a wait, so the cap above stays a real ceiling while
// attempts stop landing in lockstep with a server that restarts on its own
// rhythm.
const RECONNECT_JITTER = 0.25;
// A refused handshake usually waits on the operator, so this retry is slow on
// purpose and grows slower still. It is a real retry: the token is re-read from
// disk on every attempt, so a secret written after the extension was loaded is
// picked up without a reload.
const AUTH_RETRY_MS = 10000;
const AUTH_RETRY_MAX_MS = 120000;
// Chrome stops an idle worker after about thirty seconds and pending timers die
// with it, so a longer wait has to be an alarm, the only thing that can start
// the worker back up. Without the "alarms" permission we keep every wait inside
// that window instead: worse than the cap above, still far quieter than a flat
// two seconds.
const WORKER_IDLE_MS = 30000;
const TIMER_SAFE_MS = 25000;
const RECONNECT_ALARM = "bridge-reconnect";
// Long enough for the answer to a reload request to reach the socket before the
// worker that wrote it disappears.
const RELOAD_GRACE_MS = 250;
const BACKOFF_KEY = "bridge_backoff";
const BACKOFF_STREAK_LIMIT = 32;
const KEEPALIVE_MS = 20000;
const DEBUGGER_VERSION = "1.3";
const STATE_KEY = "bridge_state";
const BODY_LIMIT = 512 * 1024;
const NETWORK_BUFFER_LIMITS = {
  maxTotalBufferSize: 10485760,
  maxResourceBufferSize: 5242880,
  maxPostDataSize: 65536,
};

// The permission is the user's to grant in the manifest, so the worker has to
// work either way and simply retries more coarsely when alarms are missing.
const alarmsAvailable = Boolean(chrome.alarms?.onAlarm);

let socket = null;
let verified = false;
let connecting = false;
let reconnectTimer = null;
let keepaliveTimer = null;
let lastAttemptAt = 0;
let backoff = {transport: 0, auth: 0, nextAttemptAt: 0};
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

  // Nothing outside Chrome can press "Reload" on an unpacked extension card, so
  // a companion left behind by a server update sits stale until the user thinks
  // to click. From in here it is one call, but it takes this worker and its
  // socket down with it, so the answer has to be on the wire first: handleCommand
  // writes it the moment this returns, and the grace period below is the gap that
  // write needs. An alarm would be the suspension-proof way to wait, except no
  // alarm fires sooner than half a minute — and suspension cannot happen here
  // anyway, since handling this very command is the activity that keeps the
  // worker alive.
  async "runtime.reload"() {
    const {version} = chrome.runtime.getManifest();
    // The worker that comes back reads the stored schedule on startup, so a
    // pending wait left behind would make new code sit out a backoff it never
    // earned.
    clearReconnect();
    await resetBackoff();
    setTimeout(() => chrome.runtime.reload(), RELOAD_GRACE_MS);
    return {
      reloading: true,
      version,
      extension_id: chrome.runtime.id,
      in_ms: RELOAD_GRACE_MS,
    };
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

// A command runs for as long as attaching a debugger or loading a page takes,
// and the socket can be replaced meanwhile. Answering into whatever socket is
// current by then would hand page contents to a peer that has not proved it
// knows the companion token yet.
export function isLiveConnection(connection) {
  return Boolean(connection)
    && connection === socket
    && verified
    && connection.readyState === WebSocket.OPEN;
}

function sendResult(connection, id, payload) {
  if (!isLiveConnection(connection)) {
    console.warn(`bridge: dropped the result for ${id}; its connection is gone`);
    return false;
  }
  try {
    connection.send(JSON.stringify({type: "result", id, ...payload}));
  } catch (error) {
    // The socket can die between the check and the write; nobody is awaiting
    // this call, so a throw here would only surface as an unhandled rejection.
    console.warn(`bridge: could not answer ${id}`, error);
    return false;
  }
  return true;
}

export async function handleCommand(connection, message) {
  const {id, method} = message;
  const params = message.params && typeof message.params === "object" ? message.params : {};
  if (!isLiveConnection(connection)) return;
  let payload;
  try {
    await restoreState();
    const handler = commands[method];
    if (!handler) throw new Error(`Unknown bridge method: ${method}`);
    payload = {result: await handler(params)};
  } catch (error) {
    payload = {error: `${error?.name || "Error"}: ${error?.message || error}`};
  }
  sendResult(connection, id, payload);
}

function setBadge(online) {
  chrome.action.setBadgeBackgroundColor({color: online ? "#16a34a" : "#dc2626"});
  chrome.action.setBadgeText({text: online ? "ON" : "OFF"});
}

// Two schedules, because the two failures mean different things: nobody is
// listening yet (common, noisy, cheap to retry) versus a peer that answered and
// turned us away (rare, and nothing we do sooner will change its mind).
export function backoffDelay(kind, streak) {
  const base = kind === "auth" ? AUTH_RETRY_MS : RECONNECT_BASE_MS;
  const cap = kind === "auth" ? AUTH_RETRY_MAX_MS : RECONNECT_MAX_MS;
  const step = Math.min(base * RECONNECT_FACTOR ** streak, cap);
  return Math.round(step - step * RECONNECT_JITTER * Math.random());
}

function persistBackoff() {
  return Promise.resolve(chrome.storage.session.set({[BACKOFF_KEY]: backoff})).catch(() => {});
}

function clearReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (alarmsAvailable) Promise.resolve(chrome.alarms.clear(RECONNECT_ALARM)).catch(() => {});
}

function scheduleReconnect(delayMs) {
  clearReconnect();
  const wait = Math.max(0, Math.round(delayMs));
  backoff.nextAttemptAt = Date.now() + wait;
  persistBackoff();
  if (alarmsAvailable && wait >= WORKER_IDLE_MS) {
    chrome.alarms.create(RECONNECT_ALARM, {delayInMinutes: wait / 60000});
    return;
  }
  // A timer we would not live to see is worse than a shorter one: it would
  // leave the extension offline until the user thinks to click it.
  reconnectTimer = setTimeout(startConnect, alarmsAvailable ? wait : Math.min(wait, TIMER_SAFE_MS));
}

function retryAfterFailure(kind) {
  const delay = backoffDelay(kind, backoff[kind]);
  backoff[kind] = Math.min(backoff[kind] + 1, BACKOFF_STREAK_LIMIT);
  scheduleReconnect(delay);
}

function resetBackoff() {
  backoff = {transport: 0, auth: 0, nextAttemptAt: 0};
  return persistBackoff();
}

// connect() is async, so every entry point funnels through here to keep a
// rejected promise from silently stopping the reconnect chain.
function startConnect() {
  lastAttemptAt = Date.now();
  clearReconnect();
  connect().catch(error => {
    connecting = false;
    // Nothing else records this one: the rejection is caught here, so Chrome
    // never sees it, and it means our own code broke rather than the port
    // being closed.
    console.warn("bridge: connect attempt failed", error);
    setBadge(false);
    retryAfterFailure("auth");
  });
}

// Nothing arrives over a socket that was never established, so the extension
// cannot notice a server that has just started. These signals are the
// substitute: the browser starting, this extension being installed or reloaded,
// and a click on the toolbar icon, which is how someone who just launched the
// MCP server says "try now". Each clears the streak, so the floor below is what
// stops a signal that repeats, or two arriving together, from turning back into
// a fixed short loop: inside the floor the pending retry is only pulled forward.
function connectNow() {
  resetBackoff();
  const sinceLast = Date.now() - lastAttemptAt;
  if (sinceLast < RECONNECT_BASE_MS) {
    scheduleReconnect(RECONNECT_BASE_MS - sinceLast);
    return;
  }
  startConnect();
}

// Chrome evicts an idle worker and this file starts over whenever a listener
// wakes it, so the backoff has to outlive the worker; otherwise every closed tab
// would spend a fresh attempt and the flood would return through the side door.
// chrome.storage.session is the right shelf: it survives eviction and is empty
// again after a browser restart, which is exactly when an immediate attempt is
// warranted.
async function resumeConnect() {
  try {
    const stored = (await chrome.storage.session.get(BACKOFF_KEY))?.[BACKOFF_KEY];
    if (stored) {
      backoff = {
        transport: Number(stored.transport) || 0,
        auth: Number(stored.auth) || 0,
        nextAttemptAt: Number(stored.nextAttemptAt) || 0,
      };
    }
  } catch (error) {
    console.warn("bridge: could not read the stored reconnect backoff", error);
  }
  // The cap is what a clock the user moved forward, or a value written by an
  // older build, cannot get past: the longest this ever parks the bridge is one
  // ordinary wait.
  const remaining = Math.min(backoff.nextAttemptAt - Date.now(), RECONNECT_MAX_MS);
  if (remaining > 0) {
    scheduleReconnect(remaining);
    return;
  }
  startConnect();
}

function bytesToHex(bytes) {
  return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length >> 1);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

function timingSafeEqual(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left[index] ^ right[index];
  return difference === 0;
}

export function parseBridgeToken(source) {
  const match = /BRIDGE_TOKEN\s*=\s*["']([0-9a-f]{64})["']/.exec(String(source ?? ""));
  if (!match) throw new Error("bridge-token.js holds no usable token");
  return match[1];
}

// bridge-token.js is written by the Python side and is absent in a fresh clone.
// It is read rather than imported on purpose: the worker's module map caches a
// failed import for the life of the worker, so a token file that appears after
// the first attempt would never be seen without a manual reload.
export async function loadBridgeToken() {
  const response = await fetch(chrome.runtime.getURL("bridge-token.js"), {cache: "no-store"});
  if (!response.ok) throw new Error(`bridge-token.js is unreadable (HTTP ${response.status})`);
  return parseBridgeToken(await response.text());
}

async function hmacSha256(token, message) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(token),
    {name: "HMAC", hash: "SHA-256"},
    false,
    ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
}

export async function connect() {
  if (connecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  connecting = true;
  let token = null;
  let nonce = null;
  let expectedProof = null;
  try {
    token = await loadBridgeToken();
    nonce = bytesToHex(crypto.getRandomValues(new Uint8Array(16)));
    expectedProof = await hmacSha256(token, nonce);
  } catch (error) {
    // Chrome logs the failed read of bridge-token.js on its own; what it cannot
    // say is what to do about it. Once per streak is enough, because the retry
    // repeats but the advice does not change.
    if (!backoff.auth) {
      console.warn("bridge: no companion token yet, run setup_current_chrome", error);
    }
    connecting = false;
    setBadge(false);
    retryAfterFailure("auth");
    return;
  }

  verified = false;
  let handshakeStarted = false;
  socket = new WebSocket(BRIDGE_URL);
  connecting = false;
  const active = socket;
  socket.onopen = () => {
    handshakeStarted = true;
    active.send(JSON.stringify({
      type: "hello",
      protocol: PROTOCOL_VERSION,
      token,
      nonce,
      browser: {name: "Chrome", extension_version: chrome.runtime.getManifest().version},
    }));
  };
  socket.onmessage = event => {
    let message = null;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      console.warn("bridge: dropped a frame that is not JSON", error);
      return;
    }
    if (!message) return;
    if (!verified) {
      // Anything other than a valid ack — a command above all — means the peer
      // is not the local server we share a secret with.
      if (message.type !== "hello_ack") {
        console.warn(`bridge: closing an unverified peer that sent ${message.type}`);
        active.close();
        return;
      }
      completeHandshake(active, message, expectedProof);
      return;
    }
    if (message.type !== "command") return;
    const {id, method} = message;
    if (typeof id !== "string" && typeof id !== "number") return;
    if (typeof method !== "string") {
      sendResult(active, id, {error: "Error: method must be a string"});
      return;
    }
    handleCommand(active, message);
  };
  socket.onclose = event => {
    const rejected = handshakeStarted && !verified;
    // A closed port is Chrome's own error line and needs nothing from us, but a
    // peer that answered and then turned us away is worth the code and reason.
    if (rejected) console.warn(`bridge: handshake refused (${event.code}) ${event.reason || ""}`);
    // A socket the module already replaced must not clear the live one's state.
    if (socket !== active) return;
    setBadge(false);
    clearInterval(keepaliveTimer);
    verified = false;
    socket = null;
    retryAfterFailure(rejected ? "auth" : "transport");
  };
  socket.onerror = () => active.close();
}

function completeHandshake(connection, message, expectedProof) {
  let accepted = false;
  try {
    const proof = typeof message.proof === "string" ? message.proof : "";
    accepted =
      message.protocol === PROTOCOL_VERSION &&
      /^[0-9a-f]{64}$/.test(proof) &&
      timingSafeEqual(hexToBytes(proof), expectedProof);
  } catch (error) {
    console.warn("bridge: could not check the server proof", error);
  }
  if (!accepted) {
    console.warn("bridge: the server did not prove it knows the companion token");
    connection.close();
    return;
  }
  if (connection !== socket || connection.readyState !== WebSocket.OPEN) return;
  verified = true;
  // A verified peer proves the wait is over, so the next outage starts counting
  // from the floor again rather than from wherever the last one left off.
  clearReconnect();
  resetBackoff();
  setBadge(true);
  clearInterval(keepaliveTimer);
  keepaliveTimer = setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN && verified) {
      socket.send(JSON.stringify({type: "ping", at: Date.now()}));
    }
  }, KEEPALIVE_MS);
}

chrome.runtime.onInstalled.addListener(connectNow);
chrome.runtime.onStartup.addListener(connectNow);
chrome.action.onClicked.addListener(connectNow);
// The alarm carries no urgency of its own; it exists to bring the worker back
// for a wait that outlasts it, so it lands on the ordinary attempt path.
if (alarmsAvailable) {
  chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === RECONNECT_ALARM) startConnect();
  });
}
restoreState();
resumeConnect();
