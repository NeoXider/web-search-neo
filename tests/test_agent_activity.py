"""Per-tab agent activity in the companion: the pure helpers, the toolbar badge
and popup plumbing in the worker, and the worker's protocol-2 handshake."""

from __future__ import annotations

import json
import subprocess

from test_chrome_bridge import (
    EXTENSION_DIR,
    NODE,
    TEST_TOKEN,
    _WORKER_READY,
    _node_worker_eval,
    requires_node,
)

ACTIVITY_MODULE = (EXTENSION_DIR / "agent-activity.js").as_uri()
AUTH_MODULE = (EXTENSION_DIR / "bridge-auth.js").as_uri()


def _node_module_eval(module_uri: str, body: str):
    script = (
        f"const m = await import({json.dumps(module_uri)});\n"
        f"const result = await (async () => {{\n{body}\n}})();\n"
        "process.stdout.write(JSON.stringify(result ?? null));\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@requires_node
def test_activity_phases_badges_and_expiry() -> None:
    out = _node_module_eval(
        ACTIVITY_MODULE,
        """
        const t0 = 1_000_000;
        const agent = {label: "main.py#7", name: "claude", pid: 7, claim_holder: "main.py#7"};
        let map = m.recordActivity({}, 42, agent, t0, {method: "cdp.send"});
        map = m.recordActivity(map, "43", {label: "x#8", pid: 8}, t0 - 200_000);
        const ignored = m.recordActivity(map, "nope", agent, t0);
        const at = offset => ({
          phase: m.activityPhase(map["42"], t0 + offset),
          badge: m.badgeFor(map["42"], t0 + offset),
          age: m.formatAge(offset),
        });
        const expired = m.expireActivity(map, t0 + 120_000);
        return {
          entry: map["42"],
          ignoredSame: Object.keys(ignored).length,
          fresh: at(1_000), recent: at(150_000), gone: at(300_000), young: at(35_000),
          expiredIds: expired.expired, kept: Object.keys(expired.map),
          next: m.nextChangeIn(map, t0 + 10_000),
          nextEmpty: m.nextChangeIn({}, t0),
          forgotten: Object.keys(m.forgetActivity(map, 42)),
          labels: [m.agentLabel(null), m.agentLabel({program: "a.py", pid: 3}),
                   m.agentLabel({label: "bad\\u0007label"})],
          ids: [m.normalizeTabId(true), m.normalizeTabId(0), m.normalizeTabId("12"), m.normalizeTabId(1.5)],
          rows: m.listRecent(map, t0 + 90_000, {42: {title: "Docs", url: "https://d", windowId: 3}}),
        };
        """,
    )
    assert out["entry"] == {
        "tabId": 42, "at": 1_000_000, "agent": "claude", "pid": 7,
        "method": "cdp.send", "claim": "main.py#7",
    }
    assert out["ignoredSame"] == 2
    assert out["fresh"]["phase"] == "active"
    assert out["fresh"]["badge"] == {
        "text": "AI", "color": "#16a34a",
        "title": "Web Search Neo — Agent claude — active now",
    }
    assert out["recent"]["phase"] == "recent"
    assert out["recent"]["badge"]["color"] == "#d97706"
    assert out["recent"]["badge"]["title"].endswith("Agent claude — last action 2 min ago")
    # Under a minute is counted in seconds: "1 min ago" for a 35 s gap misleads.
    assert out["young"]["age"] == "last action 35 s ago"
    assert out["gone"] == {"phase": "expired", "badge": None, "age": "last action 5 min ago"}
    assert out["expiredIds"] == [43] and out["kept"] == ["42"]
    assert out["next"] == 20_000
    assert out["nextEmpty"] is None
    assert out["forgotten"] == ["43"]
    assert out["labels"] == ["agent", "a.py#3", "badlabel"]
    assert out["ids"] == [None, None, 12, None]
    assert out["rows"] == [
        {
            "tab_id": 42, "window_id": 3, "title": "Docs", "url": "https://d",
            "agent": "claude", "pid": 7, "claim": "main.py#7", "phase": "recent",
            "age_ms": 90_000, "status_text": "1 min ago",
        },
        {
            "tab_id": 43, "window_id": None, "title": "Tab 43", "url": "",
            "agent": "x#8", "pid": 8, "claim": None, "phase": "recent",
            "age_ms": 290_000, "status_text": "4 min ago",
        },
    ]


@requires_node
def test_the_activity_map_is_capped() -> None:
    size = _node_module_eval(
        ACTIVITY_MODULE,
        """
        let map = {};
        for (let id = 1; id <= m.MAX_TRACKED_TABS + 5; id += 1) map = m.recordActivity(map, id, null, id);
        return [Object.keys(map).length, "1" in map, String(m.MAX_TRACKED_TABS + 5) in map];
        """,
    )
    assert size == [200, False, True]


# A worker world that records every per-tab badge call and serves tab lookups.
_BADGE_WORLD = """
const badgeCalls = [];
globalThis.__badges = badgeCalls;
chrome.action.setBadgeText = details => { badgeCalls.push({kind: "text", ...details}); };
chrome.action.setBadgeBackgroundColor = details => { badgeCalls.push({kind: "color", ...details}); };
chrome.action.setTitle = details => { badgeCalls.push({kind: "title", ...details}); };
chrome.tabs.onUpdated = {addListener: handler => { globalThis.__tabUpdated = handler; }};
const focused = [];
globalThis.__focused = focused;
chrome.tabs.get = async tabId => {
  if (tabId === 99) throw new Error("No tab with id: 99");
  return {id: tabId, windowId: 5, title: "Tab " + tabId + " title", url: "chrome://settings"};
};
chrome.tabs.update = async (tabId, patch) => { focused.push({tabId, ...patch}); return {id: tabId, windowId: 5}; };
chrome.windows.update = async (windowId, patch) => { focused.push({windowId, ...patch}); return {id: windowId}; };
"""

_VERIFY = """
const openSocket = async () => {
  await worker.connect();
  const socket = globalThis.__sockets[globalThis.__sockets.length - 1];
  socket.onopen();
  await globalThis.__handshake(socket, TOKEN);
  return socket;
};
const run = async (socket, id, method, params, agent) => {
  const before = socket.sent.length;
  socket.onmessage({data: JSON.stringify({type: "command", id, method, params, agent})});
  await globalThis.__waitFor(() => socket.sent.length > before, "the answer to " + id);
  return JSON.parse(socket.sent[socket.sent.length - 1]);
};
const lastFor = (tabId, kind) => globalThis.__badges.filter(c => c.tabId === tabId && c.kind === kind).at(-1);
"""


@requires_node
def test_a_relayed_command_badges_its_tab_and_lists_it_in_the_popup() -> None:
    outcome = _node_worker_eval(
        _WORKER_READY
        + _VERIFY
        + """
        const socket = await openSocket();
        const agent = {label: "main.py#41", name: "claude", pid: 41, claim_holder: "main.py#41"};
        await run(socket, "c1", "tabs.get", {tabId: 12}, agent);
        await run(socket, "c2", "tabs.get", {tabId: 99}, {label: "gone#1"});
        await run(socket, "c3", "tabs.list", {}, agent);
        await globalThis.__sleep(30);
        const text = lastFor(12, "text");
        const color = lastFor(12, "color");
        const title = lastFor(12, "title");
        const listed = await globalThis.__message({type: "companion.agentTabs"});
        const focus = await globalThis.__message({type: "companion.focusAgentTab", tab_id: 12});
        const refused = await globalThis.__message({type: "companion.focusAgentTab", tab_id: 777});
        const stored = globalThis.__session().agent_activity;

        // Navigation resets Chrome's per-tab values; the worker paints again.
        const before = globalThis.__badges.length;
        globalThis.__tabUpdated(12, {status: "loading"});
        await globalThis.__sleep(20);
        const repainted = globalThis.__badges.slice(before).some(c => c.tabId === 12 && c.text === "AI");

        globalThis.__fire("tabRemoved", 12);
        await globalThis.__sleep(20);
        const afterClose = await globalThis.__message({type: "companion.agentTabs"});
        return {text, color, title, listed, focus, refused, stored: Object.keys(stored.activity),
                focused: globalThis.__focused, repainted, afterClose};
        """,
        prelude=_BADGE_WORLD,
    )
    assert outcome["text"]["text"] == "AI"
    assert outcome["color"]["color"] == "#16a34a"
    assert outcome["title"]["title"] == "Web Search Neo — Agent claude — active now"
    rows = outcome["listed"]["tabs"]
    assert [row["tab_id"] for row in rows] == [12], "a closed tab must not be listed"
    assert rows[0]["title"] == "Tab 12 title"
    assert rows[0]["agent"] == "claude" and rows[0]["claim"] == "main.py#41"
    assert rows[0]["status_text"] == "active now" and rows[0]["phase"] == "active"
    assert outcome["focus"] == {"focused": 12}
    assert outcome["focused"] == [
        {"tabId": 12, "active": True},
        {"windowId": 5, "focused": True},
    ]
    assert "no recent agent activity" in outcome["refused"]["error"]
    assert "12" in outcome["stored"]
    assert outcome["repainted"]
    assert outcome["afterClose"]["tabs"] == []


@requires_node
def test_badges_expire_back_to_the_global_badge_after_an_eviction() -> None:
    """A fresh worker finds stale activity in session storage and hands the tab back."""
    outcome = _node_worker_eval(
        """
        await globalThis.__sleep(60);
        const calls = globalThis.__badges.filter(c => c.tabId === 8);
        const alarm = globalThis.__alarms().filter(a => a.name === "agent-activity-sweep");
        globalThis.__fire("alarm", {name: "agent-activity-sweep"});
        await globalThis.__sleep(30);
        return {calls, alarm, stored: globalThis.__session().agent_activity,
                global: globalThis.__badges.filter(c => c.tabId === undefined && c.kind === "text").at(-1)};
        """,
        prelude=_BADGE_WORLD
        + """
        globalThis.__seedSession({agent_activity: {activity: {
          "8": {tabId: 8, at: Date.now() - 6 * 60 * 1000, agent: "old", pid: 1, method: null, claim: null},
          "9": {tabId: 9, at: Date.now() - 60 * 1000, agent: "new", pid: 2, method: null, claim: null},
        }, retired: []}});
        """,
    )
    kinds = {call["kind"]: call for call in outcome["calls"]}
    # Tab 8 aged out: it now mirrors the global badge (OFF while disconnected).
    assert kinds["text"]["text"] == outcome["global"]["text"] == "OFF"
    assert kinds["color"]["color"] in {"#dc2626", "#64748b"}
    assert "Web Search Neo Companion" in kinds["title"]["title"]
    assert list(outcome["stored"]["activity"]) == ["9"]
    assert 8 in outcome["stored"]["retired"]
    assert outcome["alarm"] and outcome["alarm"][-1]["periodInMinutes"] == 1


@requires_node
def test_polling_the_agent_list_neither_repaints_nor_rewrites_storage() -> None:
    """The popup asks every second; only an expired or closed tab may cause writes."""
    outcome = _node_worker_eval(
        """
        await globalThis.__sleep(60);
        const badgesBefore = globalThis.__badges.length;
        const writesBefore = globalThis.__writes.length;
        const lists = [];
        for (let i = 0; i < 3; i += 1) {
          lists.push(await globalThis.__message({type: "companion.agentTabs"}));
          await globalThis.__sleep(5);
        }
        return {
          badges: globalThis.__badges.slice(badgesBefore),
          writes: globalThis.__writes.slice(writesBefore),
          listed: lists.map(l => l.tabs.map(row => row.tab_id)),
          stored: Object.keys(globalThis.__session().agent_activity.activity),
        };
        """,
        prelude=_BADGE_WORLD
        + """
        const writes = [];
        globalThis.__writes = writes;
        const realSet = chrome.storage.session.set;
        chrome.storage.session.set = async items => {
          if ("agent_activity" in items) writes.push(Object.keys(items.agent_activity.activity));
          return realSet(items);
        };
        globalThis.__seedSession({agent_activity: {activity: {
          "9": {tabId: 9, at: Date.now() - 60 * 1000, agent: "new", pid: 2, method: null, claim: null},
          "99": {tabId: 99, at: Date.now() - 60 * 1000, agent: "gone", pid: 3, method: null, claim: null},
        }, retired: []}});
        """,
    )
    assert outcome["badges"] == [], "listing must not repaint live tabs"
    assert outcome["listed"] == [[9], [9], [9]]
    # The closed tab is pruned once; nothing else is written back.
    assert outcome["writes"] == [["9"]]
    assert outcome["stored"] == ["9"]


# -- protocol 2 from the worker's side ---------------------------------------


@requires_node
def test_the_worker_hello_carries_no_token_and_answers_only_a_proven_daemon() -> None:
    outcome = _node_worker_eval(
        _WORKER_READY
        + """
        await worker.connect();
        const good = globalThis.__sockets.at(-1);
        good.onopen();
        const done = await globalThis.__handshake(good, TOKEN);
        const status = await globalThis.__message({type: "companion.status"});
        const expected = await globalThis.__hmac(
          TOKEN, "wsn-bridge-client|" + done.serverNonce + "|" + done.hello.nonce);

        good.onclose({code: 1006, reason: ""});
        await worker.connect();
        const squatter = globalThis.__sockets.at(-1);
        squatter.onopen();
        const refused = await globalThis.__handshake(squatter, "c3".repeat(32));

        await worker.connect();
        const rude = globalThis.__sockets.at(-1);
        rude.onopen();
        rude.onmessage({data: JSON.stringify({type: "command", id: "x", method: "tabs.list"})});
        return {hello: done.hello, auth: done.auth, expected, connected: status.connected,
                squatterSent: squatter.sent.length, squatterState: squatter.readyState,
                rudeSent: rude.sent.length, rudeState: rude.readyState, refusedAuth: refused.auth};
        """
    )
    hello = outcome["hello"]
    assert hello["protocol"] == 2 and hello["role"] == "extension"
    assert "token" not in hello and TEST_TOKEN not in json.dumps(hello)
    assert len(hello["nonce"]) == 32
    assert outcome["auth"] == {"type": "auth", "proof": outcome["expected"]}
    assert outcome["connected"] is True
    # A daemon that cannot prove the secret gets nothing beyond the hello.
    assert outcome["squatterSent"] == 1 and outcome["squatterState"] == 3
    assert outcome["refusedAuth"] is None
    assert outcome["rudeSent"] == 1 and outcome["rudeState"] == 3


@requires_node
def test_answer_challenge_rejects_malformed_challenges() -> None:
    out = _node_module_eval(
        AUTH_MODULE,
        f"""
        const token = {json.dumps(TEST_TOKEN)};
        const nonce = "ab".repeat(16);
        const serverNonce = "cd".repeat(16);
        const proof = await m.hmacHex(token, m.serverProofMessage(nonce, serverNonce));
        const ok = await m.answerChallenge(token, nonce, {{protocol: 2, nonce: serverNonce, proof}});
        return {{
          ok: ok && ok.type,
          wrongProtocol: await m.answerChallenge(token, nonce, {{protocol: 1, nonce: serverNonce, proof}}),
          shortNonce: await m.answerChallenge(token, nonce, {{protocol: 2, nonce: "ab", proof}}),
          upper: await m.answerChallenge(token, nonce, {{protocol: 2, nonce: serverNonce, proof: proof.toUpperCase()}}),
          reflected: await m.answerChallenge(token, nonce, {{protocol: 2, nonce: serverNonce,
            proof: await m.hmacHex(token, m.clientProofMessage(serverNonce, nonce))}}),
        }};
        """,
    )
    assert out == {
        "ok": "auth", "wrongProtocol": None, "shortNonce": None, "upper": None, "reflected": None,
    }


@requires_node
def test_popup_renders_agent_rows_with_text_only() -> None:
    """Tab titles are page-controlled, so rows are built from textContent."""
    script = f"""
globalThis.window = globalThis;
const made = [];
const node = () => ({{
  textContent: "", title: "", value: "", dataset: {{}}, style: {{}}, hidden: false, children: [],
  attributes: {{}}, listeners: {{}},
  addEventListener(type, cb) {{ this.listeners[type] = cb; }},
  setAttribute(k, v) {{ this.attributes[k] = v; }},
  append(...items) {{ this.children.push(...items); }},
  replaceChildren(...items) {{ this.children = items; }},
}});
const nodes = new Map();
globalThis.document = {{
  querySelector: sel => {{ const id = sel.slice(1); if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }},
  createElement: tag => {{ const n = node(); n.tag = tag; made.push(n); return n; }},
  activeElement: null,
}};
const sent = [];
globalThis.chrome = {{runtime: {{sendMessage: async message => {{ sent.push(message); return {{}}; }}}}, tabs: {{}}}};
globalThis.fetch = async () => ({{ok: true, json: async () => []}});
globalThis.setInterval = () => 0;
await import({json.dumps((EXTENSION_DIR / "popup.js").as_uri())});
// Let the popup's own first refresh (status, release, agent list) settle.
await new Promise(resolve => setTimeout(resolve, 30));
window.__wsn.renderAgentTabs([
  {{tab_id: 5, title: "<img src=x onerror=alert(1)>", agent: "claude", phase: "active",
    status_text: "active now", claim: "main.py#1", url: "https://x"}},
]);
const list = nodes.get("agent-tab-list");
const button = list.children[0].children[0];
button.listeners.click();
await new Promise(resolve => setTimeout(resolve, 10));
const rendered = {{
  count: nodes.get("agent-tabs-count").textContent,
  emptyHidden: nodes.get("agent-tabs-empty").hidden,
  phase: button.dataset.phase,
  texts: button.children.map(child => child.textContent),
  label: button.attributes["aria-label"],
  focusMessage: sent.find(m => m.type === "companion.focusAgentTab"),
  usedInnerHtml: made.some(n => "innerHTML" in n),
}};
window.__wsn.renderAgentTabs([]);
rendered.emptyAfter = nodes.get("agent-tabs-empty").hidden;
rendered.countAfter = nodes.get("agent-tabs-count").textContent;
process.stdout.write(JSON.stringify(rendered));
process.exit(0);
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout)
    assert out["count"] == "1" and out["emptyHidden"] is True
    assert out["phase"] == "active"
    assert out["texts"] == ["", "<img src=x onerror=alert(1)>", "claude · active now · claimed by main.py#1"]
    assert out["label"].startswith("Focus <img")
    assert out["focusMessage"] == {"type": "companion.focusAgentTab", "tab_id": 5}
    assert out["usedInnerHtml"] is False
    assert out["emptyAfter"] is False and out["countAfter"] == "0"


def test_popup_preview_serves_sample_agent_rows() -> None:
    popup = (EXTENSION_DIR / "popup.js").read_text(encoding="utf-8")
    body = popup[popup.index("function previewSend"):popup.index("function previewReleases")]
    assert '"companion.agentTabs"' in body and '"companion.focusAgentTab"' in body
    assert popup.index("function previewAgentTabs") < popup.index("startPreview(String(PREVIEW))")
    assert "innerHTML" not in popup


def test_manifest_loads_the_new_modules_as_part_of_the_worker() -> None:
    worker = (EXTENSION_DIR / "service-worker.js").read_text(encoding="utf-8")
    assert 'from "./agent-badges.js"' in worker
    assert 'from "./bridge-auth.js"' in worker
    assert "token," not in worker.split("function connect")[1].split("socket.onmessage")[0], (
        "the hello must not carry the raw token"
    )
