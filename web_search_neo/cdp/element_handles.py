"""Elements, uploads and DOM results for the companion bridge, which has no element handles.

The bridge talks CDP through the Chrome companion and can only send page
scripts (Runtime.evaluate) and a few DevTools commands. So:

* an element is carried as something the page can find it by again - a CSS
  selector, or for a ref handle / ``a >>> b`` path the JS expression that
  resolves it;
* a script result that is a DOM node comes back as ``{}``. Returning a node by
  value yields ``{}`` on an ordinary page, but a React page keeps its fiber tree
  on every node as an own property and CDP gives up on it with "Object reference
  chain is too long" - which failed type_text on ``document.activeElement``
  before a single character was typed;
* an upload holds its file input in a page-side map for the whole attach. Finding
  it again by selector for every step lost it whenever the page re-rendered it in
  between, and the next step died on Chrome's bare "Cannot read properties of null
  (reading 'type')". Only Runtime.evaluate and DOM.setFileInputFiles are used,
  both already on the companion's allowlist.

``chrome_bridge`` is imported inside the functions: it calls into this module.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import mimetypes
from pathlib import Path
import re
from typing import Any
import uuid

# Script-result filter for ChromeBridgeDriver._wrap_script. A node is recognised by
# shape, not by ``instanceof Node``: a same-origin frame's nodes belong to its own Node.
# Nodes are replaced wherever they sit - top level, in arrays, in plain objects
# ({el: node}) - to a bounded depth; containers are copied only when one holds a node,
# and class instances are left alone.
PLAIN_RESULT_JS = (
    "const __wsnIsNode=(x)=>x!==null&&typeof x==='object'"
    "&&typeof x.nodeType==='number'&&typeof x.nodeName==='string';"
    "const __wsnPlain=(v)=>{"
    "if(v&&typeof v.then==='function')return Promise.resolve(v).then(__wsnPlain);"
    "const seen=new Set();"
    "const walk=(x,d)=>{if(__wsnIsNode(x))return {};"
    "if(x===null||typeof x!=='object'||d>20||seen.has(x))return x;seen.add(x);"
    "if(Array.isArray(x)){let c=null;for(let i=0;i<x.length;i++){const y=walk(x[i],d+1);"
    "if(y!==x[i]){c=c||x.slice();c[i]=y;}}return c||x;}"
    "const p=Object.getPrototypeOf(x);if(p!==Object.prototype&&p!==null)return x;"
    "let c=null;for(const k of Object.keys(x)){const y=walk(x[k],d+1);"
    "if(y!==x[k]){c=c||Object.assign({},x);c[k]=y;}}return c||x;};"
    "return walk(v,0);};"
)
RAW_RESULT_JS = "const __wsnPlain=(v)=>v;"

# The largest file set streamed into a page when Chrome refuses to attach files
# itself. It crosses the bridge as base64 in CDP messages, so it is kept to what a
# form upload plausibly needs.
MAX_INJECTED_UPLOAD_BYTES = 64 * 1024 * 1024
INJECTED_UPLOAD_CHUNK_CHARS = 512 * 1024

# The shape of page_perception.REF_PATTERN, which this package may not import.
_REF = re.compile(r"^ref:(?:([0-9a-fA-F]{8,64}):)?(\d+)$")
_POINTER_HINT = (
    "A pointer action aimed at the node's page-level 'center' from "
    "web_info(topic='page_outline') reaches frame content that element handles cannot."
)

_HOLD_SCRIPT = (
    "const el=arguments[0], key=arguments[1];"
    "if(!el||el.nodeType!==1)return false;"
    "(window.__wsnHeld||(window.__wsnHeld=new Map())).set(key,el);return true;"
)
_HELD = "const el=(window.__wsnHeld&&window.__wsnHeld.get(arguments[0]))||null;"
_RELEASE_SCRIPT = "if(window.__wsnHeld)window.__wsnHeld.delete(arguments[0]);return true;"
_STATE_SCRIPT = (
    _HELD + "if(!el||!el.isConnected)return {gone:true};"
    "return {type:String(el.type||'').toLowerCase(),multiple:!!el.multiple,"
    "names:Array.from(el.files||[]).map(f=>f.name)};"
)
_CLEAR_SCRIPT = _HELD + "if(el)el.value='';return true;"
# The chunks live on the input itself, so two uploads in flight never share a buffer.
_APPEND_CHUNK_SCRIPT = (
    _HELD + "const b=el.__wsnUpload||(el.__wsnUpload={});"
    "b[arguments[1]]=(b[arguments[1]]||'')+arguments[2];return b[arguments[1]].length;"
)
_SET_INJECTED_SCRIPT = (
    _HELD + "const b=el.__wsnUpload||{};const dt=new DataTransfer();"
    "for(const f of arguments[1]){const s=atob(b[f.key]||'');const u=new Uint8Array(s.length);"
    "for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
    "dt.items.add(new File([u],f.name,{type:f.type,lastModified:f.lastModified}));}"
    "delete el.__wsnUpload;el.files=dt.files;"
    "el.dispatchEvent(new Event('input',{bubbles:true}));"
    "el.dispatchEvent(new Event('change',{bubbles:true}));"
    "return Array.from(el.files).map(f=>f.name);"
)


def element_from_expression(driver: Any, expression: str, label: str) -> Any | None:
    """An element found by a JS expression (a ref handle, a piercing path), or None.

    It is found again through the expression on every later call, so it stays
    valid for as long as the page keeps the node the expression names.
    """
    from web_search_neo.chrome_bridge import ChromeBridgeElement

    found = driver.execute_script(f"const el=({expression}); return !!(el && el.nodeType === 1);")
    return ChromeBridgeElement(driver, label, expression=expression) if found else None


def resolve_locator(driver: Any, locator: str, expression: str, gone: type[Exception]) -> Any:
    """A ref handle or ``a >>> b`` path in current-Chrome mode, in the session's document.

    That is the top page, or the frame a ``frame_selector`` entered - where
    page_outline minted the handle. ``gone`` is raised for a handle from another
    document, so a wait fails at once instead of polling for a page that is gone.
    """
    element = element_from_expression(driver, expression, locator)
    if element is not None:
        return element
    match = _REF.match(locator)
    if match is None:
        raise ValueError(f"Path '{locator}' matches nothing in the page right now.")
    epoch = match.group(1)
    current = driver.execute_script(
        "const r = window.__wsnRefs; return r ? String(r.epoch).toLowerCase() : null;"
    )
    if epoch and current != epoch.lower():
        raise gone(
            f"Element handle '{locator}' was not read from the document this session is "
            "in: the page was replaced since, or the handle belongs to a frame (pass that "
            "frame as frame_selector). Read the page again with "
            f"web_info(topic='page_outline'). {_POINTER_HINT}"
        )
    raise ValueError(
        f"Element handle '{locator}' names an element that has been removed from the "
        "page, so it is stale. Read the page again with web_info(topic='page_outline')."
    )


def attach_files(driver: Any, element: Any, paths: list[str]) -> list[str]:
    """Leave a file input holding exactly ``paths``; return the names it reports."""
    with _held_element(driver, element) as key:
        state = driver.execute_script(_STATE_SCRIPT, key) or {}
        if state.get("gone"):
            from web_search_neo.chrome_bridge import NoSuchElementException

            raise NoSuchElementException(
                f"The file input '{element.selector}' left the page during the upload: the "
                "page removed or re-rendered it. Read the page again and retry the upload."
            )
        if state.get("type") != "file":
            raise ValueError("Selector does not point to an input[type=file]")
        if len(paths) > 1 and not state.get("multiple"):
            raise ValueError("Input does not accept multiple files")
        if state.get("names"):
            # Chrome appends to an input that accepts several files.
            driver.execute_script(_CLEAR_SCRIPT, key)
        _put_files(driver, key, paths)
        # A widget that swaps in a fresh input once it has taken the files is
        # normal: an input that has gone now holds nothing, and the caller's
        # evidence check decides whether the page accepted the files.
        after = driver.execute_script(_STATE_SCRIPT, key) or {}
        return [str(name) for name in (after.get("names") or [])]


def set_file_input_files(driver: Any, target: Any, paths: list[str]) -> None:
    """Attach ``paths`` to the input ``target`` (a selector or an element) without checks."""
    with _held_element(driver, target) as key:
        _put_files(driver, key, paths)


@contextmanager
def _held_element(driver: Any, target: Any):
    from web_search_neo.chrome_bridge import ChromeBridgeElement, NoSuchElementException

    element = target if isinstance(target, ChromeBridgeElement) else ChromeBridgeElement(driver, target)
    key = uuid.uuid4().hex
    if not driver.execute_script(_HOLD_SCRIPT, element, key):
        raise NoSuchElementException(f"No file input matches '{element.selector}'")
    try:
        yield key
    finally:
        try:
            driver.execute_script(_RELEASE_SCRIPT, key)
        except Exception:
            pass


# A refusal that names a policy is honoured. Chrome's usual refusal is a bare "Not
# allowed", which it gives both when the companion's "Allow access to file URLs" is
# off and (apparently) under an administrator's rule - the two cannot be told apart,
# so that case is streamed and the answer says so (attach_method: streamed).
_POLICY_WORDS = re.compile(r"polic|administrator|enterprise|group policy", re.I)

STREAMED_NOTE = (
    "Chrome refused to attach the file by path, so the server read it and handed its "
    "bytes to the page (a DataTransfer, like a drop). The input and change events are "
    "synthetic (isTrusted=false). Chrome gives the same bare 'Not allowed' when the "
    "companion's file access is off and when an administrator forbids it - if uploads "
    "should not happen here, stop."
)


def upload_refusal(message: str) -> str:
    """``policy`` when the refusal names one (never worked around), else ``streamable``."""
    return "policy" if _POLICY_WORDS.search(message) else "streamable"


def _put_files(driver: Any, key: str, paths: list[str]) -> None:
    """Hand ``paths`` to the held input: Chrome reads them itself, or the page is given them.

    DOM.setFileInputFiles is what a user's pick does - Chrome reads the files and
    fires input and change (``attach_method: set_file_input_files``). When Chrome
    refuses (usually a bare "Not allowed") the files are streamed into the page and
    set through a DataTransfer, the same File objects a drop produces, with
    synthetic events (``attach_method: streamed`` and ``stream_reason``). A refusal
    that names a policy is not worked around. The outcome is left on the driver as
    ``_wsn_last_attach`` for the upload answer.
    """
    from web_search_neo.chrome_bridge import (
        ChromeBridgeError,
        ChromeBridgeUnavailable,
        NoSuchElementException,
    )

    driver._wsn_last_attach = {}
    # Runtime.evaluate goes to the selected child target for a cross-origin frame,
    # while same-origin frames stay in the tab target and are reached through the
    # frame prefix of _wrap_script; the held node is looked up in that same
    # context, as the remote object DOM.setFileInputFiles takes.
    remote = driver._evaluate(
        driver._wrap_script(_HELD + "return el;", (key,), False, plain=False),
        return_by_value=False,
    )
    object_id = remote.get("objectId")
    if not object_id:
        raise NoSuchElementException("The file input left the page before files could be attached.")
    try:
        driver.execute_cdp_cmd("DOM.setFileInputFiles", {"objectId": object_id, "files": paths})
        driver._wsn_last_attach = {"attach_method": "set_file_input_files"}
        return
    except ChromeBridgeUnavailable:
        raise
    except ChromeBridgeError as exc:
        refused = exc
    if upload_refusal(str(refused)) == "policy":
        raise ChromeBridgeError(
            f"Chrome refused to attach the files: {refused}. That names a browser or "
            "administrator policy, which the server does not bypass; attach them by hand."
        ) from refused
    files = [Path(path) for path in paths]
    total = sum(path.stat().st_size for path in files)
    if total > MAX_INJECTED_UPLOAD_BYTES:
        raise ChromeBridgeError(
            f"Chrome refused to attach the files ({refused}) and {total} bytes is more than "
            f"the {MAX_INJECTED_UPLOAD_BYTES} that may be streamed into the page instead."
        ) from refused
    described = []
    for index, path in enumerate(files):
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        for start in range(0, len(encoded), INJECTED_UPLOAD_CHUNK_CHARS) or [0]:
            driver.execute_script(
                _APPEND_CHUNK_SCRIPT, key, index, encoded[start:start + INJECTED_UPLOAD_CHUNK_CHARS]
            )
        described.append({
            "key": index,
            "name": path.name,
            "type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "lastModified": int(path.stat().st_mtime * 1000),
        })
    driver.execute_script(_SET_INJECTED_SCRIPT, key, described)
    driver._wsn_last_attach = {"attach_method": "streamed", "stream_reason": str(refused)[:300],
                               "stream_note": STREAMED_NOTE}
