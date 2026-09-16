"""Activity badge lifecycle, independent of the session registry."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_TAB_ACTIVITY_IDLE_SECONDS = 300.0
_TAB_ACTIVITY_PING_INTERVAL = 60.0
_TAB_ACTIVITY_SOURCE = r"""
(() => {
  const KEY = "__wsnActivity";
  const IDLE_MS = 5 * 60 * 1000;
  if (window[KEY] && window[KEY].ping) {
    try { window[KEY].ping(); } catch (error) {}
    return;
  }
  const state = {links: [], timer: 0, generation: 0, stopped: false};
  window[KEY] = state;
  const robotIcon = () => "data:image/svg+xml," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    + '<circle cx="32" cy="32" r="30" fill="#22c55e"/>'
    + '<circle cx="22" cy="26" r="5" fill="#fff"/><circle cx="42" cy="26" r="5" fill="#fff"/>'
    + '<rect x="20" y="40" width="24" height="5" rx="2.5" fill="#fff"/></svg>');
  const snapshot = () => {
    try {
      state.links = Array.from(
        document.querySelectorAll('link[rel~="icon"]:not([data-wsn-activity])')).map(el => el.href || "");
    } catch (error) { state.links = []; }
  };
  const applyIcon = (href, generation) => {
    if (state.stopped || generation !== state.generation) return;
    try {
      const head = document.head || document.documentElement;
      if (!head) return;
      let link = document.querySelector('link[data-wsn-activity="1"]');
      if (!link) {
        link = document.createElement("link");
        link.setAttribute("rel", "icon");
        link.setAttribute("data-wsn-activity", "1");
        head.appendChild(link);
      }
      link.href = href;
    } catch (error) {}
  };
  const restore = () => {
    state.generation += 1;
    try {
      const badge = document.querySelector('link[data-wsn-activity="1"]');
      if (badge && badge.parentNode) badge.parentNode.removeChild(badge);
    } catch (error) {}
    state.links = [];
  };
  const setBadge = generation => {
    if (state.stopped || generation !== state.generation) return;
    // The presence script owns the favicon when it is installed; painting a
    // second badge over its icon made the tab flicker between the two.
    if (window.__wsnPresence) { restore(); return; }
    try {
      snapshot();
      const first = state.links.length ? state.links[0] : "";
      if (!first) { applyIcon(robotIcon(), generation); return; }
      const img = new Image();
      img.onload = () => {
        try {
          const canvas = document.createElement("canvas");
          canvas.width = 64; canvas.height = 64;
          const ctx = canvas.getContext("2d");
          if (!ctx) return;
          ctx.drawImage(img, 0, 0, 64, 64);
          ctx.fillStyle = "#22c55e";
          ctx.beginPath(); ctx.arc(50, 50, 15, 0, 7); ctx.fill();
          ctx.fillStyle = "#ffffff";
          ctx.font = "bold 22px sans-serif";
          ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillText("\u25CF", 50, 51);
          applyIcon(canvas.toDataURL(), generation);
        } catch (error) {}
      };
      img.onerror = () => {};
      img.src = first;
    } catch (error) {}
  };
  state.ping = () => {
    try {
      if (state.stopped) return;
      if (state.timer) clearTimeout(state.timer);
      const generation = ++state.generation;
      const run = () => { setBadge(generation); };
      if (document.readyState === "loading" && !document.head) {
        document.addEventListener("DOMContentLoaded", run, {once: true});
      } else {
        run();
      }
      state.timer = setTimeout(restore, IDLE_MS);
    } catch (error) {}
  };
  state.stop = () => {
    state.stopped = true;
    try { if (state.timer) clearTimeout(state.timer); } catch (error) {}
    state.timer = 0;
    restore();
    try { delete window[KEY]; } catch (error) {}
  };
  state.ping();
})();
"""
_TAB_ACTIVITY_PING_SCRIPT = (
    "(() => { const a = window.__wsnActivity;"
    " if (a && a.ping) a.ping(); return true; })()"
)
_TAB_ACTIVITY_STOP_SCRIPT = (
    "(() => { const a = window.__wsnActivity;"
    " if (a && a.stop) a.stop(); return true; })()"
)


def _apply_tab_activity(
    session: Any, session_id: str, *, label_tab: bool = True, enabled: bool = True
) -> None:
    """Badge one session's tab with the agent-activity dot, when it can show.

    Headless tabs have no visible favicon, and ``label_tab=False`` means the
    caller asked visuals to be left alone - both skip silently. Otherwise the
    badge script is registered for future documents and run in the live one,
    exactly like the tab-strip label. Caller holds the session lock.
    """
    if session.headless or not label_tab or not enabled:
        _remove_tab_activity(session)
        return
    if session.tab_activity_script_id:
        return
    try:
        result = session.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": _TAB_ACTIVITY_SOURCE}
        )
    except Exception as exc:
        logger.warning(
            "Tab activity badge for session '%s' was skipped: %s: %s",
            session_id,
            type(exc).__name__,
            exc,
        )
        return
    session.tab_activity_script_id = str((result or {}).get("identifier") or "")
    session.activity_pinged_at = time.monotonic()
    try:
        session.driver.execute_script(_TAB_ACTIVITY_SOURCE)
    except Exception:
        pass


def _remove_tab_activity(session: Any) -> None:
    """Stop badging one session's tab and restore its favicon.

    Best-effort throughout, like the label removal: teardown paths call this
    when the driver may already be half gone, and a badge left behind restores
    itself after five quiet minutes anyway. Caller holds the session lock.
    """
    driver = session.driver
    if not session.tab_activity_script_id:
        return
    try:
        driver.execute_script(_TAB_ACTIVITY_STOP_SCRIPT)
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd(
            "Page.removeScriptToEvaluateOnNewDocument",
            {"identifier": session.tab_activity_script_id},
        )
    except Exception:
        pass
    session.tab_activity_script_id = None


def _ping_tab_activity(session: Any) -> None:
    """Re-arm one session's badge when the server last pinged over a minute ago.

    Called from the page summary every action already takes, so pings ride the
    round-trips that exist rather than adding their own. The five-minute expiry
    lives page-side: a session that stops acting stops glowing with no call.
    Never raises; a page that refuses the ping simply keeps its last state.
    """
    if not session.tab_activity_script_id:
        return
    now = time.monotonic()
    if now - session.activity_pinged_at < _TAB_ACTIVITY_PING_INTERVAL:
        return
    session.activity_pinged_at = now
    try:
        session.driver.execute_script(_TAB_ACTIVITY_PING_SCRIPT)
    except Exception:
        pass
