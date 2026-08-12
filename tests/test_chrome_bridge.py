from __future__ import annotations

import json
import socket
import threading

from websockets.sync.client import connect

import browser_tools
from chrome_bridge import CHROME_EXTENSION_ID, ChromeBridge, ChromeBridgeDriver


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


def test_auto_prefers_current_chrome_and_headless_forces_temporary(monkeypatch) -> None:
    class ConnectedBridge:
        @staticmethod
        def wait_connected(timeout: float) -> bool:
            return True

    monkeypatch.setattr(browser_tools, "get_chrome_bridge", lambda: ConnectedBridge())
    assert browser_tools._resolve_profile_mode("auto", None) == "current"
    assert browser_tools._resolve_profile_mode("auto", True) == "temporary"
