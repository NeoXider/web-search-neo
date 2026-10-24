"""The companion's idle-tab auto-close and its macro jobs (jobs.js): Stop and ask answers ride
back on the server's own jobsReport, background jobs' tabs wear the padlock."""

from __future__ import annotations

from test_agent_activity import _BADGE_WORLD, _VERIFY
from test_chrome_bridge import _WORKER_READY, _node_worker_eval, requires_node

_JOBS_WORLD = _BADGE_WORLD + """
const removed = [];
globalThis.__removed = removed;
chrome.tabs.get = async tabId => ({id: tabId, windowId: 5, active: tabId === 72, pinned: false, audible: false,
                                   title: "Tab " + tabId, url: "https://example.test/" + tabId});
chrome.tabs.remove = async tabId => { removed.push(tabId); };
chrome.windows.get = async windowId => ({id: windowId, focused: true});
const notes = [];
globalThis.__notes = notes;
chrome.notifications = {
  create: (id, options, done) => { notes.push({id, ...options}); if (done) done(); },
  clear: () => {},
  // Chrome calls every listener (nodes.js listens for its pairing buttons too).
  onButtonClicked: {addListener: handler => {
    const previous = globalThis.__noteClick;
    globalThis.__noteClick = (id, index) => { if (previous) previous(id, index); handler(id, index); };
  }},
};
"""


@requires_node
def test_an_idle_tab_the_agent_opened_is_closed_and_nothing_else() -> None:
    outcome = _node_worker_eval(
        """
        await globalThis.__sleep(40);
        globalThis.__fire("alarm", {name: "agent-activity-sweep"});
        await globalThis.__sleep(60);
        const settings = await globalThis.__message({type: "companion.autoClose"});
        const off = await globalThis.__message({type: "companion.autoClose", settings: {enabled: false, minutes: 3}});
        return {removed: globalThis.__removed, settings, off};
        """,
        prelude=_JOBS_WORLD + """
        const old = Date.now() - 61 * 60 * 1000;
        globalThis.__seedSession({agent_activity: {activity: {
          "70": {tabId: 70, at: old, agent: "a", pid: 1, method: "tabs.create", claim: null, created: true},
          "71": {tabId: 71, at: old, agent: "a", pid: 1, method: "cdp.send", claim: null, created: false},
          "72": {tabId: 72, at: old, agent: "a", pid: 1, method: "tabs.create", claim: null, created: true},
          "73": {tabId: 73, at: Date.now() - 5 * 60 * 1000, agent: "a", pid: 1, method: "tabs.create", claim: null,
                 created: true},
        }, retired: []}});
        """,
    )
    # 70: opened by the agent, an hour idle -> closed. 71: the person's own tab the agent only
    # attached to -> never closed. 72: the person is looking at it -> left. 73: only five minutes.
    assert outcome["removed"] == [70]
    assert outcome["settings"] == {"enabled": True, "minutes": 60}
    # Off is remembered, and the minutes are kept within 6 min .. 24 h.
    assert outcome["off"] == {"enabled": False, "minutes": 6}


@requires_node
def test_a_jobs_report_lists_jobs_and_carries_stop_and_answers_back() -> None:
    outcome = _node_worker_eval(
        _WORKER_READY
        + _VERIFY
        + """
        const socket = await openSocket();
        const job = (id, state, extra = {}) => ({job_id: id, macro: "m-" + id, state, background: true,
                                                started_at: 1, running_seconds: 3, tab_ids: [33], ...extra});
        const first = await run(socket, "r1", "companion.jobsReport",
          {pid: 900, jobs: [job("j1", "running"), job("j2", "waiting", {question: {text: "Go?", options: ["yes", "no"]}})]});
        const listed = await globalThis.__message({type: "companion.jobs"});
        await globalThis.__message({type: "companion.cancelJob", job_id: "j1"});
        globalThis.__noteClick("wsn-ask-j2", 0);
        await globalThis.__sleep(20);
        const second = await run(socket, "r2", "companion.jobsReport",
          {pid: 900, jobs: [job("j1", "running"), job("j2", "waiting", {question: {text: "Go?", options: ["yes", "no"]}})]});
        const third = await run(socket, "r3", "companion.jobsReport",
          {pid: 900, jobs: [job("j1", "cancelled"), job("j2", "done")]});
        await run(socket, "t1", "tabs.get", {tabId: 33}, {label: "main.py#1"});
        await globalThis.__sleep(20);
        const wrench = globalThis.__badges.filter(c => c.tabId === 33 && c.kind === "text").at(-1);
        return {first, listed, second, third, notes: globalThis.__notes, wrench};
        """,
        prelude=_JOBS_WORLD,
    )
    assert outcome["first"]["result"] == {"cancel": [], "answers": {}}
    rows = outcome["listed"]["jobs"]
    assert [row["job_id"] for row in rows] == ["j1", "j2"] or [row["job_id"] for row in rows] == ["j2", "j1"]
    # Stop pressed in the popup and "yes" pressed on the notification travel back on the next report.
    assert outcome["second"]["result"] == {"cancel": ["j1"], "answers": {"j2": "yes"}}
    assert outcome["third"]["result"] == {"cancel": [], "answers": {}}
    ids = [note["id"] for note in outcome["notes"]]
    assert "wsn-ask-j2" in ids and "wsn-job-j1" in ids
    ask = next(note for note in outcome["notes"] if note["id"] == "wsn-ask-j2")
    assert [button["title"] for button in ask["buttons"]] == ["yes", "no"]
    # The tab a background job drove wears the padlock on the toolbar badge.
    assert outcome["wrench"]["text"] == "\U0001f512"
