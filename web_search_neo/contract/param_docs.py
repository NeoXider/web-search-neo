"""Parameter descriptions for action_schema, so a caller does not have to guess.

FastMCP builds each action's input schema from the wrapper's signature, which
gives names, types and defaults but no meaning; element shapes such as
``points`` or ``key_actions`` were opaque. ``annotate`` adds a description to
the properties named here. Only ``web_info(topic='action_schema')`` carries
them: the compact capabilities document lists required names and stays small.
"""
from __future__ import annotations

import copy
from typing import Any

_COMMON = {
    "session_id": "One session = one page. Reuse it; parallel agents use their own.",
    "frame_selector": "CSS of the iframe to act inside; coordinates are then the frame's own.",
    "wait_seconds": "Settle time after the action before the page summary is read (0 = none).",
    "include_summary": "false skips the post-action page read - faster in game loops.",
    "max_chars": "Answer budget in characters. Anything cut is flagged (truncated) with next_offset.",
    "offset": "Where this window starts: the next_offset of the previous answer.",
    "save_to": "File name under the download folder; the whole value is written there instead.",
    "sarif_to": "File name under the download folder for the findings as SARIF 2.1.0 (for CI).",
    "baseline": "A saved report file to compare against; the answer gains regression {fixed, added}.",
    "timeout_seconds": "Upper bound for the wait; the call fails (does not hang) past it.",
}

_BY_ACTION: dict[str, dict[str, str]] = {
    "press_keys": {
        "keys": ("1-8 key names pressed together as a chord (all down, then all up) - send "
                 "separate calls for a sequence. Names: a-z, 0-9, ENTER, TAB, SPACE, BACKSPACE, "
                 "DELETE, ESCAPE, ARROW_LEFT/LEFT, HOME, END, F1-F12, NUMPAD0-9, NUMPAD_ENTER, "
                 "CapsLock/NumLock/ScrollLock (sent through CDP; the session tracks the lock state); DOM "
                 "spellings too (ArrowLeft, KeyW, Digit1, ShiftLeft) and 'Control+Shift+K' chords."),
        "key_action": "tap (down and up), hold (stays down until release), release.",
    },
    "input": {
        "key_actions": "[{key, action: tap|hold|release}] - key names as in press_keys.",
        "pointer_actions": ("[{action: click|double_click|hover|move|drag|press|release|wheel, x, y, "
                            "end_x?, end_y?, button?, coordinate_mode?, delta_x?, delta_y?}] - viewport CSS px."),
    },
    "touch": {
        "points": ("[{x, y, id?, end_x?, end_y?}] - swipe reads end_x/end_y inside each point, not "
                   "at the top level; release may list only {id} to lift those fingers."),
        "touch_action": "tap | press | move | release | swipe | cancel.",
    },
    "pointer": {
        "x": "Viewport CSS px (coordinate_space='image': pixels of the last screenshot). Relative mode: the delta.",
        "y": "As x.",
        "coordinate_mode": "absolute (a point) | delta (move by x/y, stays in the window) | relative (unbounded delta, for pointer lock).",
        "coordinate_space": "viewport (CSS px) | image (pixels of this session's last screenshot; scale and scroll applied; not with frame_selector).",
        "delta_x": "Wheel only: horizontal scroll amount. Moves use x/y.",
        "delta_y": "Wheel only: vertical scroll amount (positive = down). Moves use x/y.",
    },
    "cookies": {
        "set_cookies": "[{name, value, domain?|url?, path?, secure?, httpOnly?, sameSite?, expires?}] for op='set'.",
        "op": "get (read with flags, paged) | set | clear (needs a domain unless confirm_clear_all).",
    },
    "fill": {"fields": "{css_selector: value} map; append [N] to a selector for its N-th match."},
    "run_script": {
        "script": ("Body of an async function: await works at top level, `return` returns; a one-line "
                   "expression returns itself. Arguments arrive as arguments[0..]."),
        "args": "JSON values passed as arguments[0..] - data never goes into the script text.",
    },
    "screenshot": {"wait_frames": "Render this many animation frames before capturing (fresh frame)."},
    "dialogs": {
        "policy": "accept (confirm -> true, prompt -> prompt_text) | dismiss (confirm -> false, prompt -> null).",
        "clear": "Empty the dialog log after reading it.",
    },
    "look": {
        "dx": "Horizontal turn in movement pixels (movementX total).",
        "dy": "Vertical turn in movement pixels (movementY total).",
        "steps": "Relative moves the turn is split into (1-60).",
        "duration_ms": "Time the whole turn takes.",
    },
    "wait": {"script": "Same semantics as run_script; the wait ends when it returns a truthy value."},
    "http_request": {
        "http_session": "Name of a cookie jar kept across calls, per (agent_label, name); omitted = no cookies carry over.",
        "agent_label": "Namespace of http_session: another label never sees this jar.",
        "http_session_clear": "Empty the named jar before this request is sent.",
        "show_values": "With http_session: show cookie values (in http_session, set_cookies, headers), redacted otherwise.",
        "timeout_seconds": "Capped at 120; bounds each read, the whole body gets twice that.",
        "save_to": "Path inside the download folder for the raw bytes; overwrite=true replaces a file.",
    },
    "open": {"profile_mode": ("isolated (default for a new session: a clean separate headless browser) | "
                              "current (the user's Chrome, explicit) | temporary | persistent | attach | auto. "
                              "Omitted: an existing session keeps its own; current_tab_id or persist implies current.")},
    "test_run": {
        "steps": ("[{action..., step_name?, expect?}] - ordinary actions; expect: {selector, absent, text, no_text, "
                  "url_contains, title_contains, script, no_console_errors, no_failed_requests, action_fails, "
                  "timeout_seconds}. An expect-only step just checks."),
        "url": "Opened first in session_id (isolated when the session is new, closed after unless keep_open).",
    },
    "security_report": {
        "url": "The start page (http or https); optional with scope='hosts'.",
        "scope": "page (url + paths), site (crawl of links and sitemap), hosts (each named origin).",
        "hosts": "Exact hosts in scope, <= 10: example.com, localhost:3000, http://127.0.0.1:8080. No wildcards.",
        "paths": "Your own routes to check too: /login, /api/health; <= 50, GET only.",
        "browser": "false: read the served HTML instead of rendering (no Chrome needed).",
    },
    "api_report": {
        "url": "The page to analyse: a cold load in a fresh isolated session (closed unless keep_open).",
        "hosts": "Your other API hosts, <= 10 (same rules as scope): their responses count as own too.",
        "har_to": "File name for the session journal as HAR 1.2 beside the JSON report (download folder).",
        "overwrite": "Replace an existing save_to/har_to file instead of refusing.",
    },
    "secret_scan": {
        "url": "The page to scan: a cold load in a fresh isolated session (closed unless keep_open).",
        "hosts": "Your other hosts, <= 10 (same rules as scope): their scripts count as own too.",
        "scope": "page (url + paths, browser optional), site (crawl of links and sitemap, served HTML only), hosts (each named origin, served HTML only).",
        "paths": "Your own routes to scan too: /login, /api/health; <= 50, GET only.",
        "max_pages": "Site crawl: at most this many pages (default 10, at most 50).",
    },
    "active_probe": {
        "scope": "page (url) or hosts (each named origin's front page); no site crawl - active probes multiply requests.",
    },
    "active_probe": {
        "url": "The page to probe: ordinary GETs plus OPTIONS/TRACE, all in scope and budget.",
        "checks": "cors, methods, redirects, canary (default: all).",
        "origin": "Origin header for the preflight (default https://probe.example).",
        "paths": "Your own routes to probe too: /login, /api/health; <= 50.",
    },
}


def annotate(action: str, schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``schema`` with descriptions on the properties this table knows."""
    notes = {**_COMMON, **_BY_ACTION.get(action, {})}
    annotated = copy.deepcopy(schema)
    for name, prop in (annotated.get("properties") or {}).items():
        if name in notes and isinstance(prop, dict) and not prop.get("description"):
            prop["description"] = notes[name]
    return annotated
