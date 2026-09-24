"""Downloads and JavaScript dialogs for the sessions an agent drives.

Downloads. A temporary or isolated session used to save every file into the
owner's ``~/Downloads``, with nothing in the click's answer. Browsers the server
launches now download into their own folder under the download directory
(``WEB_SEARCH_NEO_DOWNLOAD_DIR``/sessions/<session>-<stamp>), and the
``downloads`` action lists what arrived there. When Chrome refuses to reroute
downloads the session says so (``download_routing: failed``) instead of
pretending. Session folders older than a week are removed when a new one is made.

Dialogs. ``alert``/``confirm``/``prompt`` were dismissed silently: the text was
nowhere, ``confirm`` was always false, and "Delete? -> OK" could not be tested.
A small page script now answers them by the session's policy (``dismiss`` by
default - what happened before - or ``accept``, with ``prompt_text``) and logs
every dialog; the ``dialogs`` action reads the log and sets the policy, and a
click reports the dialogs it caused. It is installed automatically only in
browsers the server owns; in the user's own Chrome only when ``dialogs`` is
called for that session - and when that tab is handed back (``close`` of an
attached tab, ``open`` leaving it) the page's own functions are put back.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from web_search_neo.fetch.safety import download_root

SESSION_FOLDER_MAX_AGE_SECONDS = 7 * 24 * 3600

_DIALOG_TEMPLATE = r"""
(() => {
  if (window.__wsnDialogs) return;
  const log = [];
  const state = {policy: __POLICY__, promptText: __PROMPT__};
  const original = {alert: window.alert, confirm: window.confirm, prompt: window.prompt};
  Object.defineProperty(window, '__wsnDialogs',
    {value: {log, state, original}, enumerable: false, configurable: true});
  const record = (type, message, answer) => {
    log.push({type, message: String(message === undefined ? '' : message).slice(0, 2000),
              answer, url: String(location.href), at: Date.now()});
    if (log.length > 200) log.shift();
    return answer;
  };
  window.alert = message => { record('alert', message, null); };
  window.confirm = message => record('confirm', message, state.policy === 'accept');
  window.prompt = (message, fallback) => record('prompt', message,
    state.policy === 'accept' ? (state.promptText || String(fallback === undefined ? '' : fallback)) : null);
})();
"""

_READ_DIALOGS = r"""
const box = window.__wsnDialogs;
if (!box) return {installed: false, dialogs: [], policy: null};
if (arguments[0]) { box.state.policy = arguments[0]; box.state.promptText = arguments[1] || ''; }
const dialogs = box.log.slice();
if (arguments[2]) box.log.length = 0;
return {installed: true, dialogs: dialogs, policy: box.state.policy};
"""

# Puts the page's own alert/confirm/prompt back, in this document and in every
# same-origin frame the answerer was installed in.
RESTORE_DIALOGS_SCRIPT = r"""
let restored = 0;
const restore = win => {
  try {
    const box = win.__wsnDialogs;
    if (box && box.original) {
      win.alert = box.original.alert; win.confirm = box.original.confirm; win.prompt = box.original.prompt;
      delete win.__wsnDialogs;
      restored += 1;
    }
    for (let i = 0; i < win.frames.length; i++) restore(win.frames[i]);
  } catch (error) { /* a cross-origin frame: not ours to touch */ }
};
restore(window);
return restored;
"""


def session_download_dir(session_id: str) -> Path:
    """A fresh folder for one owned browser's downloads (old ones are swept)."""
    parent = download_root() / "sessions"
    sweep_old_folders(parent)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = parent / f"{session_id}-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sweep_old_folders(parent: Path, max_age: float = SESSION_FOLDER_MAX_AGE_SECONDS) -> int:
    """Remove session download folders untouched for ``max_age``; never raises."""
    removed = 0
    try:
        entries = list(parent.iterdir()) if parent.is_dir() else []
    except OSError:
        return 0
    cutoff = time.time() - max_age
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def route_downloads(driver: Any, folder: Path) -> str | None:
    """Send this browser's downloads to ``folder``; the refusal text when Chrome refused."""
    refusals = []
    for method, params in (
        ("Browser.setDownloadBehavior",
         {"behavior": "allow", "downloadPath": str(folder), "eventsEnabled": True}),
        ("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(folder)}),
    ):
        try:
            driver.execute_cdp_cmd(method, params)
            return None
        except Exception as exc:
            refusals.append(f"{method}: {type(exc).__name__}: {str(exc)[:160]}")
    return "; ".join(refusals)


def list_downloads(folder: str | None, since: float | None = None) -> list[dict[str, Any]]:
    """Files in the session's download folder (``in_progress`` for partial ones).

    A file Chrome renames or removes while this reads (``.crdownload`` finishing)
    is skipped rather than failing the listing.
    """
    if not folder:
        return []
    root = Path(folder)
    try:
        entries = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        return []
    files = []
    for item in entries:
        try:
            if not item.is_file():
                continue
            stat = item.stat()
        except OSError:
            continue
        if since is not None and stat.st_mtime < since:
            continue
        partial = item.suffix.lower() in {".crdownload", ".tmp"}
        files.append({"file": item.name, "path": str(item), "bytes": stat.st_size,
                      "in_progress": partial, "modified": round(stat.st_mtime, 3)})
    return sorted(files, key=lambda entry: entry["modified"])


def dialog_script(policy: str = "dismiss", prompt_text: str = "") -> str:
    return (_DIALOG_TEMPLATE.replace("__POLICY__", json.dumps(policy))
            .replace("__PROMPT__", json.dumps(prompt_text or "")))


DIALOG_SCRIPT = dialog_script()


def _track(session: Any, old: str | None, new: str | None) -> None:
    """Keep the registration in ``injected_scripts``, so every teardown removes it."""
    scripts = session.injected_scripts
    if old and old in scripts:
        scripts.remove(old)
    if new and new not in scripts:
        scripts.append(new)


def install_dialogs(session: Any, policy: str = "dismiss", prompt_text: str = "") -> str | None:
    """Install the dialog answerer in this document and every later one.

    The previous registration is replaced, so a changed policy carries over to
    the next documents instead of stacking two answerers.
    """
    driver = session.driver
    previous = session.dialog_script_id
    if previous:
        try:
            driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", {"identifier": previous})
        except Exception:
            pass
    identifier = None
    try:
        answer = driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                        {"source": dialog_script(policy, prompt_text)})
        identifier = str((answer or {}).get("identifier") or "") or None
    except Exception:
        identifier = None
    try:
        driver.execute_script(dialog_script(policy, prompt_text))
    except Exception:
        pass
    _track(session, previous, identifier)
    session.dialog_script_id = identifier
    return identifier


def restore_dialogs(session: Any) -> None:
    """Hand the tab back with its own alert/confirm/prompt; never raises.

    The session keeps its chosen policy, so a new tab it moves to gets the
    answerer again on its next ``open``.
    """
    identifier = session.dialog_script_id
    if not identifier and not session.dialog_policy_set:
        return  # never installed here: a tab handed back is not touched at all
    if identifier:
        try:
            session.driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument",
                                           {"identifier": identifier})
        except Exception:
            pass
        _track(session, identifier, None)
    session.dialog_script_id = None
    try:
        session.driver.execute_script(RESTORE_DIALOGS_SCRIPT)
    except Exception:
        pass


def setup_owned(session: Any, session_id: str) -> None:
    """Per-tab defaults before a navigation: download folder and dialog answerer.

    Browsers the server launched get both automatically. The user's own Chrome
    gets the answerer only when the session asked for it with ``dialogs`` (and
    again on a new tab after leaving a borrowed one); its downloads are never
    rerouted.
    """
    owned = session.profile_mode != "current" and session.owns_browser
    if owned and not session.download_dir and not session.download_routing_error:
        folder = session_download_dir(session_id)
        refused = route_downloads(session.driver, folder)
        if refused:
            session.download_routing_error = refused
            shutil.rmtree(folder, ignore_errors=True)
        else:
            session.download_dir = str(folder)
    if not session.dialog_script_id and (owned or session.dialog_policy_set):
        install_dialogs(session, session.dialog_policy, session.dialog_prompt_text)


def click_evidence(session: Any, started_at: float) -> dict[str, Any]:
    """Dialogs a click raised and files it downloaded, when there were any.

    A diagnostic after the click happened: it never raises, since a failure here
    must not turn a click that went through into a failed action.
    """
    found: dict[str, Any] = {}
    try:
        if session.dialog_script_id is not None or session.dialog_policy_set:
            dialogs = [d for d in read_dialogs(session.driver).get("dialogs") or []
                       if float(d.get("at") or 0) >= started_at * 1000 - 50]
            if dialogs:
                found["dialogs"] = dialogs
        if session.download_dir:  # a download still in flight shows up in the downloads action
            files = list_downloads(session.download_dir, since=started_at - 0.5)
            if files:
                found["downloads"] = files
    except Exception:
        return found
    return found


def dialogs_action(session: Any, policy: str | None, prompt_text: str | None,
                   clear: bool) -> dict[str, Any]:
    """Read the dialog log; set the policy for this and later documents."""
    if policy is not None:
        if policy not in {"accept", "dismiss"}:
            raise ValueError("policy must be 'accept' or 'dismiss'")
        session.dialog_policy, session.dialog_prompt_text = policy, prompt_text or ""
        session.dialog_policy_set = True
        install_dialogs(session, policy, prompt_text or "")
    elif session.dialog_script_id is None and not session.dialog_policy_set:
        session.dialog_policy_set = True
        install_dialogs(session)
    answer = read_dialogs(session.driver, policy, prompt_text, clear)
    return {"success": True, "policy": session.dialog_policy, "prompt_text": session.dialog_prompt_text,
            "dialogs": answer.get("dialogs") or [], "installed": bool(answer.get("installed"))}


def downloads_action(session: Any, wait_seconds: float = 0.0) -> dict[str, Any]:
    """List the session's downloads, waiting up to ``wait_seconds`` for partial ones."""
    if not session.download_dir:
        if session.download_routing_error:
            return {"success": True, "files": [], "download_dir": None, "download_routing": "failed",
                    "note": ("Chrome refused to send this session's downloads to its own folder "
                             f"({session.download_routing_error}); files go to the browser's "
                             "default download folder and are not listed here.")}
        return {"success": True, "files": [], "download_dir": None, "note": (
            "This session drives a browser the server did not launch (current Chrome or "
            "attach): its downloads go wherever that browser saves them.")}
    deadline = time.monotonic() + max(0.0, min(float(wait_seconds), 120.0))
    files = list_downloads(session.download_dir)
    while any(f["in_progress"] for f in files) and time.monotonic() < deadline:
        time.sleep(0.25)
        files = list_downloads(session.download_dir)
    return {"success": True, "download_dir": session.download_dir, "files": files,
            "in_progress": sum(1 for f in files if f["in_progress"])}


def read_dialogs(driver: Any, policy: str | None = None, prompt_text: str | None = None,
                 clear: bool = False) -> dict[str, Any]:
    """The dialogs logged in the current document; optionally set the policy."""
    if policy is not None and policy not in {"accept", "dismiss"}:
        raise ValueError("policy must be 'accept' or 'dismiss'")
    try:
        answer = driver.execute_script(_READ_DIALOGS, policy, prompt_text or "", bool(clear))
    except Exception:
        return {"installed": False, "dialogs": [], "policy": None}
    return answer if isinstance(answer, dict) else {"installed": False, "dialogs": [], "policy": None}
