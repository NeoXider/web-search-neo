"""Show a human, in the browser itself, that an agent is driving this tab.

Three signals, all aimed at the person watching rather than at the caller:

* a badge painted onto the tab's favicon while an agent is working in it, and
  for a few minutes after it stopped - the tab strip is where someone looks
  first, and the ``[session] `` title prefix in ``browser_tools`` only says
  *whose* tab it is, never *whether anything is happening in it right now*;
* a short flash over the element the last action touched, so a burst of thirty
  clicks reads as thirty visible taps instead of a page that mutates on its own;
* a ghost cursor that follows the agent's virtual pointer - synthetic CDP input
  moves no OS mouse - with a name tag and a fading ring on every press.

Everything here is pure data: the JavaScript source and the small builders that
bake values into it. The session plumbing - when to install the script, which
sessions are eligible, how to reach the driver - lives in ``browser_tools``,
which imports this module. Keeping the direction of that import one-way is what
lets the payloads be unit-tested without a browser.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

# Bumping this makes an already-installed script in a live page stand down and
# reinstall itself. Without it a long-lived tab would keep running whatever
# version was current when it first loaded, and a fix here would only reach
# pages opened afterwards.
PRESENCE_VERSION = 5

PRESENCE_ENV = "WEB_SEARCH_NEO_AGENT_PRESENCE"

# How long a single action keeps the badge in its bright "working now" state.
# Agents pause between steps to think, so a few seconds made a busy tab flicker
# to "recent"; this matches the companion's toolbar badge.
ACTIVE_MS = 30_000
# The afterglow the user asked for: the tab stays marked for five minutes after
# the agent's last action, then gives the favicon back.
RECENT_MS = 5 * 60 * 1000
# The flash is deliberately at the short end of what the eye still registers:
# with dozens of actions a second, anything longer turns into a smear of
# overlapping boxes instead of a readable sequence.
FLASH_MS = 250
# Enough concurrent flashes to see a burst, few enough that a runaway loop
# cannot fill the DOM with overlays.
MAX_FLASHES = 6
# Glide, ring life and name-tag life for the ghost cursor. A ring outlives the
# flash so a press is seen after the fact, but dies before a burst smears.
CURSOR_GLIDE_MS = 90
# A press ring lives long enough to be seen after the fact and dies fast
# enough that a burst stays a sequence rather than a smear.
RIPPLE_MS = 700
MAX_RIPPLES = 8
CURSOR_CHIP_MS = 1500

_ACTIVE_COLOR = "#2fbf5c"
_RECENT_COLOR = "#e2a03f"

# Actions after which the session's virtual pointer is meaningful. DOM clicks
# move no pointer - nor would the OS cursor - so the cursor stays put there.
_CURSOR_ACTIONS = frozenset({"pointer", "input", "pointer_lock"})
# The sub-actions that press a button down: those land a ripple, the rest only
# glide the cursor.
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

    ``WEB_SEARCH_NEO_AGENT_PRESENCE=0`` (or false/no/off) turns both off
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
        "glideMs": CURSOR_GLIDE_MS,
        "rippleMs": RIPPLE_MS,
        "maxRipples": MAX_RIPPLES,
        "chipMs": CURSOR_CHIP_MS,
    }


def _sub_action_name(value: object) -> str:
    return str(value or "").strip().lower()


def _cursor_hint(action: str, source: dict[str, Any]) -> tuple[bool, bool]:
    """Whether the action moved the virtual pointer, and pressed a button."""
    name = _sub_action_name(source.get("action"))
    if action == "pointer":
        return True, name in _TAP_POINTER_ACTIONS
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
) -> dict[str, Any]:
    """Describe one finished action in the terms the page-side script needs.

    Only a hint is passed, never a resolved element: the action has already
    happened by the time this runs, and re-resolving the selector in the page
    is both cheaper than shipping a rect back and correct in the common case
    where the click moved something.

    ``pointer`` is the session's virtual pointer after the action ran. It lands
    in the payload as ``cursor`` only for actions that actually moved it.
    """
    name = str(action or "").strip().lower()
    source = dict(arguments or {})
    payload: dict[str, Any] = {
        "action": name,
        "label": label or None,
        "ok": bool(ok),
        "flash": name not in QUIET_ACTIONS,
    }
    for key in _SELECTOR_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            payload["selector"] = value.strip()
            break
    for key in _TEXT_KEYS:
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
    """The full installer, with this build's constants baked in."""
    return _INSTALL_TEMPLATE.replace("__WSN_PRESENCE_CONFIG__", json.dumps(_config()))


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
        " presence.note(arguments[0]); return true;"
    )


RESTORE_SCRIPT = (
    "(() => { const presence = window.__wsnPresence;"
    " if (presence && typeof presence.restore === 'function') presence.restore();"
    " return true; })()"
)

# A flash lasts 250 ms and a screenshot inside that window would hand the agent
# a picture of a box its own click drew. Every capture hides the overlays first
# - flashes and rings are removed, the cursor is shown again right after - and
# the favicon badge is untouched: it is not in the picture.
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
    activeUntil: 0,
    recentUntil: 0,
    ownLink: null,
    hidden: [],
    baseHref: null,
    painted: {},
    loading: {},
    flashes: [],
    cursor: null,
    ripples: [],
  };
  window[KEY] = state;

  // ---------------------------------------------------------------- favicon

  function iconLinks() {
    if (!document.querySelectorAll) return [];
    const links = [];
    const all = document.querySelectorAll("link");
    for (let index = 0; index < all.length; index += 1) {
      const link = all[index];
      if (link === state.ownLink) continue;
      const rel = (link.getAttribute("rel") || "").toLowerCase();
      if (/(^|\s)(icon|shortcut icon|apple-touch-icon)(\s|$)/.test(rel)) links.push(link);
    }
    return links;
  }

  function baseIconHref() {
    const links = iconLinks();
    for (let index = links.length - 1; index >= 0; index -= 1) {
      const href = links[index].href;
      if (href) return href;
    }
    try {
      return new URL("/favicon.ico", location.href).href;
    } catch (error) {
      return null;
    }
  }

  // The mark is the project's slime, drawn into the bottom-right quarter over
  // the page's own favicon and slightly see-through, so the tab keeps its
  // identity and only gains a small companion. Active: awake and bright;
  // recent: dimmer and asleep.
  function slimeBody(context) {
    context.beginPath();
    context.moveTo(1.5, 15);
    context.bezierCurveTo(0, 9, 3, 3.5, 8, 3.5);
    context.bezierCurveTo(13, 3.5, 16, 9, 14.5, 15);
    context.closePath();
  }

  function drawSlime(context, phase) {
    const active = phase === "active";
    context.save();
    // 16 units scaled to 20 px: at the tab strip's 16 px a smaller mark
    // shrinks to a few pixels nobody can read.
    context.translate(12, 12);
    context.scale(1.25, 1.25);
    context.globalAlpha = active ? 0.88 : 0.78;
    // A light halo keeps the slime readable on dark favicons, the dark edge on
    // light ones; which one the page has is unknowable here.
    slimeBody(context);
    context.lineWidth = 2;
    context.strokeStyle = "rgba(255,255,255,0.9)";
    context.stroke();
    slimeBody(context);
    context.lineWidth = 0.8;
    context.strokeStyle = "rgba(10,30,18,0.8)";
    context.stroke();
    context.fillStyle = active ? CONFIG.activeColor : CONFIG.recentColor;
    context.fill();
    context.beginPath();
    context.arc(8, 3.4, 1.2, 0, Math.PI * 2);
    context.fill();
    context.beginPath();
    context.ellipse(5, 7, 1.6, 0.9, -0.6, 0, Math.PI * 2);
    context.fillStyle = "rgba(255,255,255,0.55)";
    context.fill();
    context.fillStyle = "#10261a";
    context.strokeStyle = "#10261a";
    context.lineWidth = 0.9;
    context.lineCap = "round";
    if (active) {
      for (const x of [5.6, 10.4]) {
        context.beginPath();
        context.arc(x, 9.6, 1.5, 0, Math.PI * 2);
        context.fillStyle = "#ffffff";
        context.fill();
        context.beginPath();
        context.arc(x + 0.2, 9.8, 0.8, 0, Math.PI * 2);
        context.fillStyle = "#10261a";
        context.fill();
      }
      context.beginPath();
      context.arc(8, 11.6, 1.6, 0.15 * Math.PI, 0.85 * Math.PI);
      context.stroke();
    } else {
      for (const x of [5.6, 10.4]) {
        context.beginPath();
        context.arc(x, 9.4, 1.2, 0.1 * Math.PI, 0.9 * Math.PI);
        context.stroke();
      }
      context.beginPath();
      context.moveTo(7.2, 12.6);
      context.lineTo(8.8, 12.6);
      context.stroke();
    }
    context.restore();
  }

  function paint(phase, baseImage) {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 32;
      canvas.height = 32;
      const context = canvas.getContext("2d");
      if (!context) return null;
      // No favicon at all leaves the canvas transparent: Chrome's generic
      // globe is not the page's identity, so the slime alone replaces it.
      if (baseImage) context.drawImage(baseImage, 0, 0, 32, 32);
      drawSlime(context, phase);
      return canvas.toDataURL("image/png");
    } catch (error) {
      // A cross-origin favicon without CORS taints the canvas and toDataURL
      // throws. Leave the page's icon as it is rather than swap it for
      // something else: the companion's toolbar badge still marks the tab.
      return null;
    }
  }

  function applyIcon(dataUrl) {
    if (!dataUrl || !document.head) return;
    // Chrome honours the last icon link in the document, but a page that
    // rewrites its own favicon after us would win on the next reflow. Parking
    // the page's rel attribute makes ours the only icon link there is.
    const links = iconLinks();
    for (let index = 0; index < links.length; index += 1) {
      const link = links[index];
      if (link.hasAttribute("data-wsn-rel")) continue;
      link.setAttribute("data-wsn-rel", link.getAttribute("rel") || "icon");
      link.setAttribute("rel", "wsn-parked-icon");
      state.hidden.push(link);
    }
    if (!state.ownLink) {
      state.ownLink = document.createElement("link");
      state.ownLink.setAttribute("rel", "icon");
      state.ownLink.setAttribute("data-wsn-presence", "icon");
    }
    // Inserted after the parking and re-inserted on every change: Chrome keeps
    // the page's icon when ours was already in place before the page's link
    // stopped being an icon, and re-reads the list only when a link is added.
    if (state.ownLink.parentNode) state.ownLink.parentNode.removeChild(state.ownLink);
    state.ownLink.setAttribute("href", dataUrl);
    document.head.appendChild(state.ownLink);
  }

  function repaint(phase) {
    const cached = state.painted[phase];
    if (cached) {
      applyIcon(cached);
      return;
    }
    // Painting is asynchronous - the page's own favicon has to load first - and
    // a burst of actions would otherwise start one image load per action for
    // the same picture.
    if (state.loading[phase]) return;
    state.loading[phase] = true;
    const declared = state.baseHref || (state.baseHref = baseIconHref());
    const finish = function (image) {
      state.loading[phase] = false;
      const dataUrl = paint(phase, image);
      if (!dataUrl) return;
      state.painted[phase] = dataUrl;
      if (state.phase === phase) applyIcon(dataUrl);
    };
    let fallback = null;
    try { fallback = new URL("/favicon.ico", location.href).href; } catch (error) {}
    const candidates = [declared];
    if (fallback && fallback !== declared) candidates.push(fallback);
    const attempt = function (index) {
      const href = candidates[index];
      if (!href) {
        // Nothing readable. A page that declares an icon still has one on
        // screen (Chrome fetches it without CORS), so it stays; only a page
        // with no icon at all gets the slime on its own.
        state.loading[phase] = false;
        if (iconLinks().length || state.hidden.length) return;
        finish(null);
        return;
      }
      try {
        const image = new Image();
        image.crossOrigin = "anonymous";
        image.onload = function () { finish(image); };
        image.onerror = function () { attempt(index + 1); };
        image.src = href;
      } catch (error) {
        attempt(index + 1);
      }
    };
    attempt(0);
  }

  function restoreIcon() {
    for (let index = 0; index < state.hidden.length; index += 1) {
      const link = state.hidden[index];
      try {
        const rel = link.getAttribute("data-wsn-rel");
        if (rel) link.setAttribute("rel", rel);
        link.removeAttribute("data-wsn-rel");
      } catch (error) {}
    }
    state.hidden = [];
    if (state.ownLink && state.ownLink.parentNode) {
      try { state.ownLink.parentNode.removeChild(state.ownLink); } catch (error) {}
    }
    state.ownLink = null;
    state.phase = null;
  }

  function schedule() {
    if (state.timer) {
      try { clearTimeout(state.timer); } catch (error) {}
      state.timer = null;
    }
    const now = Date.now();
    let delay;
    if (now < state.activeUntil) {
      delay = state.activeUntil - now;
    } else if (now < state.recentUntil) {
      delay = state.recentUntil - now;
    } else {
      restoreIcon();
      return;
    }
    state.timer = setTimeout(function () {
      state.timer = null;
      const phase = Date.now() < state.activeUntil
        ? "active"
        : (Date.now() < state.recentUntil ? "recent" : null);
      if (!phase) {
        restoreIcon();
        return;
      }
      state.phase = phase;
      repaint(phase);
      schedule();
    }, Math.max(50, delay));
  }

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

  function chipText(payload) {
    const parts = [];
    if (payload.label) parts.push(payload.label);
    if (payload.action) parts.push(payload.action);
    const text = parts.join(" ") || "agent";
    return payload.ok === false ? text + " ✕" : text;
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

    const box = document.createElement("div");
    const base =
      "position:fixed;box-sizing:border-box;pointer-events:none;" +
      "font:600 11px/1.4 ui-sans-serif,system-ui,Segoe UI,sans-serif;" +
      "opacity:1;transition:opacity " + CONFIG.flashMs + "ms ease-out;";
    if (rect) {
      box.style.cssText =
        base +
        "left:" + Math.round(rect.left - 2) + "px;" +
        "top:" + Math.round(rect.top - 2) + "px;" +
        "width:" + Math.round(rect.width + 4) + "px;" +
        "height:" + Math.round(rect.height + 4) + "px;" +
        "border:2px solid " + color + ";border-radius:4px;" +
        "box-shadow:0 0 0 2px rgba(0,0,0,0.25),0 0 12px " + color + ";";
    } else {
      // Nothing to point at - a scroll, a key press, a navigation. The chip
      // alone still says an action landed, which beats a silent page.
      box.style.cssText = base + "right:12px;top:12px;left:auto;";
    }
    root.appendChild(box);

    const chip = document.createElement("div");
    chip.textContent = chipText(payload);
    chip.style.cssText =
      "position:absolute;white-space:nowrap;padding:1px 6px;border-radius:3px;" +
      "color:#ffffff;background:" + color + ";" +
      "font:600 11px/1.5 ui-sans-serif,system-ui,Segoe UI,sans-serif;" +
      (rect ? "left:-2px;top:-19px;" : "right:0;top:0;");
    box.appendChild(chip);

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

  // ---------------------------------------------------------- ghost cursor

  // CDP input never moves the OS mouse: one arrow per document follows the
  // virtual pointer, with the agent's name tag, and presses land fading rings.
  function shadowed(host) {
    try {
      if (host.attachShadow) return host.attachShadow({mode: "open"});
    } catch (error) {}
    return host;
  }

  function cursorEntry() {
    if (state.cursor) return state.cursor;
    const parent = document.body || document.documentElement;
    if (!parent || !document.createElement) return null;
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.setAttribute("data-wsn-presence", "cursor");
    host.style.cssText = "all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483646;pointer-events:none;display:none;transition:left " + CONFIG.glideMs + "ms linear,top " + CONFIG.glideMs + "ms linear;";
    const root = shadowed(host);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "22"); svg.setAttribute("height", "22");
    svg.setAttribute("viewBox", "0 0 22 22");
    svg.style.cssText = "position:absolute;left:-4px;top:-4px;display:block;overflow:visible;";
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M6 3 L6 15.5 L9.6 12.3 L11.6 17.6 L13.8 16.6 L11.8 11.4 L15.8 11.4 Z");
    path.setAttribute("fill", CONFIG.activeColor);
    path.setAttribute("stroke", "#ffffff"); path.setAttribute("stroke-width", "1.6"); path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path); root.appendChild(svg);
    const chip = document.createElement("div");
    chip.style.cssText = "position:absolute;left:20px;top:18px;white-space:nowrap;padding:1px 6px;border-radius:3px;color:#ffffff;background:" + CONFIG.activeColor + ";font:600 11px/1.5 ui-sans-serif,system-ui,Segoe UI,sans-serif;opacity:0;transition:opacity 300ms ease-out;";
    root.appendChild(chip); parent.appendChild(host);
    return (state.cursor = {host: host, chip: chip, timer: null, visible: false});
  }

  function moveCursor(point, payload) {
    if (!point || !isFinite(point.x) || !isFinite(point.y)) return;
    const entry = cursorEntry();
    if (!entry) return;
    const off = point.x < 0 || point.y < 0 || point.x > window.innerWidth || point.y > window.innerHeight;
    entry.visible = !off;
    entry.host.style.display = off ? "none" : "block";
    if (off) return;
    entry.host.style.left = Math.round(point.x) + "px";
    entry.host.style.top = Math.round(point.y) + "px";
    if (!payload) return;
    entry.chip.textContent = chipText(payload);
    entry.chip.style.opacity = "1";
    if (entry.timer) { try { clearTimeout(entry.timer); } catch (error) {} }
    entry.timer = setTimeout(function () { entry.timer = null; entry.chip.style.opacity = "0"; }, CONFIG.chipMs);
  }

  function ripple(point, ok) {
    const parent = document.body || document.documentElement;
    if (!parent || !document.createElement || !point) return;
    if (!isFinite(point.x) || !isFinite(point.y)) return;
    if (point.x < 0 || point.y < 0 || point.x > window.innerWidth || point.y > window.innerHeight) return;
    const color = ok === false ? "#e2584d" : CONFIG.activeColor;
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.setAttribute("data-wsn-presence", "ripple");
    host.style.cssText = "all:initial;position:fixed;left:" + Math.round(point.x) + "px;top:" + Math.round(point.y) + "px;width:0;height:0;z-index:2147483646;pointer-events:none;";
    const ring = document.createElement("div");
    ring.style.cssText = "position:absolute;left:-9px;top:-9px;width:18px;height:18px;box-sizing:border-box;border-radius:50%;border:3px solid " + color + ";box-shadow:0 0 8px " + color + ";opacity:0.95;pointer-events:none;transform:scale(1);transition:transform " + CONFIG.rippleMs + "ms ease-out,opacity " + CONFIG.rippleMs + "ms ease-out;";
    shadowed(host).appendChild(ring);
    parent.appendChild(host);
    state.ripples.push(host);
    while (state.ripples.length > CONFIG.maxRipples) dropFlash(state.ripples.shift());
    requestAnimationFrame(function () { requestAnimationFrame(function () { ring.style.transform = "scale(2.8)"; ring.style.opacity = "0"; }); });
    setTimeout(function () { const i = state.ripples.indexOf(host); if (i !== -1) state.ripples.splice(i, 1); dropFlash(host); }, CONFIG.rippleMs + 120);
  }

  // ------------------------------------------------------------------ entry

  state.note = function (payload) {
    const data = payload && typeof payload === "object" ? payload : {};
    const now = Date.now();
    state.activeUntil = now + CONFIG.activeMs;
    state.recentUntil = now + CONFIG.recentMs;
    if (state.phase !== "active") {
      state.phase = "active";
      repaint("active");
    } else if (!state.ownLink) {
      repaint("active");
    }
    schedule();
    if (data.flash !== false) {
      try { flash(data); } catch (error) {}
    }
    if (data.cursor) {
      try { moveCursor(data.cursor, data); if (data.cursor.tap) ripple(data.cursor, data.ok); } catch (error) {}
    }
    return true;
  };

  state.hideFlashes = function () {
    clearFlashes();
    for (const host of state.ripples.splice(0)) dropFlash(host);
    if (state.cursor) state.cursor.host.style.display = "none";
    return true;
  };

  state.showEphemeral = function () {
    if (state.cursor && state.cursor.visible) state.cursor.host.style.display = "block";
    return true;
  };

  state.restore = function () {
    if (state.timer) {
      try { clearTimeout(state.timer); } catch (error) {}
      state.timer = null;
    }
    state.activeUntil = 0;
    state.recentUntil = 0;
    restoreIcon();
    clearFlashes();
    for (const host of state.ripples.splice(0)) dropFlash(host);
    if (state.cursor) {
      if (state.cursor.timer) { try { clearTimeout(state.cursor.timer); } catch (error) {} }
      dropFlash(state.cursor.host);
      state.cursor = null;
    }
    try { delete window[KEY]; } catch (error) { window[KEY] = undefined; }
  };

  return true;
})();
"""
