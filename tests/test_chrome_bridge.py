from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

import bridge_auth
import browser_tools
from chrome_bridge import CHROME_EXTENSION_ID, ChromeBridge, ChromeBridgeDriver


TEST_TOKEN = "a1" * 32
OTHER_TOKEN = "b2" * 32
NODE = shutil.which("node")
EXTENSION_DIR = Path(__file__).resolve().parents[1] / "chrome-extension"
EVENTS_MODULE = (EXTENSION_DIR / "events.js").as_uri()
SERVICE_WORKER_MODULE = (EXTENSION_DIR / "service-worker.js").as_uri()
requires_node = pytest.mark.skipif(
    NODE is None, reason="node is required to exercise the extension helpers"
)

# Enough of Chrome for service-worker.js to load and connect outside a browser:
# a socket that records what it was told to send, a token file the test owns,
# and a session store, alarm shelf and timer log the reconnect schedule lands in.
_WORKER_STUBS = """
import * as fs from "node:fs";

const sockets = [];
let tokenSource = null;
let fetchCalls = 0;
const noop = () => {};
const listeners = {};
const listener = name => ({
  addListener: handler => { (listeners[name] = listeners[name] || []).push(handler); },
});
const sessionStore = {};
const alarms = [];
const timers = [];
let reloads = 0;
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;

globalThis.setTimeout = (handler, ms, ...rest) => {
  const id = realSetTimeout(handler, ms, ...rest);
  timers.push({ms, id});
  return id;
};

globalThis.__sockets = sockets;
globalThis.__setToken = source => { tokenSource = source; };
globalThis.__fetchCalls = () => fetchCalls;
globalThis.__fire = (name, detail) => (listeners[name] || []).forEach(handler => handler(detail));
globalThis.__seedSession = items => Object.assign(sessionStore, items);
globalThis.__session = () => sessionStore;
globalThis.__alarms = () => alarms.slice();
globalThis.__reloads = () => reloads;
// Forget (and cancel) whatever was already pending so the next assertion reads
// exactly one scheduling decision.
globalThis.__resetSchedule = () => {
  for (const timer of timers) realClearTimeout(timer.id);
  timers.length = 0;
  alarms.length = 0;
};
globalThis.__scheduledWait = () => {
  if (alarms.length) return Math.round(alarms[alarms.length - 1].delayInMinutes * 60000);
  return timers.length ? timers[timers.length - 1].ms : null;
};
globalThis.__sleep = ms => new Promise(resolve => realSetTimeout(resolve, ms));
globalThis.__waitFor = async (check, label) => {
  for (let attempt = 0; attempt < 300; attempt += 1) {
    if (check()) return;
    await globalThis.__sleep(10);
  }
  throw new Error("timed out waiting for " + label);
};
globalThis.__hmac = async (token, message) => {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", encoder.encode(token),
    {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const signed = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return [...new Uint8Array(signed)].map(byte => byte.toString(16).padStart(2, "0")).join("");
};

globalThis.fetch = async () => {
  fetchCalls += 1;
  if (tokenSource === null) throw new TypeError("bridge-token.js is missing");
  return {ok: true, status: 200, text: async () => tokenSource};
};

class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    sockets.push(this);
  }
  send(data) { this.sent.push(data); }
  close() { this.readyState = FakeSocket.CLOSED; }
}
globalThis.WebSocket = FakeSocket;

globalThis.chrome = {
  runtime: {
    id: "stub-extension-id",
    getURL: path => "chrome-extension://stub/" + path,
    getManifest: () => ({version: "0.0.0-test"}),
    reload: () => { reloads += 1; },
    onInstalled: listener("installed"),
    onStartup: listener("startup"),
  },
  action: {
    setBadgeBackgroundColor: noop,
    setBadgeText: noop,
    onClicked: listener("clicked"),
  },
  alarms: {
    create: (name, info) => { alarms.push({name, ...info}); },
    clear: async () => true,
    onAlarm: listener("alarm"),
  },
  storage: {
    session: {
      get: async key => (key in sessionStore ? {[key]: sessionStore[key]} : {}),
      set: async items => { Object.assign(sessionStore, items); },
    },
  },
  debugger: {
    onEvent: listener("debuggerEvent"),
    onDetach: listener("debuggerDetach"),
    attach: async () => {},
    detach: async () => {},
    sendCommand: async () => ({}),
  },
  tabs: {
    onRemoved: listener("tabRemoved"),
    query: async () => [],
    get: async () => ({}),
    update: async () => ({}),
  },
  tabGroups: {query: async () => []},
  windows: {getAll: async () => [], update: async () => ({})},
};
"""


def _node_eval(body: str):
    """Run one snippet against chrome-extension/events.js and decode its result."""
    module = (
        f"import * as events from {json.dumps(EVENTS_MODULE)};\n"
        f"const result = await (async () => {{\n{body}\n}})();\n"
        "process.stdout.write(JSON.stringify(result ?? null));\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", module],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _node_worker_eval(body: str, prelude: str = ""):
    """Run one snippet against service-worker.js with Chrome stubbed out.

    `prelude` runs before the import, which is the only moment a test can shape
    the world the worker wakes up in: a Chrome without the alarms permission, or
    a session store left behind by a worker Chrome has already evicted.
    """
    module = (
        _WORKER_STUBS
        + f"{prelude}\n"
        + f"const worker = await import({json.dumps(SERVICE_WORKER_MODULE)});\n"
        + f"const result = await (async () => {{\n{body}\n}})();\n"
        + "fs.writeSync(1, JSON.stringify(result ?? null));\n"
        # Keepalive and reconnect timers keep the loop alive; the answer is written.
        + "process.exit(0);\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", module],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _companion_socket(port: int):
    return connect(
        f"ws://127.0.0.1:{port}",
        origin=f"chrome-extension://{CHROME_EXTENSION_ID}",
    )


def _hello(token: str | None, nonce: str = "0f" * 16) -> str:
    message: dict = {"type": "hello", "protocol": 1, "browser": {"name": "Test Chrome"}}
    if token is not None:
        message["token"] = token
        message["nonce"] = nonce
    return json.dumps(message)


def test_bridge_round_trip_accepts_extension_protocol() -> None:
    bridge = ChromeBridge(port=_free_port(), token=TEST_TOKEN)
    bridge.start()
    nonce = "1234" * 8

    def extension_client() -> None:
        with _companion_socket(bridge.port) as websocket:
            websocket.send(_hello(TEST_TOKEN, nonce))
            acknowledgement = json.loads(websocket.recv())
            assert acknowledgement["type"] == "hello_ack"
            # The ack proves the server knows the same secret, not just the port.
            assert bridge_auth.verify(TEST_TOKEN, nonce, acknowledgement["proof"])
            command = json.loads(websocket.recv())
            websocket.send(
                json.dumps(
                    {
                        "type": "result",
                        "id": command["id"],
                        "result": {"method": command["method"]},
                    }
                )
            )

    thread = threading.Thread(target=extension_client)
    thread.start()
    assert bridge.wait_connected(2.0)
    assert bridge.request("tabs.list") == {"method": "tabs.list"}
    thread.join(timeout=2.0)
    bridge.shutdown()


@pytest.mark.parametrize("token", [None, OTHER_TOKEN, "", "not-a-token"])
def test_bridge_rejects_a_client_without_the_shared_token(token) -> None:
    bridge = ChromeBridge(port=_free_port(), token=TEST_TOKEN)
    bridge.start()
    try:
        with _companion_socket(bridge.port) as websocket:
            websocket.send(_hello(token))
            with pytest.raises(ConnectionClosed) as rejection:
                websocket.recv(timeout=5.0)
        closed = rejection.value.rcvd
        assert closed.code == 1008
        assert "token mismatch" in closed.reason
        assert "chrome://extensions" in closed.reason
        assert bridge.wait_connected(0.2) is False
    finally:
        bridge.shutdown()


def test_bridge_rejects_a_hello_without_a_nonce() -> None:
    bridge = ChromeBridge(port=_free_port(), token=TEST_TOKEN)
    bridge.start()
    try:
        with _companion_socket(bridge.port) as websocket:
            websocket.send(json.dumps({"type": "hello", "protocol": 1, "token": TEST_TOKEN}))
            with pytest.raises(ConnectionClosed) as rejection:
                websocket.recv(timeout=5.0)
        assert rejection.value.rcvd.code == 1008
        assert "nonce" in rejection.value.rcvd.reason
        assert bridge.wait_connected(0.2) is False
    finally:
        bridge.shutdown()


def test_a_newer_authenticated_companion_replaces_the_previous_one() -> None:
    bridge = ChromeBridge(port=_free_port(), token=TEST_TOKEN)
    bridge.start()
    answers: list = []
    try:
        first = _companion_socket(bridge.port)
        first.send(_hello(TEST_TOKEN))
        assert json.loads(first.recv(timeout=5.0))["type"] == "hello_ack"
        assert bridge.wait_connected(2.0)

        second_nonce = "abcd" * 8
        second = _companion_socket(bridge.port)
        second.send(_hello(TEST_TOKEN, second_nonce))
        acknowledgement = json.loads(second.recv(timeout=5.0))
        assert bridge_auth.verify(TEST_TOKEN, second_nonce, acknowledgement["proof"])

        with pytest.raises(ConnectionClosed):
            first.recv(timeout=5.0)

        caller = threading.Thread(
            target=lambda: answers.append(bridge.request("tabs.list", timeout=10.0))
        )
        caller.start()
        command = json.loads(second.recv(timeout=5.0))
        second.send(json.dumps({"type": "result", "id": command["id"], "result": ["second"]}))
        caller.join(timeout=5.0)
        assert answers == [["second"]]
        second.close()
        first.close()
    finally:
        bridge.shutdown()


def test_sign_and_verify_reject_tampering() -> None:
    proof = bridge_auth.sign(TEST_TOKEN, "nonce-1")
    assert bridge_auth.verify(TEST_TOKEN, "nonce-1", proof)
    assert not bridge_auth.verify(TEST_TOKEN, "nonce-2", proof)
    assert not bridge_auth.verify(OTHER_TOKEN, "nonce-1", proof)
    assert not bridge_auth.verify(TEST_TOKEN, "nonce-1", proof[:-1] + ("0" if proof[-1] != "0" else "1"))
    assert not bridge_auth.verify(TEST_TOKEN, "nonce-1", proof.upper())
    assert not bridge_auth.verify(TEST_TOKEN, "nonce-1", None)
    assert not bridge_auth.verify(TEST_TOKEN, "nonce-1", "proof-é")
    assert bridge_auth.token_matches(TEST_TOKEN, TEST_TOKEN)
    assert not bridge_auth.token_matches(TEST_TOKEN, OTHER_TOKEN)
    assert not bridge_auth.token_matches(TEST_TOKEN, None)


def test_load_or_create_token_is_stable_across_calls(tmp_path, monkeypatch) -> None:
    target = tmp_path / "bridge-token"
    monkeypatch.setattr(bridge_auth, "token_path", lambda: target)

    minted = bridge_auth.load_or_create_token()
    assert bridge_auth.is_token(minted)
    assert bridge_auth.load_or_create_token() == minted
    assert target.read_text(encoding="utf-8").strip() == minted

    target.write_text("truncated-secret", encoding="utf-8")
    replaced = bridge_auth.load_or_create_token()
    assert bridge_auth.is_token(replaced) and replaced != minted


def test_write_extension_token_emits_a_module_and_refuses_junk(tmp_path, monkeypatch) -> None:
    target = tmp_path / "extension" / "bridge-token.js"
    monkeypatch.setattr(bridge_auth, "EXTENSION_TOKEN_FILE", target)

    assert bridge_auth.write_extension_token(TEST_TOKEN) == target
    assert target.read_text(encoding="utf-8") == f'export const BRIDGE_TOKEN = "{TEST_TOKEN}";\n'
    with pytest.raises(ValueError):
        bridge_auth.write_extension_token('"; fetch("http://evil.test"); //')


@requires_node
def test_web_crypto_hmac_matches_the_python_signature() -> None:
    token, nonce = TEST_TOKEN, "0123456789abcdef" * 2
    digest = _node_eval(
        f"""
        const encoder = new TextEncoder();
        const key = await crypto.subtle.importKey("raw", encoder.encode({json.dumps(token)}),
          {{name: "HMAC", hash: "SHA-256"}}, false, ["sign"]);
        const signed = await crypto.subtle.sign("HMAC", key, encoder.encode({json.dumps(nonce)}));
        return [...new Uint8Array(signed)].map(byte => byte.toString(16).padStart(2, "0")).join("");
        """
    )
    assert digest == bridge_auth.sign(token, nonce)


@requires_node
def test_a_command_answers_only_the_socket_it_arrived_on() -> None:
    """A slow command must not spill its result into a replacement socket."""
    outcome = _node_worker_eval(
        f"""
        const token = {json.dumps(TEST_TOKEN)};
        await globalThis.__waitFor(() => globalThis.__fetchCalls() >= 1, "the first token read");
        await globalThis.__sleep(20);
        globalThis.__setToken('export const BRIDGE_TOKEN = "' + token + '";');

        await worker.connect();
        const first = globalThis.__sockets[0];
        first.onopen();
        const hello = JSON.parse(first.sent[0]);
        first.onmessage({{data: JSON.stringify({{
          type: "hello_ack",
          protocol: hello.protocol,
          proof: await globalThis.__hmac(token, hello.nonce),
        }})}});

        first.onmessage({{data: JSON.stringify({{type: "command", id: "fast", method: "tabs.list"}})}});
        await globalThis.__waitFor(() => first.sent.length > 1, "the answer to the fast command");

        let release = null;
        chrome.tabs.query = () => new Promise(resolve => {{ release = resolve; }});
        first.onmessage({{data: JSON.stringify({{type: "command", id: "slow", method: "tabs.list"}})}});
        await globalThis.__waitFor(() => release !== null, "the slow command to start");

        first.onclose({{code: 1006, reason: "dropped"}});
        await worker.connect();
        const second = globalThis.__sockets[1];
        second.onopen();
        release([]);
        await globalThis.__sleep(60);

        return {{
          fast: JSON.parse(first.sent[1]),
          first_after: first.sent.slice(2).map(item => JSON.parse(item)),
          second_frames: second.sent.map(item => JSON.parse(item).type),
        }};
        """
    )
    assert outcome["fast"] == {"type": "result", "id": "fast", "result": []}
    assert outcome["first_after"] == [], "the answer went to a socket that is already gone"
    # The replacement has only said hello; it has not been verified, so a result
    # landing there would be page data handed to an unproven peer.
    assert outcome["second_frames"] == ["hello"]


@requires_node
def test_the_token_is_re_read_after_it_was_missing() -> None:
    """A token written after the extension loaded must be picked up without a reload."""
    outcome = _node_worker_eval(
        f"""
        const token = {json.dumps(TEST_TOKEN)};
        let failure = null;
        try {{
          await worker.loadBridgeToken();
        }} catch (error) {{
          failure = String((error && error.message) || error);
        }}
        globalThis.__setToken('export const BRIDGE_TOKEN = "' + token + '";\\n');
        const recovered = await worker.loadBridgeToken();
        let junk = null;
        try {{
          worker.parseBridgeToken('export const BRIDGE_TOKEN = "nope";');
        }} catch (error) {{
          junk = String(error.message);
        }}
        return {{failure, recovered, junk}};
        """
    )
    assert outcome["failure"], "the first read should fail while the token file is absent"
    assert outcome["recovered"] == TEST_TOKEN
    assert "no usable token" in outcome["junk"]


# The bridge port is closed for most of the day and Chrome logs every refused
# attempt as a runtime error of the extension, so the schedule below is what
# keeps chrome://extensions from filling with identical lines. The tests that
# follow all start the same way: let the worker spend its own first attempt on
# the token that is not there yet, then hand it one, so what they measure is the
# schedule and not the load order.
_WORKER_READY = (
    'await globalThis.__waitFor(() => globalThis.__fetchCalls() >= 1, "the first token read");\n'
    "await globalThis.__sleep(20);\n"
    f"globalThis.__setToken('export const BRIDGE_TOKEN = \"{TEST_TOKEN}\";');\n"
)

# One attempt that finds nobody listening: the socket is built, never opens, and
# closes the way Chrome closes a refused connection.
_FAIL_ONCE = """
const failOnce = async () => {
  await worker.connect();
  const socket = globalThis.__sockets[globalThis.__sockets.length - 1];
  globalThis.__resetSchedule();
  socket.onclose({code: 1006, reason: ""});
  return {wait: globalThis.__scheduledWait(), via_alarm: globalThis.__alarms().length > 0};
};
"""


@requires_node
def test_each_refused_connection_is_retried_later_than_the_last_up_to_a_minute() -> None:
    schedule = _node_worker_eval(
        """
        const rows = {};
        for (const kind of ["transport", "auth"]) {
          for (const streak of [0, 1, 2, 5, 40]) {
            const samples = [];
            for (let sample = 0; sample < 64; sample += 1) {
              samples.push(worker.backoffDelay(kind, streak));
            }
            rows[kind + ":" + streak] = {min: Math.min(...samples), max: Math.max(...samples)};
          }
        }
        return rows;
        """
    )
    floor = schedule["transport:0"]
    assert 1000 <= floor["min"] and floor["max"] <= 1500
    # A flat retry would repeat one number; jitter and growth both rule that out.
    assert floor["min"] < floor["max"], "the delay carries no jitter"
    for earlier, later in (
        ("transport:0", "transport:1"),
        ("transport:1", "transport:2"),
        ("transport:2", "transport:5"),
    ):
        assert schedule[later]["min"] > schedule[earlier]["max"], (earlier, later)
    capped = schedule["transport:40"]
    assert capped["max"] <= 60000 and capped["min"] >= 45000
    # A peer that answered and refused us is a slower problem than a closed port.
    assert schedule["auth:0"]["min"] >= floor["max"]
    assert schedule["auth:40"]["max"] <= 120000


@requires_node
def test_a_verified_handshake_puts_the_retry_back_on_its_floor() -> None:
    outcome = _node_worker_eval(
        _WORKER_READY
        + _FAIL_ONCE
        + """
        const waits = [];
        for (let attempt = 0; attempt < 5; attempt += 1) waits.push((await failOnce()).wait);

        await worker.connect();
        const accepted = globalThis.__sockets[globalThis.__sockets.length - 1];
        accepted.onopen();
        const hello = JSON.parse(accepted.sent[0]);
        accepted.onmessage({data: JSON.stringify({
          type: "hello_ack",
          protocol: hello.protocol,
          proof: await globalThis.__hmac(hello.token, hello.nonce),
        })});
        globalThis.__resetSchedule();
        accepted.onclose({code: 1006, reason: "server stopped"});

        return {waits, after_a_good_session: globalThis.__scheduledWait()};
        """
    )
    waits = outcome["waits"]
    assert waits[0] < 2000, "the first retry should still be prompt"
    assert waits == sorted(waits) and len(set(waits)) == len(waits), waits
    assert waits[-1] > 15000, "five refusals in a row should have slowed right down"
    assert outcome["after_a_good_session"] <= 1500, "a working session must clear the streak"


@requires_node
def test_a_refused_handshake_waits_on_its_own_slower_schedule() -> None:
    """A peer that answered and turned us away is not retried like a closed port."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + """
        await worker.connect();
        const answered = globalThis.__sockets[globalThis.__sockets.length - 1];
        answered.onopen();
        globalThis.__resetSchedule();
        answered.onclose({code: 1008, reason: "token mismatch"});
        const refused = globalThis.__scheduledWait();

        await worker.connect();
        const unanswered = globalThis.__sockets[globalThis.__sockets.length - 1];
        globalThis.__resetSchedule();
        unanswered.onclose({code: 1006, reason: ""});
        return {refused, closed_port: globalThis.__scheduledWait()};
        """
    )
    assert outcome["closed_port"] < 2000, "a closed port should be retried promptly"
    assert outcome["refused"] >= 7500 <= 120000
    assert outcome["refused"] > outcome["closed_port"] * 4
    # The two schedules are counted apart: a refusal must not slow down the
    # ordinary retry that follows it.
    assert outcome["closed_port"] <= 1500


@requires_node
def test_a_wait_the_worker_would_not_survive_is_handed_to_an_alarm() -> None:
    """MV3 suspends an idle worker, so only an alarm can bring it back."""
    steps = _node_worker_eval(
        _WORKER_READY
        + _FAIL_ONCE
        + """
        const steps = [];
        for (let attempt = 0; attempt < 8; attempt += 1) steps.push(await failOnce());
        return steps;
        """
    )
    assert [step["via_alarm"] for step in steps] == [step["wait"] >= 30000 for step in steps]
    assert any(step["via_alarm"] for step in steps), "no wait ever grew past the idle window"
    assert max(step["wait"] for step in steps) <= 60000


@requires_node
def test_without_the_alarms_permission_no_wait_outlives_the_idle_worker() -> None:
    """A timer Chrome would kill mid-wait leaves the bridge offline for good."""
    steps = _node_worker_eval(
        _WORKER_READY
        + _FAIL_ONCE
        + """
        const steps = [];
        for (let attempt = 0; attempt < 8; attempt += 1) steps.push(await failOnce());
        return {steps, alarms: globalThis.__alarms()};
        """,
        prelude="delete globalThis.chrome.alarms;",
    )
    waits = [step["wait"] for step in steps["steps"]]
    assert steps["alarms"] == []
    assert max(waits) <= 25000
    assert waits[1] > waits[0], "the backoff should still grow up to the ceiling"


@requires_node
def test_a_restarted_worker_resumes_the_stored_wait_instead_of_attempting_at_once() -> None:
    """Any event that wakes an evicted worker would otherwise spend an attempt."""
    outcome = _node_worker_eval(
        """
        await globalThis.__sleep(60);
        return {
          attempts: globalThis.__fetchCalls(),
          sockets: globalThis.__sockets.length,
          wait: globalThis.__scheduledWait(),
        };
        """,
        prelude=(
            "globalThis.__seedSession({bridge_backoff: "
            "{transport: 6, auth: 0, nextAttemptAt: Date.now() + 45000}});"
        ),
    )
    assert outcome["attempts"] == 0 and outcome["sockets"] == 0
    assert 30000 <= outcome["wait"] <= 46000


@requires_node
def test_the_toolbar_click_retries_at_once_but_two_clicks_are_not_two_attempts() -> None:
    """The click is how a user says the server is up; it must not become a loop."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + """
        await globalThis.__sleep(1600);
        globalThis.__resetSchedule();
        globalThis.__fire("clicked");
        await globalThis.__waitFor(() => globalThis.__sockets.length >= 1, "the socket the click asked for");
        const after_one = globalThis.__sockets.length;
        globalThis.__fire("clicked");
        const wait = globalThis.__scheduledWait();
        await globalThis.__sleep(50);
        return {after_one, after_two: globalThis.__sockets.length, wait};
        """
    )
    assert outcome["after_one"] == 1, "the click should connect straight away"
    assert outcome["after_two"] == 1, "a second click inside the floor must not attempt again"
    assert 0 < outcome["wait"] <= 1500


@requires_node
def test_the_reload_command_answers_before_it_takes_the_worker_down() -> None:
    """Reloading is the only way to update the companion without a manual click."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + """
        await worker.connect();
        const socket = globalThis.__sockets[globalThis.__sockets.length - 1];
        socket.onopen();
        const hello = JSON.parse(socket.sent[0]);
        socket.onmessage({data: JSON.stringify({
          type: "hello_ack",
          protocol: hello.protocol,
          proof: await globalThis.__hmac(hello.token, hello.nonce),
        })});
        // A wait left over from an earlier outage; the worker that comes back
        // would otherwise honour it instead of connecting.
        globalThis.__seedSession({bridge_backoff: {transport: 9, auth: 9, nextAttemptAt: Date.now() + 60000}});

        socket.onmessage({data: JSON.stringify({
          type: "command", id: "reload-1", method: "runtime.reload",
        })});
        await globalThis.__waitFor(() => socket.sent.length > 1, "the answer to the reload request");
        const answer = JSON.parse(socket.sent[1]);
        const before = globalThis.__reloads();
        const stored = globalThis.__session().bridge_backoff;
        await globalThis.__sleep(500);
        return {answer, before, after: globalThis.__reloads(), stored};
        """
    )
    assert outcome["answer"]["id"] == "reload-1"
    assert outcome["answer"]["result"]["reloading"] is True
    assert outcome["answer"]["result"]["version"] == "0.0.0-test"
    assert outcome["answer"]["result"]["extension_id"] == "stub-extension-id"
    assert outcome["before"] == 0, "the worker went down before the caller was answered"
    assert outcome["after"] == 1, "the reload never happened"
    assert outcome["stored"] == {"transport": 0, "auth": 0, "nextAttemptAt": 0}


def test_a_first_frame_that_is_not_an_object_is_refused_with_a_reason() -> None:
    bridge = ChromeBridge(port=_free_port(), token=TEST_TOKEN)
    bridge.start()
    try:
        for frame in ("not json at all", "123", "[1, 2]", "null"):
            with _companion_socket(bridge.port) as websocket:
                websocket.send(frame)
                with pytest.raises(ConnectionClosed) as rejection:
                    websocket.recv(timeout=5.0)
            closed = rejection.value.rcvd
            assert closed.code == 1008, frame
            assert "JSON object" in closed.reason, frame
        assert bridge.wait_connected(0.2) is False
    finally:
        bridge.shutdown()


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
        params = params or {}
        self.calls.append((method, params))
        if method == "tabs.create":
            return {"id": 41, "url": params["url"], "title": "", "group": params["group"]}
        if method == "tabs.get":
            return {"id": 41, "url": "https://example.test/", "title": "Example", "group": "Existing"}
        if method == "tabs.activate":
            return {"id": 41, "url": "https://example.test/", "title": "Example", "group": "Existing"}
        if method == "tabs.navigate":
            return {"id": 41, "url": params["url"], "title": "Example", "group": "AI"}
        if method == "cdp.send":
            expression = params["params"].get("expression", "")
            if "document.querySelector" in expression:
                return {"result": {"type": "boolean", "value": True}}
            return {"result": {"type": "undefined"}}
        if method == "debugger.detach":
            return {"detached": True}
        if method == "tabs.remove":
            return {"removed": True, "id": params["tabId"]}
        if method == "events.subscribe":
            return {"started_at": 1700000000000, "seq": 0, "domains": params["domains"]}
        if method == "events.get":
            return {
                "entries": [],
                "next_seq": params["since_seq"],
                "dropped": {"console": 0, "network": 0},
                "started_at": 1700000000000,
                "truncated": False,
            }
        if method == "events.clear":
            return {"cleared": params.get("kinds", ["console", "network"]), "seq": 12}
        if method == "events.unsubscribe":
            return {"domains": []}
        if method == "network.body":
            return {"request_id": params["requestId"], "mime": "application/json", "body": "{}"}
        raise AssertionError(method)


def test_driver_opens_new_tabs_in_ai_group() -> None:
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI")
    driver.get("https://example.test/")
    assert driver.tab_id == 41
    assert driver.actual_tab_group == "AI"
    assert bridge.calls[0] == (
        "tabs.create",
        {"url": "about:blank", "group": "AI"},
    )
    assert any(
        method == "tabs.navigate" and params["url"] == "https://example.test/"
        for method, params in bridge.calls
    )


def test_driver_claims_and_activates_existing_tab_without_regrouping() -> None:
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_id=41, tab_group="AI")
    assert driver.actual_tab_group == "Existing"
    assert [method for method, _ in bridge.calls[:2]] == ["tabs.get", "tabs.activate"]
    assert all(method != "tabs.create" for method, _ in bridge.calls)


def test_driver_keeps_held_modifier_across_atomic_key_actions() -> None:
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI")
    driver.perform_key_events([{"type": "down", "key": "\ue008"}])
    driver.perform_key_events(
        [{"type": "down", "key": "W"}, {"type": "up", "key": "W"}]
    )
    driver.perform_key_events([{"type": "up", "key": "\ue008"}])

    key_commands = [
        params["params"]
        for method, params in bridge.calls
        if method == "cdp.send" and params["method"] == "Input.dispatchKeyEvent"
    ]
    w_down = next(item for item in key_commands if item["key"] == "W" and item["type"] == "keyDown")
    assert w_down["modifiers"] & 8


def test_driver_wraps_the_new_event_commands() -> None:
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI")

    subscribed = driver.subscribe_events(["console", "Network"], include_headers=True)
    events = driver.get_events(
        kinds=["network"],
        since_seq=12,
        limit=50,
        level="warn",
        contains="timeout",
        url_pattern=r"api\.test",
        types=["XHR"],
        only_errors=True,
    )
    driver.clear_events(["console"])
    driver.unsubscribe_events(["network"])
    body = driver.get_network_body("42")
    closed = driver.close_tab()

    sent = dict(bridge.calls)
    assert subscribed["domains"] == ["console", "network"]
    assert sent["events.subscribe"] == {
        "tabId": 41,
        "domains": ["console", "network"],
        "include_headers": True,
    }
    assert sent["events.get"] == {
        "tabId": 41,
        "since_seq": 12,
        "limit": 50,
        "only_errors": True,
        "kinds": ["network"],
        "level": "warn",
        "contains": "timeout",
        "url_pattern": r"api\.test",
        "types": ["XHR"],
    }
    assert events["next_seq"] == 12
    assert sent["events.clear"] == {"tabId": 41, "kinds": ["console"]}
    assert sent["events.unsubscribe"] == {"tabId": 41, "domains": ["network"]}
    assert sent["network.body"] == {"tabId": 41, "requestId": "42"}
    assert body["mime"] == "application/json"
    assert closed == {"removed": True, "id": 41}


def test_driver_close_tab_survives_a_bridge_error() -> None:
    class _BrokenBridge(_FakeBridge):
        def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
            if method == "tabs.remove":
                raise RuntimeError("tab is already gone")
            return super().request(method, params, timeout)

    driver = ChromeBridgeDriver(bridge=_BrokenBridge(), tab_group="AI")
    assert driver.close_tab() == {"removed": False, "id": 41}


@requires_node
def test_console_types_map_onto_agent_levels() -> None:
    levels = _node_eval(
        """
        const types = ["debug", "log", "info", "dir", "dirxml", "table", "trace", "count",
          "timeEnd", "startGroup", "groupEnd", "warning", "error", "assert", "unheard-of"];
        return Object.fromEntries(types.map(type => [type, events.consoleLevel(type)]));
        """
    )
    assert levels == {
        "debug": "debug",
        "log": "info",
        "info": "info",
        "dir": "info",
        "dirxml": "info",
        "table": "info",
        "trace": "info",
        "count": "info",
        "timeEnd": "info",
        "startGroup": "info",
        "groupEnd": "info",
        "warning": "warn",
        "error": "error",
        "assert": "error",
        "unheard-of": "info",
    }


@requires_node
def test_console_entry_formats_arguments_and_keeps_error_stacks() -> None:
    result = _node_eval(
        """
        const stackTrace = {callFrames: [
          {functionName: "run", url: "https://site.test/app.js", lineNumber: 41, columnNumber: 7},
          {functionName: "boot", url: "https://site.test/app.js", lineNumber: 3, columnNumber: 1},
        ]};
        const args = [
          {type: "string", value: "hello"},
          {type: "number", value: 42},
          {type: "object", preview: {subtype: "array", properties: [{value: "1"}, {value: "2"}]}},
          {type: "object", preview: {description: "Object", properties: [{name: "hp", value: "3"}]}},
          {type: "object", description: "Error: boom"},
          {type: "string", value: "x".repeat(500)},
        ];
        const info = events.consoleEntry({type: "log", args, stackTrace},
          {seq: 7, ts: 1700000000000, frame: null});
        const failure = events.consoleEntry({type: "error", args: [{value: "nope"}], stackTrace},
          {seq: 8, ts: 1700000000001, frame: "S1"});
        const browser = events.browserEntry(
          {entry: {level: "warning", source: "network", text: "404 not found",
                   url: "https://site.test/x", lineNumber: 0, networkRequestId: "77"}},
          {seq: 9, ts: 1700000000002});
        return {info, failure, browser};
        """
    )
    info = result["info"]
    assert info["kind"] == "console" and info["level"] == "info"
    assert info["source"] == "console-api" and info["seq"] == 7
    assert info["text"].startswith("hello 42 [1, 2] {hp: 3} Error: boom ")
    assert len(info["args"][5]) == 200
    assert (info["url"], info["line"], info["col"]) == ("https://site.test/app.js", 42, 8)
    assert info["stack"] == []

    assert result["failure"]["level"] == "error"
    assert result["failure"]["stack"][0] == {
        "fn": "run",
        "url": "https://site.test/app.js",
        "line": 42,
        "col": 8,
    }
    assert result["failure"]["frame"] == "S1"

    browser = result["browser"]
    assert (browser["kind"], browser["level"], browser["source"]) == ("browser", "warn", "network")
    assert browser["network_request_id"] == "77"


@requires_node
def test_exception_and_navigation_entries() -> None:
    result = _node_eval(
        """
        const thrown = events.exceptionEntry({exceptionDetails: {
          text: "Uncaught",
          exception: {description: "TypeError: hero is undefined\\n    at run (app.js:2:3)"},
          url: "https://site.test/app.js", lineNumber: 1, columnNumber: 2,
          stackTrace: {callFrames: [{functionName: "run", url: "https://site.test/app.js",
            lineNumber: 1, columnNumber: 2}]},
        }}, {seq: 3, ts: 5});
        const moved = events.navigationEntry("https://site.test/level2", {seq: 4, ts: 6});
        return {thrown, moved};
        """
    )
    thrown = result["thrown"]
    assert (thrown["kind"], thrown["level"], thrown["source"]) == ("exception", "error", "javascript")
    assert thrown["text"].startswith("TypeError: hero is undefined")
    assert (thrown["line"], thrown["col"]) == (2, 3)
    assert len(thrown["stack"]) == 1
    assert result["moved"]["kind"] == "navigation"
    assert result["moved"]["text"] == "→ https://site.test/level2"


@requires_node
def test_network_row_is_stitched_from_four_cdp_events() -> None:
    result = _node_eval(
        """
        const buffer = events.createBuffer(1000);
        const start = {requestId: "7", request: {method: "POST", url: "https://api.test/v1"},
          type: "XHR", initiator: {type: "script"}, documentURL: "https://api.test/",
          timestamp: 100, wallTime: 1700000000, frameId: "F1"};
        events.trackPending(buffer, events.networkRow(start, {ts: 1700000000000}));
        events.applyResponse(buffer.pending.get("7"), {response: {status: 201,
          mimeType: "application/json", remoteIPAddress: "1.2.3.4", remotePort: 443,
          fromDiskCache: false, headers: {"x-trace": "abc"}}}, false);
        const done = events.applyFinished(buffer.pending.get("7"),
          {encodedDataLength: 2048, timestamp: 100.25});
        buffer.pending.delete("7");
        events.pushEntry(buffer, "network", done);

        const second = {requestId: "8", request: {method: "GET", url: "https://api.test/img.png"},
          type: "Image", timestamp: 200, wallTime: 1700000001};
        events.trackPending(buffer, events.networkRow(second, {ts: 1700000001000}));
        const failed = events.applyFailed(buffer.pending.get("8"),
          {errorText: "net::ERR_BLOCKED_BY_CLIENT", canceled: false,
           blockedReason: "inspector", timestamp: 200.1});
        buffer.pending.delete("8");
        events.pushEntry(buffer, "network", failed);

        const withHeaders = events.networkRow(start, {ts: 1});
        events.applyResponse(withHeaders, {response: {status: 200, headers: {"x-trace": "abc"}}}, true);
        return {rows: buffer.network, headers: withHeaders.headers, pending: buffer.pending.size};
        """
    )
    ok, failed = result["rows"]
    assert ok == {
        "seq": 1,
        "kind": "network",
        "level": "info",
        "id": "7",
        "ts": 1700000000000,
        "method": "POST",
        "url": "https://api.test/v1",
        "type": "XHR",
        "initiator": "script",
        "doc": "https://api.test/",
        "frame": "F1",
        "status": 201,
        "mime": "application/json",
        "from_cache": False,
        "remote": "1.2.3.4:443",
        "size": 2048,
        "ms": 250,
        "done": True,
    }
    assert "headers" not in ok
    assert failed["failed"] is True and failed["level"] == "error"
    assert failed["error"] == "net::ERR_BLOCKED_BY_CLIENT"
    assert failed["blocked_reason"] == "inspector"
    assert result["headers"] == {"x-trace": "abc"}
    assert result["pending"] == 0


@requires_node
def test_events_get_filters_inside_the_extension() -> None:
    result = _node_eval(
        """
        const buffer = events.createBuffer(1000);
        const push = (type, text) => events.pushEntry(buffer, "console",
          events.consoleEntry({type, args: [{value: text}]},
            {seq: events.nextSeq(buffer), ts: 1}));
        push("log", "boot ok");
        push("warning", "slow frame");
        push("error", "TypeError: boom");
        const row = events.networkRow({requestId: "9", type: "XHR",
          request: {method: "GET", url: "https://api.test/items"}, timestamp: 1}, {ts: 2});
        events.applyResponse(row, {response: {status: 500, mimeType: "application/json"}});
        events.pushEntry(buffer, "network",
          events.applyFinished(row, {encodedDataLength: 10, timestamp: 1.5}));
        const seqs = query => events.collectEvents(buffer, query).entries.map(entry => entry.seq);
        return {
          all: seqs({}),
          warn_plus: seqs({level: "warn"}),
          exact_level: seqs({level: ["error"]}),
          contains: seqs({contains: "BOOM"}),
          only_errors: seqs({only_errors: true}),
          since: seqs({since_seq: 2}),
          network_only: seqs({kinds: ["network"]}),
          typed: seqs({types: ["xhr"]}),
          url_pattern: seqs({url_pattern: "api\\\\.test"}),
          limited: events.collectEvents(buffer, {limit: 2}),
          drained: events.collectEvents(buffer, {}),
        };
        """
    )
    assert result["all"] == [1, 2, 3, 4]
    assert result["warn_plus"] == [2, 3, 4]
    assert result["exact_level"] == [3, 4]
    assert result["contains"] == [3]
    assert result["only_errors"] == [3, 4]
    assert result["since"] == [3, 4]
    assert result["network_only"] == [4]
    assert result["typed"] == [4]
    assert result["url_pattern"] == [4]
    assert [entry["seq"] for entry in result["limited"]["entries"]] == [1, 2]
    assert result["limited"]["truncated"] is True
    assert result["limited"]["next_seq"] == 2
    assert result["drained"]["truncated"] is False
    assert result["drained"]["next_seq"] == 4
    assert result["drained"]["dropped"] == {"console": 0, "network": 0}
    assert result["drained"]["started_at"] == 1000


@requires_node
def test_ring_buffer_drops_the_oldest_entries_and_counts_them() -> None:
    result = _node_eval(
        """
        const buffer = events.createBuffer(1);
        for (let index = 0; index < 620; index += 1) {
          events.pushEntry(buffer, "console", events.consoleEntry(
            {type: "log", args: [{value: `message ${index}`}]},
            {seq: events.nextSeq(buffer), ts: index}));
        }
        const bulky = events.createBuffer(1);
        const wide = Array.from({length: 12}, () => ({value: "y".repeat(300)}));
        for (let index = 0; index < 300; index += 1) {
          events.pushEntry(bulky, "console",
            events.consoleEntry({type: "log", args: wide},
              {seq: events.nextSeq(bulky), ts: index}));
        }
        const cleared = events.clearBuffer(buffer, ["console"]);
        return {
          bulky_kept: bulky.console.length,
          bulky_dropped: bulky.dropped.console,
          bulky_bytes: bulky.bytes.console,
          cleared,
          after_clear: {entries: buffer.console.length, dropped: buffer.dropped.console,
                        seq: buffer.seq},
        };
        """
    )
    assert result["bulky_kept"] < 400 and result["bulky_dropped"] > 0
    assert result["bulky_bytes"] <= 512 * 1024
    assert result["cleared"] == ["console"]
    # events.clear keeps the sequence counter so since_seq stays monotonic.
    assert result["after_clear"] == {"entries": 0, "dropped": 0, "seq": 620}


@requires_node
def test_ring_buffer_keeps_the_newest_five_hundred_records() -> None:
    result = _node_eval(
        """
        const buffer = events.createBuffer(1);
        for (let index = 0; index < 620; index += 1) {
          events.pushEntry(buffer, "console", events.consoleEntry(
            {type: "log", args: [{value: `message ${index}`}]},
            {seq: events.nextSeq(buffer), ts: index}));
        }
        return {
          kept: buffer.console.length,
          dropped: buffer.dropped.console,
          first_seq: buffer.console[0].seq,
          last_seq: buffer.console[buffer.console.length - 1].seq,
          visible: events.collectEvents(buffer, {since_seq: 0, limit: 2000}).entries.length,
        };
        """
    )
    assert result == {
        "kept": 500,
        "dropped": 120,
        "first_seq": 121,
        "last_seq": 620,
        "visible": 500,
    }


@requires_node
def test_events_get_replays_when_the_worker_restarted_the_counter() -> None:
    result = _node_eval(
        """
        const buffer = events.createBuffer(1);
        events.pushEntry(buffer, "console", events.consoleEntry(
          {type: "log", args: [{value: "after restart"}]},
          {seq: events.nextSeq(buffer), ts: 1}));
        return events.collectEvents(buffer, {since_seq: 900});
        """
    )
    assert result["reset"] is True
    assert [entry["text"] for entry in result["entries"]] == ["after restart"]
    assert result["next_seq"] == 1


@requires_node
def test_legacy_console_shape_and_frame_url_matching() -> None:
    result = _node_eval(
        """
        const buffer = events.createBuffer(1);
        events.pushEntry(buffer, "console", events.consoleEntry(
          {type: "error", args: [{value: "boom"}]}, {seq: events.nextSeq(buffer), ts: 99}));
        events.pushEntry(buffer, "console", events.consoleEntry(
          {type: "warning", args: [{value: "slow"}]}, {seq: events.nextSeq(buffer), ts: 100}));
        return {
          legacy: events.legacyConsole(buffer),
          same: events.sameFrameUrl("https://a.test/game?x=1#top", "https://a.test/game?x=2"),
          other: events.sameFrameUrl("https://a.test/game", "https://a.test/other"),
          textual: [events.isTextualMime("application/json"), events.isTextualMime("image/png")],
        };
        """
    )
    assert result["legacy"] == [
        {"level": "SEVERE", "message": "boom", "timestamp": 99},
        {"level": "WARNING", "message": "slow", "timestamp": 100},
    ]
    assert result["same"] is True and result["other"] is False
    assert result["textual"] == [True, False]


def test_auto_prefers_current_chrome_and_headless_forces_temporary(monkeypatch) -> None:
    class ConnectedBridge:
        @staticmethod
        def wait_connected(timeout: float) -> bool:
            return True

    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: ConnectedBridge())
    assert browser_tools._resolve_profile_mode("auto", None) == "current"
    assert browser_tools._resolve_profile_mode("auto", True) == "temporary"
