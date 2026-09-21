"""Browser-side implementation shared by the legacy facade."""

from __future__ import annotations
from typing import Any
from web_search_neo import page_perception

_INSPECT_SCRIPT = page_perception.JS_LIBRARY + r"""
const limit = arguments[0];
const offset = arguments[1];
const includeLinks = arguments[2];
const includeForms = arguments[3];
const includeButtons = arguments[4];
const filters = arguments[5] || {};
const selectedCategory = filters.category || 'all';
const enabledCategory = name => selectedCategory === 'all' ? name !== 'interactive' : selectedCategory === name;
const lower = value => String(value || '').toLocaleLowerCase();
function matches(el, name) {
  if (filters.visible_only && wsnHiddenReason(el)) return false;
  if (filters.enabled_only && (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.closest('[inert]'))) return false;
  const role = wsnRole(el);
  if (filters.role && lower(role) !== lower(filters.role)) return false;
  if (filters.text_pattern) {
    const text = [wsnName(el, role), el.innerText, el.getAttribute('placeholder'), labelFor(el)].join(' ');
    if (!lower(text).includes(lower(filters.text_pattern))) return false;
  }
  if (filters.href_pattern && ['links', 'buttons', 'interactive'].includes(name)
      && !lower(el.href).includes(lower(filters.href_pattern))) return false;
  return true;
}

// A web component keeps its controls in a shadow root and an embedded form keeps
// them in another document, and `document.querySelectorAll` sees neither. That
// returned an empty page for an app that plainly has buttons on it, which is the
// first call the built-in recipes make.
// `depth` counts frames only, and stops where every other topic stops: a walk
// that reached deeper than the outline reported controls the outline denied, and
// one that stopped shallower hid controls the outline had already handed out.
let framesTooDeep = 0;
const WSN_ELEMENT_COLLECT_LIMIT = 20000;
const collectorTruncated = {links: false, forms: false, fields: false, buttons: false, iframes: false};

function collect(selectors) {
  const found = [];
  const seen = new Set();
  let truncated = false;
  const walk = (root, depth) => {
    if (truncated) return;
    let matched;
    try {
      matched = root.querySelectorAll(selectors);
    } catch (error) {
      matched = [];
    }
    for (const el of matched) {
      if (seen.has(el)) continue;
      if (found.length >= WSN_ELEMENT_COLLECT_LIMIT) {
        truncated = true;
        break;
      }
      seen.add(el);
      found.push(el);
    }
    if (truncated) return;
    let all;
    try {
      all = root.querySelectorAll('*');
    } catch (error) {
      return;
    }
    for (const el of all) {
      const shadow = wsnShadowRoot(el);
      if (shadow) walk(shadow, depth);
      if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
        if (depth >= WSN_MAX_FRAME_DEPTH) {
          framesTooDeep += 1;
          continue;
        }
        let doc = null;
        try {
          doc = el.contentDocument;
        } catch (error) {
          doc = null;
        }
        if (doc) walk(doc, depth + 1);
      }
    }
  };
  walk(document, 0);
  return {items: found, truncated: truncated};
}

function category(name, selectors) {
  const result = collect(selectors);
  collectorTruncated[name] = result.truncated;
  return order(result.items.filter(el => matches(el, name)));
}

function visibility(el) {
  const reason = wsnHiddenReason(el);
  return {visible: !reason, hidden_reason: reason};
}

// Something nothing can reach must not push a usable control past the limit.
function order(elements) {
  const visible = [];
  const hidden = [];
  for (const el of elements) (wsnHiddenReason(el) ? hidden : visible).push(el);
  return visible.concat(hidden);
}

function labelFor(el) {
  if (el.labels && el.labels.length) return (el.labels[0].innerText || '').trim();
  let parent = null;
  try {
    parent = el.closest ? el.closest('label') : null;
  } catch (error) {
    parent = null;
  }
  return parent ? (parent.innerText || '').trim() : '';
}
function fieldInfo(el) {
  const result = Object.assign({
    selector: wsnSelector(el), tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    id: el.id || '', name: el.getAttribute('name') || '',
    label: labelFor(el), placeholder: el.getAttribute('placeholder') || '',
    required: !!el.required, disabled: !!el.disabled
  }, visibility(el));
  if (el.tagName.toLowerCase() === 'select') {
    result.options = Array.from(el.options).map(o => ({value: o.value, text: o.text, selected: o.selected}));
  }
  return result;
}
const output = {links: [], forms: [], fields: [], buttons: [], iframes: []};
const counts = {links: 0, forms: 0, fields: 0, buttons: 0, iframes: 0};
const FIELD_SELECTOR = 'input, textarea, select, [contenteditable="true"]';
if (includeLinks && enabledCategory('links')) {
  const links = category('links', 'a[href]');
  counts.links = links.length;
  output.links = links.slice(offset, offset + limit).map(a => Object.assign({
    selector: wsnSelector(a),
    // An icon-only control renders no text; its title is the only name the
    // caller can hover or match by, so it stands in for the missing words.
    text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(),
    href: a.href
  }, visibility(a)));
}
if (includeForms && (enabledCategory('forms') || enabledCategory('fields'))) {
  const forms = category('forms', 'form');
  counts.forms = enabledCategory('forms') ? forms.length : 0;
  output.forms = (enabledCategory('forms') ? forms : []).slice(offset, offset + limit).map((form, index) => Object.assign({
    index: offset + index, selector: wsnSelector(form), id: form.id || '',
    name: form.getAttribute('name') || '',
    action: form.action, method: (form.method || 'get').toLowerCase(), enctype: form.enctype,
    fields: Array.from(form.querySelectorAll(FIELD_SELECTOR)).slice(0, limit).map(fieldInfo)
  }, visibility(form)));
  const fields = category('fields', FIELD_SELECTOR);
  counts.fields = enabledCategory('fields') ? fields.length : 0;
  output.fields = (enabledCategory('fields') ? fields : []).slice(offset, offset + limit).map(fieldInfo);
}
if (includeButtons && enabledCategory('buttons')) {
  const buttons = category('buttons',
    'button, input[type="button"], input[type="submit"], input[type="reset"], ' +
    'input[type="image"], [role="button"]'
  );
  counts.buttons = buttons.length;
  output.buttons = buttons.slice(offset, offset + limit).map(button => Object.assign({
    selector: wsnSelector(button), tag: button.tagName.toLowerCase(),
    type: (button.getAttribute('type') || '').toLowerCase(), id: button.id || '',
    name: button.getAttribute('name') || '',
    // Same icon-only rule as links above: a title-bearing button with no text
    // is otherwise reported as usable with nothing to call it by.
    text: (button.innerText || button.value || button.getAttribute('aria-label') || button.getAttribute('title') || '').trim(),
    disabled: !!button.disabled
  }, visibility(button)));
}
if (selectedCategory === 'interactive') {
  const controls = category('interactive', 'a[href], button, input:not([type="hidden"]), textarea, select, summary, [contenteditable]:not([contenteditable="false"]), [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="combobox"], [role="checkbox"], [role="radio"], [role="switch"], [role="tab"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="option"], [role="slider"], [role="spinbutton"], [tabindex]:not([tabindex="-1"])');
  counts.interactive = controls.length;
  output.interactive = controls.slice(offset, offset + limit).map(el => Object.assign({
    selector: wsnSelector(el), tag: el.tagName.toLowerCase(), role: wsnRole(el),
    name: wsnName(el, wsnRole(el)), text: (el.innerText || '').trim().slice(0, 300),
    type: el.getAttribute('type') || '', href: el.href || '',
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
  }, visibility(el)));
}
const frames = enabledCategory('iframes') ? category('iframes', 'iframe, frame') : [];
counts.iframes = frames.length;
output.iframes = frames.slice(offset, offset + limit).map(frame => Object.assign({
  selector: wsnSelector(frame), id: frame.id || '', name: frame.name || '',
  src: frame.src || '', title: frame.title || '',
  same_origin: (() => {
    try {
      return !!frame.contentDocument;
    } catch (error) {
      return false;
    }
  })()
}, visibility(frame)));
output.found = counts;
output.returned = {};
output.range = {};
for (const key of Object.keys(counts)) {
  const returned = (output[key] || []).length;
  const start = Math.min(offset, counts[key]);
  const end = start + returned;
  output.returned[key] = returned;
  output.range[key] = {
    start: start,
    end: end,
    next_offset: end < counts[key] ? end : null,
    has_more: end < counts[key]
  };
}
output.offset = offset;
output.limit = limit;
output.collector_limit = WSN_ELEMENT_COLLECT_LIMIT;
output.collector_truncated = collectorTruncated;
output.truncated = Object.keys(counts).some(key => counts[key] > (output[key] || []).length);
output.frames_too_deep = framesTooDeep;
return output;
"""


_ELEMENT_LIST_KEYS = ("links", "forms", "fields", "buttons", "iframes", "interactive")


def _restate_element_ranges(payload: dict[str, Any]) -> None:
    """Re-derive ``returned``/``range`` from what is actually in the payload.

    Called only after the character budget shortened a list, so that ``has_more``
    and ``next_offset`` describe the answer that was sent rather than the one
    that was collected.
    """
    counts = payload.get("found")
    if not isinstance(counts, dict):
        return
    offset = int(payload.get("offset") or 0)
    returned: dict[str, int] = {}
    ranges: dict[str, Any] = {}
    for key, total in counts.items():
        rows = payload.get(key)
        kept = len(rows) if isinstance(rows, list) else 0
        try:
            available = int(total)
        except (TypeError, ValueError):
            available = kept
        start = min(offset, available)
        end = start + kept
        returned[key] = kept
        ranges[key] = {
            "start": start,
            "end": end,
            "next_offset": end if end < available else None,
            "has_more": end < available,
        }
    payload["returned"] = returned
    payload["range"] = ranges
    payload["truncated"] = any(value["has_more"] for value in ranges.values())
