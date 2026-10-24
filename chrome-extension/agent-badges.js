// Shows, in the browser's own toolbar, which tabs an agent is on right now or
// was on during the last five minutes. It lives in the extension rather than
// in the page, so it works on chrome:// pages, PDFs and pages whose scripts
// refuse ours - which the in-page favicon badge cannot.
//
// The toolbar badge is per tab: green "AI" while an agent acted on the tab in
// the last minute, yellow until six minutes have passed, then red until the
// auto-close time (an hour by default): a tab the agent opened is closed then,
// unless the popup switched auto-close off, and any other tab goes back to the
// global ON/OFF badge. The map is mirrored into chrome.storage.session and a
// one-minute alarm sweeps it, so expiry survives MV3 worker eviction.
import {
  activityPhase,
  badgeFor,
  dueForClose,
  expireActivity,
  forgetActivity,
  listRecent,
  nextChangeIn,
  normalizeTabId,
  recordActivity,
} from "./agent-activity.js";
import {pulseToolbar, syncGroups} from "./tab-status.js";
import {jobCommands, jobsMessage, macroTabIds} from "./jobs.js";

// The service worker registers the jobs report command through this module.
export {jobCommands};

const STORAGE_KEY = "agent_activity";
export const AGENT_ALARM = "agent-activity-sweep";
// The popup's "Close idle AI tabs" setting: on by default, after an hour.
const AUTO_CLOSE_KEY = "auto_close_tabs";
const AUTO_CLOSE_DEFAULT = {enabled: true, minutes: 60};
let autoClose = {...AUTO_CLOSE_DEFAULT};
const staleMs = () => Math.max(6, Number(autoClose.minutes) || 60) * 60 * 1000;
const phaseOf = (entry, now) => activityPhase(entry, now, staleMs());

let activity = {};
// Tabs whose per-tab badge was handed back: they mirror the global badge, so a
// global change has to reach them too (Chrome has no per-tab "reset" for the
// colour or the title).
let retired = new Set();
let globalBadge = {text: "", color: "#64748b", title: "Web Search Neo Companion"};
// Laid over the ON/OFF badge while the user has taken over (control.js), so a
// reconnect repainting ON cannot hide that agents are paused.
let badgeOverride = null;
const shownBadge = () => ({...globalBadge, ...(badgeOverride || {})});
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
        return chrome.storage?.local?.get(AUTO_CLOSE_KEY);
      })
      .then(stored => { autoClose = {...AUTO_CLOSE_DEFAULT, ...(stored?.[AUTO_CLOSE_KEY] || {})}; })
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
  const shown = shownBadge();
  action("setBadgeText", {tabId, text: shown.text});
  action("setBadgeBackgroundColor", {tabId, color: shown.color});
  action("setTitle", {tabId, title: shown.title});
}

function paint(tabId, now) {
  const entry = activity[String(tabId)];
  const badge = entry && badgeFor(entry, now, staleMs(), macroTabIds.has(tabId));
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
  const wait = nextChangeIn(activity, now, staleMs());
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
  await closeIdleTabs(now);
  const {map, expired} = expireActivity(activity, now, staleMs());
  activity = map;
  for (const tabId of expired) paint(tabId, now);
  for (const key of Object.keys(activity)) paint(Number(key), now);
  persist();
  schedule(now);
  syncGroups(activity, now, phaseOf);
}

// Auto-close: a tab an agent opened and has not used for the set time is closed -
// unless the person is looking at it right now, pinned it, or it is playing sound.
async function closeIdleTabs(now) {
  if (!autoClose.enabled || !chrome.tabs?.remove) return;
  for (const tabId of dueForClose(activity, now, staleMs())) {
    try {
      const tab = await chrome.tabs.get(tabId);
      const watched = tab.active && (await chrome.windows.get(tab.windowId)).focused;
      if (watched || tab.pinned || tab.audible) continue;
      await chrome.tabs.remove(tabId);
    } catch (error) {
      // Already gone: the entry is dropped below either way.
    }
    activity = forgetActivity(activity, tabId);
    retired.delete(tabId);
  }
}

export async function autoCloseSettings(next) {
  await load();
  if (next && typeof next === "object") {
    const minutes = Math.round(Number(next.minutes ?? autoClose.minutes));
    autoClose = {enabled: next.enabled === undefined ? autoClose.enabled : Boolean(next.enabled),
                 minutes: Number.isFinite(minutes) ? Math.min(Math.max(minutes, 6), 24 * 60) : autoClose.minutes};
    await chrome.storage?.local?.set({[AUTO_CLOSE_KEY]: autoClose});
    sweep();
  }
  return {...autoClose};
}

// The in-page badge lives in the document; after a navigation the new page has the
// machinery (registered for every new document) but not the time of the last action.
// Hand it over, through the debugger the agent already holds on its tabs; a tab not
// attached right now simply keeps the toolbar badge.
function resumePageBadge(tabId, at, mode) {
  try {
    const expression = `window.__wsnPresence && window.__wsnPresence.resume && window.__wsnPresence.resume(${Number(at)}, ${JSON.stringify(mode)})`;
    Promise.resolve(chrome.debugger?.sendCommand?.({tabId}, "Runtime.evaluate", {expression})).catch(() => {});
  } catch (error) {
    // No debugger on this tab: nothing to resume.
  }
}

// Called for every verified command the bridge relays. Never throws: a badge
// must not be able to fail a command.
export function noteAgentCommand(message, params, payload) {
  try {
    const now = Date.now();
    const agent = message?.agent && typeof message.agent === "object" ? message.agent : null;
    const extra = {method: message?.method, created: message?.method === "tabs.create"};
    let tabId = normalizeTabId(params?.tabId);
    if (tabId === null && message?.method === "tabs.create") tabId = normalizeTabId(payload?.result?.id);
    if (tabId === null || (message?.method === "tabs.remove" && !payload?.error)) return;
    activity = recordActivity(activity, tabId, agent, now, extra);
    retired.delete(tabId);
    paint(tabId, now);
    pulseToolbar();
    syncGroups(activity, now, phaseOf);
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
  const shown = shownBadge();
  action("setBadgeBackgroundColor", {color: shown.color});
  action("setBadgeText", {text: shown.text});
  action("setTitle", {title: shown.title});
  for (const tabId of retired) showGlobal(tabId);
}

// null lifts the override and shows the plain ON/OFF badge again.
export function setBadgeOverride(next) {
  badgeOverride = next || null;
  applyGlobalBadge({});
}

function forget(tabId) {
  const id = normalizeTabId(tabId);
  if (id === null) return;
  load().then(() => {
    activity = forgetActivity(activity, id);
    retired.delete(id);
    persist();
    schedule(Date.now());
    syncGroups(activity, Date.now(), phaseOf);
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
  const {map, expired} = expireActivity(activity, now, staleMs());
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
  return {tabs: listRecent(live, now, details, staleMs()), now, auto_close: {...autoClose}};
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
  if (message?.type === "companion.autoClose") return autoCloseSettings(message.settings);
  return jobsMessage(message);
}

chrome.tabs?.onRemoved?.addListener(tabId => forget(tabId));
// Chrome drops a tab's per-tab action values when it navigates, so a driven tab
// that loads a new page is painted again.
chrome.tabs?.onUpdated?.addListener((tabId, changeInfo) => {
  if (!changeInfo?.status && !changeInfo?.url) return;
  load().then(() => {
    const entry = activity[String(tabId)];
    if (!entry) return;
    paint(tabId, Date.now());
    if (changeInfo.status === "complete") resumePageBadge(tabId, entry.at, macroTabIds.has(tabId) ? "macro" : "agent");
  });
});
chrome.alarms?.onAlarm?.addListener(alarm => {
  if (alarm?.name === AGENT_ALARM) sweep();
});
// A fresh worker may inherit badges whose time ran out while it was gone.
load().then(() => {
  if (Object.keys(activity).length || retired.size) sweep();
});
