"""Plain words for what one action is doing, for the person watching the tab.

The caption is the line the presence overlay shows beside the ghost cursor or
in a corner of the page: "Click · Submit", "Typing…", "Scrolling ↓",
"Waiting for #results". Short, present tense, and never a secret - typed text,
filled values, storage contents and the path or query of a URL never reach the
screen. What does: a verb, a key name, a host, a selector the caller wrote, or
the visible name of the element under the pointer (which the page itself
reads, only when the second value returned here asks it to).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

MAX_DETAIL = 40

_POINTER_VERBS = {
    "click": "Click",
    "double_click": "Double-click",
    "hover": "Hover",
    "move": "Move",
    "drag": "Drag",
    "press": "Press",
    "release": "Release",
}
_BUTTON_CLICKS = {"right": "Right-click", "middle": "Middle-click"}
# The pointer verbs whose target is worth naming: the page says what is under
# the point, so "Click" becomes "Click · Sign in".
_NAMED_POINTER = frozenset({"click", "double_click", "hover", "press", "drag"})
_TOUCH_VERBS = {
    "tap": "Tap",
    "press": "Touch",
    "move": "Touch move",
    "release": "Lift finger",
    "swipe": "Swipe",
    "cancel": "Cancel touch",
}
_FIXED = {
    "type_text": "Typing…",
    "submit": "Submitting form",
    "reload": "Reloading",
    "screenshot": "Looking at the page",
    "look": "Reading the page",
    "run_script": "Running a script",
    "inject_script": "Running a script",
    "wait_challenge": "Waiting for the challenge",
    "captcha": "Checking the captcha",
    "wait_frames": "Waiting for frames",
    "release_inputs": "Releasing keys",
    "dialogs": "Handling a dialog",
    "downloads": "Checking downloads",
    "cookies": "Reading cookies",
    "local_storage": "Reading storage",
    "show": "Showing the tab",
    # Reading topics, for a caller that marks reads too (see dispatch.mark_page_read).
    "page_text": "Reading the page",
    "page_outline": "Reading the page",
    "element_text": "Reading an element",
    "page_elements": "Scanning controls",
    "screen_state": "Checking the screen",
    "game_probe": "Watching the game",
}
# Session bookkeeping a person has nothing to read into: no caption at all.
_SILENT = frozenset({
    "close", "close_all", "close_tabs", "attach_tab", "reattach", "setup_current_chrome",
    "step", "render", "tabs", "context", "set_extra_headers", "stealth", "unthrottle",
    "touch_emulation", "update_companion", "mock",
})
# Keys whose names say nothing about what was typed (compared without "_").
_MODIFIERS = frozenset({"SHIFT", "CONTROL", "CTRL", "ALT", "META", "CMD", "WIN", "COMMAND"})
_NAMED_KEYS = _MODIFIERS | frozenset({
    "ENTER", "RETURN", "TAB", "ESC", "ESCAPE", "BACKSPACE", "DELETE", "INSERT", "HOME", "END",
    "PAGEUP", "PAGEDOWN", "SPACE", "UP", "DOWN", "LEFT", "RIGHT", "ARROWUP", "ARROWDOWN",
    "ARROWLEFT", "ARROWRIGHT",
})


def short(value: object, limit: int = MAX_DETAIL) -> str:
    """One line, at most ``limit`` characters, an ellipsis where it was cut."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _scroll(delta_x: object, delta_y: object) -> str:
    dx, dy = _number(delta_x), _number(delta_y)
    if abs(dy) >= abs(dx):
        return "Scrolling ↓" if dy >= 0 else "Scrolling ↑"
    return "Scrolling →" if dx > 0 else "Scrolling ←"


def _key_name(key: str) -> str | None:
    """A key's name if it is a named key, or None for a character that was typed."""
    compact = key.upper().replace("_", "")
    if compact in _NAMED_KEYS or (compact[:1] == "F" and compact[1:].isdigit()):
        return key.replace("_", " ").title() if key.isupper() else key
    return None


def _keys(keys: object) -> str:
    items = keys if isinstance(keys, (list, tuple)) else [keys]
    parts = [part.strip() for item in items for part in str(item or "").split("+") if part.strip()]
    # A shortcut - a modifier plus a character - is a command, not typing, so
    # its letter may be shown; a lone character never is.
    chord = len(parts) > 1 and any(part.upper() in _MODIFIERS for part in parts)
    names = [_key_name(part) or (part.upper() if chord and len(part) == 1 else None) for part in parts]
    if not names or None in names:
        return "Pressing keys"
    return "Keys · " + short("+".join(name for name in names if name), 24)


def _wait(arguments: dict[str, Any]) -> str:
    selector = arguments.get("selector")
    if isinstance(selector, str) and selector.strip():
        state = str(arguments.get("state") or "").strip().lower()
        suffix = " to go away" if state in {"hidden", "detached", "gone", "absent"} else ""
        return f"Waiting for {short(selector, 32)}{suffix}"
    if arguments.get("script"):
        return "Waiting for the page"
    seconds = _number(arguments.get("seconds"))
    return f"Waiting {seconds:g}s" if seconds > 0 else "Waiting"


def _count(total: int, one: str, many: str) -> str:
    if total == 1:
        return one
    return many.format(total) if total else many.replace("{} ", "")


def _size(value: object) -> int:
    return len(value) if isinstance(value, (list, dict, tuple)) else 0


def caption_for(action: str, arguments: dict[str, Any] | None = None) -> tuple[str | None, bool]:
    """The caption for one action, and whether the page should name its target."""
    name = str(action or "").strip().lower()
    source = arguments or {}
    if name in _SILENT or not name:
        return None, False
    if name in {"click", "click_text"}:
        text = source.get("text")
        if isinstance(text, str) and text.strip():
            return f"Click · {short(text, 32)}", False
        return "Click", True
    if name == "pointer":
        verb = str(source.get("pointer_action") or source.get("action") or "").strip().lower()
        if verb == "wheel":
            return _scroll(source.get("delta_x"), source.get("delta_y")), False
        label = _POINTER_VERBS.get(verb, "Pointer")
        if verb == "click":
            label = _BUTTON_CLICKS.get(str(source.get("button") or "").lower(), label)
        return label, verb in _NAMED_POINTER
    if name == "scroll":
        return _scroll(source.get("delta_x"), source.get("delta_y")), False
    if name == "press_keys":
        return _keys(source.get("keys")), False
    if name == "input":
        pointers = source.get("pointer_actions") or []
        keys = source.get("key_actions") or []
        if pointers and keys:
            return "Keys + pointer", False
        if pointers:
            last = pointers[-1] if isinstance(pointers[-1], dict) else {}
            return _POINTER_VERBS.get(str(last.get("action") or "").lower(), "Pointer"), False
        return ("Keys" if keys else "Input"), False
    if name == "touch":
        return _TOUCH_VERBS.get(str(source.get("touch_action") or "").lower(), "Touch"), False
    if name == "fill":
        total = _size(source.get("fields")) + _size(source.get("files"))
        return _count(total, "Filling a field", "Filling {} fields"), False
    if name == "upload":
        return _count(_size(source.get("file_paths")), "Uploading a file", "Uploading {} files"), False
    if name == "wait":
        return _wait(source), False
    if name in {"open", "navigate", "open_many"}:
        host = urlsplit(str(source.get("url") or "")).hostname or ""
        return (f"Opening {short(host, 32)}" if host else "Opening a page"), False
    if name == "pointer_lock":
        operation = str(source.get("operation") or source.get("action") or "").lower()
        return {"acquire": "Locking the pointer", "release": "Unlocking the pointer"}.get(operation), False
    if name in _FIXED:
        return _FIXED[name], False
    return short(name.replace("_", " ").capitalize(), 24), False
