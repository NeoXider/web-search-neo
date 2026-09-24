"""Page-side evidence that an input action did something, and text-as-keys input.

Every helper here takes a driver and returns plain data; the facade decides what
to do with the answer. Nothing raises on a page that refuses a probe: a missing
observation is reported as ``None`` rather than read as "no effect", and nothing
here ever suggests repeating an action - a click that looked like it did nothing
may still have sent a form or a payment.
"""
from __future__ import annotations

from typing import Any

from web_search_neo import key_table

# Installed before a click. The probe lives under a registry Symbol as a
# non-enumerable property, so page code that walks window's keys never sees it,
# and it keeps the DOM nodes each mutation touched (bounded) so the collect step
# can count only the changes in the clicked element's subtree or its ancestors -
# a ticking clock or a rotating ad elsewhere on the page is not the click's doing.
# Our own overlays (presence flash, ghost cursor, favicon badge in <head>) are
# never recorded.
CLICK_ARM_SCRIPT = r"""
const key = Symbol.for('wsn.clickProbe');
try {
  const previous = window[key];
  if (previous && previous.observer) previous.observer.disconnect();
  const ours = node => {
    try {
      const el = node && (node.nodeType === 1 ? node : node.parentElement);
      return !!(el && el.closest && (el.closest('[data-wsn-presence]') || el.closest('head')));
    } catch (error) { return false; }
  };
  const probe = {count: 0, nodes: [], observer: null, focus: ''};
  probe.observer = new MutationObserver(records => {
    for (const record of records) {
      if (ours(record.target) || String(record.attributeName || '').startsWith('data-wsn')) continue;
      const moved = [...record.addedNodes, ...record.removedNodes];
      if (moved.length && moved.every(ours)) continue;
      probe.count += 1;
      if (probe.nodes.length < 2000) probe.nodes.push(record.target);
    }
  });
  probe.observer.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  const active = document.activeElement;
  probe.focus = active ? (active.tagName || '').toLowerCase() + '#' + (active.id || '') : '';
  Object.defineProperty(window, key, {value: probe, configurable: true, enumerable: false, writable: true});
  return {url: String(location.href), title: String(document.title), armed: true};
} catch (error) {
  return {url: String(location.href), title: String(document.title), armed: false};
}
"""

# Read after the click settled; the observer is disconnected whatever happens.
# ``arguments[0]`` is the clicked element (or null) and ``arguments[1]`` whether
# one was given; on the companion bridge the element is re-resolved from its
# selector, so "attached" means "the selector still finds an element".
CLICK_COLLECT_SCRIPT = r"""
const key = Symbol.for('wsn.clickProbe');
const probe = window[key];
try {
  const target = arguments[0];
  const active = document.activeElement;
  const focus = active ? (active.tagName || '').toLowerCase() + '#' + (active.id || '') : '';
  let attached = null;
  try { attached = target ? !!target.isConnected : (arguments[1] ? false : null); } catch (error) { attached = null; }
  let scoped = false;
  try { scoped = !!(target && target.getRootNode && target.getRootNode() !== document); } catch (error) { scoped = true; }
  let near = null;
  if (probe && target && attached && !scoped) {
    near = probe.nodes.filter(node => {
      try { return node === target || target.contains(node) || node.contains(target); }
      catch (error) { return false; }
    }).length;
  }
  return {
    mutations: probe ? probe.count : null, near_target: near, scoped: scoped,
    focus_before: probe ? probe.focus : null, focus_after: focus,
    focus_on_target: !!(target && active === target),
    focused_element: active && active !== document.body ? {
      tag: (active.tagName || '').toLowerCase(), id: active.id || '',
      role: (active.getAttribute && active.getAttribute('role')) || ''
    } : null,
    target_attached: attached,
    dialog_open: !!document.querySelector('dialog[open], [role="dialog"]:not([aria-hidden="true"]), [aria-modal="true"]')
  };
} finally {
  if (probe && probe.observer) probe.observer.disconnect();
  try { delete window[key]; } catch (error) { /* non-configurable on an odd page */ }
}
"""


def arm_click_probe(driver: Any) -> dict[str, Any]:
    """Install the mutation counter; returns the pre-click url/title (``{}`` on failure)."""
    try:
        state = driver.execute_script(CLICK_ARM_SCRIPT)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


CLICK_DISARM_SCRIPT = r"""
const key = Symbol.for('wsn.clickProbe');
const probe = window[key];
if (probe && probe.observer) probe.observer.disconnect();
try { delete window[key]; } catch (error) { /* ignore */ }
return true;
"""


def disarm_click_probe(driver: Any) -> None:
    """Stop and remove a probe whose click never happened. Never raises."""
    try:
        driver.execute_script(CLICK_DISARM_SCRIPT)
    except Exception:
        pass


def _collect(driver: Any, element: Any) -> dict[str, Any]:
    try:
        evidence = driver.execute_script(CLICK_COLLECT_SCRIPT, element, element is not None) or {}
    except Exception as exc:
        if element is None or "stale" not in f"{type(exc).__name__} {exc}".lower():
            return {}
        # A stale handle is the answer: the click removed its target.
        try:
            evidence = dict(driver.execute_script(CLICK_COLLECT_SCRIPT, None, False) or {})
        except Exception:
            return {}
        evidence["target_attached"] = False
    return evidence if isinstance(evidence, dict) else {}


_NO_REPEAT = (
    "Do not repeat the click on this evidence alone: a submit, order or payment may "
    "already be on its way. Read page_text/page_elements (and network) first."
)


def collect_click_effects(
    driver: Any, element: Any, pre: dict[str, Any], post: dict[str, Any],
    *, measurable: bool = True,
) -> dict[str, Any]:
    """Fold the page evidence into ``page_changed``/``effect_detected``/``verified``.

    ``verified`` is true when the URL or title moved, the clicked element's
    subtree or ancestors mutated, focus moved, or the element left the document;
    false only when the probe watched the whole time, the element is in the top
    document (not a frame or shadow root) and none of that happened; ``None``
    whenever the evidence cannot decide - a frame or shadow target
    (``measurable=False``), a probe that did not survive, a navigation mid-read.
    """
    page_changed: bool | None = None
    if pre:
        page_changed = (post.get("url") != pre.get("url")) or (post.get("title") != pre.get("title"))
    evidence = _collect(driver, element) if pre.get("armed") else {}
    near = evidence.get("near_target")
    # A mouse click focuses the button it lands on; that alone is not an effect.
    focus_moved = (
        evidence.get("focus_before") is not None
        and evidence.get("focus_before") != evidence.get("focus_after")
        and not evidence.get("focus_on_target")
    )
    detached = evidence.get("target_attached") is False
    measurable = measurable and not evidence.get("scoped")
    fields: dict[str, Any] = {
        "page_changed": page_changed,
        "post_state": {
            "dom_mutations": evidence.get("mutations"),
            "dom_mutations_near_target": near,
            "focus_changed": focus_moved if evidence else None,
            "focused_element": evidence.get("focused_element"),
            "target_still_attached": evidence.get("target_attached"),
            "dialog_open": evidence.get("dialog_open"),
        },
    }
    if not measurable:
        effect: bool | None = True if page_changed else None
    elif page_changed is True or bool(near) or focus_moved or detached:
        effect = True
    elif near == 0 and evidence.get("target_attached") is True and page_changed is False:
        effect = False
    else:
        effect = None
    fields["effect_detected"] = effect
    fields["verified"] = effect
    if effect is False:
        fields["no_observable_change"] = True
        fields["change_note"] = (
            "Nothing observable changed around the clicked element: same URL and "
            "title, no DOM change in it or its ancestors, focus did not move, and it "
            "is still in place. " + _NO_REPEAT
        )
    elif effect is None and page_changed is False:
        fields["no_observable_change"] = True if not evidence else None
        fields["change_note"] = (
            "URL and title are unchanged and the click's effect could not be measured "
            "(frame or shadow target, or the probe did not survive); confirm it with "
            "page_text/page_elements. " + _NO_REPEAT
        )
    return {key: value for key, value in fields.items() if value is not None or key in {
        "page_changed", "effect_detected", "verified"}}


# Whether the focused element can take typed text. The walk descends through open
# shadow roots and same-origin frames to the element that really has focus (a
# web-component editor, TinyMCE's iframe, an itch.io game frame); a cross-origin
# frame cannot be looked into, so the answer there is "unknown" (editable: null),
# never a refusal.
FOCUSED_EDITABLE_SCRIPT = r"""
const answer = {editable: false, tag: '', in_frame: false, focused: false};
let el = document.activeElement;
for (let depth = 0; el && depth < 12; depth += 1) {
  if (el.shadowRoot && el.shadowRoot.activeElement) {
    el = el.shadowRoot.activeElement;
    answer.in_shadow = true;
    continue;
  }
  const tag = (el.tagName || '').toLowerCase();
  if (tag !== 'iframe' && tag !== 'frame') break;
  answer.in_frame = true;
  let inner = null;
  try { inner = el.contentDocument; } catch (error) { inner = null; }
  if (!inner) return {editable: null, tag: tag, in_frame: true, cross_origin: true, focused: true};
  el = inner.activeElement;
}
if (!el) return answer;
const doc = el.ownerDocument || document;
const tag = (el.tagName || '').toLowerCase();
answer.tag = tag;
const designed = doc.designMode === 'on' || !!el.isContentEditable;
if ((el === doc.body || el === doc.documentElement) && !designed) return answer;
answer.focused = true;
// A custom element whose shadow root is closed (or open but not telling) hides
// where focus really is - a web-component editor or game surface - so it is
// "unknown", never "not editable".
if (tag.indexOf('-') > 0 && !(el.shadowRoot && el.shadowRoot.activeElement)) {
  return Object.assign(answer, {editable: null, custom_element: true});
}
answer.tabindex = el.hasAttribute('tabindex');
answer.control = ['button', 'a', 'summary', 'select', 'option'].includes(tag)
  || ['button', 'link', 'menuitem', 'tab', 'checkbox', 'radio', 'switch'].includes(String(el.getAttribute('role') || ''))
  || (tag === 'input' && ['button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'file'].includes(
    String(el.getAttribute('type') || '').toLowerCase()));
const type = String(el.getAttribute('type') || 'text').toLowerCase();
const role = String(el.getAttribute('role') || '');
const textual = tag === 'textarea' || (tag === 'input' && !['checkbox', 'radio', 'button',
  'submit', 'reset', 'file', 'image', 'range', 'color', 'hidden'].includes(type));
answer.type = type;
answer.canvas = tag === 'canvas';
answer.readonly = !!(el.readOnly || el.disabled || el.getAttribute('aria-readonly') === 'true'
  || el.getAttribute('aria-disabled') === 'true');
answer.editable = !!(textual || designed || ['textbox', 'searchbox', 'combobox'].includes(role));
return answer;
"""


def focused_editable(driver: Any) -> dict[str, Any] | None:
    """Describe the focused element, or ``None`` when the page could not be asked."""
    try:
        state = driver.execute_script(FOCUSED_EDITABLE_SCRIPT)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


# React (and Preact/Solid adapters that copy it) keeps a private "last value it
# saw" tracker on every controlled input and only fires onChange when the DOM
# value differs from it. A write that reaches the DOM without reaching the
# tracker's event path leaves the app believing the field is still empty. When
# the tracker disagrees with the DOM, reset it and raise one bubbling input
# event: React then sees exactly one real change. When they agree - the common
# case - nothing is dispatched at all.
REACT_SYNC_SCRIPT = r"""
const el = arguments[0];
if (!el) return {synced: false, reason: 'no element'};
const tracker = el._valueTracker;
if (!tracker || typeof tracker.getValue !== 'function') return {synced: false, reason: 'no tracker'};
const current = String(el.value == null ? '' : el.value);
if (tracker.getValue() === current) return {synced: false, reason: 'in sync'};
tracker.setValue(current === '' ? '\u0000' : '');
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
return {synced: true};
"""


def sync_framework_value(driver: Any, element: Any) -> bool:
    """Make a React-style controlled input notice a value it missed; true when nudged."""
    try:
        outcome = driver.execute_script(REACT_SYNC_SCRIPT, element) or {}
    except Exception:
        return False
    return bool(isinstance(outcome, dict) and outcome.get("synced"))


MAX_KEY_TEXT = 500
_TEXT_KEY_NAMES = {"\n": "ENTER", "\r": None, "\t": "TAB", " ": "SPACE"}


def text_key_events(text: str) -> list[dict[str, Any]]:
    """One real key down/up pair per character, for canvas apps that read keydown.

    ``Input.insertText`` never produces keydown/keypress, and a Unity WebGL or
    other canvas engine that builds its text from key events receives nothing
    from it. Here every character is its own key press carrying the character as
    ``key``/``text`` - including non-Latin letters such as Cyrillic, which have
    no US-layout physical key and are sent with an empty ``code``. An upper-case
    Latin letter is wrapped in Shift so the key reports the capital, exactly as a
    keyboard would; everything else is sent as the character itself.
    """
    events: list[dict[str, Any]] = []
    shift = key_table.SELENIUM_KEYS["SHIFT"]
    for char in text:
        if char in _TEXT_KEY_NAMES:
            name = _TEXT_KEY_NAMES[char]
            if name is None:
                continue
            key = key_table.SELENIUM_KEYS[name]
            events += [{"type": "down", "key": key}, {"type": "up", "key": key}]
            continue
        if "A" <= char <= "Z":
            events += [
                {"type": "down", "key": shift}, {"type": "down", "key": char},
                {"type": "up", "key": char}, {"type": "up", "key": shift},
            ]
            continue
        events += [{"type": "down", "key": char}, {"type": "up", "key": char}]
    return events
