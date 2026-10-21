// "Take over": the person at the keyboard pauses every agent in this browser.
//
// Agents drive Chrome through this worker, so this is the one place that can
// stop all of them at once without closing anything: while paused, every
// command that could change a page or a tab is refused with a sentence the
// agent can act on, and reads that only look at state already held here keep
// working, so a paused agent can still tell what happened. The pause lives in
// session storage - a browser restart ends it - and Resume lifts it at once.

import {setBadgeOverride} from "./agent-badges.js";
import {setPausedLook} from "./tab-status.js";

const PAUSE_KEY = "companion_paused_since";
// The manifest command (Alt+Shift+P by default): stop every agent without opening anything.
const TAKE_OVER_COMMAND = "take-over";
const PAUSED_BADGE = {
  text: "II", color: "#d97706",
  title: "Web Search Neo: you took over - agents are paused. Alt+Shift+P or the panel resumes them.",
};
// Commands that change nothing: tab metadata, buffered console/network, and the
// release of a debugger the agent no longer needs.
const ALLOWED_WHILE_PAUSED = new Set([
  "tabs.get", "tabs.list", "tabs.resolve", "frames.resolve", "console.get", "events.get",
  "network.body", "mock.list", "debugger.detach",
]);
const storage = () => chrome.storage?.session || chrome.storage?.local;

let pausedSince = 0;
let loaded = null;

export function loadPause() {
  loaded ??= Promise.resolve(storage()?.get(PAUSE_KEY))
    .then(stored => { pausedSince = Number(stored?.[PAUSE_KEY]) || 0; })
    .catch(() => { pausedSince = 0; })
    .then(showPause);
  return loaded;
}

function showPause() {
  try {
    setBadgeOverride(pausedSince ? PAUSED_BADGE : null);
    setPausedLook(pausedSince > 0);
  } catch (error) {
    console.warn("companion: could not paint the pause badge", error);
  }
}

export function pauseState() {
  return {paused: pausedSince > 0, paused_since: pausedSince || 0};
}

// Throws the error an agent receives instead of the effect; returns when allowed.
export async function refuseWhilePaused(method) {
  await loadPause();
  if (!pausedSince || ALLOWED_WHILE_PAUSED.has(method)) return;
  throw new Error(
    "The user took over this browser in the Web Search Neo companion (Take over), " +
    `so ${method} was not run. Wait for them to press Resume, then continue; do not retry ` +
    "in a loop, and do not open a different browser to work around it.",
  );
}

export async function setPaused(value) {
  await loadPause();
  pausedSince = value ? pausedSince || Date.now() : 0;
  await storage()?.set({[PAUSE_KEY]: pausedSince});
  showPause();
  return pauseState();
}

// Popup messages; returns a promise, or null for a type this module does not own.
export function controlMessage(message) {
  if (message?.type === "companion.setPaused") return setPaused(Boolean(message.paused));
  return null;
}

chrome.commands?.onCommand?.addListener(command => {
  if (command === TAKE_OVER_COMMAND) loadPause().then(() => setPaused(!pausedSince));
});
loadPause();
