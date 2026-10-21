// What the browser chrome itself shows about agent work, beyond the badges:
//
// * the toolbar icon animates - a green arc circling the slime - for as long as
//   commands keep arriving, and settles a moment after the last one, so "an agent
//   is doing something right now" is visible without opening anything;
// * every tab group holding a driven tab takes the colour of its liveliest tab:
//   green while an agent acted in the last minute, yellow for the five-minute
//   afterglow, orange while the user has taken over; afterwards the group gets the
//   colour it had before we touched it. Titles are never changed: the server finds
//   its group by title.
//
// Nothing here runs per frame while idle: the icon timer stops with the activity,
// and group colours are written only when they change.

const SPIN_FRAME_MS = 90;
const SPIN_LINGER_MS = 2500;
const SPIN_STEPS = 12;
const ICON_SIZES = [16, 32];
const ACTIVE_ARC = "#22c55e";
const GROUP_COLORS = {active: "green", recent: "yellow", paused: "orange"};
const GROUP_STORE = "agent_group_colors";
const SYNC_DELAY_MS = 250;

let paused = false;
let lastPulse = 0;
let spinTimer = null;
let spinStep = 0;
let bitmapsPromise = null;
let syncTimer = null;
let latest = null;

function canAnimate() {
  return typeof OffscreenCanvas === "function" && typeof createImageBitmap === "function"
    && Boolean(chrome.action?.setIcon) && Boolean(chrome.runtime?.getURL);
}

function bitmaps() {
  bitmapsPromise ??= Promise.all(ICON_SIZES.map(async size => {
    const response = await fetch(chrome.runtime.getURL(`icons/slime-${size}.png`));
    return [size, await createImageBitmap(await response.blob())];
  })).then(Object.fromEntries).catch(error => {
    bitmapsPromise = null;
    throw error;
  });
  return bitmapsPromise;
}

function frame(bitmap, size, step) {
  const canvas = new OffscreenCanvas(size, size);
  const context = canvas.getContext("2d");
  context.drawImage(bitmap, 0, 0, size, size);
  const width = Math.max(2, size / 8);
  const radius = size / 2 - width / 2;
  const start = (step / SPIN_STEPS) * Math.PI * 2 - Math.PI / 2;
  context.lineCap = "round";
  context.lineWidth = width;
  context.strokeStyle = "rgba(34, 197, 94, 0.28)";
  context.beginPath();
  context.arc(size / 2, size / 2, radius, 0, Math.PI * 2);
  context.stroke();
  context.strokeStyle = ACTIVE_ARC;
  context.beginPath();
  context.arc(size / 2, size / 2, radius, start, start + Math.PI * 0.75);
  context.stroke();
  return context.getImageData(0, 0, size, size);
}

async function tick() {
  if (paused || Date.now() - lastPulse > SPIN_LINGER_MS) {
    stopSpin();
    return;
  }
  spinStep = (spinStep + 1) % SPIN_STEPS;
  try {
    const images = await bitmaps();
    const imageData = Object.fromEntries(ICON_SIZES.map(size => [size, frame(images[size], size, spinStep)]));
    await chrome.action.setIcon({imageData});
  } catch (error) {
    stopSpin();
  }
}

function stopSpin() {
  if (spinTimer === null) return;
  clearInterval(spinTimer);
  spinTimer = null;
  const icons = chrome.runtime.getManifest?.().action?.default_icon;
  Promise.resolve(icons && chrome.action.setIcon({path: icons})).catch(() => {});
}

// Called for every relayed agent command: cheap, it only moves a timestamp.
export function pulseToolbar() {
  if (paused || !canAnimate()) return;
  lastPulse = Date.now();
  if (spinTimer === null) spinTimer = setInterval(tick, SPIN_FRAME_MS);
}

// -- tab groups -----------------------------------------------------------------

async function storedColors() {
  try {
    const stored = await chrome.storage?.session?.get(GROUP_STORE);
    return stored?.[GROUP_STORE] || {original: {}, applied: {}};
  } catch (error) {
    return {original: {}, applied: {}};
  }
}

async function saveColors(colors) {
  try {
    await chrome.storage?.session?.set({[GROUP_STORE]: colors});
  } catch (error) {
    // Losing this only means a group may keep our colour after a worker restart.
  }
}

async function groupsOfDrivenTabs(activity, now, phaseOf) {
  const best = new Map();
  await Promise.all(Object.values(activity || {}).map(async entry => {
    const phase = phaseOf(entry, now);
    if (phase === "expired") return;
    try {
      const tab = await chrome.tabs.get(entry.tabId);
      if (!Number.isInteger(tab?.groupId) || tab.groupId < 0) return;
      if (best.get(tab.groupId) !== "active") best.set(tab.groupId, phase);
    } catch (error) {
      // The tab closed; its entry is forgotten elsewhere.
    }
  }));
  return best;
}

async function syncNow() {
  syncTimer = null;
  if (!latest) return;
  const {activity, phaseOf} = latest;
  const now = Date.now();
  const colors = await storedColors();
  const best = await groupsOfDrivenTabs(activity, now, phaseOf);
  let changed = false;
  for (const [groupId, phase] of best) {
    const key = String(groupId);
    const wanted = paused ? GROUP_COLORS.paused : GROUP_COLORS[phase];
    if (colors.applied[key] === wanted) continue;
    try {
      if (!(key in colors.original)) colors.original[key] = (await chrome.tabGroups.get(groupId)).color;
      await chrome.tabGroups.update(groupId, {color: wanted});
      colors.applied[key] = wanted;
      changed = true;
    } catch (error) {
      delete colors.applied[key];
      delete colors.original[key];
    }
  }
  for (const key of Object.keys(colors.applied)) {
    if (best.has(Number(key))) continue;
    const original = colors.original[key];
    delete colors.applied[key];
    delete colors.original[key];
    changed = true;
    if (original) await Promise.resolve(chrome.tabGroups.update(Number(key), {color: original})).catch(() => {});
  }
  if (changed) await saveColors(colors);
}

// Recolour the groups of driven tabs; debounced, so a burst of commands costs one pass.
export function syncGroups(activity, now, phaseOf) {
  latest = {activity, now, phaseOf};
  if (syncTimer !== null || !chrome.tabGroups?.update || !chrome.tabs?.get) return;
  syncTimer = setTimeout(() => syncNow().catch(() => { syncTimer = null; }), SYNC_DELAY_MS);
}

// Take over (control.js): orange groups and a still icon until Resume.
export function setPausedLook(value) {
  paused = Boolean(value);
  if (paused) stopSpin();
  if (latest) syncGroups(latest.activity, Date.now(), latest.phaseOf);
}
