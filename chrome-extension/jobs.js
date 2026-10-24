// Macro jobs reported by Web Search Neo servers (web_search_neo/macro_jobs.py). Every couple of
// seconds a server with a running script sends companion.jobsReport with its jobs; the popup
// lists them with Stop and, for a job waiting on an `ask` step, the answer buttons. The report's
// own answer carries those presses back to the server, so nothing here needs a channel of its
// own. A notification says when a background job ends, and asks the question with buttons.
//
// The tabs a background job drove are kept in macroTabIds: the toolbar badge shows the padlock
// there instead of the agent's robot.

const STORE = "macro_jobs";
const REPORTER_STALE_MS = 15000;
const ACTIVE = new Set(["starting", "running", "waiting"]);
const ENDED_TEXT = {done: "finished", failed: "failed", cancelled: "was stopped", interrupted: "was interrupted"};

export const macroTabIds = new Set();
let cache = null;

async function load() {
  if (!cache) {
    try {
      cache = (await chrome.storage?.session?.get(STORE))?.[STORE] || null;
    } catch (error) {
      cache = null;
    }
    cache = {reporters: {}, cancel: {}, answers: {}, seen: {}, asks: {}, ...(cache || {})};
    rebuildMacroTabs();
  }
  return cache;
}

function save() {
  Promise.resolve(chrome.storage?.session?.set({[STORE]: cache})).catch(() => {});
}

function rebuildMacroTabs() {
  macroTabIds.clear();
  for (const reporter of Object.values(cache?.reporters || {})) {
    for (const job of reporter.jobs || []) {
      if (!job.background) continue;
      for (const tabId of job.tab_ids || []) if (Number.isInteger(tabId)) macroTabIds.add(tabId);
    }
  }
}

function label(job) {
  return String(job.macro || "inline script").slice(0, 60);
}

function notify(id, options) {
  try {
    chrome.notifications?.create?.(id, {type: "basic", iconUrl: "icons/slime-128.png", priority: 1, ...options},
      () => void chrome.runtime?.lastError);
  } catch (error) {
    // Notifications are a courtesy; the popup shows the same thing.
  }
}

// Say it once per change: a background job that ended, any job that started waiting.
function announce(state, job) {
  const before = state.seen[job.job_id];
  state.seen[job.job_id] = job.state;
  if (before === job.state) return;
  if (job.state === "waiting" && job.question) {
    const options = (job.question.options || []).map(String).slice(0, 2);
    state.asks[job.job_id] = options;
    notify(`wsn-ask-${job.job_id}`, {
      title: `Macro ${label(job)} asks`,
      message: String(job.question.text || "Continue?").slice(0, 200),
      buttons: options.map(title => ({title: title.slice(0, 40)})),
      requireInteraction: true,
    });
    return;
  }
  if (before && ACTIVE.has(before) && ENDED_TEXT[job.state] && job.background) {
    const detail = job.state === "done" ? "" : ` ${String(job.error || job.failed_step || "").slice(0, 120)}`;
    notify(`wsn-job-${job.job_id}`, {title: `Macro ${label(job)} ${ENDED_TEXT[job.state]}`, message: detail || " "});
  }
}

export const jobCommands = {
  async "companion.jobsReport"(params) {
    const state = await load();
    const pid = String(params?.pid ?? "?");
    const jobs = Array.isArray(params?.jobs) ? params.jobs.slice(-30) : [];
    state.reporters[pid] = {at: Date.now(), jobs};
    const reply = {cancel: [], answers: {}};
    for (const job of jobs) {
      const id = String(job?.job_id || "");
      if (!id) continue;
      if (state.cancel[id]) {
        if (ACTIVE.has(job.state)) reply.cancel.push(id);
        else delete state.cancel[id];
      }
      if (id in state.answers) {
        if (job.state === "waiting") reply.answers[id] = state.answers[id];
        else delete state.answers[id];
      }
      announce(state, job);
    }
    // A server that has not reported for a long while and has nothing running is forgotten.
    for (const [key, reporter] of Object.entries(state.reporters)) {
      const stale = Date.now() - reporter.at > 60 * 60 * 1000;
      if (stale && !(reporter.jobs || []).some(job => ACTIVE.has(job.state))) delete state.reporters[key];
    }
    rebuildMacroTabs();
    save();
    return reply;
  },
};

export async function listJobs() {
  const state = await load();
  const now = Date.now();
  const rows = [];
  for (const [pid, reporter] of Object.entries(state.reporters)) {
    const silent = now - reporter.at > REPORTER_STALE_MS;
    for (const job of reporter.jobs || []) {
      const shown = {...job, reporter_pid: pid};
      // A running job whose server stopped reporting: the server is gone or stuck.
      if (silent && ACTIVE.has(job.state)) shown.state = "unknown";
      shown.stop_requested = Boolean(state.cancel[job.job_id]);
      shown.answer_sent = job.job_id in state.answers;
      rows.push(shown);
    }
  }
  rows.sort((a, b) => (ACTIVE.has(b.state) - ACTIVE.has(a.state)) || ((b.started_at || 0) - (a.started_at || 0)));
  return {jobs: rows.slice(0, 30), now};
}

async function requestCancel(jobId) {
  const state = await load();
  state.cancel[String(jobId)] = Date.now();
  save();
  return {stop_requested: String(jobId)};
}

async function sendAnswer(jobId, value) {
  const state = await load();
  state.answers[String(jobId)] = value;
  save();
  try { chrome.notifications?.clear?.(`wsn-ask-${jobId}`); } catch (error) {}
  return {answered: String(jobId), answer: value};
}

// Popup messages; a promise, or null for a type this module does not own.
export function jobsMessage(message) {
  if (message?.type === "companion.jobs") return listJobs();
  if (message?.type === "companion.cancelJob") return requestCancel(message.job_id);
  if (message?.type === "companion.answerJob") return sendAnswer(message.job_id, message.answer);
  return null;
}

chrome.notifications?.onButtonClicked?.addListener((notificationId, index) => {
  if (!String(notificationId).startsWith("wsn-ask-")) return;
  const jobId = String(notificationId).slice("wsn-ask-".length);
  load().then(state => {
    const options = state.asks[jobId] || [];
    if (index < options.length) sendAnswer(jobId, options[index]);
  });
});
load();
