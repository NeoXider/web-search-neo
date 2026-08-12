"""Loopback bridge and WebDriver-compatible adapter for the companion Chrome extension."""

from __future__ import annotations

import atexit
import base64
from dataclasses import dataclass
import json
import os
import re
import threading
import time
from typing import Any
import uuid

from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException


class ChromeBridgeError(RuntimeError):
    """Raised when the companion extension isn't connected or rejects a command."""


CHROME_EXTENSION_ID = "ndbmcjhbdjpefojkoljacjhammmcigao"


@dataclass
class _PendingRequest:
    event: threading.Event
    connection: Any = None
    result: Any = None
    error: str | None = None


class ChromeBridge:
    """Small request/response server used by the Web Search Neo Chrome extension."""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        self.port = int(port or os.getenv("WEB_SEARCH_NEO_BRIDGE_PORT", "8765"))
        self._connection: Any = None
        self._connection_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        self._started = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._startup_error: str | None = None
        self._browser_info: dict[str, Any] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._connection_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._serve,
                name="web-search-neo-chrome-bridge",
                daemon=True,
            )
            self._thread.start()
        self._started.wait(timeout=2.0)

    def _serve(self) -> None:
        try:
            from websockets.sync.server import serve

            extension_origin = re.compile(
                rf"^chrome-extension://{re.escape(CHROME_EXTENSION_ID)}/?$"
            )
            with serve(
                self._handle_connection,
                self.host,
                self.port,
                origins=[extension_origin],
                ping_interval=20,
                ping_timeout=20,
                max_size=64 * 1024 * 1024,
            ) as server:
                self._server = server
                self._started.set()
                server.serve_forever()
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            self._started.set()
            self._fail_pending("Chrome bridge stopped")

    def _handle_connection(self, websocket: Any) -> None:
        try:
            first = json.loads(websocket.recv(timeout=5.0))
            if first.get("type") != "hello" or first.get("protocol") != 1:
                websocket.close(code=1008, reason="Expected Web Search Neo protocol hello")
                return
            with self._connection_lock:
                previous = self._connection
                if previous is not None and previous is not websocket:
                    websocket.close(
                        code=1008,
                        reason="Another Chrome companion is already connected",
                    )
                    return
                self._connection = websocket
                self._browser_info = dict(first.get("browser") or {})
                self._connected.set()
            websocket.send(json.dumps({"type": "hello_ack", "protocol": 1}))
            for raw_message in websocket:
                message = json.loads(raw_message)
                message_type = message.get("type")
                if message_type == "result":
                    request_id = str(message.get("id", ""))
                    with self._connection_lock:
                        pending = self._pending.get(request_id)
                    if pending:
                        pending.result = message.get("result")
                        pending.error = message.get("error")
                        pending.event.set()
                elif message_type == "ping":
                    websocket.send(json.dumps({"type": "pong", "at": time.time()}))
        except Exception:
            pass
        finally:
            with self._connection_lock:
                if self._connection is websocket:
                    self._connection = None
                    self._browser_info = {}
                    self._connected.clear()
            self._fail_pending("Chrome companion extension disconnected", websocket)

    def _fail_pending(self, message: str, connection: Any | None = None) -> None:
        with self._connection_lock:
            pending_items = [
                item
                for item in self._pending.values()
                if connection is None or item.connection is connection
            ]
        for pending in pending_items:
            pending.error = pending.error or message
            pending.event.set()

    def wait_connected(self, timeout: float = 0.0) -> bool:
        self.start()
        if self._startup_error:
            return False
        return self._connected.wait(max(0.0, timeout))

    @property
    def connected(self) -> bool:
        return self.wait_connected(0.0)

    @property
    def startup_error(self) -> str | None:
        self.start()
        return self._startup_error

    @property
    def browser_info(self) -> dict[str, Any]:
        with self._connection_lock:
            return dict(self._browser_info)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Any:
        if not self.wait_connected(min(max(timeout, 0.0), 3.0)):
            detail = f" ({self._startup_error})" if self._startup_error else ""
            raise ChromeBridgeError(
                "Chrome companion extension is not connected. Load chrome-extension/ "
                f"in chrome://extensions and keep Chrome open{detail}."
            )
        request_id = uuid.uuid4().hex
        with self._connection_lock:
            connection = self._connection
            pending = _PendingRequest(event=threading.Event(), connection=connection)
            self._pending[request_id] = pending
        if connection is None:
            with self._connection_lock:
                self._pending.pop(request_id, None)
            raise ChromeBridgeError("Chrome companion extension disconnected")
        try:
            payload = json.dumps(
                {
                    "type": "command",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                },
                ensure_ascii=False,
            )
            with self._send_lock:
                connection.send(payload)
            if not pending.event.wait(max(0.1, timeout)):
                raise TimeoutError(f"Chrome bridge command '{method}' timed out")
            if pending.error:
                raise ChromeBridgeError(pending.error)
            return pending.result
        finally:
            with self._connection_lock:
                self._pending.pop(request_id, None)

    def status(self, wait_seconds: float = 0.0) -> dict[str, Any]:
        connected = self.wait_connected(wait_seconds)
        return {
            "connected": connected,
            "host": self.host,
            "port": self.port,
            "startup_error": self._startup_error,
            "browser": self.browser_info,
        }

    def shutdown(self) -> None:
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass


_bridge = ChromeBridge()


def get_chrome_bridge() -> ChromeBridge:
    return _bridge


class _BridgeService:
    def stop(self) -> None:
        return None


class ChromeBridgeElement:
    """Small Selenium WebElement subset backed by a stable CSS selector."""

    def __init__(self, driver: "ChromeBridgeDriver", selector: str) -> None:
        self.parent = driver
        self.selector = selector

    @property
    def tag_name(self) -> str:
        return str(
            self.parent.execute_script(
                "return arguments[0].tagName.toLowerCase();", self
            )
        )

    def get_attribute(self, name: str) -> Any:
        return self.parent.execute_script(
            "const el=arguments[0], name=arguments[1];"
            "if (name === 'value') return el.value;"
            "if (name === 'checked') return el.checked;"
            "return el.getAttribute(name);",
            self,
            name,
        )

    def is_selected(self) -> bool:
        return bool(self.parent.execute_script("return !!arguments[0].checked;", self))

    def is_displayed(self) -> bool:
        return bool(
            self.parent.execute_script(
                "const el=arguments[0], r=el.getBoundingClientRect(), s=getComputedStyle(el);"
                "return !!(r.width && r.height && s.display !== 'none' && "
                "s.visibility !== 'hidden' && s.opacity !== '0');",
                self,
            )
        )

    def is_enabled(self) -> bool:
        return not bool(
            self.parent.execute_script(
                "return !!(arguments[0].disabled || arguments[0].getAttribute('aria-disabled') === 'true');",
                self,
            )
        )

    def click(self) -> None:
        point = self.parent.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'});"
            "const r=arguments[0].getBoundingClientRect();"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};",
            self,
        )
        for event_type, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)):
            self.parent.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": point["x"],
                    "y": point["y"],
                    "button": "left" if event_type != "mouseMoved" else "none",
                    "buttons": buttons,
                    "clickCount": 1,
                },
            )

    def clear(self) -> None:
        self.parent.execute_script(
            "const el=arguments[0]; el.focus();"
            "if (el.isContentEditable) el.textContent=''; else el.value='';"
            "el.dispatchEvent(new Event('input',{bubbles:true}));",
            self,
        )

    def send_keys(self, value: str) -> None:
        text = str(value)
        input_type = str(self.get_attribute("type") or "").lower()
        if input_type == "file":
            self.parent.set_file_input_files(self.selector, text.splitlines())
            return
        self.parent.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'}); arguments[0].focus();",
            self,
        )
        self.parent.execute_cdp_cmd("Input.insertText", {"text": text})


class _BridgeSwitchTo:
    def __init__(self, driver: "ChromeBridgeDriver") -> None:
        self.driver = driver

    def default_content(self) -> None:
        self.driver._frame_session_id = None
        self.driver._same_origin_frame_selector = None

    def frame(self, frame: ChromeBridgeElement) -> None:
        resolved = self.driver.bridge.request(
            "frames.resolve",
            {"tabId": self.driver.tab_id, "selector": frame.selector},
            timeout=10.0,
        )
        self.driver._frame_session_id = resolved.get("sessionId")
        self.driver._same_origin_frame_selector = (
            frame.selector if resolved.get("sameOrigin") else None
        )


class ChromeBridgeDriver:
    """WebDriver-shaped adapter that controls an already-open Chrome via extension."""

    is_extension_bridge = True

    def __init__(
        self,
        bridge: ChromeBridge | None = None,
        tab_id: int | None = None,
        tab_group: str = "AI",
    ) -> None:
        self.bridge = bridge or get_chrome_bridge()
        self.tab_group = tab_group.strip() or "AI"
        self._script_timeout = 15.0
        self._page_load_timeout = 30.0
        self._frame_session_id: str | None = None
        self._same_origin_frame_selector: str | None = None
        self.service = _BridgeService()
        self._modifier_mask = 0
        if tab_id is None:
            tab = self.bridge.request(
                "tabs.create", {"url": "about:blank", "group": self.tab_group}, timeout=10.0
            )
        else:
            tab = self.bridge.request("tabs.get", {"tabId": int(tab_id)}, timeout=10.0)
            tab = self.bridge.request("tabs.activate", {"tabId": int(tab_id)}, timeout=10.0)
        self.tab_id = int(tab["id"])
        self.actual_tab_group = tab.get("group")

    @property
    def switch_to(self) -> _BridgeSwitchTo:
        return _BridgeSwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(self.bridge.request("tabs.get", {"tabId": self.tab_id})["url"])

    @property
    def title(self) -> str:
        return str(self.bridge.request("tabs.get", {"tabId": self.tab_id}).get("title") or "")

    def set_page_load_timeout(self, seconds: float) -> None:
        self._page_load_timeout = max(1.0, float(seconds))

    def set_script_timeout(self, seconds: float) -> None:
        self._script_timeout = max(1.0, float(seconds))

    def get(self, url: str) -> None:
        self.switch_to.default_content()
        self.bridge.request(
            "tabs.navigate",
            {"tabId": self.tab_id, "url": url},
            timeout=self._page_load_timeout,
        )

    def _argument_expression(self, value: Any) -> str:
        if isinstance(value, ChromeBridgeElement):
            return f"document.querySelector({json.dumps(value.selector)})"
        return json.dumps(value, ensure_ascii=False)

    def _wrap_script(self, script: str, args: tuple[Any, ...], asynchronous: bool) -> str:
        arguments = ",".join(self._argument_expression(arg) for arg in args)
        frame_prefix = ""
        frame_suffix = ""
        if self._same_origin_frame_selector:
            selector = json.dumps(self._same_origin_frame_selector)
            frame_prefix = (
                f"const __frame=document.querySelector({selector});"
                "if(!__frame||!__frame.contentWindow) throw new Error('Frame is unavailable');"
                "const window=__frame.contentWindow, document=window.document, "
                "performance=window.performance, requestAnimationFrame=window.requestAnimationFrame.bind(window), "
                "cancelAnimationFrame=window.cancelAnimationFrame.bind(window);"
            )
        if asynchronous:
            return (
                "new Promise((__done,__reject)=>{try{"
                + frame_prefix
                + f"const arguments=[{arguments},__done];"
                + f"(function(){{{script}}}).apply(window,arguments);"
                + frame_suffix
                + "}catch(__error){__reject(__error);}})"
            )
        return (
            "(()=>{"
            + frame_prefix
            + f"const arguments=[{arguments}];"
            + f"return (function(){{{script}}}).apply(window,arguments);"
            + frame_suffix
            + "})()"
        )

    def _evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = True,
        return_by_value: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        response = self.bridge.request(
            "cdp.send",
            {
                "tabId": self.tab_id,
                "sessionId": self._frame_session_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "awaitPromise": await_promise,
                    "returnByValue": return_by_value,
                    "userGesture": True,
                },
            },
            timeout=timeout or self._script_timeout,
        )
        if response.get("exceptionDetails"):
            detail = response["exceptionDetails"].get("text") or "JavaScript evaluation failed"
            raise WebDriverException(detail)
        return response.get("result") or {}

    def execute_script(self, script: str, *args: Any) -> Any:
        result = self._evaluate(self._wrap_script(script, args, False))
        return result.get("value")

    def execute_async_script(self, script: str, *args: Any) -> Any:
        try:
            result = self._evaluate(
                self._wrap_script(script, args, True),
                timeout=self._script_timeout + 1.0,
            )
        except TimeoutError as exc:
            raise TimeoutException(str(exc)) from exc
        return result.get("value")

    def execute_cdp_cmd(self, command: str, params: dict[str, Any]) -> Any:
        return self.bridge.request(
            "cdp.send",
            {
                "tabId": self.tab_id,
                "sessionId": self._frame_session_id,
                "method": command,
                "params": params,
            },
            timeout=self._script_timeout,
        )

    def find_element(self, by: str, value: str) -> ChromeBridgeElement:
        if by not in {"css selector", "css"}:
            raise ValueError("Current Chrome bridge currently supports CSS selectors")
        exists = self.execute_script("return !!document.querySelector(arguments[0]);", value)
        if not exists:
            raise NoSuchElementException(f"No element matches selector: {value}")
        return ChromeBridgeElement(self, value)

    def select_option(self, selector: str, value: str) -> None:
        selected = self.execute_script(
            "const el=document.querySelector(arguments[0]), wanted=String(arguments[1]);"
            "if(!el) return false; const option=Array.from(el.options).find(o=>o.value===wanted)"
            "||Array.from(el.options).find(o=>o.text===wanted); if(!option)return false;"
            "el.value=option.value; el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true})); return true;",
            selector,
            value,
        )
        if not selected:
            raise ValueError(f"No select option matches '{value}'")

    def set_file_input_files(self, selector: str, paths: list[str]) -> None:
        expression = f"document.querySelector({json.dumps(selector)})"
        remote_object = self._evaluate(expression, return_by_value=False)
        object_id = remote_object.get("objectId")
        if not object_id:
            raise NoSuchElementException(f"No file input matches selector: {selector}")
        self.execute_cdp_cmd(
            "DOM.setFileInputFiles", {"objectId": object_id, "files": paths}
        )

    def perform_key_events(self, events: list[dict[str, Any]]) -> None:
        aliases = {
            "\ue007": ("Enter", "Enter", 13),
            "\ue006": ("Enter", "Enter", 13),
            "\ue00c": ("Escape", "Escape", 27),
            "\ue004": ("Tab", "Tab", 9),
            "\ue003": ("Backspace", "Backspace", 8),
            "\ue017": ("Delete", "Delete", 46),
            "\ue011": ("Home", "Home", 36),
            "\ue010": ("End", "End", 35),
            "\ue00e": ("PageUp", "PageUp", 33),
            "\ue00f": ("PageDown", "PageDown", 34),
            "\ue013": ("ArrowUp", "ArrowUp", 38),
            "\ue015": ("ArrowDown", "ArrowDown", 40),
            "\ue012": ("ArrowLeft", "ArrowLeft", 37),
            "\ue014": ("ArrowRight", "ArrowRight", 39),
            "\ue008": ("Shift", "ShiftLeft", 16),
            "\ue009": ("Control", "ControlLeft", 17),
            "\ue00a": ("Alt", "AltLeft", 18),
            "\ue00d": (" ", "Space", 32),
        }
        modifier_bits = {"Shift": 8, "Control": 2, "Alt": 1}
        modifiers = self._modifier_mask
        for event in events:
            if event["type"] == "pause":
                time.sleep(max(0.0, float(event.get("seconds", 0.0))))
                continue
            raw = str(event["key"])
            key, code, key_code = aliases.get(
                raw,
                (raw, f"Key{raw.upper()}" if len(raw) == 1 and raw.isalpha() else raw, ord(raw.upper()) if len(raw) == 1 else 0),
            )
            event_type = "keyDown" if event["type"] == "down" else "keyUp"
            bit = modifier_bits.get(key, 0)
            if event_type == "keyDown":
                modifiers |= bit
            params = {
                "type": event_type,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": key_code,
                "nativeVirtualKeyCode": key_code,
                "modifiers": modifiers,
            }
            if event_type == "keyDown" and len(key) == 1 and not (modifiers & 3):
                params["text"] = key
            self.execute_cdp_cmd("Input.dispatchKeyEvent", params)
            if event_type == "keyUp":
                modifiers &= ~bit
        self._modifier_mask = modifiers

    def get_screenshot_as_png(self) -> bytes:
        capture = self.execute_cdp_cmd("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(capture["data"])

    def get_log(self, log_type: str) -> list[dict[str, Any]]:
        if log_type != "browser":
            return []
        return list(
            self.bridge.request("console.get", {"tabId": self.tab_id}, timeout=5.0) or []
        )

    def quit(self) -> None:
        try:
            self.bridge.request("debugger.detach", {"tabId": self.tab_id}, timeout=5.0)
        except Exception:
            pass


def list_current_chrome_tabs(wait_seconds: float = 1.0) -> dict[str, Any]:
    bridge = get_chrome_bridge()
    status = bridge.status(wait_seconds)
    if not status["connected"]:
        return {**status, "tabs": []}
    tabs = bridge.request("tabs.list", timeout=10.0)
    return {**status, "tabs": tabs}


atexit.register(_bridge.shutdown)
