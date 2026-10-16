"""What's on screen right now: title, focused control, open modals.

page_text can miss a modal that arrived after the read, and a text search can
miss its buttons for the same reason. This read goes back to the live DOM and
reports only the foreground - the focused control plus every visible dialog
with its text and buttons - so the next click targets what is actually up.
"""
from __future__ import annotations

from typing import Any

from web_search_neo import browser_tools

_SCREEN_STATE_SCRIPT = """(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const nameOf = (el) => ((el.getAttribute && el.getAttribute('aria-label')) ||
    ((el.innerText || '').trim().split('\\n')[0]) || el.id || el.tagName).slice(0, 120);
  const modals = Array.from(document.querySelectorAll(
    '[role="dialog"], [role="alertdialog"], [aria-modal="true"]'
  )).filter(vis).slice(0, 5).map((m) => ({
    role: m.getAttribute('role') || 'dialog',
    name: nameOf(m),
    text: (m.innerText || '').trim().slice(0, 500),
    buttons: Array.from(m.querySelectorAll('button')).filter(vis)
      .map((b) => (b.innerText || '').trim()).filter((t) => t).slice(0, 10),
  }));
  const active = document.activeElement;
  return {
    title: document.title,
    url: location.href,
    focused: (active && active !== document.body)
      ? {tag: active.tagName, role: active.getAttribute ? active.getAttribute('role') : null,
         name: nameOf(active)}
      : null,
    modals: modals,
  };
})()"""


def read(session_id: str = "default") -> dict[str, Any]:
    """Collect the foreground state through the shared script funnel."""
    answer = browser_tools.execute_js(_SCREEN_STATE_SCRIPT, session_id=session_id)
    if not answer.get("success"):
        return answer
    value = answer.get("value") or {}
    return {
        "success": True,
        "session_id": session_id,
        "title": value.get("title"),
        "url": value.get("url"),
        "focused": value.get("focused"),
        "modals": value.get("modals") or [],
    }
