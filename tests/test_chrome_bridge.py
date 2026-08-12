from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading

import pytest
from websockets.sync.client import connect

import browser_tools
from chrome_bridge import CHROME_EXTENSION_ID, ChromeBridge, ChromeBridgeDriver


NODE = shutil.which("node")
EVENTS_MODULE = (
    Path(__file__).resolve().parents[1] / "chrome-extension" / "events.js"
).as_uri()
requires_node = pytest.mark.skipif(
    NODE is None, reason="node is required to exercise the extension helpers"
)


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


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def test_bridge_round_trip_accepts_extension_protocol() -> None:
    bridge = ChromeBridge(port=_free_port())
    bridge.start()

    def extension_client() -> None:
        with connect(
            f"ws://127.0.0.1:{bridge.port}",
            origin=f"chrome-extension://{CHROME_EXTENSION_ID}",
        ) as websocket:
            websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": 1,
                        "browser": {"name": "Test Chrome"},
                    }
                )
            )
            assert json.loads(websocket.recv())["type"] == "hello_ack"
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
