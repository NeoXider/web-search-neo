from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

import bridge_auth
import bridge_daemon
import browser_tools
import chrome_bridge
import main
from bridge_daemon import BridgeDaemon
from chrome_bridge import CHROME_EXTENSION_ID, ChromeBridge, ChromeBridgeDriver, ChromeBridgeError


TEST_TOKEN = "a1" * 32
OTHER_TOKEN = "b2" * 32
NODE = shutil.which("node")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
const localStore = {};
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
globalThis.__seedLocal = items => Object.assign(localStore, items);
globalThis.__session = () => sessionStore;
globalThis.__local = () => localStore;
globalThis.__message = request => new Promise((resolve, reject) => {
  const handlers = listeners.runtimeMessage || [];
  if (!handlers.length) return reject(new Error("no runtime message listener"));
  const timer = realSetTimeout(() => reject(new Error("message response timed out")), 3000);
  const sendResponse = value => {
    realClearTimeout(timer);
    resolve(value);
  };
  for (const handler of handlers) {
    if (handler(request, {}, sendResponse) !== false) return;
  }
  realClearTimeout(timer);
  reject(new Error("message was not handled"));
});
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
    onMessage: listener("runtimeMessage"),
  },
  action: {
    setBadgeBackgroundColor: noop,
    setBadgeText: noop,
    setTitle: noop,
    onClicked: listener("clicked"),
  },
  alarms: {
    create: (name, info) => { alarms.push({name, ...info}); },
    clear: async () => true,
    onAlarm: listener("alarm"),
  },
  storage: {
    local: {
      get: async key => (key in localStore ? {[key]: localStore[key]} : {}),
      set: async items => { Object.assign(localStore, items); },
    },
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


def answer_error(answer: dict) -> str | None:
    """The error out of a companion result frame, so a failure says what it was."""
    return answer.get("error")


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _companion_socket(port: int):
    return connect(
        f"ws://127.0.0.1:{port}",
        origin=f"chrome-extension://{CHROME_EXTENSION_ID}",
    )


def _hello(
    token: str | None,
    nonce: str = "0f" * 16,
    role: str | None = None,
    run: str | None = None,
) -> str:
    message: dict = {"type": "hello", "protocol": 1, "browser": {"name": "Test Chrome"}}
    if role is not None:
        message["role"] = role
    # Left out entirely by default, because that is what a companion older than
    # 1.3.2 sends and the daemon has to keep serving it.
    if run is not None:
        message["browser"]["browser_run"] = run
    if token is not None:
        message["token"] = token
        message["nonce"] = nonce
    return json.dumps(message)


@contextlib.contextmanager
def _running_daemon(port: int | None = None, **kwargs):
    """A daemon on a port of its own, always stopped again."""
    daemon = BridgeDaemon(port=port or _free_port(), token=TEST_TOKEN, **kwargs)
    daemon.start()
    assert daemon.startup_error is None, daemon.startup_error
    try:
        yield daemon
    finally:
        daemon.shutdown()


@contextlib.contextmanager
def _attached_client(daemon: BridgeDaemon, **kwargs):
    """An MCP server's side of the bridge, pointed at one daemon that is already up."""
    kwargs.setdefault("spawn", False)
    client = ChromeBridge(port=daemon.port, token=TEST_TOKEN, **kwargs)
    client.start()
    try:
        yield client
    finally:
        client.shutdown()


class _FakeCompanion:
    """The extension's half of the protocol, driven by a test instead of Chrome."""

    def __init__(self, port: int, nonce: str = "0f" * 16, run: str | None = None) -> None:
        self.socket = _companion_socket(port)
        self.socket.send(_hello(TEST_TOKEN, nonce, run=run))
        acknowledgement = json.loads(self.socket.recv(timeout=5.0))
        assert acknowledgement["type"] == "hello_ack"
        # The ack proves the daemon knows the same secret, not just the port.
        assert bridge_auth.verify(TEST_TOKEN, nonce, acknowledgement["proof"])

    def take_command(self, timeout: float = 5.0) -> dict:
        return json.loads(self.socket.recv(timeout=timeout))

    def answer(self, command: dict, result) -> None:
        self.socket.send(
            json.dumps({"type": "result", "id": command["id"], "result": result})
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.socket.close()


def test_bridge_round_trip_accepts_extension_protocol() -> None:
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port)
        try:
            with _attached_client(daemon) as client:
                assert client.wait_connected(2.0)

                answers: list = []
                caller = threading.Thread(
                    target=lambda: answers.append(client.request("tabs.list", timeout=10.0))
                )
                caller.start()
                command = companion.take_command()
                assert command["type"] == "command" and command["method"] == "tabs.list"
                companion.answer(command, {"method": command["method"]})
                caller.join(timeout=5.0)
                assert answers == [{"method": "tabs.list"}]
        finally:
            companion.close()


@pytest.mark.parametrize("role", [None, "client"])
@pytest.mark.parametrize("token", [None, OTHER_TOKEN, "", "not-a-token"])
def test_bridge_rejects_a_client_without_the_shared_token(token, role) -> None:
    with _running_daemon() as daemon:
        with _companion_socket(daemon.port) as websocket:
            websocket.send(_hello(token, role=role))
            with pytest.raises(ConnectionClosed) as rejection:
                websocket.recv(timeout=5.0)
        closed = rejection.value.rcvd
        assert closed.code == 1008
        assert "token mismatch" in closed.reason
        assert "chrome://extensions" in closed.reason
        assert daemon.connected is False
        assert daemon.client_count == 0


def test_a_daemon_that_outlived_a_rotated_secret_accepts_the_new_one(tmp_path, monkeypatch) -> None:
    """Rotating the token must not require hunting down the running daemon.

    The documented way to retire a secret is to delete the file and let the next
    start mint another. A daemon caches what it read at startup, so before it
    learned to re-read, it would refuse every peer for as long as it ran and the
    only cure was killing it by hand.
    """
    token_file = tmp_path / "bridge-token"
    monkeypatch.setattr(bridge_auth, "token_path", lambda: token_file)
    monkeypatch.setattr(bridge_auth, "EXTENSION_TOKEN_FILE", tmp_path / "bridge-token.js")
    token_file.write_text(TEST_TOKEN, encoding="utf-8")

    # token=None makes the daemon own the on-disk secret, as it does in real use.
    daemon = BridgeDaemon(port=_free_port())
    daemon.start()
    assert daemon.startup_error is None, daemon.startup_error
    try:
        with _companion_socket(daemon.port) as websocket:
            websocket.send(_hello(TEST_TOKEN))
            assert json.loads(websocket.recv(timeout=5.0))["type"] == "hello_ack"

        token_file.write_text(OTHER_TOKEN, encoding="utf-8")

        with _companion_socket(daemon.port) as websocket:
            websocket.send(_hello(OTHER_TOKEN))
            assert json.loads(websocket.recv(timeout=5.0))["type"] == "hello_ack"

        # Adopting the newer secret must retire the old one, not widen the door.
        with _companion_socket(daemon.port) as websocket:
            websocket.send(_hello(TEST_TOKEN))
            with pytest.raises(ConnectionClosed) as rejection:
                websocket.recv(timeout=5.0)
        assert rejection.value.rcvd.code == 1008
    finally:
        daemon.shutdown()


def test_bridge_rejects_a_hello_without_a_nonce() -> None:
    with _running_daemon() as daemon:
        with _companion_socket(daemon.port) as websocket:
            websocket.send(json.dumps({"type": "hello", "protocol": 1, "token": TEST_TOKEN}))
            with pytest.raises(ConnectionClosed) as rejection:
                websocket.recv(timeout=5.0)
        assert rejection.value.rcvd.code == 1008
        assert "nonce" in rejection.value.rcvd.reason
        assert daemon.connected is False


def test_a_newer_authenticated_companion_replaces_the_previous_one() -> None:
    answers: list = []
    with _running_daemon() as daemon:
        first = _companion_socket(daemon.port)
        first.send(_hello(TEST_TOKEN))
        assert json.loads(first.recv(timeout=5.0))["type"] == "hello_ack"
        assert daemon.connected

        second = _FakeCompanion(daemon.port, nonce="abcd" * 8)
        with pytest.raises(ConnectionClosed):
            first.recv(timeout=5.0)

        with _attached_client(daemon) as client:
            assert client.wait_connected(2.0)
            caller = threading.Thread(
                target=lambda: answers.append(client.request("tabs.list", timeout=10.0))
            )
            caller.start()
            command = second.take_command()
            second.answer(command, ["second"])
            caller.join(timeout=5.0)
            assert answers == [["second"]]
        second.close()
        first.close()


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
def test_popup_can_disable_reenable_and_release_the_companion() -> None:
    outcome = _node_worker_eval(
        """
        await globalThis.__sleep(30);
        const initially = await globalThis.__message({type: "companion.status"});
        let detaches = 0;
        chrome.debugger.detach = async () => { detaches += 1; };

        const enabling = await globalThis.__message({
          type: "companion.setEnabled", enabled: true,
        });
        await globalThis.__waitFor(() => globalThis.__sockets.length === 1, "enable to connect");
        const socket = globalThis.__sockets[0];
        socket.onopen();
        const hello = JSON.parse(socket.sent[0]);
        socket.onmessage({data: JSON.stringify({
          type: "hello_ack",
          protocol: hello.protocol,
          proof: await globalThis.__hmac(hello.token, hello.nonce),
        })});
        const connected = await globalThis.__message({type: "companion.status"});
        const disabled = await globalThis.__message({
          type: "companion.setEnabled", enabled: false,
        });
        return {
          initially, enabling, connected, disabled, detaches,
          stored: globalThis.__local().companion_enabled,
          socketState: socket.readyState,
        };
        """,
        prelude=(
            "globalThis.__seedLocal({companion_enabled: false});\n"
            "globalThis.__seedSession({bridge_state: {attachedTabs: [7]}});\n"
            f"globalThis.__setToken('export const BRIDGE_TOKEN = \"{TEST_TOKEN}\";');"
        ),
    )
    assert outcome["initially"]["enabled"] is False
    assert outcome["initially"]["connected"] is False
    assert outcome["enabling"]["enabled"] is True
    assert outcome["connected"]["connected"] is True
    assert outcome["disabled"]["enabled"] is False
    assert outcome["disabled"]["detached_tabs"] == 1
    assert outcome["detaches"] == 1
    assert outcome["stored"] is False
    assert outcome["socketState"] == 3


def test_popup_controls_have_unique_ids() -> None:
    popup = (PROJECT_ROOT / "chrome-extension" / "popup.html").read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', popup)
    assert len(ids) == len(set(ids))
    assert {"release-status", "release-tabs", "check-release", "open-github"} <= set(ids)


@requires_node
def test_popup_checks_the_release_version_and_opens_github() -> None:
    popup_script = (PROJECT_ROOT / "chrome-extension" / "popup.js").as_uri()
    module = f"""
const callbacks = {{}};
const ids = [
  "enabled", "reconnect", "release-tabs", "status", "tabs", "bridge", "version",
  "release-status", "message", "check-release", "open-github",
];
const nodes = new Map(ids.map(id => [id, {{
  textContent: "", title: "", dataset: {{}}, checked: false, disabled: false,
  addEventListener(type, callback) {{ callbacks[`${{id}}:${{type}}`] = callback; }},
}}]));
globalThis.document = {{querySelector: selector => nodes.get(selector.slice(1))}};
let opened = null;
globalThis.chrome = {{
  runtime: {{sendMessage: async () => ({{
    enabled: true, connected: true, connecting: false, controlled_tabs: 2,
    bridge_url: "ws://127.0.0.1:8765", version: "1.3.4",
  }})}},
  tabs: {{create: options => {{ opened = options.url; }}}},
}};
globalThis.fetch = async () => ({{
  ok: true,
  json: async () => ([{{tag_name: "v1.3.4", html_url: "https://github.com/NeoXider/web-search-neo/releases/tag/v1.3.4"}}]),
}});
globalThis.setInterval = () => 0;
await import({json.dumps(popup_script)});
await new Promise(resolve => setTimeout(resolve, 20));
await callbacks["open-github:click"]();
await callbacks["check-release:click"]();
await new Promise(resolve => setTimeout(resolve, 20));
process.stdout.write(JSON.stringify({{
  release: nodes.get("release-status").textContent,
  releaseState: nodes.get("release-status").dataset.state,
  opened,
}}));
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", module],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome == {
        "release": "Up to date (v1.3.4)",
        "releaseState": "current",
        "opened": "https://github.com/NeoXider/web-search-neo",
    }


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


# A worker restart and a browser restart look almost the same from inside the
# extension - the module starts over either way - and the run id has to tell them
# apart, so both are spelled out here rather than argued about. Importing the
# module again under a different query string is a real second module instance
# with its own module scope, which is exactly what Chrome hands a worker it
# evicted; what carries over is the session store, exactly as in a browser that
# never closed.
_WORKER_RESTART = (
    "const restarted = await import("
    + json.dumps(SERVICE_WORKER_MODULE + "?worker-restart=1")
    + ");\n"
)
# Chrome empties chrome.storage.session when the browser shuts down. Nothing else
# about a browser restart is visible to the extension, so this is what one is.
_BROWSER_RESTART = (
    "const store = globalThis.__session();\n"
    "for (const key of Object.keys(store)) delete store[key];\n"
    "const restarted = await import("
    + json.dumps(SERVICE_WORKER_MODULE + "?browser-restart=1")
    + ");\n"
)
# A module that has just started is already connecting on its own, so this waits
# for a socket that did not exist before rather than taking the newest one: taking
# the newest one reads the hello of whichever instance got there first, which is
# how the first draft of these tests managed to pass while proving nothing.
_HELLO_ONCE = """
const helloFrom = async instance => {
  const before = globalThis.__sockets.length;
  await instance.connect();
  await globalThis.__waitFor(
    () => globalThis.__sockets.length > before, "a socket from this instance");
  const socket = globalThis.__sockets[globalThis.__sockets.length - 1];
  socket.onopen();
  return JSON.parse(socket.sent[socket.sent.length - 1]);
};
"""


@requires_node
def test_the_browser_run_id_holds_across_a_worker_restart() -> None:
    """MV3 evicts the worker constantly; an id that changed with it would be noise."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _HELLO_ONCE
        + """
        const first = await helloFrom(worker);
        """
        + _WORKER_RESTART
        + """
        const second = await helloFrom(restarted);
        return {
          first: first.browser,
          second: second.browser,
          stored: globalThis.__session().browser_run,
        };
        """
    )
    run = outcome["first"]["browser_run"]
    assert isinstance(run, str) and len(run) == 32 and int(run, 16) >= 0
    assert outcome["second"]["browser_run"] == run, "an evicted worker is not a new browser"
    assert outcome["stored"] == run
    assert outcome["first"]["name"] == "Chrome"


@requires_node
def test_the_browser_run_id_changes_when_the_browser_restarts() -> None:
    """The whole point: the id is what says the tab ids started over."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _HELLO_ONCE
        + """
        const first = await helloFrom(worker);
        """
        + _BROWSER_RESTART
        + """
        const second = await helloFrom(restarted);
        return {first: first.browser.browser_run, second: second.browser.browser_run};
        """
    )
    assert outcome["first"] and outcome["second"]
    assert outcome["first"] != outcome["second"]


@requires_node
def test_a_browser_start_replaces_an_id_that_outlived_its_browser() -> None:
    """If the session store was not emptied, onStartup is what still catches it."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _HELLO_ONCE
        + """
        const first = await helloFrom(worker);
        const socket = globalThis.__sockets[globalThis.__sockets.length - 1];
        globalThis.__fire("startup");
        await globalThis.__waitFor(
          () => globalThis.__session().browser_run !== first.browser.browser_run,
          "the browser start to mint a new run id",
        );
        const closed = socket.readyState === 3;
        const second = await helloFrom(worker);
        return {first: first.browser.browser_run, second: second.browser.browser_run, closed};
        """,
        prelude='globalThis.__seedSession({browser_run: "0123456789abcdef0123456789abcdef"});',
    )
    assert outcome["first"] == "0123456789abcdef0123456789abcdef"
    assert outcome["second"] != outcome["first"], "a browser start must not inherit an id"
    assert outcome["closed"], "the daemon still holds the stale id until we say hello again"


@requires_node
def test_a_browser_start_keeps_the_id_this_worker_just_minted() -> None:
    """Re-minting here would invalidate healthy sessions - the same bug, mirrored."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _HELLO_ONCE
        + """
        const first = await helloFrom(worker);
        const socket = globalThis.__sockets[globalThis.__sockets.length - 1];
        globalThis.__fire("startup");
        await globalThis.__sleep(120);
        return {
          first: first.browser.browser_run,
          stored: globalThis.__session().browser_run,
          open: socket.readyState === 1,
        };
        """
    )
    assert outcome["stored"] == outcome["first"]
    assert outcome["open"], "the live connection was dropped over an id that never changed"


def test_the_run_id_reaches_a_client_when_the_browser_arrives() -> None:
    """The fresh-connect path: a client that was already waiting is pushed the state."""
    with _running_daemon() as daemon:
        with _attached_client(daemon) as client:
            assert client.browser_info == {}
            companion = _FakeCompanion(daemon.port, run="run-of-the-first-browser")
            try:
                assert client.wait_connected(2.0)
                assert client.browser_info["browser_run"] == "run-of-the-first-browser"
                assert client.browser_run == "run-of-the-first-browser"
                assert client.status()["browser"]["browser_run"] == "run-of-the-first-browser"
                assert daemon.browser_info["browser_run"] == "run-of-the-first-browser"
            finally:
                companion.close()


def test_a_client_attaching_to_a_linked_daemon_learns_the_run_id() -> None:
    """The daemon outlives every client, so this is the common path, not the rare one."""
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port, run="run-of-the-second-browser")
        try:
            # No client was listening when the companion said hello, so nothing was
            # pushed; the status call the client makes on connect is all it gets.
            with _attached_client(daemon) as client:
                assert client.wait_connected(2.0)
                assert client.browser_run == "run-of-the-second-browser"
        finally:
            companion.close()


def test_a_restarted_browser_reaches_a_client_that_never_disconnected() -> None:
    """The defect end to end: the daemon stays up across the restart, so it must tell."""
    with _running_daemon() as daemon:
        first = _FakeCompanion(daemon.port, run="before-the-restart")
        try:
            with _attached_client(daemon) as client:
                assert client.wait_connected(2.0)
                assert client.browser_run == "before-the-restart"
                # Chrome closed and opened again; the companion of the new run
                # dials the same daemon and takes the connection over.
                second = _FakeCompanion(daemon.port, nonce="1a" * 16, run="after-the-restart")
                try:
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        if client.browser_run == "after-the-restart":
                            break
                        time.sleep(0.05)
                    assert client.browser_run == "after-the-restart", (
                        "a client holding tab ids from the old run was never told"
                    )
                finally:
                    second.close()
        finally:
            first.close()


def test_two_agents_cannot_drive_the_same_tab() -> None:
    """The guard used to live in one process's globals, which two processes ignore."""
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port, run="one-browser")
        try:
            with _attached_client(daemon) as first, _attached_client(daemon) as second:
                assert first.wait_connected(2.0) and second.wait_connected(2.0)
                granted = first.claim_tab(41)
                assert granted["status"] == "granted" and granted["granted"] is True
                assert granted["browser_run"] == "one-browser"

                refused = second.claim_tab(41)
                assert refused["status"] == "refused" and refused["granted"] is False
                # The message is shown to a person, so it has to name the holder
                # rather than only saying no.
                assert "41" in refused["reason"] and "another agent" in refused["reason"]
                assert refused["holder"]["label"] == daemon.claimed_tabs[41]
                assert refused["holder"]["held_seconds"] >= 0

                # A different tab is nobody's business but the asker's.
                assert second.claim_tab(42)["granted"] is True
                # And renewing your own grip is not a conflict with yourself.
                assert first.claim_tab(41)["granted"] is True

                assert first.release_tab(41) == {"released": True, "tab_id": 41, "reason": None}
                assert second.claim_tab(41)["granted"] is True
                # Releasing what is now someone else's must not take it from them.
                assert first.release_tab(41)["released"] is False
                assert 41 in daemon.claimed_tabs, "the tab lost its owner"
        finally:
            companion.close()


def test_a_tab_claim_dies_with_the_client_that_made_it() -> None:
    """A claim nobody can release would take the tab out of use until a restart."""
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port, run="one-browser")
        try:
            # A raw socket, so the drop is abrupt: no release, no goodbye, exactly
            # what an MCP server that is killed mid-session leaves behind.
            doomed = connect(f"ws://127.0.0.1:{daemon.port}")
            doomed.send(_hello(TEST_TOKEN, nonce="2b" * 16, role="client"))
            assert json.loads(doomed.recv(timeout=5.0))["type"] == "hello_ack"
            doomed.send(
                json.dumps({"type": "control", "id": "c1", "method": "claim_tab", "tab_id": 41})
            )
            assert json.loads(doomed.recv(timeout=5.0))["result"]["granted"] is True
            assert 41 in daemon.claimed_tabs

            doomed.close()
            deadline = time.monotonic() + 5.0
            while daemon.claimed_tabs and time.monotonic() < deadline:
                time.sleep(0.05)
            assert daemon.claimed_tabs == {}, "the tab is stranded until the daemon restarts"

            with _attached_client(daemon) as survivor:
                assert survivor.wait_connected(2.0)
                assert survivor.claim_tab(41)["granted"] is True
        finally:
            companion.close()


def test_claims_are_dropped_when_the_browser_behind_them_changes() -> None:
    """Tab 41 of the new browser has nothing to do with tab 41 of the old one."""
    with _running_daemon() as daemon:
        first_browser = _FakeCompanion(daemon.port, run="before-the-restart")
        try:
            with _attached_client(daemon) as client:
                assert client.wait_connected(2.0)
                assert client.claim_tab(41)["granted"] is True
                assert 41 in daemon.claimed_tabs

                second_browser = _FakeCompanion(
                    daemon.port, nonce="3c" * 16, run="after-the-restart"
                )
                try:
                    deadline = time.monotonic() + 5.0
                    while daemon.claimed_tabs and time.monotonic() < deadline:
                        time.sleep(0.05)
                    assert daemon.claimed_tabs == {}
                    with _attached_client(daemon) as newcomer:
                        assert newcomer.wait_connected(2.0)
                        fresh = newcomer.claim_tab(41)
                        assert fresh["granted"] is True
                        assert fresh["browser_run"] == "after-the-restart"
                finally:
                    second_browser.close()
        finally:
            first_browser.close()


def test_a_companion_reconnecting_does_not_cost_anyone_their_tab() -> None:
    """Only a different browser invalidates a claim; a dropped socket does not."""
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port, run="one-browser")
        with _attached_client(daemon) as client:
            assert client.wait_connected(2.0)
            assert client.claim_tab(41)["granted"] is True
            companion.close()
            deadline = time.monotonic() + 5.0
            while daemon.connected and time.monotonic() < deadline:
                time.sleep(0.05)
            again = _FakeCompanion(daemon.port, nonce="4d" * 16, run="one-browser")
            try:
                assert client.wait_connected(2.0)
                assert set(daemon.claimed_tabs) == {41}
                with _attached_client(daemon) as other:
                    assert other.wait_connected(2.0)
                    assert other.claim_tab(41)["granted"] is False
            finally:
                again.close()


def _settled(check, label: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {label}")


def test_a_client_asks_for_its_tabs_again_on_a_link_it_had_to_rebuild() -> None:
    """A claim belongs to a connection, and connections do not last.

    Dropping the link and letting it form again is what a daemon restart looks
    like from this side, and a daemon restart is precisely when another agent is
    starting up too - so a guard that quietly forgot itself here would be missing
    at the one moment it is needed.
    """
    port = _free_port()
    client = ChromeBridge(port=port, token=TEST_TOKEN, spawn=False)
    try:
        with _running_daemon(port=port) as daemon:
            client.start()
            assert client.claim_tab(41)["granted"] is True
            assert client.claimed_tabs == (41,)

            client.shutdown()
            _settled(lambda: daemon.claimed_tabs == {}, "the daemon to free the orphaned tab")

            client.start()
            _settled(lambda: set(daemon.claimed_tabs) == {41}, "the client to ask again")
            assert client.claimed_tabs == (41,)
            with _attached_client(daemon) as newcomer:
                assert newcomer.claim_tab(41)["granted"] is False
    finally:
        client.shutdown()


def test_a_tab_lost_while_the_link_was_down_stops_being_ours() -> None:
    """Telling a caller it still owns a tab that it does not is the worse failure."""
    port = _free_port()
    client = ChromeBridge(port=port, token=TEST_TOKEN, spawn=False)
    try:
        with _running_daemon(port=port) as daemon:
            client.start()
            assert client.claim_tab(41)["granted"] is True
            client.shutdown()
            _settled(lambda: daemon.claimed_tabs == {}, "the daemon to free the orphaned tab")

            with _attached_client(daemon) as squatter:
                # Somebody else got there first while the link was down.
                assert squatter.claim_tab(41)["granted"] is True
                client.start()
                _settled(lambda: client.claimed_tabs == (), "the client to give the tab up")
                # And the tab stayed with whoever actually holds it.
                assert 41 in daemon.claimed_tabs
                assert squatter.claimed_tabs == (41,)
    finally:
        client.shutdown()


def test_a_claim_survives_the_daemon_learning_which_browser_it_serves() -> None:
    """A daemon starts knowing no run at all, and that is not a browser change.

    Anything claimed before the companion said hello was claimed against the
    browser that is about to say hello - there is only one - so learning its name
    must adopt those claims, not throw them away behind the holder's back.
    """
    with _running_daemon() as daemon:
        with _attached_client(daemon) as holder:
            assert holder.claim_tab(41)["granted"] is True
            companion = _FakeCompanion(daemon.port, run="run-X")
            try:
                _settled(lambda: holder.browser_run == "run-X", "the client to see the browser")
                assert set(daemon.claimed_tabs) == {41}, "the claim was thrown away"
                assert holder.claimed_tabs == (41,)
                with _attached_client(daemon) as newcomer:
                    assert newcomer.claim_tab(41)["granted"] is False
            finally:
                companion.close()


def test_a_re_asserted_claim_survives_the_companion_coming_back() -> None:
    """The whole sequence: daemon replaced, client re-claims, companion returns."""
    port = _free_port()
    client = ChromeBridge(port=port, token=TEST_TOKEN, spawn=False)
    try:
        with _running_daemon(port=port) as first_daemon:
            companion = _FakeCompanion(first_daemon.port, run="run-X")
            client.start()
            assert client.claim_tab(41)["browser_run"] == "run-X"
            companion.close()
        # The daemon is replaced - the version handshake does exactly this on an
        # upgrade - and the client, which reconnects in well under a second, gets
        # there long before the companion is out of its own backoff.
        with _running_daemon(port=port) as second_daemon:
            _settled(lambda: set(second_daemon.claimed_tabs) == {41}, "the client to re-claim")
            returning = _FakeCompanion(second_daemon.port, nonce="5e" * 16, run="run-X")
            try:
                _settled(lambda: client.browser_run == "run-X", "the state push")
                assert set(second_daemon.claimed_tabs) == {41}, "the re-asserted claim was lost"
                assert client.claimed_tabs == (41,)
                with _attached_client(second_daemon) as newcomer:
                    assert newcomer.claim_tab(41)["granted"] is False
            finally:
                returning.close()
    finally:
        client.shutdown()


def test_a_re_asserted_claim_still_goes_when_the_browser_really_changed() -> None:
    """The other half: adopting the unknown must not adopt the genuinely stale."""
    port = _free_port()
    client = ChromeBridge(port=port, token=TEST_TOKEN, spawn=False)
    try:
        with _running_daemon(port=port) as first_daemon:
            companion = _FakeCompanion(first_daemon.port, run="run-X")
            client.start()
            assert client.claim_tab(41)["browser_run"] == "run-X"
            companion.close()
        with _running_daemon(port=port) as second_daemon:
            _settled(lambda: set(second_daemon.claimed_tabs) == {41}, "the client to re-claim")
            # A different browser this time: tab 41 is somebody else's tab now.
            other_browser = _FakeCompanion(second_daemon.port, nonce="6f" * 16, run="run-Y")
            try:
                _settled(lambda: second_daemon.claimed_tabs == {}, "the stale claim to go")
                _settled(lambda: client.claimed_tabs == (), "the client to stop believing")
                with _attached_client(second_daemon) as newcomer:
                    assert newcomer.claim_tab(41)["granted"] is True
            finally:
                other_browser.close()
    finally:
        client.shutdown()


def test_a_claim_made_against_a_browser_that_is_gone_is_refused() -> None:
    """A re-assert naming a run the daemon knows is over cannot be honoured."""
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port, run="run-now")
        try:
            with _attached_client(daemon) as client:
                _settled(lambda: client.browser_run == "run-now", "the browser state")
                answer = client._control("claim_tab", {"tab_id": 41, "browser_run": "run-before"})
                assert answer["granted"] is False
                assert "different browser" in answer["reason"]
                assert daemon.claimed_tabs == {}
        finally:
            companion.close()


def test_a_peer_that_accepts_and_then_says_nothing_is_hung_up_on() -> None:
    """An abandoned socket keeps a reader thread, and the retry dials another.

    The trigger is narrow - something is listening on the port and will not talk,
    a wedged daemon or a foreign server - but the server that meets it runs for
    days, and every attempt would leave one more socket behind it.
    """

    class _SilentPeer:
        def __init__(self, on_send: Exception | None = None) -> None:
            self.closed: list[int] = []
            self.on_send = on_send

        def send(self, text: str) -> None:
            if self.on_send is not None:
                raise self.on_send

        def recv(self, timeout: float | None = None):
            raise TimeoutError("timed out waiting for the hello_ack")

        def close(self, code: int = 1000, reason: str = "") -> None:
            self.closed.append(code)

    client = ChromeBridge(port=_free_port(), token=TEST_TOKEN, spawn=False, start_timeout=0.1)
    try:
        for peer in (_SilentPeer(), _SilentPeer(on_send=OSError("broken pipe"))):
            with pytest.raises(ChromeBridgeError):
                client._handshake(peer)
            assert peer.closed, "the socket was left open for the garbage collector"
    finally:
        client.shutdown()


def test_a_claim_with_nobody_to_ask_is_not_a_refusal() -> None:
    """'Nobody answered' and 'someone else has it' must not look the same."""
    lonely = ChromeBridge(port=_free_port(), token=TEST_TOKEN, spawn=False, start_timeout=0.2)
    try:
        answer = lonely.claim_tab(41)
        assert answer["status"] == "unavailable"
        # Failing open: a browser nobody is guarding is still usable, and the
        # caller's own in-process guard still covers the single-server case.
        assert answer["granted"] is True
        assert "could not be asked" in answer["reason"]
        assert lonely.release_tab(41)["released"] is False
    finally:
        lonely.shutdown()


def test_a_daemon_that_never_heard_of_claims_reads_as_unavailable() -> None:
    """An older daemon answers with an error, which is not a "no" either."""

    class _DaemonWithoutClaims(BridgeDaemon):
        def _answer_control(self, client, message) -> None:
            if message.get("method") in {"claim_tab", "release_tab"}:
                client.send_quietly(
                    {
                        "type": "control_result",
                        "id": message.get("id"),
                        "error": f"Unknown bridge control method {message.get('method')!r}",
                    }
                )
                return
            super()._answer_control(client, message)

    daemon = _DaemonWithoutClaims(port=_free_port(), token=TEST_TOKEN)
    daemon.start()
    assert daemon.startup_error is None, daemon.startup_error
    try:
        with _attached_client(daemon) as client:
            answer = client.claim_tab(41)
            assert answer["status"] == "unavailable" and answer["granted"] is True
            assert "Unknown bridge control method" in answer["reason"]
    finally:
        daemon.shutdown()


def test_a_tab_id_that_is_not_a_number_is_refused_rather_than_stored() -> None:
    with _running_daemon() as daemon:
        with _attached_client(daemon) as client:
            answer = client._control("claim_tab", {"tab_id": None})
            assert answer["granted"] is False and "numeric" in answer["reason"]
            assert daemon.claimed_tabs == {}


def test_a_companion_too_old_to_mint_a_run_id_is_still_served() -> None:
    """Requiring the id would take the browser away from anyone mid-update."""
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port)
        try:
            with _attached_client(daemon) as client:
                assert client.wait_connected(2.0)
                # Present and empty, not missing: a caller reading it never has to
                # tell "the companion said nothing" from "the daemon dropped it".
                assert client.browser_info["browser_run"] is None
                assert client.browser_run is None
                answers: list = []
                caller = threading.Thread(
                    target=lambda: answers.append(client.request("tabs.list", timeout=10.0))
                )
                caller.start()
                command = companion.take_command()
                companion.answer(command, [])
                caller.join(timeout=5.0)
                assert answers == [[]], "an old companion must still be able to work"
        finally:
            companion.close()


@pytest.mark.parametrize("junk", [17, "", {"id": "x"}, ["x"], "x" * 257])
def test_a_run_id_that_is_not_an_identity_is_reported_as_none(junk) -> None:
    """Passing junk on would let it be compared against a later, real id."""
    hello = {
        "type": "hello",
        "protocol": 1,
        "browser": {"name": "Test Chrome", "browser_run": junk},
    }
    assert bridge_daemon.browser_state(hello)["browser_run"] is None
    assert bridge_daemon.browser_state(hello)["name"] == "Test Chrome"


def test_a_run_id_at_the_top_level_of_the_hello_is_accepted_too() -> None:
    """So the daemon need not ship in lockstep with a companion that moves it."""
    state = bridge_daemon.browser_state(
        {"type": "hello", "protocol": 1, "browser_run": "top-level"}
    )
    assert state["browser_run"] == "top-level"


# Enough of a browser for the tab commands: windows the user may be looking at,
# tab groups that may already be ours, and a record of every call the worker
# makes, since what these tests are about is exactly which flags it passes.
_TAB_WORLD = """
const world = {windows: [], groups: [], created: null, updated: [], raised: []};
globalThis.__world = world;
chrome.windows.getAll = async () => world.windows.map(item => ({...item}));
chrome.windows.update = async (windowId, patch) => {
  world.raised.push({windowId, ...patch});
  return {id: windowId};
};
chrome.tabGroups.query = async (filter = {}) => world.groups
  .filter(item => filter.windowId === undefined || item.windowId === filter.windowId)
  .map(item => ({...item}));
chrome.tabGroups.update = async (groupId, patch) => ({id: groupId, ...patch});
chrome.tabs.create = async info => {
  world.created = {...info};
  return {id: 7, windowId: info.windowId ?? 1, active: Boolean(info.active),
          status: "complete", url: info.url, title: ""};
};
chrome.tabs.get = async tabId => ({
  id: tabId, windowId: world.created?.windowId ?? 1, active: false,
  status: "complete", url: "about:blank", title: ""});
chrome.tabs.update = async (tabId, patch) => {
  world.updated.push({tabId, ...patch});
  return {id: tabId, windowId: 1, active: Boolean(patch.active),
          status: "complete", url: patch.url || "about:blank", title: ""};
};
chrome.tabs.group = async ({groupId}) => groupId ?? 99;
"""

# A command is only answered on the socket it arrived on, and only after the
# handshake, so a test that wants to call one has to earn a verified socket.
_VERIFIED_SOCKET = """
const openSocket = async () => {
  await worker.connect();
  const socket = globalThis.__sockets[globalThis.__sockets.length - 1];
  socket.onopen();
  const hello = JSON.parse(socket.sent[0]);
  socket.onmessage({data: JSON.stringify({
    type: "hello_ack",
    protocol: hello.protocol,
    proof: await globalThis.__hmac(hello.token, hello.nonce),
  })});
  return socket;
};
const ask = async (socket, method, params) => {
  const before = socket.sent.length;
  socket.onmessage({data: JSON.stringify({type: "command", id: method, method, params})});
  await globalThis.__waitFor(
    () => socket.sent.length > before, "the answer to " + method);
  return JSON.parse(socket.sent[socket.sent.length - 1]);
};
"""


@requires_node
def test_a_new_tab_opens_behind_the_window_the_user_is_working_in() -> None:
    """The default has to be the polite one: the browser is not ours."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [
          {id: 1, focused: true, state: "normal"},
          {id: 2, focused: false, state: "minimized"},
          {id: 3, focused: false, state: "normal"}];
        const socket = await openSocket();
        const answer = await ask(socket, "tabs.create", {url: "about:blank", group: "AI"});
        return {answer, created: globalThis.__world.created, raised: globalThis.__world.raised};
        """
    )
    assert answer_error(outcome["answer"]) is None
    assert outcome["created"]["active"] is False
    # Not window 1, which the user is in, and not the minimized window 2, which
    # is the last place to leave work someone may want to look at.
    assert outcome["created"]["windowId"] == 3
    assert outcome["raised"] == [], "nothing may raise a window on its own"


@requires_node
def test_a_new_tab_joins_the_window_that_already_holds_the_agents_group() -> None:
    """Agent tabs cluster where the agent already works, not where the user does."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [
          {id: 1, focused: true}, {id: 2, focused: false}, {id: 3, focused: false}];
        globalThis.__world.groups = [
          {id: 10, title: "Other", windowId: 2}, {id: 11, title: "AI", windowId: 3}];
        const socket = await openSocket();
        await ask(socket, "tabs.create", {url: "about:blank", group: "AI"});
        return globalThis.__world.created;
        """
    )
    assert outcome["windowId"] == 3
    assert outcome["active"] is False


@requires_node
def test_the_agents_own_group_does_not_win_a_minimized_window() -> None:
    """A tab that is never painted cannot be photographed, group or no group.

    Chrome does not composite a minimized window, so a session parked there
    spends the full capture budget on every screenshot and then reports an
    obscured window - for good, and with nothing to point the user back at the
    window they minimized an hour ago. A duplicate group heading somewhere else
    is the cheaper mess by a wide margin.
    """
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.groups = [{id: 11, title: "AI", windowId: 3}];
        globalThis.__world.windows = [
          {id: 1, focused: true, state: "normal"},
          {id: 2, focused: false, state: "normal"},
          {id: 3, focused: false, state: "minimized"}];
        const socket = await openSocket();
        await ask(socket, "tabs.create", {url: "about:blank", group: "AI"});
        const elsewhere = {...globalThis.__world.created};

        // And with nowhere else to go, the user's own window still beats it: a
        // window that already exists is better than one Chrome would raise.
        globalThis.__world.windows = [
          {id: 1, focused: true, state: "normal"},
          {id: 3, focused: false, state: "minimized"}];
        const answer = await ask(socket, "tabs.create", {url: "about:blank", group: "AI"});
        return {elsewhere, answer, lastResort: globalThis.__world.created};
        """
    )
    assert outcome["elsewhere"]["windowId"] == 2, "the group's window is minimized"
    assert outcome["elsewhere"]["active"] is False
    assert answer_error(outcome["answer"]) is None
    assert outcome["lastResort"]["windowId"] == 1, "a minimized window is never the answer"
    assert outcome["lastResort"]["active"] is False


@requires_node
def test_a_tab_asked_for_in_front_opens_where_the_user_is_looking() -> None:
    """Opting in has to give the old behaviour back, or it is not an escape hatch."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [{id: 1, focused: false}, {id: 2, focused: true}];
        globalThis.__world.groups = [{id: 11, title: "AI", windowId: 1}];
        const socket = await openSocket();
        await ask(socket, "tabs.create", {url: "about:blank", group: "AI", active: true});
        return globalThis.__world.created;
        """
    )
    assert outcome["active"] is True
    assert outcome["windowId"] == 2, "a tab to be watched belongs in the window in front"


@requires_node
def test_a_lone_window_gets_the_background_tab_rather_than_a_new_window() -> None:
    """A window of our own would raise itself, which is the interruption we avoid."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [{id: 5, focused: true}];
        const socket = await openSocket();
        const answer = await ask(socket, "tabs.create", {url: "about:blank", group: ""});
        return {answer, created: globalThis.__world.created};
        """
    )
    assert answer_error(outcome["answer"]) is None
    # The window it was given is the window it used: naming the only window there
    # is, rather than leaving the choice to Chrome, is what keeps a new window out
    # of it. (Chrome itself decides when no window is named - see the test above -
    # so an omitted windowId, not a call to chrome.windows.create, is the shape a
    # regression would take here.)
    assert outcome["created"] == {"url": "about:blank", "active": False, "windowId": 5}


@requires_node
def test_a_background_tab_prefers_the_focused_window_to_a_minimized_one() -> None:
    """Chrome does not paint a minimized window, and an unpainted tab cannot be seen.

    Every screenshot of a tab parked there spends the whole capture budget and
    comes back as the obscured-window error, for the life of the session. The
    user's own window is the lesser intrusion by far: their tab stays in front.
    """
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [
          {id: 1, focused: true, state: "normal"},
          {id: 2, focused: false, state: "minimized"}];
        const socket = await openSocket();
        await ask(socket, "tabs.create", {url: "about:blank", group: "AI"});
        return globalThis.__world.created;
        """
    )
    assert outcome["windowId"] == 1
    assert outcome["active"] is False


@requires_node
def test_a_window_that_closes_under_the_new_tab_is_not_fatal() -> None:
    """The window is chosen and then used; a user can close it in between."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [
          {id: 1, focused: true, state: "normal"},
          {id: 3, focused: false, state: "normal"}];
        const attempts = [];
        const create = chrome.tabs.create;
        chrome.tabs.create = async info => {
          attempts.push({...info});
          if (info.windowId !== undefined) throw new Error("No window with id: 3.");
          return create(info);
        };
        const socket = await openSocket();
        const answer = await ask(socket, "tabs.create", {url: "about:blank", group: "AI"});
        return {answer, attempts};
        """
    )
    assert answer_error(outcome["answer"]) is None, "a closed window must not fail the call"
    assert outcome["attempts"][0]["windowId"] == 3
    assert "windowId" not in outcome["attempts"][1], "the retry must let Chrome choose"
    assert outcome["answer"]["result"]["id"] == 7


@requires_node
def test_a_browser_with_no_window_left_still_gets_its_tab() -> None:
    """Chrome can outlive its last window, and a tab has to live in one."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [];
        const socket = await openSocket();
        const answer = await ask(socket, "tabs.create", {url: "about:blank", group: ""});
        return {answer, created: globalThis.__world.created};
        """
    )
    assert answer_error(outcome["answer"]) is None
    # Chrome makes the window itself, and that window will raise itself: the one
    # case where a background tab cannot help being seen.
    assert outcome["created"] == {"url": "about:blank", "active": False}


@requires_node
def test_navigating_does_not_pull_the_screen_to_the_agents_tab() -> None:
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [{id: 1, focused: true}];
        const socket = await openSocket();
        await ask(socket, "tabs.navigate", {tabId: 7, url: "https://example.test/"});
        await ask(socket, "tabs.navigate", {tabId: 7, url: "https://example.test/2", active: true});
        return globalThis.__world.updated;
        """
    )
    assert outcome[0] == {"tabId": 7, "url": "https://example.test/"}
    assert "active" not in outcome[0], "the tab keeps whatever place the user gave it"
    assert outcome[1] == {"tabId": 7, "url": "https://example.test/2", "active": True}


@requires_node
def test_attaching_the_debugger_makes_a_hidden_tab_controllable() -> None:
    """Measured, not assumed: without this a background tab drops every keystroke."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        const sent = [];
        chrome.debugger.sendCommand = async (target, method, params) => {
          sent.push({tabId: target.tabId, method, params: params || null});
          return {};
        };
        const socket = await openSocket();
        const answer = await ask(socket, "cdp.send",
          {tabId: 7, method: "Runtime.evaluate", params: {expression: "1"}});
        return {answer, sent};
        """
    )
    assert answer_error(outcome["answer"]) is None
    focus = [item for item in outcome["sent"]
             if item["method"] == "Emulation.setFocusEmulationEnabled"]
    assert focus == [{"tabId": 7, "method": "Emulation.setFocusEmulationEnabled",
                      "params": {"enabled": True}}]
    # It has to be in place before the first command the caller actually wanted,
    # or that command is the one that gets dropped.
    methods = [item["method"] for item in outcome["sent"]]
    assert methods.index("Emulation.setFocusEmulationEnabled") < methods.index("Runtime.evaluate")


@requires_node
def test_a_tab_that_could_not_be_configured_is_not_recorded_as_attached() -> None:
    """Recording a half-attached tab as done is how focus emulation goes missing.

    Chrome refuses a second attach whether the debugger holding the tab is this
    worker's own or a DevTools window the user opened, so an attach that "worked"
    proves nothing. The first command is what proves it, and until it answers the
    tab must stay unattached - otherwise one failure leaves the tab silently
    uncontrollable for the life of the worker: input accepted and dropped, no
    frames, timers clamped.
    """
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        const attaches = [];
        const sent = [];
        let detaches = 0;
        let failFirstEnable = true;
        chrome.debugger.attach = async target => { attaches.push(target.tabId); };
        chrome.debugger.detach = async () => { detaches += 1; };
        chrome.debugger.sendCommand = async (target, method, params) => {
          if (method === "Runtime.enable" && failFirstEnable) {
            failFirstEnable = false;
            throw new Error("Debugger is not attached to the tab with id: 7");
          }
          sent.push({method, params: params || null});
          return {};
        };
        const socket = await openSocket();
        const first = await ask(socket, "cdp.send",
          {tabId: 7, method: "Runtime.evaluate", params: {expression: "1"}});
        const second = await ask(socket, "cdp.send",
          {tabId: 7, method: "Runtime.evaluate", params: {expression: "2"}});
        return {first, second, attaches, detaches, methods: sent.map(item => item.method)};
        """
    )
    assert answer_error(outcome["first"]), "a tab that could not be set up must say so"
    assert answer_error(outcome["second"]) is None, "the next call must try again"
    assert outcome["attaches"] == [7, 7], "the failed attach was never retried"
    assert outcome["detaches"] == 1, "a half-open session was left behind"
    assert "Emulation.setFocusEmulationEnabled" in outcome["methods"]
    # And the command the caller actually asked for ran only after the setup.
    assert outcome["methods"].index("Emulation.setFocusEmulationEnabled") < outcome[
        "methods"
    ].index("Runtime.evaluate")


@requires_node
def test_a_stored_attachment_that_chrome_dropped_is_repaired() -> None:
    """The MV3 shelf may say attached after Chrome dropped the debugger itself."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        let attaches = 0;
        let firstProbe = true;
        const methods = [];
        chrome.debugger.attach = async () => { attaches += 1; };
        chrome.debugger.sendCommand = async (target, method) => {
          methods.push(method);
          if (method === "Runtime.enable" && firstProbe) {
            firstProbe = false;
            throw new Error("Debugger is not attached to the tab with id: 7");
          }
          return {};
        };
        const socket = await openSocket();
        const answer = await ask(socket, "cdp.send",
          {tabId: 7, method: "Runtime.evaluate", params: {expression: "1"}});
        return {answer, attaches, methods, stored: globalThis.__session().bridge_state};
        """,
        prelude="globalThis.__seedSession({bridge_state: {attachedTabs: [7]}});",
    )
    assert answer_error(outcome["answer"]) is None
    assert outcome["attaches"] == 1
    assert outcome["methods"].count("Runtime.enable") == 2
    assert "Emulation.setFocusEmulationEnabled" in outcome["methods"]
    assert outcome["methods"][-1] == "Runtime.evaluate"
    assert outcome["stored"]["attachedTabs"] == [7]


@requires_node
def test_a_tab_another_debugger_holds_is_reported_rather_than_silently_broken() -> None:
    """DevTools open on the tab is the everyday case, and it must not read as fine."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        let attaches = 0;
        chrome.debugger.attach = async () => {
          attaches += 1;
          throw new Error("Another debugger is already attached to the tab with id: 7");
        };
        chrome.debugger.sendCommand = async (target, method) => {
          if (method === "Runtime.enable") {
            throw new Error("Debugger is not attached to the tab with id: 7");
          }
          return {};
        };
        const socket = await openSocket();
        const answer = await ask(socket, "cdp.send",
          {tabId: 7, method: "Runtime.evaluate", params: {expression: "1"}});
        // The user closes DevTools and asks again.
        chrome.debugger.attach = async () => { attaches += 1; };
        chrome.debugger.sendCommand = async () => ({});
        const recovered = await ask(socket, "cdp.send",
          {tabId: 7, method: "Runtime.evaluate", params: {expression: "1"}});
        return {answer, recovered, attaches};
        """
    )
    failure = answer_error(outcome["answer"])
    assert failure and "DevTools" in failure, "the agent cannot act on a bare CDP error"
    assert "7" in failure
    assert answer_error(outcome["recovered"]) is None, "closing DevTools must fix it"
    assert outcome["attaches"] == 2


@requires_node
def test_activating_a_tab_still_raises_its_window() -> None:
    """The one command that means "show me": it must keep working, and stay alone."""
    outcome = _node_worker_eval(
        _WORKER_READY
        + _TAB_WORLD
        + _VERIFIED_SOCKET
        + """
        globalThis.__world.windows = [{id: 1, focused: true}];
        const socket = await openSocket();
        await ask(socket, "tabs.activate", {tabId: 7});
        return {updated: globalThis.__world.updated, raised: globalThis.__world.raised};
        """
    )
    assert outcome["updated"] == [{"tabId": 7, "active": True}]
    assert outcome["raised"] == [{"windowId": 1, "focused": True}]


def test_a_first_frame_that_is_not_an_object_is_refused_with_a_reason() -> None:
    with _running_daemon() as daemon:
        for frame in ("not json at all", "123", "[1, 2]", "null"):
            with _companion_socket(daemon.port) as websocket:
                websocket.send(frame)
                with pytest.raises(ConnectionClosed) as rejection:
                    websocket.recv(timeout=5.0)
            closed = rejection.value.rcvd
            assert closed.code == 1008, frame
            assert "JSON object" in closed.reason, frame
        assert daemon.connected is False


def test_two_mcp_servers_hold_commands_in_flight_at_the_same_time() -> None:
    """The whole point of the daemon: a second agent must not be locked out."""
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port)
        try:
            with _attached_client(daemon) as first, _attached_client(daemon) as second:
                assert first.wait_connected(2.0) and second.wait_connected(2.0)
                assert daemon.client_count == 2

                answers: dict[str, object] = {}
                callers = [
                    threading.Thread(
                        target=lambda name=name, client=client: answers.__setitem__(
                            name, client.request("tabs.list", timeout=10.0)
                        )
                    )
                    for name, client in (("first", first), ("second", second))
                ]
                for caller in callers:
                    caller.start()

                commands = [companion.take_command(), companion.take_command()]
                assert commands[0]["id"] != commands[1]["id"], "ids were not mapped per client"
                # Answering out of order is what proves the daemon routes by the
                # id it minted rather than by whoever asked last.
                companion.answer(commands[1], ["for the second caller"])
                companion.answer(commands[0], ["for the first caller"])
                for caller in callers:
                    caller.join(timeout=5.0)

                assert set(answers) == {"first", "second"}
                assert sorted(str(answer) for answer in answers.values()) == [
                    "['for the first caller']",
                    "['for the second caller']",
                ]
                assert answers["first"] != answers["second"]
        finally:
            companion.close()


def test_the_daemon_never_mistakes_a_local_client_for_the_browser() -> None:
    """An MCP server holding the token must not make the companion look present."""
    with _running_daemon() as daemon:
        with _attached_client(daemon) as client:
            assert client.wait_connected(0.5) is False
            assert daemon.connected is False
            assert daemon.status()["clients"] == 1
            with pytest.raises(ChromeBridgeError) as failure:
                client.request("tabs.list", timeout=1.0)
            assert "not connected" in str(failure.value)


def test_a_command_in_flight_fails_when_the_daemon_dies_instead_of_hanging() -> None:
    with _running_daemon() as daemon:
        companion = _FakeCompanion(daemon.port)
        try:
            with _attached_client(daemon) as client:
                assert client.wait_connected(2.0)
                failures: list[BaseException] = []

                def ask() -> None:
                    try:
                        client.request("tabs.list", timeout=30.0)
                    except BaseException as exc:  # noqa: BLE001 - the test inspects it
                        failures.append(exc)

                caller = threading.Thread(target=ask)
                caller.start()
                companion.take_command()  # taken and deliberately never answered
                daemon.shutdown()

                caller.join(timeout=10.0)
                assert not caller.is_alive(), "the caller waited out its own timeout"
                assert len(failures) == 1
                assert isinstance(failures[0], ChromeBridgeError)
                assert "daemon" in str(failures[0])
        finally:
            companion.close()


def test_a_daemon_of_the_same_version_is_left_alone() -> None:
    spawns: list[int] = []
    with _running_daemon(version="4.5.6") as daemon:
        with _attached_client(daemon, version="4.5.6", spawn=spawns.append) as client:
            status = client.status(0.0)
            assert status["daemon"]["linked"] and status["daemon"]["version"] == "4.5.6"
            assert spawns == [], "a matching daemon was replaced for no reason"
            assert daemon.stopped is False


def test_a_daemon_running_other_code_is_replaced_before_any_command() -> None:
    """A daemon spawned before a git pull would keep serving the code it started with."""
    port = _free_port()
    replacements: list[BridgeDaemon] = []

    def spawn_current(_port: int) -> None:
        replacement = BridgeDaemon(port=port, token=TEST_TOKEN, version="9.9.9")
        replacement.start()
        replacements.append(replacement)

    stale = BridgeDaemon(port=port, token=TEST_TOKEN, version="0.0.1-stale")
    stale.start()
    client = ChromeBridge(
        port=port, token=TEST_TOKEN, version="9.9.9", spawn=spawn_current, connect_timeout=20.0
    )
    try:
        client.start()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not client.status(0.0)["daemon"]["linked"]:
            time.sleep(0.1)
        status = client.status(0.0)
        assert status["daemon"]["linked"], status
        assert status["daemon"]["version"] == "9.9.9"
        assert len(replacements) == 1
        assert stale.stopped, "the outdated daemon was left running"
    finally:
        client.shutdown()
        stale.shutdown()
        for replacement in replacements:
            replacement.shutdown()


def test_a_replacement_that_is_also_outdated_stops_instead_of_looping() -> None:
    """Two checkouts fighting over the port must end in one error, not forever."""
    port = _free_port()
    spawned: list[BridgeDaemon] = []

    def spawn_stale(_port: int) -> None:
        replacement = BridgeDaemon(port=port, token=TEST_TOKEN, version="0.0.1-stale")
        replacement.start()
        spawned.append(replacement)

    first = BridgeDaemon(port=port, token=TEST_TOKEN, version="0.0.1-stale")
    first.start()
    client = ChromeBridge(
        port=port, token=TEST_TOKEN, version="9.9.9", spawn=spawn_stale, connect_timeout=20.0
    )
    try:
        assert client.wait_connected(1.0) is False
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline and not client.status(0.0)["startup_error"]:
            time.sleep(0.1)
        error = client.status(0.0)["startup_error"] or ""
        assert "9.9.9" in error and "0.0.1-stale" in error, error
        assert len(spawned) <= chrome_bridge.MAX_DAEMON_REPLACEMENTS, spawned

        # Latched: further calls report the skew instead of restarting the fight.
        attempts = len(spawned)
        with pytest.raises(ChromeBridgeError) as failure:
            client.request("tabs.list", timeout=1.0)
        assert "0.0.1-stale" in str(failure.value)
        assert len(spawned) == attempts
    finally:
        client.shutdown()
        first.shutdown()
        for daemon in spawned:
            daemon.shutdown()


def test_the_daemon_exits_once_neither_the_browser_nor_a_client_is_left() -> None:
    """Nothing registers the daemon for autostart, so nothing may leak it either."""
    with _running_daemon(idle_seconds=0.5) as daemon:
        companion = _FakeCompanion(daemon.port)
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not daemon.connected:
                time.sleep(0.05)
            assert daemon.connected
            time.sleep(1.0)
            assert not daemon.stopped, "a connected companion kept nobody alive"
        finally:
            companion.close()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not daemon.stopped:
            time.sleep(0.05)
        assert daemon.stopped, "the idle daemon stayed up"


def test_the_command_line_stops_a_daemon_and_says_so_when_there_is_none() -> None:
    """A daemon outlives the checkout that started it, so it must be stoppable."""
    port = _free_port()
    command = [sys.executable, str(PROJECT_ROOT / "main.py"), "--bridge", "--stop"]
    environment = dict(os.environ, WEB_SEARCH_NEO_BRIDGE_PORT=str(port))

    idle = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=120
    )
    assert idle.returncode == 0, idle.stderr
    assert "No bridge daemon" in idle.stdout

    # The daemon holds the machine secret, because that is the only one the
    # command line can present: it is started from a plain checkout.
    daemon = BridgeDaemon(port=port, version=main.__version__)
    daemon.start()
    try:
        stopped = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=120
        )
        assert stopped.returncode == 0, stopped.stderr
        assert "asked to stop" in stopped.stdout
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not daemon.stopped:
            time.sleep(0.05)
        assert daemon.stopped
    finally:
        daemon.shutdown()


def test_two_servers_starting_together_converge_on_one_daemon() -> None:
    """The loser of the bind race becomes a client rather than reporting failure."""
    port = _free_port()
    clients = [
        ChromeBridge(port=port, spawn=chrome_bridge.spawn_bridge_daemon, connect_timeout=45.0)
        for _ in range(2)
    ]
    try:
        starters = [threading.Thread(target=client.start) for client in clients]
        for starter in starters:
            starter.start()
        for starter in starters:
            starter.join(timeout=60.0)

        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline and not all(
            client.status(0.0)["daemon"]["linked"] for client in clients
        ):
            time.sleep(0.2)

        states = [client.status(0.0) for client in clients]
        assert all(state["daemon"]["linked"] for state in states), states
        assert states[0]["daemon"]["pid"] == states[1]["daemon"]["pid"], states
        assert all(state["startup_error"] is None for state in states), states
    finally:
        # The second client has to let go first: a client still watching the link
        # would answer the stop by starting another daemon.
        for client in clients[1:]:
            client.shutdown()
        clients[0].stop_daemon("the test is over")
        clients[0].shutdown()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1.0)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                break
        time.sleep(0.2)
    else:
        raise AssertionError("the spawned daemon is still holding the port")


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


def test_driver_opens_new_tabs_in_ai_group_without_taking_the_screen() -> None:
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI")
    driver.get("https://example.test/")
    assert driver.tab_id == 41
    assert driver.actual_tab_group == "AI"
    assert bridge.calls[0] == (
        "tabs.create",
        {"url": "about:blank", "group": "AI", "active": False},
    )
    navigations = [params for method, params in bridge.calls if method == "tabs.navigate"]
    assert navigations and navigations[0]["url"] == "https://example.test/"
    # The user's view is theirs: an agent navigating its own tab must not pull
    # the screen away from whatever they are reading in another one.
    assert all(params["active"] is False for params in navigations)
    assert all(method != "tabs.activate" for method, _ in bridge.calls)


def test_driver_claims_an_existing_tab_without_raising_it() -> None:
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_id=41, tab_group="AI")
    assert driver.actual_tab_group == "Existing"
    assert [method for method, _ in bridge.calls] == ["tabs.get", "events.subscribe"]
    assert all(method != "tabs.create" for method, _ in bridge.calls)


def test_a_foreground_driver_still_opens_claims_and_navigates_in_front() -> None:
    """The old behaviour is one flag away for whoever is watching the agent work."""
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI", foreground=True)
    driver.get("https://example.test/")
    assert bridge.calls[0] == (
        "tabs.create",
        {"url": "about:blank", "group": "AI", "active": True},
    )
    assert any(
        method == "tabs.navigate" and params["active"] is True
        for method, params in bridge.calls
    )

    claimed = _FakeBridge()
    ChromeBridgeDriver(bridge=claimed, tab_id=41, tab_group="AI", foreground=True)
    assert [method for method, _ in claimed.calls[:2]] == ["tabs.get", "tabs.activate"]


def test_a_screenshot_waits_longer_than_a_script_and_says_why_when_it_cannot() -> None:
    """A capture of a window nothing is painting takes seconds, not milliseconds."""

    class _SlowScreenshots(_FakeBridge):
        def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
            params = params or {}
            if method == "cdp.send" and params["method"] == "Page.captureScreenshot":
                self.calls.append((params["method"], {"timeout": timeout}))
                raise TimeoutError("Chrome bridge command 'cdp.send' timed out")
            return super().request(method, params, timeout)

    bridge = _SlowScreenshots()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI")
    driver.set_script_timeout(15.0)
    with pytest.raises(TimeoutError) as failure:
        driver.get_screenshot_as_png()
    waited = dict(bridge.calls)["Page.captureScreenshot"]["timeout"]
    assert waited == chrome_bridge.SCREENSHOT_TIMEOUT > 15.0
    # The agent can act on this; "cdp.send timed out" only tells it to give up.
    assert "obscured" in str(failure.value) and "front" in str(failure.value)


def test_a_background_driver_can_still_be_told_to_show_the_tab() -> None:
    """Background mode is a default, not a cage: "show me" stays one call away."""
    bridge = _FakeBridge()
    driver = ChromeBridgeDriver(bridge=bridge, tab_group="AI")
    driver.activate_tab()
    assert ("tabs.activate", {"tabId": 41}) in bridge.calls


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
