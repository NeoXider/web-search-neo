"""The tab-strip badge: the slime painted onto the favicon, in three states.

``agent_presence`` splices this script into its installer at
``__WSN_PRESENCE_BADGE__``; it shares that closure (``CONFIG``, ``state``). The
tab strip is where a person looks first, so the states are told apart by shape
and motion, not by shade alone:

* **working** - an action is in flight, or landed in the last few seconds:
  the green, awake slime inside a ring that pulses outward, about seven frames
  a second;
* **active** - the agent acted within the last minute: green and awake, and
  breathing - it bobs gently under a soft halo that swells and fades;
* **recent** - quiet for more than a minute: yellow and asleep, a small "z"
  drifting up beside it, until six minutes after the last action;
* **stale** - the agent left: a still red slime, until STALE_MS (an hour)
  after the last action, when the page's own favicon comes back. The companion
  closes a tab the agent opened at that point (its auto-close setting).

Who is on the tab is told by the shape (the owner, 2026-10-24): a **robot head**
for an agent working live, a **wrench** for a background macro job
(``presence_live.MACRO_RUN``), and the robot with a small wrench beside it when
both acted on the tab within six minutes of each other. The colour still tells
the time since the last action: green, yellow, red.

The badge survives the document: each action's time is kept in sessionStorage
(a reload or a same-site navigation picks it up at once), and the companion
hands it over after any other navigation (``resume(at)``). A page that puts
its own favicon back - a chat counting unread messages - is watched and parked
again, so the mark does not vanish under it.

Each state has its own rhythm - fast for working, slow for the others - so the
strip reads at a glance even in peripheral vision. ``prefers-reduced-motion``
gets the still version of every state (a still ring for working). The motion
stays cheap: a handful of 32x32 frames painted once per document and swapped on
one timer, which stops with the badge; Chrome's own throttling slows it further
in a background tab.

Every action also makes the slime hop once, whatever the state: a quick arc up
and a squash on landing (about a third of a second), on its own fast timer that
hands back to the state's rhythm when it ends. Reduced motion skips the hop.

A slow step announced before it runs (``presence_live.lead_action``) carries
``inFlightMs``, its own time limit, so the ring keeps pulsing through a long
wait; the step's own note afterwards ends it a few seconds later.
"""

from __future__ import annotations

from typing import Any

# How long after an action the badge keeps pulsing.
BUSY_MS = 4000
# A led step pulses for at most this long without an answer, however long its
# own time limit is: a wait that died with its caller must not pulse for ever.
IN_FLIGHT_MAX_MS = 60_000
PULSE_FRAMES = 6
PULSE_FRAME_MS = 150
# The quiet states move too, much slower: a 2 s breath, a 4 s doze.
BREATHE_FRAMES = 6
BREATHE_FRAME_MS = 340
DOZE_FRAMES = 4
DOZE_FRAME_MS = 1000
# The hop on each action: lift in canvas pixels per frame, and the squash of the
# landing frame. Short enough that quick successive actions read as a bounce.
HOP_LIFTS = (3.0, 7.0, 9.0, 7.0, 3.0, 0.0)
HOP_SQUASH = 0.8
HOP_FRAME_MS = 55
BUSY_COLOR = "#5dff8f"
# After the yellow minutes the slime turns red and stays until this long after
# the last action (matches the companion's auto-close time).
STALE_MS = 60 * 60 * 1000
STALE_COLOR = "#e5484d"


def config() -> dict[str, Any]:
    """The badge's constants, merged into the installer's CONFIG."""
    return {
        "busyMs": BUSY_MS,
        "inFlightMaxMs": IN_FLIGHT_MAX_MS,
        "pulseFrames": PULSE_FRAMES,
        "pulseFrameMs": PULSE_FRAME_MS,
        "breatheFrames": BREATHE_FRAMES,
        "breatheFrameMs": BREATHE_FRAME_MS,
        "dozeFrames": DOZE_FRAMES,
        "dozeFrameMs": DOZE_FRAME_MS,
        "hopLifts": list(HOP_LIFTS),
        "hopSquash": HOP_SQUASH,
        "hopFrameMs": HOP_FRAME_MS,
        "busyColor": BUSY_COLOR,
        "staleMs": STALE_MS,
        "staleColor": STALE_COLOR,
    }


SOURCE = r"""
  // ---------------------------------------------------------------- favicon

  state.staleUntil = 0;
  state.lastAt = 0;
  state.agentAt = 0;
  state.macroAt = 0;
  state.watch = null;
  const AT_KEY = "__wsnPresenceAt";

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

  function phaseColor(phase) {
    if (phase === "stale") return CONFIG.staleColor;
    return phase === "recent" ? CONFIG.recentColor : CONFIG.activeColor;
  }

  // Who drove the tab lately: "agent", "macro" (a background job) or "both".
  function markMode(now) {
    const agent = now - state.agentAt < CONFIG.staleMs;
    const macro = now - state.macroAt < CONFIG.staleMs;
    if (agent && macro && Math.abs(state.agentAt - state.macroAt) < CONFIG.recentMs) return "both";
    return macro && state.macroAt >= state.agentAt ? "macro" : "agent";
  }

  // A background macro's mark: a wrench, its open jaw up and right. `scale` and the
  // offsets let it sit small beside the robot when both are on the tab.
  function drawWrench(context, phase, scale, dx, dy) {
    context.save();
    context.translate(CONFIG.slimeOrigin[0] + (dx || 0), CONFIG.slimeOrigin[1] + (dy || 0));
    context.scale(CONFIG.slimeScale * (scale || 1), CONFIG.slimeScale * (scale || 1));
    const tilt = -Math.PI / 4;
    const shape = function () {
      context.beginPath();
      context.moveTo(3, 14);
      context.lineTo(9.4, 7.6);
      context.moveTo(11.2 + 2.9 * Math.cos(tilt + 0.55), 4.8 + 2.9 * Math.sin(tilt + 0.55));
      context.arc(11.2, 4.8, 2.9, tilt + 0.55, tilt - 0.55 + Math.PI * 2);
    };
    context.lineCap = "round";
    for (const [width, colour] of [[5, "rgba(255,255,255,0.9)"], [3.8, "rgba(10,30,18,0.85)"], [2.4, phaseColor(phase)]]) {
      shape();
      context.lineWidth = width;
      context.strokeStyle = colour;
      context.stroke();
    }
    context.restore();
  }

  // The agent's mark: a robot head with an antenna, same corner, coloured by the time.
  function drawRobot(context, phase, lift, squash) {
    const awake = phase !== "recent" && phase !== "stale";
    const color = phaseColor(phase);
    context.save();
    context.translate(CONFIG.slimeOrigin[0], CONFIG.slimeOrigin[1] - (lift || 0));
    context.scale(CONFIG.slimeScale, CONFIG.slimeScale);
    if (squash && squash !== 1) {
      context.translate(0, 15);
      context.scale(1 / squash, squash);
      context.translate(0, -15);
    }
    context.globalAlpha = CONFIG.slimeAlpha;
    const head = function () {
      context.beginPath();
      if (context.roundRect) context.roundRect(1.5, 5, 13, 10, 3);
      else context.rect(1.5, 5, 13, 10);
    };
    context.lineCap = "round";
    context.beginPath();
    context.moveTo(8, 5);
    context.lineTo(8, 2.4);
    context.lineWidth = 2.6;
    context.strokeStyle = "rgba(255,255,255,0.9)";
    context.stroke();
    context.lineWidth = 1.1;
    context.strokeStyle = "#10261a";
    context.stroke();
    context.beginPath();
    context.arc(8, 1.9, 1.4, 0, Math.PI * 2);
    context.fillStyle = color;
    context.fill();
    head();
    context.lineWidth = 2;
    context.strokeStyle = "rgba(255,255,255,0.9)";
    context.stroke();
    head();
    context.lineWidth = 0.8;
    context.strokeStyle = "rgba(10,30,18,0.8)";
    context.stroke();
    context.fillStyle = color;
    context.fill();
    context.fillStyle = "#10261a";
    context.strokeStyle = "#10261a";
    context.lineWidth = 1;
    for (const x of [5.4, 10.6]) {
      if (awake) {
        context.beginPath();
        context.arc(x, 9.6, 1.6, 0, Math.PI * 2);
        context.fillStyle = "#ffffff";
        context.fill();
        context.beginPath();
        context.arc(x, 9.7, 0.8, 0, Math.PI * 2);
        context.fillStyle = "#10261a";
        context.fill();
      } else {
        context.beginPath();
        context.moveTo(x - 1.4, 9.8);
        context.lineTo(x + 1.4, 9.8);
        context.stroke();
      }
    }
    context.beginPath();
    context.moveTo(6.2, 12.8);
    context.lineTo(9.8, 12.8);
    context.stroke();
    context.restore();
  }

  // "Working right now": a ring behind the slime that swells and fades over
  // the pulse frames. Frame -1 is the still ring reduced motion gets.
  function drawRing(context, frame) {
    const t = frame < 0 ? 0.3 : frame / CONFIG.pulseFrames;
    const cx = CONFIG.slimeOrigin[0] + 8 * CONFIG.slimeScale;
    const cy = CONFIG.slimeOrigin[1] + 9.5 * CONFIG.slimeScale;
    context.save();
    clipToCorner(context, 2);
    context.globalAlpha = frame < 0 ? 1 : 1 - 0.7 * t;
    context.beginPath();
    context.arc(cx, cy, (8.5 + 3 * t) * CONFIG.slimeScale, 0, Math.PI * 2);
    context.lineWidth = 3.4;
    context.strokeStyle = "rgba(10,30,18,0.75)";
    context.stroke();
    context.lineWidth = 2;
    context.strokeStyle = CONFIG.busyColor;
    context.stroke();
    context.restore();
  }

  // The glow and the ring stay in the slime's own corner: a clip to the square
  // the slime occupies (plus a margin for the glow), so they can never spill over
  // the page's icon. Painted unclipped, a filled circle of radius 10-13 on this
  // 32 px canvas covered more than half of the favicon and turned it all green.
  function clipToCorner(context, margin) {
    context.beginPath();
    context.rect(CONFIG.slimeOrigin[0] - margin, CONFIG.slimeOrigin[1] - margin,
      32 - CONFIG.slimeOrigin[0] + margin, 32 - CONFIG.slimeOrigin[1] + margin);
    context.clip();
  }

  // A soft halo behind the slime; `t` (0..1) is how far it has swollen. At
  // strip size (16 px) a glow that grows and fades is what still reads as
  // motion, where finer detail is lost. A radial gradient, strongest under the
  // slime and fully clear at the rim, rather than a flat disc.
  function drawHalo(context, color, t, base, swell) {
    const cx = CONFIG.slimeOrigin[0] + 8 * CONFIG.slimeScale;
    const cy = CONFIG.slimeOrigin[1] + 9.5 * CONFIG.slimeScale;
    const radius = (8 + 0.5 * swell * t) * CONFIG.slimeScale;
    context.save();
    clipToCorner(context, 2);
    const glow = context.createRadialGradient(cx, cy, 0, cx, cy, radius);
    glow.addColorStop(0, color);
    glow.addColorStop(1, "rgba(0,0,0,0)");
    context.globalAlpha = Math.min(1, base + 0.5 * t);
    context.beginPath();
    context.arc(cx, cy, radius, 0, Math.PI * 2);
    context.fillStyle = glow;
    context.fill();
    context.restore();
  }

  function breath(frame, frames) {
    return 0.5 - 0.5 * Math.cos((frame / frames) * Math.PI * 2);
  }

  // "Active": the green halo swells and fades, and the slime rises with it.
  function drawBreath(context, frame) {
    const t = breath(frame, CONFIG.breatheFrames);
    drawHalo(context, CONFIG.activeColor, t, 0.3, 3);
    return 1.4 * t;
  }

  // "Recent": a slower amber glow behind the sleeping slime...
  function drawDozeGlow(context, frame) {
    drawHalo(context, CONFIG.recentColor, breath(frame, CONFIG.dozeFrames), 0.15, 2);
  }

  // ...and a z drifting up and fading beside it.
  function drawDoze(context, frame) {
    const t = frame / CONFIG.dozeFrames;
    context.save();
    context.globalAlpha = 1 - 0.5 * t;
    context.font = "900 " + (9 + 3 * t).toFixed(1) + "px sans-serif";
    context.lineWidth = 2.5;
    context.strokeStyle = "rgba(10,30,18,0.9)";
    context.fillStyle = "#ffd27a";
    const x = CONFIG.slimeOrigin[0] - 5 + 3 * t;
    const y = CONFIG.slimeOrigin[1] + 5 - 6 * t;
    context.strokeText("z", x, y);
    context.fillText("z", x, y);
    context.restore();
  }

  // `key` is a state, or "<state>:<frame>" for one frame of its motion.
  function paint(key, baseImage) {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 32;
      canvas.height = 32;
      const context = canvas.getContext("2d");
      if (!context) return null;
      // No favicon at all leaves the canvas transparent: only the corner mark
      // shows, so an iconless tab gains a mark instead of a replacement.
      if (baseImage) context.drawImage(baseImage, 0, 0, 32, 32);
      const parts = String(key).split(":");
      // "<state>:hop<i>" is hop frame i over the state's still decoration.
      const hop = parts.length > 1 ? /^hop(\d+)$/.exec(parts[1]) : null;
      const frame = parts.length > 1 && !hop ? Number(parts[1]) : null;
      let lift = 0;
      let squash = 1;
      if (hop) {
        const index = Math.min(Number(hop[1]), CONFIG.hopLifts.length - 1);
        lift = CONFIG.hopLifts[index];
        if (index === CONFIG.hopLifts.length - 1) squash = CONFIG.hopSquash;
      }
      const still = hop ? 2 : frame;
      if (parts[0] === "busy") drawRing(context, hop ? 1 : frame);
      if (parts[0] === "active" && still !== null) lift += drawBreath(context, still);
      if (parts[0] === "recent" && still !== null) drawDozeGlow(context, still % CONFIG.dozeFrames);
      const mode = markMode(Date.now());
      if (mode === "macro") drawWrench(context, parts[0], 1, 0, -(lift || 0));
      else drawRobot(context, parts[0], lift, squash);
      if (mode === "both") drawWrench(context, parts[0], 0.55, -6, 7);
      if (parts[0] === "recent" && still !== null) drawDoze(context, still % CONFIG.dozeFrames);
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
    watchHead();
  }

  // A page that writes its favicon again (unread counters, a player's state) adds
  // a new icon link, and Chrome shows the newest: park it and put ours back on top.
  function watchHead() {
    if (state.watch || typeof MutationObserver !== "function" || !document.head) return;
    let queued = false;
    state.watch = new MutationObserver(function () {
      if (queued || !state.phase || !state.shown) return;
      const fresh = iconLinks().some(function (link) { return !link.hasAttribute("data-wsn-rel"); });
      if (!fresh) return;
      queued = true;
      setTimeout(function () {
        queued = false;
        const cached = state.shown && state.painted[state.shown];
        if (cached && state.phase) applyIcon(cached);
      }, 60);
    });
    try {
      state.watch.observe(document.head, {childList: true, subtree: true, attributes: true,
                                          attributeFilter: ["rel", "href"]});
    } catch (error) {
      state.watch = null;
    }
  }

  // The page's own icon, loaded once per document and shared by every state
  // and frame. undefined: not asked yet; null: the page has none, so the mark
  // goes on its own; false: unreadable, and the page's icon is left alone.
  function withBase(done) {
    if (state.base !== undefined) { done(state.base); return; }
    state.waiting.push(done);
    if (state.waiting.length > 1) return;
    const settle = function (value) {
      state.base = value;
      const waiting = state.waiting.splice(0);
      for (let index = 0; index < waiting.length; index += 1) waiting[index](value);
    };
    const declared = baseIconHref();
    let fallback = null;
    try { fallback = new URL("/favicon.ico", location.href).href; } catch (error) {}
    const candidates = [declared];
    if (fallback && fallback !== declared) candidates.push(fallback);
    const attempt = function (index) {
      const href = candidates[index];
      if (!href) {
        // Nothing readable. A page that declares an icon still has one on
        // screen (Chrome fetches it without CORS), so it stays; only a page
        // with no icon at all gets the corner mark on its own.
        settle(iconLinks().length || state.hidden.length ? false : null);
        return;
      }
      try {
        const image = new Image();
        image.crossOrigin = "anonymous";
        image.onload = function () { settle(image); };
        image.onerror = function () { attempt(index + 1); };
        image.src = href;
      } catch (error) {
        attempt(index + 1);
      }
    };
    attempt(0);
  }

  function show(key) {
    const cacheKey = markMode(Date.now()) + "/" + key;
    state.shown = cacheKey;
    const cached = state.painted[cacheKey];
    if (cached) {
      applyIcon(cached);
      return;
    }
    withBase(function (image) {
      if (image === false) return;
      const dataUrl = paint(key, image);
      if (!dataUrl) {
        state.base = false;
        return;
      }
      state.painted[cacheKey] = dataUrl;
      if (state.shown === cacheKey) applyIcon(dataUrl);
    });
  }

  function stopPulse() {
    if (state.pulse) {
      try { clearTimeout(state.pulse); } catch (error) {}
      state.pulse = null;
    }
  }

  function restoreIcon() {
    stopPulse();
    if (state.watch) {
      try { state.watch.disconnect(); } catch (error) {}
      state.watch = null;
    }
    state.staleUntil = 0;
    try { sessionStorage.removeItem(AT_KEY); } catch (error) {}
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
    state.shown = null;
  }

  function badgePhase(now) {
    if (now < state.busyUntil) return "busy";
    if (now < state.activeUntil) return "active";
    if (now < state.recentUntil) return "recent";
    return now < state.staleUntil ? "stale" : null;
  }

  function stillBadge() {
    try {
      return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (error) {
      return false;
    }
  }

  // Each state's frame count and frame time: fast for working, slow for the rest.
  function motionOf(phase) {
    if (phase === "busy") return [CONFIG.pulseFrames, CONFIG.pulseFrameMs];
    if (phase === "active") return [CONFIG.breatheFrames, CONFIG.breatheFrameMs];
    return [CONFIG.dozeFrames, CONFIG.dozeFrameMs];
  }

  // The one repeating timer: it animates whichever state the badge is in and
  // stops itself the moment that state is over (schedule() starts the next).
  function pulse() {
    state.pulse = null;
    const phase = badgePhase(Date.now());
    if (!phase || phase !== state.phase) return;
    if (phase === "stale") { show("stale"); return; }  // red is still: no timer for an idle tab
    const motion = motionOf(phase);
    state.frame = (state.frame + 1) % motion[0];
    show(phase + ":" + state.frame);
    state.pulse = setTimeout(pulse, motion[1]);
  }

  // One hop: the frames play on a fast timer in place of the state's own, then
  // the state's rhythm resumes. A note that lands mid-hop lets it finish, unless
  // it is already on its way down, so a steady stream of actions bounces on.
  function hopStep() {
    state.pulse = null;
    const phase = badgePhase(Date.now());
    if (!phase || phase !== state.phase) { state.hop = -1; return; }
    state.hop += 1;
    if (state.hop >= CONFIG.hopLifts.length) {
      state.hop = -1;
      const motion = motionOf(phase);
      show(phase + ":" + Math.max(0, state.frame));
      state.pulse = setTimeout(pulse, motion[1]);
      return;
    }
    show(phase + ":hop" + state.hop);
    state.pulse = setTimeout(hopStep, CONFIG.hopFrameMs);
  }

  function startHop() {
    if (stillBadge() || !state.phase) return;
    if (typeof state.hop === "number" && state.hop >= 0 && state.hop < CONFIG.hopLifts.length - 3) return;
    stopPulse();
    state.hop = -1;
    hopStep();
  }

  function enter(phase) {
    const changed = phase !== state.phase;
    if (changed || !phase) stopPulse();
    if (!phase) {
      restoreIcon();
      return;
    }
    const lost = !state.ownLink || !state.ownLink.isConnected;
    state.phase = phase;
    if (!stillBadge()) {
      if (changed) state.frame = -1;
      if (!state.pulse) pulse();
    } else if (changed || lost) {
      show(phase === "busy" ? "busy:-1" : phase);
    }
  }

  function schedule() {
    if (state.timer) {
      try { clearTimeout(state.timer); } catch (error) {}
      state.timer = null;
    }
    const now = Date.now();
    const phase = badgePhase(now);
    enter(phase);
    if (!phase) return;
    const end = phase === "busy" ? state.busyUntil : (phase === "active" ? state.activeUntil
      : (phase === "recent" ? state.recentUntil : state.staleUntil));
    state.timer = setTimeout(function () {
      state.timer = null;
      schedule();
    }, Math.max(50, end - now));
  }

  // Every note lights the badge: working now (longer when a slow step says
  // how long it may take), green for the minute, amber for the five.
  function badgeNote(data) {
    const now = Date.now();
    const flight = typeof data.inFlightMs === "number" && isFinite(data.inFlightMs)
      ? Math.max(0, Math.min(data.inFlightMs, CONFIG.inFlightMaxMs)) : 0;
    state.busyUntil = now + flight + CONFIG.busyMs;
    state.activeUntil = Math.max(now + CONFIG.activeMs, state.busyUntil);
    state.recentUntil = Math.max(now + CONFIG.recentMs, state.activeUntil);
    state.staleUntil = Math.max(now + CONFIG.staleMs, state.recentUntil);
    state.lastAt = now;
    if (data.macro) state.macroAt = now; else state.agentAt = now;
    try { sessionStorage.setItem(AT_KEY, state.agentAt + "|" + state.macroAt); } catch (error) {}
    schedule();
    startHop();
  }

  // The time of the agent's last action, from the companion after a navigation or
  // from this tab's sessionStorage after a reload: the badge picks up where it was.
  state.resume = function (at, mode) {
    const time = Number(at);
    if (!isFinite(time) || time <= state.lastAt || Date.now() - time >= CONFIG.staleMs) return false;
    state.lastAt = time;
    if (mode === "macro") state.macroAt = Math.max(state.macroAt, time);
    else state.agentAt = Math.max(state.agentAt, time);
    state.activeUntil = time + CONFIG.activeMs;
    state.recentUntil = time + CONFIG.recentMs;
    state.staleUntil = time + CONFIG.staleMs;
    schedule();
    return true;
  };

  (function resumeFromStorage() {
    let saved = null;
    try { saved = sessionStorage.getItem(AT_KEY); } catch (error) { saved = null; }
    if (!saved) return;
    const parts = String(saved).split("|").map(Number);
    const go = function () {
      try {
        // The older first: resume takes only a time newer than the last one it took.
        const marks = [[parts[0] || 0, "agent"], [parts[1] || 0, "macro"]].sort(function (a, b) { return a[0] - b[0]; });
        for (const [time, mode] of marks) if (time > 0) state.resume(time, mode);
      } catch (error) {}
    };
    if (document.head) go();
    else document.addEventListener("DOMContentLoaded", go, {once: true});
  })();
"""
