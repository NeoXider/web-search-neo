// Pure bookkeeping for "which tabs is an agent on": no chrome.* in here, so the
// state transitions, expiry and badge wording can be unit-tested under Node.
//
// An activity map is a plain object keyed by tab id (as a string, because it is
// persisted as JSON in chrome.storage.session):
//   {"42": {tabId: 42, at: 1700000000000, agent: "claude", pid: 1234,
//           method: "cdp.send", claim: "main.py#1234"}}
// Every function returns a new map rather than mutating the one it was given.

export const ACTIVE_MS = 60 * 1000;
// Yellow from one minute to six after the last action (the owner, 2026-10-24: "a minute
// without action - yellow for five minutes").
export const RECENT_MS = 6 * 60 * 1000;
// Grey from then on: the agent left the tab, which is not an error, so not red (the owner,
// 2026-10-28). The mark lasts until STALE_MS - the auto-close
// time, an hour unless the popup says otherwise - when a tab an agent opened is closed (if
// auto-close is on) and any other tab drops the mark.
export const STALE_MS = 60 * 60 * 1000;
export const MAX_TRACKED_TABS = 200;
export const ACTIVE_COLOR = "#16a34a";
export const RECENT_COLOR = "#ca8a04";
export const STALE_COLOR = "#6b7280";
// Who is on the tab, by shape (the owner, 2026-10-24): an agent's robot, a background macro's wrench.
export const BADGE_TEXT = "\u{1F916}";
export const MACRO_BADGE_TEXT = "\u{1F512}";
const LABEL_LIMIT = 64;

export function normalizeTabId(value) {
  if (typeof value === "boolean" || value === null || value === undefined || value === "") return null;
  const tabId = Number(value);
  return Number.isInteger(tabId) && tabId > 0 ? tabId : null;
}

function clean(value) {
  // Printable text only: the label ends up in a tooltip and a popup row.
  return String(value ?? "").replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, LABEL_LIMIT);
}

// The name a person reads: an explicit agent name, else the daemon's
// "program#pid" label, else whatever the frame says about the process.
export function agentLabel(agent) {
  if (!agent || typeof agent !== "object") return "agent";
  const named = clean(agent.name) || clean(agent.label);
  if (named) return named;
  const program = clean(agent.program);
  if (program && Number.isInteger(agent.pid)) return `${program}#${agent.pid}`;
  return program || "agent";
}

// Entries beyond the cap are dropped oldest first; a browser with hundreds of
// driven tabs in five minutes is not a case worth unbounded storage.
function capped(map) {
  const keys = Object.keys(map);
  if (keys.length <= MAX_TRACKED_TABS) return map;
  const keep = keys.sort((a, b) => map[b].at - map[a].at).slice(0, MAX_TRACKED_TABS);
  return Object.fromEntries(keep.map(key => [key, map[key]]));
}

export function recordActivity(map, tabId, agent, now, extra = {}) {
  const id = normalizeTabId(tabId);
  if (id === null) return map || {};
  const entry = {
    tabId: id,
    at: Number(now),
    agent: agentLabel(agent),
    pid: agent && Number.isInteger(agent.pid) ? agent.pid : null,
    method: clean(extra.method) || null,
    claim: clean(extra.claim ?? agent?.claim_holder) || null,
    // Sticky: a tab the agent opened stays "the agent's own" whatever it does next,
    // which is what lets auto-close leave the person's own tabs alone.
    created: Boolean(extra.created || map?.[String(id)]?.created),
  };
  return capped({...(map || {}), [String(id)]: entry});
}

export function forgetActivity(map, tabId) {
  const key = String(normalizeTabId(tabId));
  if (!map || !(key in map)) return map || {};
  const next = {...map};
  delete next[key];
  return next;
}

export function activityPhase(entry, now, staleMs = STALE_MS) {
  const age = Number(now) - Number(entry?.at);
  if (!Number.isFinite(age)) return "expired";
  if (age < ACTIVE_MS) return "active";
  if (age < RECENT_MS) return "recent";
  if (age < Math.max(RECENT_MS, Number(staleMs) || STALE_MS)) return "stale";
  return "expired";
}

// Splits a map into what is still worth showing and the tab ids that just
// aged out (whose per-tab badge has to be handed back to the global one).
export function expireActivity(map, now, staleMs = STALE_MS) {
  const kept = {};
  const expired = [];
  for (const [key, entry] of Object.entries(map || {})) {
    if (activityPhase(entry, now, staleMs) === "expired") expired.push(Number(key));
    else kept[key] = entry;
  }
  return {map: kept, expired};
}

export function formatAge(ageMs) {
  const age = Math.max(0, Number(ageMs) || 0);
  if (age < ACTIVE_MS) return "active now";
  if (age < 60000) return `last action ${Math.max(1, Math.floor(age / 1000))} s ago`;
  return `last action ${Math.floor(age / 60000)} min ago`;
}

export function describeActivity(entry, now) {
  return `Agent ${entry.agent || "agent"} — ${formatAge(Number(now) - entry.at)}`;
}

// The per-tab badge, or null when the tab should show the global badge again.
export function badgeFor(entry, now, staleMs = STALE_MS, macro = false) {
  const phase = activityPhase(entry, now, staleMs);
  if (phase === "expired") return null;
  return {
    text: macro ? MACRO_BADGE_TEXT : BADGE_TEXT,
    color: phase === "active" ? ACTIVE_COLOR : (phase === "recent" ? RECENT_COLOR : STALE_COLOR),
    title: `Web Search Neo — ${describeActivity(entry, now)}`,
  };
}

// Milliseconds until the next entry changes phase (active -> recent -> stale ->
// expired), or null when nothing is tracked.
export function nextChangeIn(map, now, staleMs = STALE_MS) {
  let soonest = null;
  for (const entry of Object.values(map || {})) {
    const age = Number(now) - entry.at;
    const edge = age < ACTIVE_MS ? ACTIVE_MS : (age < RECENT_MS ? RECENT_MS : Math.max(RECENT_MS, staleMs));
    const wait = Math.max(0, edge - age);
    if (soonest === null || wait < soonest) soonest = wait;
  }
  return soonest;
}

// Rows for the popup, newest first. `tabs` maps tab id -> {title, url, windowId}.
export function listRecent(map, now, tabs = {}, staleMs = STALE_MS) {
  return Object.values(map || {})
    .filter(entry => activityPhase(entry, now, staleMs) !== "expired")
    .sort((a, b) => b.at - a.at)
    .map(entry => {
      const tab = tabs[entry.tabId] || {};
      const phase = activityPhase(entry, now, staleMs);
      return {
        tab_id: entry.tabId,
        window_id: Number.isInteger(tab.windowId) ? tab.windowId : null,
        title: clean(tab.title) || clean(tab.url) || `Tab ${entry.tabId}`,
        url: String(tab.url || ""),
        agent: entry.agent,
        pid: entry.pid,
        claim: entry.claim,
        phase,
        created: Boolean(entry.created),
        age_ms: Math.max(0, now - entry.at),
        status_text: phase === "active" ? "active now" : `idle ${Math.max(1, Math.floor((now - entry.at) / 60000))} min`,
      };
    });
}

// Tabs an agent opened and has not touched for closeMs: auto-close takes them.
// A tab the agent only attached to (the person's own) is never in this list.
export function dueForClose(map, now, closeMs) {
  const limit = Number(closeMs);
  if (!Number.isFinite(limit) || limit <= 0) return [];
  return Object.values(map || {})
    .filter(entry => entry.created && Number(now) - Number(entry.at) >= limit)
    .map(entry => entry.tabId);
}
