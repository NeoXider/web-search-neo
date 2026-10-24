// The popup's Macros card and the idle AI tab setting.
//
// Loaded after popup.js as a classic script, so it reuses that file's send(), messageNode
// and PREVIEW. Jobs come from jobs.js in the service worker, which hears them from the
// servers' companion.jobsReport; Stop and the ask answers go back the same way. Every string
// a job reports goes into the page through textContent only.

const jobsCard = document.querySelector("#macro-jobs");
const jobListNode = document.querySelector("#job-list");
const jobCountNode = document.querySelector("#macro-jobs-count");
const autoCloseInput = document.querySelector("#auto-close");
const autoCloseMinutes = document.querySelector("#auto-close-minutes");
const saveAutoClose = document.querySelector("#save-auto-close");
const JOB_ACTIVE = new Set(["starting", "running", "waiting"]);

function jobSeconds(job) {
  const seconds = Math.max(0, Number(job.running_seconds) || 0);
  return seconds < 90 ? `${Math.round(seconds)} s` : `${Math.round(seconds / 60)} min`;
}

function jobMeta(job) {
  if (job.state === "waiting") return "waiting for an answer";
  if (JOB_ACTIVE.has(job.state)) {
    const progress = job.progress || {};
    const where = progress.step ? `step ${progress.step} · ${progress.action || ""}` : "starting";
    return `${where} · ${jobSeconds(job)}${job.background ? " · background" : ""}`;
  }
  if (job.state === "failed") return String(job.error || job.failed_step || "failed");
  if (job.state === "unknown") return "its server stopped reporting";
  const tail = (job.recent || job.trace || []).slice(-1)[0];
  return tail ? String(tail) : `${job.state} · ${jobSeconds(job)}`;
}

function jobButton(text, onClick, secondary = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = secondary ? "btn secondary" : "btn";
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function jobRow(job) {
  const item = document.createElement("li");
  item.className = "job-row";
  const title = document.createElement("span");
  title.className = "job-title";
  title.textContent = String(job.macro || "inline script");
  title.title = String(job.job_id || "");
  const state = document.createElement("span");
  state.className = "job-state";
  state.dataset.state = String(job.state || "");
  state.textContent = job.stop_requested && JOB_ACTIVE.has(job.state) ? "stopping" : String(job.state || "");
  const meta = document.createElement("span");
  meta.className = "job-meta";
  meta.textContent = jobMeta(job);
  item.append(title, state, meta);
  if (job.state === "waiting" && job.question) {
    const question = document.createElement("p");
    question.className = "job-question";
    question.textContent = String(job.question.text || "");
    item.append(question);
  }
  if (JOB_ACTIVE.has(job.state)) {
    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (job.state === "waiting" && job.question && !job.answer_sent) {
      for (const option of (job.question.options || []).slice(0, 4)) {
        actions.append(jobButton(String(option), () => answerJob(job.job_id, option)));
      }
    }
    if (!job.stop_requested) actions.append(jobButton("Stop", () => stopJob(job.job_id), true));
    item.append(actions);
  }
  return item;
}

function renderJobs(rows) {
  const list = Array.isArray(rows) ? rows : [];
  jobsCard.hidden = list.length === 0;
  jobCountNode.textContent = String(list.filter(job => JOB_ACTIVE.has(job.state)).length);
  if (!jobListNode.replaceChildren) return;
  jobListNode.replaceChildren(...list.map(jobRow));
}

async function refreshJobs() {
  try {
    renderJobs((await send("companion.jobs"))?.jobs);
  } catch (error) {
    // An older worker without jobs: the card stays hidden.
  }
}

async function stopJob(jobId) {
  try {
    await send("companion.cancelJob", {job_id: jobId});
    messageNode.textContent = "Stop sent; the macro stops before its next step.";
  } catch (error) {
    messageNode.textContent = error.message;
  }
  refreshJobs();
}

async function answerJob(jobId, answer) {
  try {
    await send("companion.answerJob", {job_id: jobId, answer});
    messageNode.textContent = `Answered "${answer}".`;
  } catch (error) {
    messageNode.textContent = error.message;
  }
  refreshJobs();
}

async function loadAutoClose() {
  try {
    const settings = await send("companion.autoClose");
    autoCloseInput.checked = settings?.enabled !== false;
    autoCloseMinutes.value = String(settings?.minutes || 60);
  } catch (error) {
    autoCloseInput.disabled = true;
  }
}

saveAutoClose.addEventListener("click", async () => {
  try {
    const settings = await send("companion.autoClose", {
      settings: {enabled: autoCloseInput.checked, minutes: Number.parseInt(autoCloseMinutes.value, 10) || 60},
    });
    autoCloseMinutes.value = String(settings.minutes);
    messageNode.textContent = settings.enabled
      ? `Idle AI tabs close after ${settings.minutes} min.`
      : "Idle AI tabs stay open (red after six quiet minutes).";
  } catch (error) {
    messageNode.textContent = error.message;
  }
});
autoCloseInput.addEventListener("change", () => saveAutoClose.click());

// --- preview: sample jobs in the exact shape jobs.js returns -----------------------------------
let previewAuto = {enabled: true, minutes: 60};
const previewJobRows = [
  {job_id: "job-101500-a1b2c3", macro: "gpt-image", state: "running", background: true, running_seconds: 140,
   progress: {step: "6", action: "run_script"}, recent: ["5 run_script ok"]},
  {job_id: "job-101800-d4e5f6", macro: "youtube-publish", state: "waiting", background: true, running_seconds: 31,
   question: {text: "Publish the Short publicly now?", options: ["yes", "no"]}},
  {job_id: "job-095900-0a0b0c", macro: "daily-report", state: "done", background: true, running_seconds: 52,
   recent: ["7 return"]},
];
window.__wsnPreviewJobs = function (type, extra) {
  if (type === "companion.jobs") return Promise.resolve({jobs: previewJobRows, now: Date.now()});
  if (type === "companion.cancelJob" || type === "companion.answerJob") {
    const job = previewJobRows.find(row => row.job_id === extra.job_id);
    if (job && type === "companion.cancelJob") job.stop_requested = true;
    if (job && type === "companion.answerJob") job.answer_sent = true;
    return Promise.resolve({ok: true});
  }
  if (type === "companion.autoClose") {
    if (extra?.settings) previewAuto = {...previewAuto, ...extra.settings};
    return Promise.resolve(previewAuto);
  }
  return Promise.resolve(null);
};

loadAutoClose();
refreshJobs();
setInterval(refreshJobs, 1000);
