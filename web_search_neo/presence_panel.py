"""The status panel on a driven page: who works here, what it did, and a click to open or close it.

Spliced into the overlay's script (``presence_overlay.SOURCE``, at
``__WSN_PRESENCE_PANEL__``) and sharing its closure: ``state``, ``CONFIG``, ``clock``,
``overlayHost``, ``shadowed``, ``FONT``, ``phaseColor`` and ``markMode``.

A small panel in the bottom-left corner of the viewport while an agent or a macro
job holds the tab: the agent's name and state (working, active, idle), and - opened
- the last action and how long ago, how many actions this page has seen and over
how long, a background macro job, and for an RL environment (``env``) the step,
reward and keys. Clicking it opens or closes it; the choice is kept for the tab
(sessionStorage).

It is the one presence layer a person can click, so it must never take an agent's
click: it lets pointer events through while the agent is acting (the badge's busy
phase), it is hidden before every screenshot with the rest of the overlay, and the
page readers and the click verifier skip it (``data-wsn-presence``).
"""

PANEL_REFRESH_MS = 1000

SOURCE = r"""
  // ------------------------------------------------------------ the panel
  const PANEL_KEY = "__wsnPanelOpen";
  const panel = {entry: null, open: false, count: 0, since: 0, last: "", lastOk: true, lastAt: 0, env: null,
                 macro: "", timer: null};
  try { panel.open = sessionStorage.getItem(PANEL_KEY) === "1"; } catch (error) {}

  function agoText(ms) {
    const seconds = Math.max(0, Math.round(ms / 1000));
    if (seconds < 60) return seconds + "s";
    const minutes = Math.floor(seconds / 60);
    return minutes < 60 ? minutes + "m" : Math.floor(minutes / 60) + "h " + (minutes % 60) + "m";
  }

  function panelRow(parent, text, strong) {
    const row = document.createElement("div");
    row.style.cssText = "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:300px;" +
      (strong ? "font-weight:700;" : "font-weight:500;opacity:0.86;margin-top:2px;");
    row.textContent = text;
    parent.appendChild(row);
    return row;
  }

  function panelEntry() {
    if (panel.entry && panel.entry.host.isConnected) return panel.entry;
    const host = overlayHost("panel", 2147483645);
    if (!host) return null;
    host.style.left = "10px";
    host.style.top = "auto";
    host.style.bottom = "10px";
    host.style.width = "auto";
    host.style.height = "auto";
    host.style.cursor = "pointer";
    const box = document.createElement("div");
    box.style.cssText = "padding:4px 10px 5px 8px;border-radius:10px;color:#ffffff;" +
      "background:rgba(17,24,39,0.88);box-shadow:0 1px 5px rgba(0,0,0,0.38);user-select:none;" + FONT;
    shadowed(host).appendChild(box);
    host.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      panel.open = !panel.open;
      try { sessionStorage.setItem(PANEL_KEY, panel.open ? "1" : "0"); } catch (error) {}
      panelPaint();
    }, true);
    panel.entry = {host: host, box: box};
    return panel.entry;
  }

  function phaseWord(phase) {
    if (phase === "busy") return "working";
    if (phase === "active") return "active";
    return phase ? "idle" : "";
  }

  function panelPaint() {
    if (!state.phase) { panelRestore(); return; }
    const entry = panelEntry();
    if (!entry) return;
    const now = clock();
    const box = entry.box;
    while (box.firstChild) box.removeChild(box.firstChild);
    const head = panelRow(box, "", true);
    const dot = document.createElement("span");
    dot.style.cssText = "display:inline-block;width:8px;height:8px;margin:0 6px 1px 0;border-radius:50%;" +
      "vertical-align:middle;background:" + phaseColor(state.phase) + ";";
    head.appendChild(dot);
    const mode = markMode(Date.now());
    head.appendChild(document.createTextNode((motion.label || "agent") + " · " + phaseWord(state.phase) +
      (mode !== "agent" ? " · macro" : "") + (panel.open ? "  ▾" : "  ▸")));
    if (panel.open) {
      if (panel.last) panelRow(box, "Last: " + panel.last + (panel.lastOk ? "" : " ✕") + " · " + agoText(now - panel.lastAt) + " ago");
      if (panel.count) panelRow(box, "Actions: " + panel.count + " in " + agoText(now - panel.since));
      if (panel.macro && mode !== "agent") panelRow(box, "Macro job: " + panel.macro);
      const env = panel.env;
      if (env) {
        panelRow(box, "Env step " + env.step + " · reward " + env.reward + " (total " + env.total + ")");
        if (env.keys) panelRow(box, "Keys: " + env.keys + (env.probability !== null && env.probability !== undefined
          ? " · p " + env.probability : ""));
      }
    } else if (panel.last) {
      panelRow(box, panel.last);
    }
    // Clicks pass through while the agent is acting, so a click it aims at this corner reaches the page.
    entry.host.style.pointerEvents = state.phase === "busy" ? "none" : "auto";
    entry.host.style.display = state.hidden ? "none" : "block";
    if (!panel.timer) {
      panel.timer = setTimeout(function () { panel.timer = null; panelPaint(); }, CONFIG.panelRefreshMs);
    }
  }

  function panelNote(data) {
    if (data.label) motion.label = String(data.label);
    const now = clock();
    if (!panel.since) panel.since = now;
    if (!data.lead) {
      panel.count += 1;
      if (typeof data.caption === "string" && data.caption) {
        panel.last = data.caption;
        panel.lastOk = data.ok !== false;
      }
      panel.lastAt = now;
    }
    if (data.macro) panel.macro = String(data.macro);
    panelPaint();
  }

  state.panelStats = function (stats) {
    panel.env = stats && typeof stats === "object" ? stats : null;
    if (!panel.since) panel.since = clock();
    panelPaint();
    return true;
  };

  function panelHide() {
    if (panel.entry) panel.entry.host.style.display = "none";
  }

  function panelShow() {
    if (panel.entry && state.phase) panel.entry.host.style.display = "block";
  }

  function panelRestore() {
    if (panel.timer) { try { clearTimeout(panel.timer); } catch (error) {} panel.timer = null; }
    if (panel.entry) dropFlash(panel.entry.host);
    panel.entry = null;
  }
"""
