"""Show a human, in the browser itself, that an agent is driving this tab.

Two signals, both aimed at the person watching rather than at the caller:

* a badge painted onto the tab's favicon while an agent is working in it, and
  for a few minutes after it stopped - the tab strip is where someone looks
  first, and the ``[session] `` title prefix in ``browser_tools`` only says
  *whose* tab it is, never *whether anything is happening in it right now*;
* a short flash over the element the last action touched, so a burst of thirty
  clicks reads as thirty visible taps instead of a page that mutates on its own.

Everything here is pure data: the JavaScript source and the small builders that
bake values into it. The session plumbing - when to install the script, which
sessions are eligible, how to reach the driver - lives in ``browser_tools``,
which imports this module. Keeping the direction of that import one-way is what
lets the payloads be unit-tested without a browser.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Bumping this makes an already-installed script in a live page stand down and
# reinstall itself. Without it a long-lived tab would keep running whatever
# version was current when it first loaded, and a fix here would only reach
# pages opened afterwards.
PRESENCE_VERSION = 1

PRESENCE_ENV = "WEB_SEARCH_NEO_AGENT_PRESENCE"

# How long a single action keeps the badge in its bright "working now" state.
# An action lasts milliseconds, so this is really "how long the eye needs to
# catch it" - shorter and a fast burst of steps looks like nothing happened.
ACTIVE_MS = 2500
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

_ACTIVE_COLOR = "#2fbf5c"
_RECENT_COLOR = "#e2a03f"

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
    }


def payload_for(
    action: str,
    arguments: dict[str, Any] | None = None,
    *,
    ok: bool = True,
    label: str | None = None,
) -> dict[str, Any]:
    """Describe one finished action in the terms the page-side flash needs.

    Only a hint is passed, never a resolved element: the action has already
    happened by the time this runs, and re-resolving the selector in the page
    is both cheaper than shipping a rect back and correct in the common case
    where the click moved something.
    """
    source = dict(arguments or {})
    payload: dict[str, Any] = {
        "action": str(action or "").strip().lower(),
        "label": label or None,
        "ok": bool(ok),
        "flash": str(action or "").strip().lower() not in QUIET_ACTIONS,
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

# A flash lasts 250 ms and a screenshot can be taken inside that window, which
# would hand an agent a picture of a green box its own click drew and let it
# reason about the box as if the page had put it there. Every capture clears the
# overlays first. The favicon badge is untouched: it is not in the picture.
HIDE_FLASHES_SCRIPT = (
    "(() => { const presence = window.__wsnPresence;"
    " if (presence && typeof presence.hideFlashes === 'function') presence.hideFlashes();"
    " return true; })()"
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

  function drawBadge(context, color) {
    const cx = 23;
    const cy = 23;
    // A ring in the page-independent direction: dark under a light favicon,
    // light under a dark one is unknowable here, so the badge carries its own
    // contrast with a two-tone outline instead of guessing.
    context.beginPath();
    context.arc(cx, cy, 9.5, 0, Math.PI * 2);
    context.fillStyle = "rgba(255,255,255,0.92)";
    context.fill();
    context.beginPath();
    context.arc(cx, cy, 8, 0, Math.PI * 2);
    context.fillStyle = "rgba(17,24,28,0.55)";
    context.fill();
    context.beginPath();
    context.arc(cx, cy, 6.5, 0, Math.PI * 2);
    context.fillStyle = color;
    context.fill();
  }

  function paint(phase, baseImage) {
    let canvas;
    try {
      canvas = document.createElement("canvas");
      canvas.width = 32;
      canvas.height = 32;
      const context = canvas.getContext("2d");
      if (!context) return null;
      if (baseImage) {
        context.drawImage(baseImage, 0, 0, 32, 32);
      } else {
        // No readable favicon: a neutral tile still gives the tab strip a
        // visibly different icon, which is the whole point of the signal.
        context.fillStyle = "#20272e";
        context.fillRect(0, 0, 32, 32);
      }
      drawBadge(context, phase === "active" ? CONFIG.activeColor : CONFIG.recentColor);
      return canvas.toDataURL("image/png");
    } catch (error) {
      // A cross-origin favicon taints the canvas and toDataURL throws. Fall
      // back to the badge alone rather than leaving the tab unmarked.
      if (baseImage) return paint(phase, null);
      return null;
    }
  }

  function applyIcon(dataUrl) {
    if (!dataUrl || !document.head) return;
    if (!state.ownLink) {
      state.ownLink = document.createElement("link");
      state.ownLink.setAttribute("rel", "icon");
      state.ownLink.setAttribute("data-wsn-presence", "icon");
    }
    state.ownLink.setAttribute("href", dataUrl);
    if (state.ownLink.parentNode !== document.head) document.head.appendChild(state.ownLink);
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
    const href = state.baseHref || (state.baseHref = baseIconHref());
    const finish = function (image) {
      state.loading[phase] = false;
      const dataUrl = paint(phase, image);
      if (!dataUrl) return;
      state.painted[phase] = dataUrl;
      if (state.phase === phase) applyIcon(dataUrl);
    };
    if (!href) {
      finish(null);
      return;
    }
    try {
      const image = new Image();
      image.crossOrigin = "anonymous";
      image.onload = function () { finish(image); };
      image.onerror = function () { finish(null); };
      image.src = href;
      if (image.complete && image.naturalWidth) finish(image);
    } catch (error) {
      finish(null);
    }
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
    return true;
  };

  state.hideFlashes = function () {
    clearFlashes();
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
    try { delete window[KEY]; } catch (error) { window[KEY] = undefined; }
  };

  return true;
})();
"""
