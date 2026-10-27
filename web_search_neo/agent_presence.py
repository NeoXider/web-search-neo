"""Show a human, in the browser itself, that an agent is driving this tab.

Three signals, all aimed at the person watching rather than at the caller:

* a badge painted onto the tab's favicon - pulsing while an agent is working
  in it, green for a minute after, amber for the rest of five minutes
  (``presence_badge``) - because the tab strip is where someone looks first,
  and the ``[session] `` title prefix in ``browser_tools`` only says *whose*
  tab it is, never *whether anything is happening in it right now*;
* a short flash over the element the last action touched, so a burst of thirty
  clicks reads as thirty visible taps instead of a page that mutates on its own;
* a ghost cursor that glides after the agent's virtual pointer - synthetic CDP
  input moves no OS mouse - with a caption saying what the agent is doing, a
  ring on every press and a trail while a button is held (``presence_overlay``).

This module holds the script and payload builders. Session plumbing lives in
``presence_live`` so payloads can be tested independently of a browser.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from web_search_neo import presence_badge, presence_captions, presence_overlay
from web_search_neo.presence_overlay import CAPTION_MS, MAX_RIPPLES, RIPPLE_MS  # noqa: F401

# Bumping this makes an already-installed script in a live page stand down and
# reinstall itself. Without it a long-lived tab would keep running whatever
# version was current when it first loaded, and a fix here would only reach
# pages opened afterwards.
PRESENCE_VERSION = 16

PRESENCE_ENV = "WEB_SEARCH_NEO_AGENT_PRESENCE"

# How long a single action keeps the badge green, its first few seconds
# pulsing (presence_badge.BUSY_MS). A minute, because that is how long a person
# watching the tab strip needs to
# still see "the model was here" after the agent paused to think: a few seconds
# made a busy tab flip to amber between two actions, which read as "nobody is
# driving this". Matches the companion's toolbar badge (agent-activity.js).
ACTIVE_MS = 60_000
# Yellow from one to six minutes after the last action, then red until an hour
# (presence_badge.STALE_MS) - the owner's states, 2026-10-24.
RECENT_MS = 6 * 60 * 1000
# The flash is deliberately at the short end of what the eye still registers:
# with dozens of actions a second, anything longer turns into a smear of
# overlapping boxes instead of a readable sequence.
FLASH_MS = 250
# Enough concurrent flashes to see a burst, few enough that a runaway loop
# cannot fill the DOM with overlays.
MAX_FLASHES = 6

_ACTIVE_COLOR = "#2fbf5c"
_RECENT_COLOR = "#f2c230"

# The corner mark's geometry on the 32x32 canvas: 16-unit slime drawn at full
# size (half the tab icon) in the bottom-right corner, with no transparency.
# A see-through 12 px mark was invisible at tab-strip size - which defeats the
# whole point of painting it there - and "the agent is on this tab" is not a
# claim you should have to squint for. The offset keeps the 2 px halo stroke
# inside the canvas instead of clipped at the edge.
SLIME_ORIGIN = (14, 15)
SLIME_SCALE = 1.0
SLIME_ALPHA = 1.0

# Coordinate presses and selector clicks both have a human-visible indicator.
_TAP_POINTER_ACTIONS = frozenset({"click", "double_click", "press"})

# The actions that are not worth a flash: they either have no place on the page
# or fire so often that the overlay would never be off. The favicon badge still
# gets its ping for every one of them - "the agent is in this tab" is true even
# when the step was a read.
QUIET_ACTIONS = frozenset(
    {
        "close",
        "close_all",
        "close_tabs",
        "cookies",
        "local_storage",
        "set_extra_headers",
        "stealth",
        "attach_tab",
        "setup_current_chrome",
        "release_inputs",
        "wait",
        "step",
        "render",
    }
)

# The argument names that name a target, in the order a flash should prefer
# them. `selector` is exact; `text` is what click_text was aiming at and is
# resolved the same way the click did; coordinates are the last resort.
_SELECTOR_KEYS = ("selector", "frame_selector")
_TEXT_KEYS = ("text",)


def enabled() -> bool:
    """Whether the presence signals may be shown at all.

    ``WEB_SEARCH_NEO_AGENT_PRESENCE=0`` (or false/no/off) turns every signal off
    everywhere, for the user who wants their tab strip left alone.
    """
    return str(os.environ.get(PRESENCE_ENV, "")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _config() -> dict[str, Any]:
    return {
        "version": PRESENCE_VERSION,
        "activeMs": ACTIVE_MS,
        "recentMs": RECENT_MS,
        "flashMs": FLASH_MS,
        "maxFlashes": MAX_FLASHES,
        "activeColor": _ACTIVE_COLOR,
        "recentColor": _RECENT_COLOR,
        "slimeOrigin": list(SLIME_ORIGIN),
        "slimeScale": SLIME_SCALE,
        "slimeAlpha": SLIME_ALPHA,
        **presence_badge.config(),
        **presence_overlay.config(),
    }


def _sub_action_name(value: object) -> str:
    return str(value or "").strip().lower()


def _cursor_hint(action: str, source: dict[str, Any]) -> tuple[bool, bool]:
    """Whether the action moved the virtual pointer, and pressed a button."""
    name = _sub_action_name(source.get("pointer_action") or source.get("operation") or source.get("action"))
    if action == "pointer":
        return True, name in _TAP_POINTER_ACTIONS
    if action == "click_text" or (action == "click" and (source.get("trusted") or source.get("text") is not None or source.get("x") is not None)):
        return True, True
    if action == "pointer_lock":
        # Only acquiring clicks; the rest never visits the pointer.
        owns = name == "acquire"
        return owns, owns
    if action == "input":
        items = source.get("pointer_actions")
        subs = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        return bool(subs), any(_sub_action_name(item.get("action")) in _TAP_POINTER_ACTIONS for item in subs)
    return False, False


def payload_for(
    action: str,
    arguments: dict[str, Any] | None = None,
    *,
    ok: bool = True,
    label: str | None = None,
    pointer: tuple[float, float] | None = None,
    held: bool = False,
) -> dict[str, Any]:
    """Build a page-side indicator; pointer coordinates use the main viewport.

    ``held`` says a mouse button is still down after the action (a drag in
    progress), which the cursor shows as a grip and a trail.
    """
    name = str(action or "").strip().lower()
    source = dict(arguments or {})
    payload: dict[str, Any] = {
        "action": name,
        "label": label or None,
        "ok": bool(ok),
        "flash": name not in QUIET_ACTIONS,
    }
    caption, named = presence_captions.caption_for(name, source)
    if caption:
        payload["caption"] = caption
        if named:
            payload["captionTarget"] = True
    if held:
        payload["held"] = True
    if name in {"click", "click_text"} and not source.get("frame_selector"):
        payload["cursorTarget"] = True
        payload["cursorTap"] = True
    for key in _SELECTOR_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            payload["selector"] = value.strip()
            break
    # Only a click's text names a target; type_text's is what was typed.
    for key in _TEXT_KEYS if name in {"click", "click_text"} else ():
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            payload["text"] = value.strip()[:80]
            break
    x, y = source.get("x"), source.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        payload["x"] = float(x)
        payload["y"] = float(y)
    if pointer is not None:
        moves, tap = _cursor_hint(name, source)
        px, py = pointer
        finite = isinstance(px, (int, float)) and isinstance(py, (int, float)) and math.isfinite(px) and math.isfinite(py)
        if moves and finite:
            payload["cursor"] = {"x": float(px), "y": float(py), "tap": tap}
    return payload


def install_source() -> str:
    """The full installer, with this build's overlay and constants baked in."""
    source = _INSTALL_TEMPLATE.replace("__WSN_PRESENCE_BADGE__", presence_badge.SOURCE)
    source = source.replace("__WSN_PRESENCE_OVERLAY__", presence_overlay.SOURCE)
    return source.replace("__WSN_PRESENCE_CONFIG__", json.dumps(_config()))


def ping_script() -> str:
    """The cheap per-action call: ping an installed script, or admit it is absent.

    Returns ``false`` when nothing is installed in the current document, which
    is the caller's signal to run ``install_source`` and try once more. Doing it
    in that order keeps the common case at one small script instead of shipping
    the whole installer on every single action.
    """
    return (
        "const presence = window.__wsnPresence;"
        " if (!presence || presence.version !== "
        f"{PRESENCE_VERSION}) return false;"
        " return presence.note(arguments[0]);"
    )


RESTORE_SCRIPT = (
    "(() => { const presence = window.__wsnPresence;"
    " if (presence && typeof presence.restore === 'function') presence.restore();"
    " return true; })()"
)

# A flash lasts 250 ms and a screenshot inside that window would hand the agent
# a picture of a box its own click drew. Every capture hides the overlays first
# - flashes and rings are removed; the cursor, its trail and the caption are
# hidden and shown again right after - and the favicon badge is untouched: it
# is not in the picture.
HIDE_FLASHES_SCRIPT = (
    "(() => { const presence = window.__wsnPresence;"
    " if (presence && typeof presence.hideFlashes === 'function') presence.hideFlashes();"
    " return true; })()"
)

SHOW_EPHEMERAL_SCRIPT = (
    "(() => { const presence = window.__wsnPresence;"
    " if (presence && presence.showEphemeral) presence.showEphemeral(); return true; })()"
)


# The installer runs in the page's main world, both from
# Page.addScriptToEvaluateOnNewDocument (so it survives navigation and beats the
# page's own scripts) and from a direct execute_script for the document that is
# already open. It must therefore be idempotent and must never throw into a page
# that has nothing to do with us.
#
# Two rules shape most of what follows. Everything it adds to the DOM carries
# aria-hidden="true", which is the one marker every reading topic in this server
# already skips, so an overlay can never turn up in page_text or page_outline.
# And no <style> element is ever created - inline style properties are used
# instead, because a page with a strict style-src CSP would drop a stylesheet
# and leave the overlay unstyled and opaque over the content.
_INSTALL_TEMPLATE = r"""
(() => {
  const CONFIG = __WSN_PRESENCE_CONFIG__;
  const KEY = "__wsnPresence";
  const existing = window[KEY];
  if (existing && existing.version === CONFIG.version) return true;
  if (existing && typeof existing.restore === "function") {
    try { existing.restore(); } catch (error) {}
  }

  const state = {
    version: CONFIG.version,
    phase: null,
    timer: null,
    pulse: null,
    frame: 0, hop: -1,
    busyUntil: 0,
    activeUntil: 0,
    recentUntil: 0,
    ownLink: null,
    parked: [],  // the page's icon links taken over; not `hidden`, the overlay's flag of that name
    painted: {},
    waiting: [],
    flashes: [],
    cursor: null,
    ripples: [],
  };
  window[KEY] = state;

__WSN_PRESENCE_BADGE__

  // ------------------------------------------------------------------ flash

  function pierce(selector) {
    // The server's own `a >>> b` piercing path: querySelector cannot cross a
    // shadow boundary, so walk it the way the selector was written.
    const parts = String(selector).split(">>>");
    let scope = document;
    let node = null;
    for (let index = 0; index < parts.length; index += 1) {
      const part = parts[index].trim();
      if (!part || !scope || !scope.querySelector) return null;
      node = scope.querySelector(part);
      if (!node) return null;
      scope = node.shadowRoot || node;
    }
    return node;
  }

  function findByText(text) {
    const wanted = String(text).trim().toLowerCase();
    if (!wanted || !document.querySelectorAll) return null;
    const candidates = document.querySelectorAll(
      "a, button, input, select, textarea, [role], [onclick], label, summary"
    );
    for (let index = 0; index < candidates.length; index += 1) {
      const node = candidates[index];
      const own = (node.innerText || node.textContent || node.value || "").trim().toLowerCase();
      if (own && own.indexOf(wanted) !== -1 && own.length < wanted.length + 40) return node;
    }
    return null;
  }

  function targetRect(payload) {
    // Cursor pixels are main-frame viewport pixels; x/y arguments may be frame-local.
    const point = payload.cursor;
    if (point && isFinite(point.x) && isFinite(point.y)) {
      return {left: point.x - 14, top: point.y - 14, width: 28, height: 28};
    }
    let node = null;
    if (payload.selector) {
      try {
        node = payload.selector.indexOf(">>>") !== -1
          ? pierce(payload.selector)
          : document.querySelector(payload.selector);
      } catch (error) {
        node = null;
      }
    }
    if (!node && payload.text) {
      try { node = findByText(payload.text); } catch (error) { node = null; }
    }
    if (node && node.getBoundingClientRect) {
      const box = node.getBoundingClientRect();
      if (box.width > 0 && box.height > 0) {
        return {left: box.left, top: box.top, width: box.width, height: box.height};
      }
    }
    if (typeof payload.x === "number" && typeof payload.y === "number") {
      return {left: payload.x - 14, top: payload.y - 14, width: 28, height: 28};
    }
    return null;
  }

  function dropFlash(host) {
    if (host && host.parentNode) {
      try { host.parentNode.removeChild(host); } catch (error) {}
    }
  }

  function trimFlashes() {
    while (state.flashes.length > CONFIG.maxFlashes) {
      dropFlash(state.flashes.shift());
    }
  }

  function clearFlashes() {
    const open = state.flashes.slice();
    state.flashes = [];
    for (let index = 0; index < open.length; index += 1) dropFlash(open[index]);
  }

  function flash(payload) {
    const parent = document.body || document.documentElement;
    if (!parent || !document.createElement) return;
    const rect = targetRect(payload);
    // Nothing to point at - a scroll, a key press, a navigation: the caption
    // says what landed, and an empty box would add nothing to it.
    if (!rect) return;
    const color = payload.ok === false ? "#e2584d" : CONFIG.activeColor;

    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.setAttribute("data-wsn-presence", "flash");
    host.style.cssText =
      "all:initial;position:fixed;left:0;top:0;width:0;height:0;" +
      "z-index:2147483647;pointer-events:none;";
    let root = host;
    try {
      if (host.attachShadow) root = host.attachShadow({mode: "open"});
    } catch (error) {
      root = host;
    }

    // An outline only: what the action was is the caption's job, and a label
    // that lives 250 ms cannot be read anyway.
    const box = document.createElement("div");
    box.style.cssText =
      "position:fixed;box-sizing:border-box;pointer-events:none;" +
      "opacity:1;transition:opacity " + CONFIG.flashMs + "ms ease-out;" +
      "left:" + Math.round(rect.left - 2) + "px;" +
      "top:" + Math.round(rect.top - 2) + "px;" +
      "width:" + Math.round(rect.width + 4) + "px;" +
      "height:" + Math.round(rect.height + 4) + "px;" +
      "border:2px solid " + color + ";border-radius:4px;" +
      "box-shadow:0 0 0 2px rgba(0,0,0,0.25),0 0 12px " + color + ";";
    root.appendChild(box);
    parent.appendChild(host);
    state.flashes.push(host);
    trimFlashes();

    const remove = function () {
      const index = state.flashes.indexOf(host);
      if (index !== -1) state.flashes.splice(index, 1);
      dropFlash(host);
    };
    // Two frames, so the transition has a painted starting opacity to run from;
    // one frame is enough on a quiet page and not on a busy one.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { box.style.opacity = "0"; });
    });
    setTimeout(remove, CONFIG.flashMs + 120);
  }

__WSN_PRESENCE_OVERLAY__

  // ------------------------------------------------------------------ entry

  state.note = function (payload) {
    const data = payload && typeof payload === "object" ? payload : {};
    try { badgeNote(data); } catch (error) {}
    try { overlayNote(data); } catch (error) {}
    if (data.flash !== false) {
      try { flash(data); } catch (error) {}
    }
    return (state.needBase && state.needBase()) || true;
  };

  state.hideFlashes = function () {
    clearFlashes();
    overlayHide();
    return true;
  };

  state.showEphemeral = function () {
    overlayShow();
    return true;
  };

  state.restore = function () {
    if (state.timer) {
      try { clearTimeout(state.timer); } catch (error) {}
      state.timer = null;
    }
    state.busyUntil = 0;
    state.activeUntil = 0;
    state.recentUntil = 0;
    restoreIcon();
    clearFlashes();
    overlayRestore();
    try { delete window[KEY]; } catch (error) { window[KEY] = undefined; }
  };

  return true;
})();
"""
