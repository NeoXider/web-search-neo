"""Current-Chrome (companion bridge) regressions: DOM nodes by value, uploads, ref handles.

Two field failures drove these:

* ``type_text`` on the focused control of a React page failed with CDP's
  "Object reference chain is too long" before typing a character. The lookup
  returned ``document.activeElement`` by value, and React keeps its fiber tree
  on every DOM node as an own property. The node tests below evaluate the real
  script wrapper in node against such a node and serialise the result the way
  CDP does, so they fail the way Chrome failed.
* ``upload`` died on "Cannot read properties of null (reading 'type')": every
  step looked the input up again by selector, and a page that re-rendered it in
  between left one step holding a null.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from selenium.common.exceptions import NoSuchElementException

from web_search_neo import browser_tools
from web_search_neo.chrome_bridge import (
    ChromeBridgeDriver,
    ChromeBridgeElement,
    ChromeBridgeError,
)

NODE = shutil.which("node")

# A React-shaped input: a node by shape (nodeType/nodeName) carrying a fiber-like
# chain far deeper than CDP's by-value serialiser will follow.
_REACT_PAGE = """
const deep = {}; let cur = deep;
for (let i = 0; i < 5000; i++) { cur.next = {i}; cur = cur.next; }
const input = {nodeType: 1, nodeName: 'INPUT', tagName: 'INPUT', value: ''};
input['__reactFiber$test'] = deep;
globalThis.window = globalThis;
globalThis.document = {activeElement: input, querySelector: () => input};
"""

# CDP's returnByValue gives up on deep object graphs; this is the same verdict.
_BY_VALUE = """
const walk = (v, d) => {
  if (d > 1000) throw new Error('Object reference chain is too long');
  if (v && typeof v === 'object') for (const k of Object.keys(v)) walk(v[k], d + 1);
};
(async () => {
  try {
    const value = await (__EXPRESSION__);
    walk(value, 0);
    process.stdout.write(JSON.stringify({ok: true, value: value === undefined ? null : value}));
  } catch (error) {
    process.stdout.write(JSON.stringify({ok: false, error: String(error.message)}));
  }
})();
"""


class _TabBridge:
    """The bridge calls a ChromeBridgeDriver makes while it opens its tab."""

    def __init__(self) -> None:
        self.cdp: list[dict] = []

    def request(self, method: str, params: dict | None = None, timeout: float = 20.0):
        params = params or {}
        if method == "tabs.create":
            return {"id": 7, "url": params.get("url", ""), "title": "", "group": params.get("group")}
        if method == "events.subscribe":
            return {"started_at": 0, "seq": 0, "domains": params.get("domains", [])}
        if method == "cdp.send":
            self.cdp.append(params)
            return self.cdp_send(params["method"], params.get("params") or {})
        raise AssertionError(f"unexpected bridge call {method}")

    def cdp_send(self, method: str, params: dict):
        raise AssertionError(f"unexpected CDP call {method}")


class _NodePageBridge(_TabBridge):
    """Runs Runtime.evaluate in node against the React-shaped page above."""

    def cdp_send(self, method: str, params: dict):
        assert method == "Runtime.evaluate"
        script = _REACT_PAGE + _BY_VALUE.replace("__EXPRESSION__", params["expression"])
        completed = subprocess.run(
            [NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
        outcome = json.loads(completed.stdout)
        if not outcome["ok"]:
            # What the companion relays when CDP refuses to serialise a value.
            raise ChromeBridgeError(
                'Error: {"code":-32000,"message":"' + outcome["error"] + '"}'
            )
        return {"result": {"type": "object", "value": outcome["value"]}}


needs_node = pytest.mark.skipif(NODE is None, reason="node evaluates the page-side scripts")


@needs_node
def test_a_react_node_returned_by_value_arrives_as_an_empty_object() -> None:
    driver = ChromeBridgeDriver(bridge=_NodePageBridge())

    assert driver.execute_script("return document.activeElement") == {}
    assert driver.execute_script("return [document.activeElement, 2]") == [{}, 2]
    assert driver.execute_script("return Promise.resolve(document.activeElement)") == {}
    nested = driver.execute_script("return {el: document.activeElement, n: 1, list: [{deep: document.activeElement}]}")
    assert nested == {"el": {}, "n": 1, "list": [{"deep": {}}]}


@needs_node
def test_the_unsanitised_lookup_fails_exactly_as_chrome_did() -> None:
    """Guards the test itself: without the plain-result wrapper the stub page fails."""
    driver = ChromeBridgeDriver(bridge=_NodePageBridge())
    expression = driver._wrap_script("return document.activeElement;", (), False, plain=False)

    with pytest.raises(ChromeBridgeError, match="Object reference chain is too long"):
        driver._evaluate(expression)


@needs_node
def test_async_scripts_hand_back_plain_values_too() -> None:
    driver = ChromeBridgeDriver(bridge=_NodePageBridge())

    assert driver.execute_async_script("arguments[arguments.length - 1](document.activeElement)") == {}


class _FileInputPage(_TabBridge):
    """A scripted page with one file input, answering the upload's page scripts."""

    def __init__(self, *, present: bool = True, multiple: bool = False,
                 refuse_attach: bool = False, swaps_input: bool = False,
                 refusal: str = "Error: DOM.setFileInputFiles is not allowed") -> None:
        super().__init__()
        self.refusal = refusal
        self.present = present
        self.multiple = multiple
        self.refuse_attach = refuse_attach
        self.swaps_input = swaps_input
        self.names: list[str] = []
        self.chunks: dict[int, str] = {}
        self.held = False

    def cdp_send(self, method: str, params: dict):
        if method == "DOM.setFileInputFiles":
            assert params["objectId"] == "held-input"
            if self.refuse_attach:
                raise ChromeBridgeError(self.refusal)
            self._attach([Path(path).name for path in params["files"]])
            return {}
        assert method == "Runtime.evaluate"
        expression = params["expression"]
        if not params.get("returnByValue", True):
            return {"result": {"type": "object", "objectId": "held-input"}}
        if "window.__wsnHeld=new Map()" in expression:
            self.held = self.present
            return self._value(self.present)
        if "__wsnHeld.delete" in expression:
            self.held = False
            return self._value(True)
        if "return {type:String(el.type" in expression:
            if self.swaps_input and self.names:
                return self._value({"gone": True})
            return self._value({"type": "file", "multiple": self.multiple, "names": self.names})
        if "el.value=''" in expression:
            self.names = []
            return self._value(True)
        if "el.__wsnUpload||(el.__wsnUpload={})" in expression:
            args = json.loads("[" + expression.split("const arguments=[", 1)[1].split("];", 1)[0] + "]")
            self.chunks[args[1]] = self.chunks.get(args[1], "") + args[2]
            return self._value(len(self.chunks[args[1]]))
        if "new DataTransfer()" in expression:
            args = json.loads("[" + expression.split("const arguments=[", 1)[1].split("];", 1)[0] + "]")
            self._attach([item["name"] for item in args[1]])
            return self._value(self.names)
        raise AssertionError(f"unexpected page script: {expression[:200]}")

    def _attach(self, names: list[str]) -> None:
        self.names = names

    @staticmethod
    def _value(value):
        return {"result": {"type": "object", "value": value}}

    def selector_lookups(self, selector: str) -> int:
        needle = f"document.querySelector({json.dumps(selector)})"
        return sum(needle in call["params"].get("expression", "") for call in self.cdp)


def test_upload_finds_the_input_once_and_holds_it_for_every_step(tmp_path) -> None:
    picture = tmp_path / "red.png"
    picture.write_bytes(b"\x89PNG")
    page = _FileInputPage()
    driver = ChromeBridgeDriver(bridge=page)

    names = driver.attach_files(ChromeBridgeElement(driver, "input[data-x=f1]"), [str(picture)])

    assert names == ["red.png"]
    assert page.selector_lookups("input[data-x=f1]") == 1
    assert not page.held, "the page-side hold must be released"


def test_upload_streams_the_files_in_when_chrome_refuses_to_attach_them(tmp_path) -> None:
    picture = tmp_path / "blue.png"
    picture.write_bytes(bytes(range(256)) * 5)
    page = _FileInputPage(refuse_attach=True)
    driver = ChromeBridgeDriver(bridge=page)

    names = driver.attach_files(ChromeBridgeElement(driver, "#f1"), [str(picture)])

    assert names == ["blue.png"]
    assert base64.b64decode(page.chunks[0]) == picture.read_bytes()


def test_an_input_missing_when_the_upload_starts_is_named_plainly(tmp_path) -> None:
    picture = tmp_path / "red.png"
    picture.write_bytes(b"x")
    driver = ChromeBridgeDriver(bridge=_FileInputPage(present=False))

    with pytest.raises(NoSuchElementException, match="No file input matches '#gone'"):
        driver.attach_files(ChromeBridgeElement(driver, "#gone"), [str(picture)])


def test_a_widget_that_swaps_the_input_after_taking_the_file_is_not_an_error(tmp_path) -> None:
    """A dropzone replaces its input once it has the file; that input now holds nothing."""
    picture = tmp_path / "red.png"
    picture.write_bytes(b"x")
    driver = ChromeBridgeDriver(bridge=_FileInputPage(swaps_input=True))

    assert driver.attach_files(ChromeBridgeElement(driver, "#drop"), [str(picture)]) == []


def test_upload_keeps_refusing_what_the_input_cannot_take(tmp_path) -> None:
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    driver = ChromeBridgeDriver(bridge=_FileInputPage(multiple=False))

    with pytest.raises(ValueError, match="does not accept multiple files"):
        driver.attach_files(ChromeBridgeElement(driver, "#one"), [str(first), str(second)])


def test_the_upload_action_goes_through_the_bridge_attach() -> None:
    class _Driver:
        def __init__(self):
            self.calls = []

        def attach_files(self, element, paths):
            self.calls.append((element, paths))
            return ["red.png"]

    driver = _Driver()
    assert browser_tools._attach_files(driver, "element", ["C:/red.png"]) == ["red.png"]
    assert driver.calls == [("element", ["C:/red.png"])]


class _RefPage(_TabBridge):
    def __init__(self, *, resolves: bool, epoch: str) -> None:
        super().__init__()
        self.resolves = resolves
        self.epoch = epoch

    def cdp_send(self, method: str, params: dict):
        expression = params["expression"]
        if "el.nodeType === 1" in expression:
            return {"result": {"type": "boolean", "value": self.resolves}}
        if "window.__wsnRefs; return r ?" in expression:
            return {"result": {"type": "string", "value": self.epoch}}
        raise AssertionError(expression[:200])


def test_a_ref_handle_resolves_in_current_chrome_mode() -> None:
    driver = ChromeBridgeDriver(bridge=_RefPage(resolves=True, epoch="648cbd24c17e2d1a"))

    element = browser_tools._resolve_element(driver, "ref:648cbd24c17e2d1a:3")

    assert isinstance(element, ChromeBridgeElement)
    assert "window.__wsnRefs" in driver._argument_expression(element)


def test_a_ref_from_another_document_is_gone_not_retried() -> None:
    driver = ChromeBridgeDriver(bridge=_RefPage(resolves=False, epoch="648cbd24c17e2d1a"))

    with pytest.raises(browser_tools.LocatorGone, match="was not read from the document"):
        browser_tools._resolve_element(driver, "ref:deadbeefdeadbeef:3")


def test_a_bare_not_allowed_is_streamed_and_says_so(tmp_path) -> None:
    """Chrome's real refusal text is "Not allowed" - for file access switched off and,
    apparently, for an administrator's rule alike. It cannot be told apart, so the file
    is streamed in and the answer names the method and the reason."""
    picture = tmp_path / "red.png"
    picture.write_bytes(b"x")
    page = _FileInputPage(refuse_attach=True, refusal="Not allowed")
    driver = ChromeBridgeDriver(bridge=page)
    assert driver.attach_files(ChromeBridgeElement(driver, "#f1"), [str(picture)]) == ["red.png"]
    assert driver._wsn_last_attach["attach_method"] == "streamed"
    assert driver._wsn_last_attach["stream_reason"].endswith("Not allowed")
    assert "isTrusted=false" in driver._wsn_last_attach["stream_note"]

    ok = _FileInputPage()
    fine = ChromeBridgeDriver(bridge=ok)
    fine.attach_files(ChromeBridgeElement(fine, "#f1"), [str(picture)])
    assert fine._wsn_last_attach == {"attach_method": "set_file_input_files"}


def test_a_refusal_that_names_a_policy_is_an_error(tmp_path) -> None:
    from web_search_neo.cdp.element_handles import upload_refusal

    picture = tmp_path / "red.png"
    picture.write_bytes(b"x")
    page = _FileInputPage(refuse_attach=True,
                          refusal="File selection is blocked by your administrator's policy")
    driver = ChromeBridgeDriver(bridge=page)
    with pytest.raises(ChromeBridgeError, match="policy"):
        driver.attach_files(ChromeBridgeElement(driver, "#f1"), [str(picture)])
    assert not page.chunks and not page.names  # nothing was streamed in
    assert not page.held, "the page-side hold must be released"
    assert upload_refusal("Not allowed") == "streamable"
