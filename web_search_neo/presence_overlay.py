"""The ghost cursor and the action caption, spliced into the presence installer.

``agent_presence`` owns the favicon badge, the flash and the installer's entry
points; this module owns what moves. Its script is inserted into the installer
at ``__WSN_PRESENCE_OVERLAY__`` and shares that closure (``CONFIG``, ``state``,
``targetRect``, ``pierce``, ``dropFlash``).

What a person watching the tab sees:

* an arrow that glides to where the agent's virtual pointer went - eased, on a
  slight arc, faster for short hops than for long reaches - and that turns
  smoothly when a new target arrives mid-glide instead of jumping;
* a squash and a ring on every press, a held ring and a short trail while a
  button is down (a drag), and a soft halo where it hovered;
* one caption at a time saying what the agent is doing ("Click · Sign in",
  "Typing…", "Scrolling ↓"): beside the arrow when the action has a point,
  otherwise beside the element it touched, otherwise in a corner away from the
  arrow. It fades on its own.

Everything is ``pointer-events:none``, ``aria-hidden`` and tagged
``data-wsn-presence`` (what the page readers, the click verifier and the
screenshot hiding all key on), is styled inline only (a strict style-src CSP
drops a <style> element), never uses innerHTML (Trusted Types), and honours
``prefers-reduced-motion`` by moving without animating.

Two timing sources keep the arrow and the real events together. An action's
own note arrives after the action, so the arrow glides there afterwards - with
its tap ring drawn at once, because the press already landed. A gesture that
``pointer_motion`` sends over time (an approach, a timed drag) is announced
first with a ``lead``: the arrow then plays the same arc, over the same
milliseconds, while the events are sent, and the action's later note only
confirms it.
"""

from __future__ import annotations

from typing import Any

from web_search_neo import presence_panel
from web_search_neo.actions import pointer_path

# The arrow's own glide when nothing dictates its timing: Fitts-shaped like the
# real approach, but a little slower, because it is there to be watched.
GLIDE_MIN_MS = 120
GLIDE_MAX_MS = 450
GLIDE_BASE_MS = 90
GLIDE_SLOPE_MS = 55
GLIDE_SCALE_PX = 12
# Notes arriving faster than a glide (a game loop) shorten it to the gap
# between them, never below this, so the arrow keeps up instead of trailing.
GLIDE_FAST_MS = 40
# A press ring lives long enough to be seen after the fact and dies fast
# enough that a burst stays a sequence rather than a smear.
RIPPLE_MS = 700
MAX_RIPPLES = 8
# How long a caption stays readable before it fades.
CAPTION_MS = 1800
# The trail behind a held button: a fifth of a second of dots.
TRAIL_MS = 220
TRAIL_DOTS = 10
# How long after a lead the action's own note at the same point is taken as
# its echo (no second glide, no second ring) rather than as a new gesture.
LEAD_ECHO_MS = 4000


def config() -> dict[str, Any]:
    """The overlay's constants, merged into the installer's CONFIG."""
    return {
        "glideMinMs": GLIDE_MIN_MS,
        "glideMaxMs": GLIDE_MAX_MS,
        "glideBaseMs": GLIDE_BASE_MS,
        "glideSlopeMs": GLIDE_SLOPE_MS,
        "glideScalePx": GLIDE_SCALE_PX,
        "glideFastMs": GLIDE_FAST_MS,
        "minGlidePx": pointer_path.MIN_DISTANCE,
        "bendRatio": pointer_path.BEND_RATIO,
        "bendMax": pointer_path.BEND_MAX,
        "maxInitialSpeed": 2.5,
        "rippleMs": RIPPLE_MS,
        "maxRipples": MAX_RIPPLES,
        "captionMs": CAPTION_MS,
        "trailMs": TRAIL_MS,
        "trailDots": TRAIL_DOTS,
        "leadEchoMs": LEAD_ECHO_MS,
        "panelRefreshMs": presence_panel.PANEL_REFRESH_MS,
    }


SOURCE = r"""
  // ------------------------------------------------ ghost cursor & caption
  //
  // CDP input never moves the OS mouse, so one arrow per document follows the
  // agent's virtual pointer. The host's left/top say where the pointer *is* -
  // written once per action, so the page and its readers see the real position
  // at once - and the arrow is drawn on its way there by a transform offset,
  // from one animation-frame loop that runs only while something moves. No
  // frame touches layout, and an idle page pays nothing.

  const SVG_NS = "http://www.w3.org/2000/svg";
  const FONT = "font:600 11px/1.5 ui-sans-serif,system-ui,Segoe UI,sans-serif;";
  const PILL = "white-space:nowrap;max-width:340px;overflow:hidden;text-overflow:ellipsis;" +
    "padding:2px 9px 2px 7px;border-radius:999px;color:#ffffff;background:rgba(17,24,39,0.86);" +
    "box-shadow:0 1px 4px rgba(0,0,0,0.35);" + FONT;
  // Captured now, before page scripts run: a page that patches these later
  // must not be able to stall or speed up the overlay.
  const perf = window.performance;
  const perfNow = perf && perf.now;
  const rafNative = window.requestAnimationFrame;
  const cafNative = window.cancelAnimationFrame;
  function clock() {
    try { if (perfNow) return perfNow.call(perf); } catch (error) {}
    return Date.now();
  }
  function nextFrame(callback) {
    return rafNative ? rafNative.call(window, callback) : setTimeout(callback, 16);
  }
  let motionQuery = null;
  try {
    motionQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  } catch (error) {}
  function calm() { return !!(motionQuery && motionQuery.matches); }
  function num(value) { return typeof value === "number" && isFinite(value); }
  function onScreen(x, y) {
    return num(x) && num(y) && x >= 0 && y >= 0 && x <= window.innerWidth && y <= window.innerHeight;
  }

  const motion = {
    x: NaN, y: NaN, vx: 0, vy: 0, at: 0, carry: false, seg: null, queue: [],
    frame: 0, held: false, trail: [], lead: null, lastNote: 0, gap: 0, label: "", ok: true,
  };
  state.hidden = false;
  state.hud = null;
  state.trail = null;

  function shadowed(host) {
    try {
      if (host.attachShadow) return host.attachShadow({mode: "open"});
    } catch (error) {}
    return host;
  }

  function overlayHost(kind, z) {
    const parent = document.documentElement || document.body;
    if (!parent || !document.createElement) return null;
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.setAttribute("data-wsn-presence", kind);
    host.style.cssText = "all:initial;position:fixed;left:0;top:0;width:0;height:0;" +
      "z-index:" + z + ";pointer-events:none;display:none;";
    parent.appendChild(host);
    return host;
  }

  function attached(entry) {
    if (entry && !entry.host.isConnected) {
      const parent = document.documentElement || document.body;
      if (parent) parent.appendChild(entry.host);
    }
    return entry;
  }

  function cursorEntry() {
    if (state.cursor) return attached(state.cursor);
    const host = overlayHost("cursor", 2147483646);
    if (!host) return null;
    host.style.willChange = "transform";
    host.style.transition = "opacity 160ms ease-out";
    const root = shadowed(host);
    // The arrow and its ring squash together about the tip, which sits exactly
    // on the host's origin.
    const body = document.createElement("div");
    body.style.cssText = "position:absolute;left:0;top:0;width:0;height:0;transform-origin:0 0;" +
      "transition:transform 110ms cubic-bezier(.3,.7,.4,1.4);";
    const ring = document.createElement("div");
    ring.style.cssText = "position:absolute;left:-11px;top:-11px;width:22px;height:22px;" +
      "box-sizing:border-box;border-radius:50%;border:2px solid " + CONFIG.activeColor + ";" +
      "background:rgba(47,191,92,0.18);opacity:0;transform:scale(0.6);" +
      "transition:opacity 140ms ease-out,transform 140ms ease-out;";
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("width", "22"); svg.setAttribute("height", "22");
    svg.setAttribute("viewBox", "0 0 22 22");
    svg.style.cssText = "position:absolute;left:-6px;top:-3px;display:block;overflow:visible;" +
      "filter:drop-shadow(0 1px 1.5px rgba(0,0,0,0.45));";
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M6 3 L6 15.5 L9.6 12.3 L11.6 17.6 L13.8 16.6 L11.8 11.4 L15.8 11.4 Z");
    path.setAttribute("fill", CONFIG.activeColor);
    path.setAttribute("stroke", "#ffffff"); path.setAttribute("stroke-width", "1.6");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path); body.appendChild(ring); body.appendChild(svg); root.appendChild(body);
    const pill = document.createElement("div");
    pill.style.cssText = "position:absolute;left:18px;top:20px;" + PILL +
      "opacity:0;transform:translateY(-3px);transition:opacity 180ms ease-out,transform 180ms ease-out;";
    root.appendChild(pill);
    return (state.cursor = {host: host, body: body, ring: ring, pill: pill, timer: null,
      visible: false, tx: 0, ty: 0});
  }

  // ------------------------------------------------------------ the motion

  // The same arc as actions/pointer_path.py: a quadratic whose widest bow is
  // `bend` pixels along the move's left-hand normal, upward for sideways moves.
  function bendFor(x0, y0, x1, y1) {
    const distance = Math.hypot(x1 - x0, y1 - y0);
    if (distance < CONFIG.minGlidePx) return 0;
    const size = Math.min(CONFIG.bendMax, distance * CONFIG.bendRatio);
    return x1 - x0 > 0 ? -size : size;
  }

  // pointer_path.hermite: starts at speed s0, stops dead; s0 = 0 is smoothstep.
  function ease(u, s0) {
    return s0 * (u * u * u - 2 * u * u + u) + (3 * u * u - 2 * u * u * u);
  }

  function glideMs(distance) {
    let ms = CONFIG.glideBaseMs + CONFIG.glideSlopeMs * Math.log2(1 + distance / CONFIG.glideScalePx);
    ms = Math.max(CONFIG.glideMinMs, Math.min(CONFIG.glideMaxMs, ms));
    if (motion.gap > 0) ms = Math.max(CONFIG.glideFastMs, Math.min(ms, motion.gap * 0.9));
    return ms;
  }

  function begin(spec, now) {
    if (!num(motion.x) || !num(motion.y)) { motion.x = spec.x; motion.y = spec.y; }
    const x0 = motion.x, y0 = motion.y, x1 = spec.x, y1 = spec.y;
    const dx = x1 - x0, dy = y1 - y0, distance = Math.hypot(dx, dy);
    const seg = {spec: spec, x0: x0, y0: y0, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, s0: 0,
      start: now, ms: num(spec.ms) && spec.ms > 0 ? spec.ms : glideMs(distance || 0)};
    const speed = Math.hypot(motion.vx, motion.vy);
    if (motion.carry && speed > 0.05 && distance > 0) {
      // Interrupted mid-glide: leave the way it was already going, at the
      // speed it had, so a retarget bends instead of stopping dead first.
      const reach = Math.min(distance / 2, speed * seg.ms / 3);
      seg.cx = x0 + motion.vx / speed * reach;
      seg.cy = y0 + motion.vy / speed * reach;
      seg.s0 = Math.min(CONFIG.maxInitialSpeed, speed * seg.ms / (2 * reach));
    } else if (distance > 0) {
      const bend = num(spec.bend) ? spec.bend : bendFor(x0, y0, x1, y1);
      seg.cx -= dy / distance * 2 * bend;
      seg.cy += dx / distance * 2 * bend;
    }
    motion.carry = false;
    if (calm() || !(distance >= 0.5)) seg.ms = 0;
    return (motion.seg = seg);
  }

  function draw() {
    const entry = state.cursor;
    if (!entry || !num(motion.x)) return;
    entry.host.style.transform = "translate3d(" + (motion.x - entry.tx).toFixed(1) + "px," +
      (motion.y - entry.ty).toFixed(1) + "px,0)";
  }

  function place(x, y, now, held) {
    if (num(motion.x) && now > motion.at) {
      motion.vx = (x - motion.x) / (now - motion.at);
      motion.vy = (y - motion.y) / (now - motion.at);
    }
    motion.x = x; motion.y = y; motion.at = now;
    if (held && !calm()) motion.trail.push({x: x, y: y, at: now});
    draw();
  }

  function tick() {
    motion.frame = 0;
    if (state.cursor === undefined) return;
    const now = clock();
    let seg = motion.seg;
    if (!seg && motion.queue.length) seg = begin(motion.queue.shift(), now);
    if (seg) {
      const u = seg.ms > 0 ? Math.min(1, (now - seg.start) / seg.ms) : 1;
      const e = ease(u, seg.s0), k = 1 - e;
      place(k * k * seg.x0 + 2 * k * e * seg.cx + e * e * seg.spec.x,
        k * k * seg.y0 + 2 * k * e * seg.cy + e * e * seg.spec.y, now, !!seg.spec.held);
      if (u >= 1) {
        motion.seg = null; motion.vx = 0; motion.vy = 0;
        effect(seg.spec.end, seg.spec.x, seg.spec.y, false);
      }
    }
    drawTrail(now);
    if (motion.seg || motion.queue.length || motion.trail.length) pump();
  }

  function pump() {
    if (!motion.frame) motion.frame = nextFrame(tick) || 1;
  }

  // A new gesture replaces whatever was still playing. What the old one still
  // owed - a ring where a press landed, a button let go - happens now, at its
  // own target, so nothing the agent did goes unshown.
  function flush() {
    const pending = (motion.seg ? [motion.seg.spec] : []).concat(motion.queue);
    motion.carry = !!motion.seg && num(motion.x);
    motion.seg = null;
    motion.queue = [];
    for (let index = 0; index < pending.length; index += 1) {
      effect(pending[index].end, pending[index].x, pending[index].y, true);
    }
  }

  // ------------------------------------------------- presses, holds, hovers

  function effect(fx, x, y, flushed) {
    if (!fx) return;
    const entry = state.cursor;
    if (fx.ripple) ripple({x: x, y: y}, motion.ok);
    if (typeof fx.grip === "boolean") grip(entry, fx.grip);
    if (flushed || !entry || calm()) return;
    if (fx.squash) {
      entry.body.style.transform = "scale(0.8)";
      setTimeout(function () { entry.body.style.transform = motion.held ? "scale(0.9)" : "scale(1)"; }, 110);
    }
    if (fx.dwell && !motion.held) {
      entry.ring.style.opacity = "0.55"; entry.ring.style.transform = "scale(1.15)";
      setTimeout(function () {
        if (motion.held) return;
        entry.ring.style.opacity = "0"; entry.ring.style.transform = "scale(0.6)";
      }, 420);
    }
  }

  function grip(entry, on) {
    motion.held = on;
    if (!entry) return;
    entry.ring.style.opacity = on ? "1" : "0";
    entry.ring.style.transform = on ? "scale(1)" : "scale(0.6)";
    if (!calm()) entry.body.style.transform = on ? "scale(0.9)" : "scale(1)";
  }

  function ripple(point, ok) {
    const parent = document.body || document.documentElement;
    if (!parent || !document.createElement || !point || !onScreen(point.x, point.y)) return;
    const color = ok === false ? "#e2584d" : CONFIG.activeColor;
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.setAttribute("data-wsn-presence", "ripple");
    host.style.cssText = "all:initial;position:fixed;left:" + Math.round(point.x) + "px;top:" +
      Math.round(point.y) + "px;width:0;height:0;z-index:2147483646;pointer-events:none;";
    const ring = document.createElement("div");
    const still = calm();
    ring.style.cssText = "position:absolute;left:-9px;top:-9px;width:18px;height:18px;" +
      "box-sizing:border-box;border-radius:50%;border:3px solid " + color + ";box-shadow:0 0 8px " +
      color + ";opacity:0.95;pointer-events:none;transform:scale(" + (still ? 1 : 0.6) + ");" +
      "transition:transform " + CONFIG.rippleMs + "ms cubic-bezier(.2,.7,.3,1),opacity " +
      CONFIG.rippleMs + "ms ease-out;";
    shadowed(host).appendChild(ring);
    parent.appendChild(host);
    state.ripples.push(host);
    while (state.ripples.length > CONFIG.maxRipples) dropFlash(state.ripples.shift());
    nextFrame(function () {
      nextFrame(function () {
        if (!still) ring.style.transform = "scale(2.8)";
        ring.style.opacity = "0";
      });
    });
    setTimeout(function () {
      const index = state.ripples.indexOf(host);
      if (index !== -1) state.ripples.splice(index, 1);
      dropFlash(host);
    }, CONFIG.rippleMs + 120);
  }

  // While a button is held the arrow leaves a short fading trail, so a drag
  // reads as a drag and not as a pointer that wandered.
  function drawTrail(now) {
    const points = motion.trail;
    while (points.length && now - points[0].at > CONFIG.trailMs) points.shift();
    if (!points.length && !state.trail) return;
    let entry = attached(state.trail);
    if (!entry) {
      const host = overlayHost("trail", 2147483645);
      if (!host) return;
      const svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("width", "1"); svg.setAttribute("height", "1");
      svg.style.cssText = "position:absolute;left:0;top:0;display:block;overflow:visible;";
      const dots = [];
      for (let index = 0; index < CONFIG.trailDots; index += 1) {
        const dot = document.createElementNS(SVG_NS, "circle");
        dot.setAttribute("fill", CONFIG.activeColor); dot.setAttribute("r", "0");
        svg.appendChild(dot); dots.push(dot);
      }
      shadowed(host).appendChild(svg);
      entry = state.trail = {host: host, dots: dots, timer: null};
    }
    entry.host.style.display = points.length && !state.hidden ? "block" : "none";
    const stride = Math.max(1, points.length / entry.dots.length);
    for (let index = 0; index < entry.dots.length; index += 1) {
      const dot = entry.dots[index];
      const point = points[Math.floor(points.length - 1 - index * stride)];
      if (!point) { dot.setAttribute("r", "0"); continue; }
      const age = Math.min(1, (now - point.at) / CONFIG.trailMs);
      dot.setAttribute("cx", point.x.toFixed(1)); dot.setAttribute("cy", point.y.toFixed(1));
      dot.setAttribute("r", (3.2 * (1 - age) + 0.6).toFixed(2));
      dot.setAttribute("fill-opacity", (0.5 * (1 - age)).toFixed(2));
    }
  }

  // -------------------------------------------------------------- caption

  // A short visible name for the element under the action. Never what someone
  // typed: a field's value, a text area, an editable region are skipped, and a
  // container with a paragraph of text is not a name at all.
  function nameOf(node) {
    if (!node || node.nodeType !== 1 || node === document.body || node === document.documentElement) return "";
    const target = (node.closest && node.closest(
      "a,button,label,summary,option,input,select,textarea,[role],[aria-label],[title]")) || node;
    const tag = String(target.tagName || "").toLowerCase();
    let text = target.getAttribute("aria-label") || "";
    if (!text && tag === "input") {
      const type = String(target.type || "").toLowerCase();
      text = /^(button|submit|reset)$/.test(type) ? target.value
        : (target.getAttribute("placeholder") || target.getAttribute("name") || "");
    } else if (!text && tag !== "textarea" && tag !== "select" && !target.isContentEditable) {
      text = target.textContent || "";
    }
    text = String(text || target.getAttribute("title") || target.getAttribute("alt") || "")
      .replace(/\s+/g, " ").trim();
    if (!text || text.length > 120) return "";
    return text.length > 32 ? text.slice(0, 31) + "…" : text;
  }

  function targetName(data, point) {
    let node = null;
    try {
      if (data.selector) {
        node = data.selector.indexOf(">>>") !== -1 ? pierce(data.selector) : document.querySelector(data.selector);
      }
      // Our own layers are pointer-events:none, so the hit test sees the page.
      if (!node && point && document.elementFromPoint) node = document.elementFromPoint(point.x, point.y);
      return nameOf(node);
    } catch (error) {
      return "";
    }
  }

  function captionLine(data, point) {
    let text = typeof data.caption === "string" ? data.caption : "";
    if (!text) return null;
    if (data.captionTarget) {
      const name = targetName(data, point);
      if (name) text += " · " + name;
    }
    return {label: motion.label, text: text, ok: data.ok !== false};
  }

  function fillCaption(box, line) {
    while (box.firstChild) box.removeChild(box.firstChild);
    const dot = document.createElement("span");
    dot.style.cssText = "display:inline-block;width:7px;height:7px;margin:0 6px 1px 0;" +
      "border-radius:50%;vertical-align:middle;background:" + (line.ok ? CONFIG.activeColor : "#e2584d") + ";";
    box.appendChild(dot);
    if (line.label) {
      const who = document.createElement("span");
      who.textContent = line.label;
      who.style.cssText = "font-weight:700;opacity:0.72;margin-right:6px;";
      box.appendChild(who);
    }
    box.appendChild(document.createTextNode(line.text + (line.ok ? "" : " ✕")));
  }

  function fadeLater(entry, box, hiddenTransform) {
    if (entry.timer) { try { clearTimeout(entry.timer); } catch (error) {} }
    entry.timer = setTimeout(function () {
      entry.timer = null;
      box.style.opacity = "0";
      if (hiddenTransform) box.style.transform = hiddenTransform;
    }, CONFIG.captionMs);
  }

  function showPill(entry, line) {
    if (!line) return;
    fillCaption(entry.pill, line);
    // Kept on the page: flipped to the arrow's left near the right edge and
    // above it near the bottom.
    const right = entry.tx > window.innerWidth - 260, low = entry.ty > window.innerHeight - 56;
    entry.pill.style.left = right ? "auto" : "18px";
    entry.pill.style.right = right ? "10px" : "auto";
    entry.pill.style.top = low ? "auto" : "20px";
    entry.pill.style.bottom = low ? "14px" : "auto";
    entry.pill.style.opacity = "1";
    entry.pill.style.transform = "translateY(0)";
    fadeLater(entry, entry.pill, "translateY(-3px)");
    if (state.hud) { state.hud.box.style.opacity = "0"; state.hud.until = 0; }
  }

  function showHud(line, rect) {
    // A caption with nothing to stand beside would sit in a corner and repeat
    // what the status panel already says: the panel shows it instead.
    const beside = rect && rect.width > 0 && rect.top < window.innerHeight && rect.top + rect.height > 0;
    if (!beside && panel.entry && state.phase) return;
    let entry = attached(state.hud);
    if (!entry) {
      const host = overlayHost("caption", 2147483646);
      if (!host) return;
      const box = document.createElement("div");
      box.style.cssText = "position:absolute;left:0;top:0;" + PILL + "opacity:0;transition:opacity 180ms ease-out;";
      shadowed(host).appendChild(box);
      entry = state.hud = {host: host, box: box, timer: null, until: 0};
    }
    fillCaption(entry.box, line);
    const width = window.innerWidth, height = window.innerHeight;
    // The top-right corner by default, hugged by the box's right edge.
    let left = width - 12, top = 12, leftward = true;
    if (rect && rect.width > 0 && rect.top < height && rect.top + rect.height > 0) {
      // Beside the element it is about: above it when there is room, else below.
      left = Math.max(6, Math.min(width - 160, rect.left));
      top = rect.top > 34 ? rect.top - 26 : Math.min(height - 26, rect.top + rect.height + 6);
      leftward = false;
    } else if (state.cursor && state.cursor.visible && state.cursor.tx > width - 380 && state.cursor.ty < 90) {
      // The arrow is in that corner: use the other one.
      left = 12;
      leftward = false;
    }
    entry.host.style.left = Math.round(left) + "px";
    entry.host.style.top = Math.round(Math.max(4, top)) + "px";
    entry.box.style.left = leftward ? "auto" : "0";
    entry.box.style.right = leftward ? "0" : "auto";
    entry.host.style.display = state.hidden ? "none" : "block";
    entry.box.style.opacity = "1";
    entry.until = Date.now() + CONFIG.captionMs;
    fadeLater(entry, entry.box, null);
    if (state.cursor) state.cursor.pill.style.opacity = "0";
  }

  // ------------------------------------------------------- following a note

  function aim(entry, x, y) {
    const shown = onScreen(x, y);
    entry.visible = shown;
    entry.host.style.display = shown && !state.hidden ? "block" : "none";
    if (!shown) {
      flush();
      motion.x = NaN; motion.y = NaN; motion.trail = [];
      return false;
    }
    entry.tx = Math.round(x); entry.ty = Math.round(y);
    entry.host.style.left = entry.tx + "px";
    entry.host.style.top = entry.ty + "px";
    draw();
    return true;
  }

  function arrive(entry, x, y) {
    // First sight of the arrow (or back from off-screen): it fades in where it
    // starts instead of flying in from wherever it was last seen.
    motion.x = x; motion.y = y; motion.vx = 0; motion.vy = 0; motion.at = clock();
    draw();
    if (calm()) return;
    entry.host.style.opacity = "0";
    nextFrame(function () { entry.host.style.opacity = "1"; });
  }

  // An action's own note: it already happened, so its ring is drawn where it
  // landed at once and the arrow glides there after it.
  function moveCursor(point, data, line, now) {
    const entry = state.cursor;
    const lead = motion.lead;
    motion.lead = null;
    if (lead && now < lead.until && Math.abs(lead.x - point.x) < 2 && Math.abs(lead.y - point.y) < 2) {
      // The lead already played this gesture while it was sent; the note only
      // confirms it, or marks it failed.
      if (entry.visible) showPill(entry, data.ok !== false && lead.line ? lead.line : line);
      return;
    }
    const fresh = !num(motion.x) || !entry.visible;
    flush();
    if (!aim(entry, point.x, point.y)) return;
    if (fresh) arrive(entry, point.x, point.y);
    const was = motion.held, held = !!data.held;
    if (point.tap) ripple(point, data.ok);
    const end = held !== was ? {grip: held} : (point.tap ? {squash: true} : null);
    motion.queue = [{x: point.x, y: point.y, held: was, end: end}];
    pump();
    showPill(entry, line);
  }

  // A gesture announced before it is sent: play it over the same milliseconds
  // and the same arc the events take - the approach, then a timed drag.
  function leadTo(point, lead, line, now) {
    const entry = state.cursor;
    const dragging = num(lead.dragX) && num(lead.dragY);
    const final = dragging ? {x: lead.dragX, y: lead.dragY} : point;
    const fresh = !num(motion.x) || !entry.visible;
    flush();
    if (!aim(entry, final.x, final.y)) return;
    if (fresh) {
      const from = onScreen(lead.fromX, lead.fromY) ? {x: lead.fromX, y: lead.fromY} : point;
      arrive(entry, from.x, from.y);
    }
    const approachMs = num(lead.ms) && lead.ms > 0 ? lead.ms : glideMs(Math.hypot(point.x - motion.x, point.y - motion.y));
    const queue = [{x: point.x, y: point.y, ms: approachMs, bend: lead.bend, held: motion.held,
      end: dragging ? {ripple: true, grip: true} : (lead.tap ? {ripple: true, squash: true} : {dwell: true})}];
    let total = approachMs;
    if (dragging) {
      const dragMs = Math.max(16, num(lead.dragMs) ? lead.dragMs : 0);
      queue.push({x: final.x, y: final.y, ms: dragMs, bend: 0, held: true, end: {grip: false}});
      total += dragMs;
    }
    motion.queue = queue;
    motion.lead = {x: final.x, y: final.y, until: now + total + CONFIG.leadEchoMs, line: line};
    pump();
    showPill(entry, line);
  }

  // The overlay's half of a note: move the arrow, then say what happened -
  // beside the arrow when there is one, else beside the element, else in a
  // corner. Returns nothing the caller has to use.
  function overlayNote(data) {
    try { panelNote(data); } catch (error) {}
    const now = clock();
    if (data.label) motion.label = String(data.label);
    motion.gap = motion.lastNote ? now - motion.lastNote : 0;
    motion.lastNote = now;
    motion.ok = data.ok !== false;
    let point = data.cursor && num(data.cursor.x) && num(data.cursor.y) ? data.cursor : null;
    if (!point && data.cursorTarget) {
      const rect = targetRect(data);
      if (rect) point = {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, tap: data.cursorTap};
    }
    const line = captionLine(data, point);
    if (point && cursorEntry()) {
      if (data.lead && typeof data.lead === "object") leadTo(point, data.lead, line, now);
      else moveCursor(point, data, line, now);
      if (state.cursor.visible) return;
    }
    if (line) showHud(line, data.selector || data.flash !== false ? targetRect(data) : null);
  }

  function overlayHide() {
    state.hidden = true;
    panelHide();
    for (const host of state.ripples.splice(0)) dropFlash(host);
    for (const key of ["cursor", "hud", "trail"]) {
      if (state[key]) state[key].host.style.display = "none";
    }
  }

  function overlayShow() {
    state.hidden = false;
    panelShow();
    if (state.cursor && state.cursor.visible) state.cursor.host.style.display = "block";
    if (state.hud && state.hud.until > Date.now()) state.hud.host.style.display = "block";
  }

  function overlayRestore() {
    motion.seg = null; motion.queue = []; motion.trail = [];
    if (motion.frame) {
      // Cancelled the way it was asked for: a timer id passed to the frame
      // canceller (or the reverse) could cancel something of the page's.
      try {
        if (rafNative && cafNative) cafNative.call(window, motion.frame);
        else clearTimeout(motion.frame);
      } catch (error) {}
      motion.frame = 0;
    }
    for (const host of state.ripples.splice(0)) dropFlash(host);
    for (const key of ["cursor", "hud", "trail"]) {
      const entry = state[key];
      if (!entry) continue;
      if (entry.timer) { try { clearTimeout(entry.timer); } catch (error) {} }
      dropFlash(entry.host);
    }
    state.cursor = undefined; state.hud = null; state.trail = null;
    panelRestore();
  }

__WSN_PRESENCE_PANEL__
"""
SOURCE = SOURCE.replace("__WSN_PRESENCE_PANEL__", presence_panel.SOURCE)
