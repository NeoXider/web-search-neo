// Shows, in the browser's own toolbar, which tabs an agent is on right now or
// was on during the last five minutes. It lives in the extension rather than
// in the page, so it works on chrome:// pages, PDFs and pages whose scripts
// refuse ours - which the in-page favicon badge cannot.
//
// The toolbar badge is per tab: green "AI" while an agent acted on the tab in
// the last ~30 s, amber until five minutes have passed, then the tab goes back
// to the global ON/OFF badge. The map is mirrored into chrome.storage.session
// and a one-minute alarm sweeps it, so expiry survives MV3 worker eviction.
import {
  badgeFor,
  expireActivity,
  forgetActivity,
  listRecent,
  nextChangeIn,
  normalizeTabId,
  recordActivity,
} from "./agent-activity.js";

const STORAGE_KEY = "agent_activity";
export const AGENT_ALARM = "agent-activity-sweep";

let activity = {};
// Tabs whose per-tab badge was handed back: they mirror the global badge, so a
// global change has to reach them too (Chrome has no per-tab "reset" for the
// colour or the title).
let retired = new Set();
let globalBadge = {text: "", color: "#64748b", title: "Web Search Neo Companion"};
let loadPromise = null;
let sweepTimer = null;
// null: unknown (a fresh worker may inherit an armed alarm from its predecessor).
let alarmArmed = null;

function action(method, details) {
  try {
    const pending = chrome.action?.[method]?.(details);
    if (pending && typeof pending.catch === "function") pending.catch(() => {});
  } catch (error) {
    // A tab that closed between the check and the call is not worth a log line.
  }
}

function load() {
  if (!loadPromise) {
    loadPromise = Promise.resolve(chrome.storage?.session?.get(STORAGE_KEY))
      .then(stored => {
        const saved = stored?.[STORAGE_KEY] || {};
        // Anything recorded by this worker before the read finished wins.
        activity = {...(saved.activity || {}), ...activity};
        for (const tabId of saved.retired || []) retired.add(Number(tabId));
      })
      .catch(error => console.warn("bridge: agent activity restore failed", error));
  }
  return loadPromise;
}

function persist() {
  Promise.resolve(chrome.storage?.session?.set({
    [STORAGE_KEY]: {activity, retired: [...retired]},
  })).catch(error => console.warn("bridge: agent activity persist failed", error));
}

function showGlobal(tabId) {
  action("setBadgeText", {tabId, text: globalBadge.text});
  action("setBadgeBackgroundColor", {tabId, color: globalBadge.color});
  action("setTitle", {tabId, title: globalBadge.title});
}

function paint(tabId, now) {
  const entry = activity[String(tabId)];
  const badge = entry && badgeFor(entry, now);
  if (!badge) {
    retired.add(tabId);
    showGlobal(tabId);
    return;
  }
  retired.delete(tabId);
  action("setBadgeText", {tabId, text: badge.text});
  action("setBadgeBackgroundColor", {tabId, color: badge.color});
  action("setTitle", {tabId, title: badge.title});
}

function schedule(now) {
  clearTimeout(sweepTimer);
  const wait = nextChangeIn(activity, now);
  const hasEntries = wait !== null;
  if (hasEntries && !alarmArmed && chrome.alarms?.create) {
    alarmArmed = true;
    chrome.alarms.create(AGENT_ALARM, {periodInMinutes: 1});
  } else if (!hasEntries && alarmArmed !== false && chrome.alarms?.clear) {
    alarmArmed = false;
    Promise.resolve(chrome.alarms.clear(AGENT_ALARM)).catch(() => {});
  }
  // A timer gives the exact active -> recent edge while the worker is alive;
  // the alarm is the fallback that outlives an evicted worker.
  if (hasEntries) sweepTimer = setTimeout(() => sweep(), Math.min(wait + 50, 60000));
}

export async function sweep(now = Date.now()) {
  await load();
  const {map, expired} = expireActivity(activity, now);
  activity = map;
  for (const tabId of expired) paint(tabId, now);
  for (const key of Object.keys(activity)) paint(Number(key), now);
  persist();
  schedule(now);
}

// Called for every verified command the bridge relays. Never throws: a badge
// must not be able to fail a command.
export function noteAgentCommand(message, params, payload) {
  try {
    const now = Date.now();
    const agent = message?.agent && typeof message.agent === "object" ? message.agent : null;
    const extra = {method: message?.method};
    let tabId = normalizeTabId(params?.tabId);
    if (tabId === null && message?.method === "tabs.create") tabId = normalizeTabId(payload?.result?.id);
    if (tabId === null || (message?.method === "tabs.remove" && !payload?.error)) return;
    activity = recordActivity(activity, tabId, agent, now, extra);
    retired.delete(tabId);
    paint(tabId, now);
    load().then(() => {
      persist();
      schedule(Date.now());
    });
  } catch (error) {
    console.warn("bridge: could not record agent activity", error);
  }
}

// The global ON/OFF badge. Tabs that mirror it follow along.
export function applyGlobalBadge(next) {
  globalBadge = {...globalBadge, ...next};
  action("setBadgeBackgroundColor", {color: globalBadge.color});
  action("setBadgeText", {text: globalBadge.text});
  action("setTitle", {title: globalBadge.title});
  for (const tabId of retired) showGlobal(tabId);
}

function forget(tabId) {
  const id = normalizeTabId(tabId);
  if (id === null) return;
  load().then(() => {
    activity = forgetActivity(activity, id);
    retired.delete(id);
    persist();
    schedule(Date.now());
  });
}

async function tabDetails(tabIds) {
  const details = {};
  await Promise.all(tabIds.map(async tabId => {
    try {
      const tab = await chrome.tabs.get(tabId);
      details[tabId] = {title: tab.title, url: tab.url || tab.pendingUrl, windowId: tab.windowId};
    } catch (error) {
      details[tabId] = null;
    }
  }));
  return details;
}

// The popup polls this every second, so it only reads: a tab is repainted and
// the map written back only when an entry actually expired or its tab is gone.
// The active -> recent edge of live tabs belongs to the timer and the alarm.
export async function agentTabs() {
  await load();
  const now = Date.now();
  const {map, expired} = expireActivity(activity, now);
  if (expired.length) {
    activity = map;
    for (const tabId of expired) paint(tabId, now);
    persist();
    schedule(now);
  }
  const ids = Object.values(activity).map(entry => entry.tabId);
  const details = await tabDetails(ids);
  for (const tabId of ids) if (details[tabId] === null) forget(tabId);
  const live = Object.fromEntries(Object.entries(activity).filter(([, e]) => details[e.tabId]));
  return {tabs: listRecent(live, now, details), now};
}

export async function focusAgentTab(value) {
  await load();
  const tabId = normalizeTabId(value);
  if (tabId === null || !activity[String(tabId)]) throw new Error("That tab has no recent agent activity");
  const tab = await chrome.tabs.update(tabId, {active: true});
  if (Number.isInteger(tab?.windowId)) await chrome.windows.update(tab.windowId, {focused: true});
  return {focused: tabId};
}

// Popup messages; returns a promise, or null for a type this module does not own.
export function agentMessage(message) {
  if (message?.type === "companion.agentTabs") return agentTabs();
  if (message?.type === "companion.focusAgentTab") return focusAgentTab(message.tab_id);
  return null;
}

chrome.tabs?.onRemoved?.addListener(tabId => forget(tabId));
// Chrome drops a tab's per-tab action values when it navigates, so a driven tab
// that loads a new page is painted again.
chrome.tabs?.onUpdated?.addListener((tabId, changeInfo) => {
  if (!changeInfo?.status && !changeInfo?.url) return;
  load().then(() => {
    if (activity[String(tabId)]) paint(tabId, Date.now());
  });
});
chrome.alarms?.onAlarm?.addListener(alarm => {
  if (alarm?.name === AGENT_ALARM) sweep();
});
// A fresh worker may inherit badges whose time ran out while it was gone.
load().then(() => {
  if (Object.keys(activity).length || retired.size) sweep();
});
