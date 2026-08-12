"""Page perception: accessibility outline, readable text, and semantic element search.

Every public helper takes a WebDriver-shaped ``driver`` as its first argument and
only uses ``execute_script`` / ``execute_cdp_cmd``, so Selenium's ``webdriver.Chrome``
and ``chrome_bridge.ChromeBridgeDriver`` are equally supported. Each call is a single
round-trip: the page-side script does the whole traversal and scoring, Python only
formats the result. User input is never concatenated into a script; it always travels
as an ``execute_script`` argument.
"""

from __future__ import annotations

import json
import re
from typing import Any


# ``ref:<epoch>:<N>`` is the only handle that resolves; ``ref:N`` still parses so
# that an old transcript gets a real explanation instead of a CSS syntax error.
# The epoch is minted as lowercase hex, so a handle is lowercased before it is
# compared - an uppercase copy would otherwise never match and silently miss.
REF_PATTERN = re.compile(r"^ref:(?:([0-9a-fA-F]{8,64}):)?(\d+)$")
_LEGACY_REF_HINT = (
    "Element handle '{handle}' carries no document epoch. Ref numbers restart at 1 "
    "in every document, so a bare 'ref:N' could name a different element than the "
    "one you read. Read the page again with web_info(topic='page_outline') and use "
    "the 'ref:<epoch>:N' handle it reports."
)
_EMPTY_SEGMENT_HINT = (
    "Piercing path '{selector}' has an empty segment. Write it as 'host >>> inner', "
    "with a selector on both sides of every ' >>> '."
)
_PIERCING_SEPARATOR = " >>> "
_MAX_OUTLINE_NODES = 1000
_MAX_FIND_MATCHES = 25
_MAX_TEXT_CHARS = 200000


# ---------------------------------------------------------------------------
# Element reference registry
# ---------------------------------------------------------------------------

REF_REGISTRY_SCRIPT = r"""
(() => {
  const current = window.__wsnRefs;
  if (!current || !current.nodes || !current.byNode) {
    const bytes = new Uint8Array(8);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let index = 0; index < bytes.length; index += 1) {
        bytes[index] = Math.floor(Math.random() * 256);
      }
    }
    let epoch = '';
    for (const byte of bytes) epoch += byte.toString(16).padStart(2, '0');
    window.__wsnRefs = {
      epoch: epoch,
      nodes: new Map(),
      next: 1,
      byNode: new WeakMap(),
      closedShadowRoots: 0
    };
  }
  // Closed shadow roots are unreachable from JavaScript, so the only honest way to
  // report them is to count them while they are being created.
  const attachShadow = Element.prototype.attachShadow;
  if (attachShadow && !attachShadow.__wsnPatched) {
    const patched = function (init) {
      if (init && init.mode === 'closed' && window.__wsnRefs) {
        window.__wsnRefs.closedShadowRoots += 1;
      }
      return attachShadow.apply(this, arguments);
    };
    patched.__wsnPatched = true;
    Element.prototype.attachShadow = patched;
  }
})();
"""


def parse_ref(ref_id: int | str) -> tuple[str, int]:
    """Split a ``ref:<epoch>:<N>`` handle into its epoch and its number.

    A handle without an epoch - the legacy ``ref:N``, or a bare number - is
    rejected rather than answered from the current document: it would resolve to
    whichever element happens to hold that number now, which is exactly the
    wrong-element bug the epoch exists to prevent.
    """
    if isinstance(ref_id, bool):
        raise ValueError("ref_id must be a 'ref:<epoch>:N' string")
    if isinstance(ref_id, str):
        match = REF_PATTERN.match(ref_id.strip())
        if match:
            epoch, number = match.group(1), int(match.group(2))
        else:
            epoch, number = None, int(ref_id.strip())
    else:
        epoch, number = None, int(ref_id)
    if number < 1:
        raise ValueError("ref_id must be a positive integer")
    if not epoch:
        raise ValueError(_LEGACY_REF_HINT.format(handle=str(ref_id).strip()))
    return epoch.lower(), number


def ref_expression(ref_id: int | str) -> str:
    """Return the JS expression that resolves a ref handle back to its element.

    Ref numbers restart at 1 in every document, so the epoch carried by the handle
    is compared with the live registry before the lookup: a handle minted on a page
    that has since been replaced resolves to ``null`` instead of to whatever element
    now happens to hold that number.
    """
    epoch, number = parse_ref(ref_id)
    return (
        "(() => {"
        "const registry = window.__wsnRefs;"
        "if (!registry || !registry.nodes) return null;"
        f"if (String(registry.epoch).toLowerCase() !== {json.dumps(epoch, ensure_ascii=True)})"
        " return null;"
        f"const node = registry.nodes.get({number});"
        # A ref outlives its element; a detached node would accept actions that no
        # user could ever perform on the page they are looking at.
        "return node && node.isConnected ? node : null;"
        "})()"
    )


def register_ref_registry(driver: Any) -> dict[str, Any]:
    """Install the ref registry in every future document and in the current one."""
    identifier = None
    try:
        response = driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": REF_REGISTRY_SCRIPT},
        )
        if isinstance(response, dict):
            identifier = response.get("identifier")
    except Exception as exc:  # CDP is optional; the lazy installer still works.
        return {"registered": False, "error": f"{type(exc).__name__}: {exc}"}
    epoch = driver.execute_script(_EPOCH_SCRIPT)
    return {"registered": True, "identifier": identifier, "dom_epoch": epoch}


# ---------------------------------------------------------------------------
# Shared page-side helper library
# ---------------------------------------------------------------------------

_JS_LIB = (
    "function wsnRegistry() {\n"
    + REF_REGISTRY_SCRIPT
    + "\n  return window.__wsnRefs;\n}\n"
    + r"""
const WSN_INTERACTIVE_ROLES = new Set([
  'link', 'button', 'checkbox', 'radio', 'combobox', 'listbox', 'textbox', 'searchbox',
  'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'switch', 'option', 'slider',
  'spinbutton', 'disclosure', 'file'
]);
const WSN_LANDMARK_ROLES = new Set([
  'navigation', 'main', 'banner', 'contentinfo', 'complementary', 'form', 'search',
  'region', 'dialog'
]);
const WSN_STRUCTURAL_ROLES = new Set([
  'main', 'navigation', 'banner', 'contentinfo', 'complementary', 'form', 'search',
  'region', 'table', 'iframe', 'canvas', 'dialog', 'list', 'article'
]);
const WSN_CANDIDATE_SELECTOR = [
  'a[href]', 'button', 'input', 'select', 'textarea', 'label', 'summary', 'details',
  '[role]', '[aria-label]', '[aria-labelledby]', '[contenteditable]', '[tabindex]',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'nav', 'main', 'header', 'footer', 'aside',
  'form', 'dialog', 'canvas', 'iframe', 'table', 'img[alt]'
].join(', ');
const WSN_SKIP_TAGS = new Set([
  'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'TITLE', 'META', 'LINK', 'BASE',
  'SVG', 'PATH', 'DEFS', 'SYMBOL', 'USE'
]);
const WSN_BLOCK_TAGS = new Set([
  'ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'BODY', 'DD', 'DETAILS', 'DIALOG',
  'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'HEADER',
  'HGROUP', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'SUMMARY', 'TABLE', 'TR', 'UL'
]);
const WSN_NAME_LIMIT = 80;
const WSN_VALUE_LIMIT = 60;
const WSN_TEXT_LIMIT = 200;

function wsnClean(value, limit) {
  const text = String(value === null || value === undefined ? '' : value)
    .replace(/\s+/g, ' ')
    .trim();
  if (!limit || text.length <= limit) return text;
  return text.slice(0, limit - 1).trim() + '…';
}

function wsnAttr(el, attribute) {
  if (!el.getAttribute) return '';
  const value = el.getAttribute(attribute);
  return value === null || value === undefined ? '' : String(value);
}

function wsnRole(el) {
  const explicit = wsnAttr(el, 'role').trim().toLowerCase().split(/\s+/)[0];
  if (explicit) {
    if (explicit === 'presentation' || explicit === 'none') return 'generic';
    return explicit;
  }
  const tag = el.tagName.toLowerCase();
  const type = wsnAttr(el, 'type').toLowerCase();
  if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
  if (tag === 'area') return el.hasAttribute('href') ? 'link' : 'generic';
  if (tag === 'button') return 'button';
  if (tag === 'input') {
    if (type === 'hidden') return 'hidden';
    if (type === 'button' || type === 'submit' || type === 'reset' || type === 'image') {
      return 'button';
    }
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'file') return 'file';
    if (type === 'range') return 'slider';
    if (type === 'search') return 'searchbox';
    return 'textbox';
  }
  if (tag === 'select') {
    return el.multiple || (el.size && el.size > 1) ? 'listbox' : 'combobox';
  }
  if (tag === 'textarea') return 'textbox';
  if (/^h[1-6]$/.test(tag)) return 'heading';
  if (tag === 'nav') return 'navigation';
  if (tag === 'main') return 'main';
  if (tag === 'header') return 'banner';
  if (tag === 'footer') return 'contentinfo';
  if (tag === 'aside') return 'complementary';
  if (tag === 'form') return 'form';
  if (tag === 'dialog') return 'dialog';
  if (tag === 'summary') return 'disclosure';
  if (tag === 'canvas') return 'canvas';
  if (tag === 'iframe' || tag === 'frame') return 'iframe';
  if (tag === 'table') return 'table';
  if (tag === 'img') {
    return el.hasAttribute('alt') && !wsnAttr(el, 'alt') ? 'generic' : 'image';
  }
  const editable = wsnAttr(el, 'contenteditable').toLowerCase();
  if (el.hasAttribute('contenteditable') && editable !== 'false') return 'textbox';
  return 'generic';
}

function wsnLabelledBy(el) {
  const ids = wsnAttr(el, 'aria-labelledby').trim();
  if (!ids) return '';
  const root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
  const parts = [];
  for (const id of ids.split(/\s+/)) {
    let target = null;
    if (root && root.getElementById) target = root.getElementById(id);
    if (!target && el.ownerDocument) target = el.ownerDocument.getElementById(id);
    if (!target) continue;
    const text = target.innerText === undefined ? target.textContent : target.innerText;
    if (text) parts.push(String(text));
  }
  return wsnClean(parts.join(' '), WSN_NAME_LIMIT);
}

function wsnName(el, role) {
  const labelled = wsnLabelledBy(el);
  if (labelled) return labelled;
  const ariaLabel = wsnClean(wsnAttr(el, 'aria-label'), WSN_NAME_LIMIT);
  if (ariaLabel) return ariaLabel;
  if (el.labels && el.labels.length) {
    const label = el.labels[0];
    const text = wsnClean(
      label.innerText === undefined ? label.textContent : label.innerText,
      WSN_NAME_LIMIT
    );
    if (text) return text;
  }
  const alt = wsnClean(wsnAttr(el, 'alt'), WSN_NAME_LIMIT);
  if (alt) return alt;
  const title = wsnClean(wsnAttr(el, 'title'), WSN_NAME_LIMIT);
  if (title) return title;
  const placeholder = wsnClean(wsnAttr(el, 'placeholder'), WSN_NAME_LIMIT);
  if (placeholder) return placeholder;
  // Landmarks and media wrap the whole page; their own text is never a useful name.
  if (!WSN_STRUCTURAL_ROLES.has(role)) {
    const own = el.innerText === undefined ? el.textContent : el.innerText;
    const text = wsnClean(own, WSN_NAME_LIMIT);
    if (text) return text;
  }
  if (typeof el.value === 'string' && wsnAttr(el, 'type').toLowerCase() !== 'password') {
    const value = wsnClean(el.value, WSN_NAME_LIMIT);
    if (value) return value;
  }
  return '';
}

function wsnValue(el, role) {
  const tag = el.tagName.toLowerCase();
  if (role === 'checkbox' || role === 'radio') return '';
  if (tag === 'select') {
    const option = el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0] : null;
    return option ? wsnClean(option.text || option.value, WSN_VALUE_LIMIT) : '';
  }
  if (wsnAttr(el, 'type').toLowerCase() === 'password') {
    return el.value ? '***' : '';
  }
  if (tag === 'input' || tag === 'textarea') return wsnClean(el.value, WSN_VALUE_LIMIT);
  if (el.hasAttribute('contenteditable')) {
    const own = el.innerText === undefined ? el.textContent : el.innerText;
    return wsnClean(own, WSN_VALUE_LIMIT);
  }
  return '';
}

function wsnStates(el) {
  const states = [];
  const aria = name => wsnAttr(el, name).toLowerCase();
  if (el.disabled === true || aria('aria-disabled') === 'true') states.push('disabled');
  if (el.checked === true || aria('aria-checked') === 'true') states.push('checked');
  if (aria('aria-expanded') === 'true' || (el.tagName === 'DETAILS' && el.open)) {
    states.push('expanded');
  }
  if (el.selected === true || aria('aria-selected') === 'true') states.push('selected');
  if (el.required === true || aria('aria-required') === 'true') states.push('required');
  if (aria('aria-invalid') === 'true') states.push('invalid');
  else if (el.validity && el.validity.valid === false && !el.validity.valueMissing) {
    states.push('invalid');
  }
  if (el.readOnly === true || aria('aria-readonly') === 'true') states.push('readonly');
  const doc = el.ownerDocument;
  const root = el.getRootNode ? el.getRootNode() : doc;
  if ((root && root.activeElement === el) || (doc && doc.activeElement === el)) {
    states.push('focused');
  }
  const current = aria('aria-current');
  if (current && current !== 'false') states.push('current');
  return states;
}

function wsnVisible(el) {
  try {
    if (typeof el.checkVisibility === 'function') {
      // Chrome 116 ignores unknown options, so the computed-style fallback stays below.
      return !!el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
    }
  } catch (error) {
    // Fall through to the computed-style path.
  }
  const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
  if (!view || !view.getComputedStyle) return true;
  const style = view.getComputedStyle(el);
  if (!style) return true;
  if (style.display === 'none') return false;
  if (style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  if (parseFloat(style.opacity || '1') === 0) return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 || rect.height > 0;
}

function wsnDisplayed(el) {
  const tag = el.tagName;
  if (tag === 'BODY' || tag === 'HTML') return true;
  if (el.offsetParent !== null && el.offsetParent !== undefined) return true;
  const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
  if (!view || !view.getComputedStyle) return true;
  const style = view.getComputedStyle(el);
  return !style || style.display !== 'none';
}

function wsnContains(el, node) {
  let current = node;
  while (current) {
    if (current === el) return true;
    if (current.parentElement) {
      current = current.parentElement;
      continue;
    }
    const root = current.getRootNode ? current.getRootNode() : null;
    current = root && root.host ? root.host : null;
  }
  return false;
}

function wsnOccluded(el, rect) {
  if (rect.w <= 0 || rect.h <= 0) return false;
  const doc = el.ownerDocument;
  const view = doc ? doc.defaultView : null;
  if (!view || !doc.elementFromPoint) return false;
  const x = rect.x + rect.w / 2;
  const y = rect.y + rect.h / 2;
  if (x < 0 || y < 0 || x >= view.innerWidth || y >= view.innerHeight) return false;
  let root = doc;
  let hit = null;
  for (let depth = 0; depth < 8; depth += 1) {
    const found = root.elementFromPoint ? root.elementFromPoint(x, y) : null;
    if (!found || found === hit) break;
    hit = found;
    if (!hit.shadowRoot) break;
    root = hit.shadowRoot;
  }
  if (!hit) return false;
  return !(hit === el || wsnContains(el, hit) || wsnContains(hit, el));
}

function wsnChildNodes(node) {
  if (node.tagName === 'SLOT' && node.assignedNodes) {
    const assigned = node.assignedNodes({flatten: true});
    if (assigned && assigned.length) return assigned;
  }
  return Array.prototype.slice.call(node.childNodes || []);
}

function wsnSelector(el) {
  if (!el || !el.tagName) return '';
  const escape = value => (window.CSS && CSS.escape
    ? CSS.escape(value)
    : String(value).replace(/[^a-zA-Z0-9_-]/g, character => '\\' + character));
  const doc = el.ownerDocument;
  if (el.id && doc && doc.querySelectorAll('#' + escape(el.id)).length === 1) {
    return '#' + escape(el.id);
  }
  const parts = [];
  let node = el;
  let depth = 0;
  while (node && node.nodeType === 1 && depth < 6) {
    let part = node.tagName.toLowerCase();
    const parent = node.parentElement;
    if (parent) {
      const siblings = Array.prototype.filter.call(
        parent.children, other => other.tagName === node.tagName
      );
      if (siblings.length > 1) {
        part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
    }
    parts.unshift(part);
    if (!parent || parent.tagName === 'BODY' || parent.tagName === 'HTML') break;
    node = parent;
    depth += 1;
  }
  return parts.join(' > ');
}

function wsnFrameOffset(el, rect) {
  const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
  let left = 0;
  let top = 0;
  if (view && view.getComputedStyle) {
    const style = view.getComputedStyle(el);
    if (style) {
      left = (parseFloat(style.borderLeftWidth) || 0) + (parseFloat(style.paddingLeft) || 0);
      top = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.paddingTop) || 0);
    }
  }
  return {x: rect.x + left, y: rect.y + top};
}

function wsnRefFor(el, registry) {
  const existing = registry.byNode.get(el);
  if (existing && registry.nodes.get(existing) === el) return existing;
  const id = registry.next;
  registry.next = id + 1;
  registry.nodes.set(id, el);
  registry.byNode.set(el, id);
  return id;
}

function wsnHandle(el, registry) {
  return 'ref:' + registry.epoch + ':' + wsnRefFor(el, registry);
}

function wsnPruneRegistry(registry) {
  // The registry holds strong references, so a page that rebuilds its DOM would
  // pile up detached nodes forever. Every fresh read drops what is already gone.
  for (const entry of Array.from(registry.nodes)) {
    const node = entry[1];
    if (!node || node.isConnected === false) registry.nodes.delete(entry[0]);
  }
}

function wsnNorm(value) {
  return String(value === null || value === undefined ? '' : value)
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();
}

function wsnSplitIdentifier(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_\-.]+/g, ' ');
}

function wsnTrigrams(value) {
  const padded = '  ' + value + ' ';
  const grams = new Set();
  for (let index = 0; index + 3 <= padded.length; index += 1) {
    grams.add(padded.slice(index, index + 3));
  }
  return grams;
}

function wsnDice(left, right) {
  if (!left || !right) return 0;
  const first = wsnTrigrams(left);
  const second = wsnTrigrams(right);
  if (!first.size || !second.size) return 0;
  let shared = 0;
  for (const gram of first) if (second.has(gram)) shared += 1;
  return (2 * shared) / (first.size + second.size);
}

function wsnTokenMatch(query, field) {
  if (!query || !field) return false;
  if (query === field) return true;
  const queryStem = query.slice(0, Math.max(4, query.length - 2));
  if (field.startsWith(queryStem)) return true;
  const fieldStem = field.slice(0, Math.max(4, field.length - 2));
  return query.startsWith(fieldStem);
}

function wsnFieldScore(query, queryTokens, value) {
  const target = wsnNorm(value);
  if (!target || !query) return 0;
  let best = 0;
  if (target === query) best = 100;
  else if (target.startsWith(query)) best = 62;
  else if (target.indexOf(query) >= 0) best = 45;
  if (queryTokens.length) {
    const fieldTokens = target.split(' ');
    let matched = 0;
    for (const token of queryTokens) {
      if (fieldTokens.some(field => wsnTokenMatch(token, field))) matched += 1;
    }
    best = Math.max(best, 34 * (matched / queryTokens.length));
  }
  const dice = wsnDice(query, target);
  if (dice >= 0.55) best = Math.max(best, 30 * dice);
  return best;
}
"""
)


_EPOCH_SCRIPT = _JS_LIB + "\nreturn wsnRegistry().epoch;\n"


# ---------------------------------------------------------------------------
# Outline
# ---------------------------------------------------------------------------

_OUTLINE_SCRIPT = _JS_LIB + r"""
const limit = arguments[0];
const includeOcclusion = arguments[1];
const registry = wsnRegistry();
wsnPruneRegistry(registry);

const ctx = {
  out: [],
  limit: limit,
  truncated: false,
  occlusion: includeOcclusion,
  closed: registry.closedShadowRoots || 0,
  buffer: [],
  bufferDepth: 0,
  lastText: '',
  registry: registry,
  hosts: null,
  offsets: {x: 0, y: 0, frame: null},
  view: {width: window.innerWidth, height: window.innerHeight},
  counts: {
    nodes: 0, interactive: 0, headings: 0, landmarks: 0, links: 0,
    text_blocks: 0, frames: 0, images: 0
  }
};

function wsnBufferText(context, text, depth) {
  if (!context.buffer.length) context.bufferDepth = depth;
  context.buffer.push(text);
}

function wsnFlushText(context) {
  if (!context.buffer.length) return;
  const text = wsnClean(context.buffer.join(' '), WSN_TEXT_LIMIT);
  context.buffer = [];
  if (!text || text === context.lastText) return;
  if (context.out.length >= context.limit) {
    context.truncated = true;
    return;
  }
  context.lastText = text;
  context.counts.text_blocks += 1;
  context.out.push({kind: 'text', depth: context.bufferDepth, text: text});
}

function wsnShadowAncestors(root) {
  const hosts = new Set();
  let elements;
  try {
    elements = root.querySelectorAll('*');
  } catch (error) {
    return hosts;
  }
  for (const el of elements) {
    if (!el.shadowRoot) continue;
    hosts.add(el);
    let parent = el.parentElement;
    while (parent && !hosts.has(parent)) {
      hosts.add(parent);
      parent = parent.parentElement;
    }
  }
  return hosts;
}

function wsnInteresting(el, role) {
  if (role !== 'generic') return true;
  if (wsnAttr(el, 'aria-label') || wsnAttr(el, 'aria-labelledby')) return true;
  const tabindex = wsnAttr(el, 'tabindex');
  return !!tabindex && tabindex.trim() !== '-1';
}

function wsnContainer(el, context) {
  if (el.shadowRoot) return true;
  if (context.hosts && context.hosts.has(el)) return true;
  if (!el.firstElementChild) return false;
  try {
    return !!el.querySelector(WSN_CANDIDATE_SELECTOR);
  } catch (error) {
    return true;
  }
}

function wsnWalkRoot(root, depth, context, offsetX, offsetY, framePath) {
  const previousHosts = context.hosts;
  const previousOffsets = context.offsets;
  context.hosts = wsnShadowAncestors(root);
  context.offsets = {x: offsetX, y: offsetY, frame: framePath};
  const start = root.nodeType === 9 ? (root.body || root.documentElement) : root;
  if (start) wsnWalkChildren(start, depth, context);
  wsnFlushText(context);
  context.hosts = previousHosts;
  context.offsets = previousOffsets;
}

function wsnWalkChildren(node, depth, context) {
  if (context.truncated) return;
  for (const child of wsnChildNodes(node)) {
    if (context.truncated) return;
    if (child.nodeType === 3) {
      const text = wsnClean(child.nodeValue, 0);
      if (text) wsnBufferText(context, text, depth);
      continue;
    }
    if (child.nodeType !== 1) continue;
    wsnVisitElement(child, depth, context);
  }
}

function wsnVisitElement(el, depth, context) {
  const tag = el.tagName;
  if (WSN_SKIP_TAGS.has(tag)) return;
  if (tag === 'SLOT') {
    wsnWalkChildren(el, depth, context);
    return;
  }
  if (wsnAttr(el, 'aria-hidden') === 'true' || el.hasAttribute('hidden')) return;
  const role = wsnRole(el);
  if (role === 'hidden') return;
  if (!wsnDisplayed(el)) return;
  if (wsnInteresting(el, role)) {
    wsnEmit(el, role, depth, context);
    return;
  }
  if (wsnContainer(el, context)) {
    if (el.shadowRoot) {
      wsnWalkRoot(el.shadowRoot, depth, context, context.offsets.x, context.offsets.y,
                  context.offsets.frame);
    } else {
      wsnWalkChildren(el, depth, context);
    }
    return;
  }
  if (tag === 'LABEL' && el.control) return;
  const own = el.innerText === undefined ? el.textContent : el.innerText;
  const text = wsnClean(own, 0);
  if (text) wsnBufferText(context, text, depth);
}

function wsnDescend(el, depth, context, node) {
  if (el.shadowRoot) {
    wsnWalkRoot(el.shadowRoot, depth, context, context.offsets.x, context.offsets.y,
                context.offsets.frame);
    return;
  }
  if (wsnContainer(el, context)) {
    wsnWalkChildren(el, depth, context);
    return;
  }
  // Nothing interesting inside: keep the payload (table cells, listitem prose)
  // instead of dropping it, unless the node already carries it as its own name.
  if (node && !node.name && !node.interactive) {
    const own = el.innerText === undefined ? el.textContent : el.innerText;
    const text = wsnClean(own, WSN_TEXT_LIMIT);
    if (text) wsnBufferText(context, text, depth);
  }
}

function wsnEmit(el, role, depth, context) {
  wsnFlushText(context);
  if (context.out.length >= context.limit) {
    context.truncated = true;
    return;
  }
  const box = el.getBoundingClientRect();
  const width = Math.round(box.width);
  const height = Math.round(box.height);
  const visible = wsnVisible(el);
  if (!visible && width === 0 && height === 0) {
    wsnDescend(el, depth, context, null);
    return;
  }
  const offsets = context.offsets;
  const local = {x: Math.round(box.left), y: Math.round(box.top), w: width, h: height};
  const page = {
    x: Math.round(box.left + offsets.x),
    y: Math.round(box.top + offsets.y),
    w: width,
    h: height
  };
  const tag = el.tagName.toLowerCase();
  const interactive = WSN_INTERACTIVE_ROLES.has(role);
  const node = {
    kind: 'node',
    depth: depth,
    ref: wsnHandle(el, context.registry),
    tag: tag,
    role: role,
    name: wsnName(el, role),
    states: wsnStates(el),
    rect: local,
    page_rect: page,
    center: {x: page.x + Math.round(page.w / 2), y: page.y + Math.round(page.h / 2)},
    visible: visible,
    interactive: interactive,
    in_viewport: width > 0 && height > 0 && page.x + page.w > 0 && page.y + page.h > 0
      && page.x < context.view.width && page.y < context.view.height
  };
  if (role === 'heading') {
    const level = parseInt(wsnAttr(el, 'aria-level'), 10);
    node.level = level || (/^h[1-6]$/.test(tag) ? parseInt(tag.slice(1), 10) : 2);
  }
  const placeholder = wsnClean(wsnAttr(el, 'placeholder'), WSN_VALUE_LIMIT);
  if (placeholder && placeholder !== node.name) node.placeholder = placeholder;
  const value = wsnValue(el, role);
  if (value) node.value = value;
  if (role === 'link' && el.href) node.href = String(el.href);
  if (offsets.frame) node.frame = offsets.frame;
  node.occluded = context.occlusion ? wsnOccluded(el, local) : null;
  context.out.push(node);
  context.counts.nodes += 1;
  if (interactive) context.counts.interactive += 1;
  if (role === 'link') context.counts.links += 1;
  if (role === 'heading') context.counts.headings += 1;
  if (role === 'image') context.counts.images += 1;
  if (WSN_LANDMARK_ROLES.has(role)) context.counts.landmarks += 1;
  if (role === 'iframe') {
    context.counts.frames += 1;
    wsnDescendFrame(el, node, depth, context, local);
    return;
  }
  wsnDescend(el, depth + 1, context, node);
}

function wsnDescendFrame(el, node, depth, context, local) {
  let doc = null;
  try {
    doc = el.contentDocument;
  } catch (error) {
    doc = null;
  }
  node.src = wsnAttr(el, 'src');
  if (!doc) {
    node.same_origin = false;
    return;
  }
  node.same_origin = true;
  let frameName = el.tagName.toLowerCase();
  const selector = wsnSelector(el);
  if (selector) frameName = selector;
  const framePath = context.offsets.frame
    ? context.offsets.frame + ' >>> ' + frameName
    : frameName;
  try {
    const frameWindow = el.contentWindow;
    if (frameWindow && frameWindow.__wsnRefs) {
      context.closed += frameWindow.__wsnRefs.closedShadowRoots || 0;
    }
  } catch (error) {
    // Ignore cross-origin probing failures.
  }
  const origin = wsnFrameOffset(el, local);
  wsnWalkRoot(doc, depth + 1, context, context.offsets.x + origin.x,
              context.offsets.y + origin.y, framePath);
}

wsnWalkRoot(document, 0, ctx, 0, 0, null);
wsnFlushText(ctx);

return {
  url: String(location.href),
  title: String(document.title || ''),
  dom_epoch: registry.epoch,
  closed_shadow_roots: ctx.closed,
  truncated: ctx.truncated,
  counts: ctx.counts,
  viewport: ctx.view,
  nodes: ctx.out
};
"""


def _format_rect(rect: dict[str, Any] | None) -> str:
    if not rect:
        return ""
    return f"@{rect.get('x', 0)},{rect.get('y', 0)} {rect.get('w', 0)}x{rect.get('h', 0)}"


def _format_outline_text(nodes: list[dict[str, Any]]) -> str:
    """Render one compact line per node, keeping every line agent-readable."""
    lines: list[str] = []
    for node in nodes:
        depth = max(0, min(int(node.get("depth", 0) or 0), 10))
        indent = "  " * depth
        if node.get("kind") == "text":
            text = str(node.get("text") or "")
            if text:
                lines.append(f"{indent}{text}")
            continue
        role = str(node.get("role") or "generic")
        level = node.get("level")
        if level:
            role = f"{role}{level}"
        parts: list[str] = [role]
        name = str(node.get("name") or "")
        if name:
            parts.append(f'"{name}"')
        placeholder = str(node.get("placeholder") or "")
        if placeholder:
            parts.append(f'placeholder="{placeholder}"')
        value = str(node.get("value") or "")
        if value:
            parts.append(f'value="{value}"')
        states = [str(state) for state in node.get("states") or []]
        if states:
            parts.append("[" + " ".join(states) + "]")
        if node.get("role") == "link" and node.get("href"):
            href = str(node["href"])
            parts.append(f"-> {href if len(href) <= 70 else href[:69] + '…'}")
        if node.get("role") == "iframe":
            if node.get("same_origin") is False:
                parts.append("cross-origin")
            if node.get("src"):
                src = str(node["src"])
                parts.append(f"-> {src if len(src) <= 70 else src[:69] + '…'}")
        if node.get("interactive"):
            box = _format_rect(node.get("page_rect"))
            if box:
                parts.append(box)
        if node.get("occluded"):
            parts.append("occluded")
        if node.get("visible") is False:
            parts.append("invisible")
        lines.append(f"{indent}{node.get('ref', 'ref:?')}  " + " ".join(parts))
    return "\n".join(lines)


def outline(
    driver: Any,
    *,
    limit: int = 200,
    include_occlusion: bool = True,
    format: str = "text",
) -> dict[str, Any]:
    """Return the accessibility outline of the page as compact text or JSON nodes.

    The traversal enters open shadow roots and same-origin iframes; cross-origin
    frames are reported as stubs. Closed shadow roots cannot be walked at all and are
    only counted, so the caller knows the outline is incomplete.
    """
    node_limit = max(1, min(int(limit), _MAX_OUTLINE_NODES))
    selected = str(format or "text").strip().lower()
    if selected not in {"text", "json"}:
        raise ValueError("format must be 'text' or 'json'")
    raw = driver.execute_script(_OUTLINE_SCRIPT, node_limit, bool(include_occlusion))
    if not isinstance(raw, dict):
        raise RuntimeError("Page outline script returned an unexpected result")
    nodes = [node for node in raw.get("nodes") or [] if isinstance(node, dict)]
    summary = {
        "url": raw.get("url", ""),
        "title": raw.get("title", ""),
        "dom_epoch": raw.get("dom_epoch", ""),
        "counts": raw.get("counts", {}),
        "truncated": bool(raw.get("truncated")),
        "closed_shadow_roots": int(raw.get("closed_shadow_roots") or 0),
        "limit": node_limit,
        "occlusion_checked": bool(include_occlusion),
        "format": selected,
    }
    if selected == "json":
        summary["nodes"] = nodes
    else:
        summary["outline"] = _format_outline_text(nodes)
    return summary


# ---------------------------------------------------------------------------
# Readable text
# ---------------------------------------------------------------------------

_PAGE_TEXT_SCRIPT = _JS_LIB + r"""
let mainOnly = arguments[0];
const includeLinks = arguments[1];

const WSN_NOISE_SELECTOR = 'nav, header, footer, aside, form, [role="navigation"], '
  + '[role="banner"], [role="complementary"]';
const WSN_DROP_SELECTOR = 'script, style, noscript, template, svg, [aria-hidden="true"], [hidden]';

function wsnTextWeight(el) {
  const text = el.innerText === undefined ? (el.textContent || '') : (el.innerText || '');
  if (text.length <= 200) return {length: text.length, weight: 0};
  let noise = 0;
  let junk;
  try {
    junk = el.querySelectorAll(WSN_NOISE_SELECTOR);
  } catch (error) {
    junk = [];
  }
  for (const node of junk) {
    const inner = node.innerText === undefined ? (node.textContent || '') : (node.innerText || '');
    noise += inner.length;
  }
  return {length: text.length, weight: text.length - noise};
}

function wsnPickRoot(doc) {
  const direct = doc.querySelector('main')
    || doc.querySelector('[role="main"]')
    || doc.querySelector('article');
  if (direct) return {root: direct, reason: direct.tagName.toLowerCase()};
  let best = null;
  let bestWeight = 0;
  const visit = (el, depth) => {
    if (depth > 12) return;
    // innerText falls back to textContent for unrendered nodes, so an inline
    // <script> would otherwise win the text-weight contest on script-heavy pages.
    if (WSN_SKIP_TAGS.has(el.tagName) || wsnDropped(el) || !wsnDisplayed(el)) return;
    const measured = wsnTextWeight(el);
    if (measured.length <= 200) return;
    if (measured.weight > bestWeight) {
      bestWeight = measured.weight;
      best = el;
    }
    for (const child of el.children) visit(child, depth + 1);
  };
  if (doc.body) visit(doc.body, 0);
  return {root: best || doc.body, reason: best ? 'text-weight' : 'body'};
}

function wsnDropped(el) {
  try {
    if (el.matches(WSN_DROP_SELECTOR)) return true;
  } catch (error) {
    return false;
  }
  if (mainOnly) {
    try {
      if (el.matches(WSN_NOISE_SELECTOR)) return true;
    } catch (error) {
      return false;
    }
  }
  return false;
}

const chunks = [];
const links = [];

function wsnPreformatted(el) {
  const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
  if (!view || !view.getComputedStyle) return false;
  const style = view.getComputedStyle(el);
  return !!style && String(style.whiteSpace || '').startsWith('pre');
}

function wsnEmitText(node, parent) {
  const raw = String(node.nodeValue || '');
  if (!raw.trim()) return;
  // Only pay for a style lookup when collapsing would actually destroy layout.
  if (raw.indexOf('\n') >= 0 && parent && wsnPreformatted(parent)) {
    chunks.push(raw);
    return;
  }
  chunks.push(raw.replace(/[ \t\r\n]+/g, ' '));
}

function wsnTextWalk(el, budget) {
  if (budget.nodes > 60000) return;
  budget.nodes += 1;
  const tag = el.tagName;
  if (WSN_SKIP_TAGS.has(tag)) return;
  if (wsnDropped(el)) return;
  if (!wsnDisplayed(el)) return;
  if (tag === 'BR') {
    chunks.push('\n');
    return;
  }
  const heading = /^H[1-6]$/.test(tag);
  if (heading) chunks.push('\n\n## ');
  else if (tag === 'LI') chunks.push('\n- ');
  else if (tag === 'TD' || tag === 'TH') chunks.push(' | ');
  else if (WSN_BLOCK_TAGS.has(tag)) chunks.push('\n\n');
  const linkStart = chunks.length;
  // A host with a shadow root renders only what the shadow tree lays out, and
  // slotted light-DOM nodes are reached through <slot>. Walking both roots emits
  // every slotted node twice.
  const source = el.shadowRoot ? el.shadowRoot : el;
  // Light-DOM text arrives through the slot with no whitespace of its own, so
  // without a boundary it glues onto whatever the shadow tree emitted before it.
  const slotted = tag === 'SLOT';
  if (slotted) chunks.push(' ');
  for (const child of wsnChildNodes(source)) {
    if (child.nodeType === 3) wsnEmitText(child, el);
    else if (child.nodeType === 1) wsnTextWalk(child, budget);
  }
  if (slotted) chunks.push(' ');
  if (includeLinks && tag === 'A' && el.getAttribute('href')) {
    const label = wsnClean(chunks.slice(linkStart).join(' '), WSN_VALUE_LIMIT);
    const index = links.length + 1;
    links.push({index: index, text: label, url: String(el.href || el.getAttribute('href'))});
    chunks.push(' [' + index + ']');
  }
  if (heading) chunks.push('\n');
  else if (tag === 'TR') chunks.push('\n');
  else if (WSN_BLOCK_TAGS.has(tag)) chunks.push('\n\n');
}

const picked = wsnPickRoot(document);
let root = picked.root;
let reason = picked.reason;
let fallbackUsed = false;
if (root) wsnTextWalk(root, {nodes: 0});
// Emptiness is the only reliable signal: on a page that is one big <form> the
// noise list eats everything, and an over-eager root guess does the same. Either
// way the caller must not be handed a blank page with no way to tell why.
if (!chunks.join('').trim() && document.body && (mainOnly || root !== document.body)) {
  chunks.length = 0;
  links.length = 0;
  fallbackUsed = true;
  mainOnly = false;
  root = document.body;
  reason = 'body-fallback';
  wsnTextWalk(root, {nodes: 0});
}

return {
  url: String(location.href),
  title: String(document.title || ''),
  root_tag: root ? root.tagName.toLowerCase() : '',
  root_selector: root ? wsnSelector(root) : '',
  root_reason: reason,
  fallback_used: fallbackUsed,
  text: chunks.join(''),
  links: links
};
"""


_MULTI_NEWLINE = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_NEWLINE = re.compile(r"[ \t]+\n")
_LEADING_CELL = re.compile(r"\n ?\| ")
_MARKER_PATTERN = re.compile(r"\[(\d+)\]")


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_SPACE.sub(" ", text)
    text = _LEADING_CELL.sub("\n", text)
    text = _SPACE_BEFORE_NEWLINE.sub("\n", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def _clip_on_boundary(text: str, limit: int) -> tuple[str, bool]:
    """Cut at a paragraph boundary so the tail is never half a sentence."""
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    boundary = window.rfind("\n\n")
    return (window[:boundary].rstrip() if boundary > limit // 4 else window.rstrip()), True


def _link_listing(text: str, links: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    kept = {int(marker) for marker in _MARKER_PATTERN.findall(text)}
    selected = [
        link
        for link in links
        if isinstance(link, dict) and int(link.get("index", 0)) in kept
    ]
    listing = "\n".join(
        f"[{link['index']}] {link.get('text') or ''} -> {link.get('url') or ''}"
        for link in selected
    )
    return listing, selected


def page_text(
    driver: Any,
    *,
    max_chars: int = 20000,
    mode: str = "main",
    include_links: bool = False,
) -> dict[str, Any]:
    """Extract the readable text of the rendered page, keeping block structure.

    ``mode='main'`` also drops navigation, header, footer, aside and form chrome;
    ``mode='full'`` keeps everything that is visible. A page whose whole content is
    chrome - a login or checkout form - would come back empty, so an empty result
    is retried without the noise list and reported as ``fallback_used``.
    """
    selected_mode = str(mode or "main").strip().lower()
    if selected_mode not in {"main", "full"}:
        raise ValueError("mode must be 'main' or 'full'")
    limit = max(200, min(int(max_chars), _MAX_TEXT_CHARS))
    raw = driver.execute_script(
        _PAGE_TEXT_SCRIPT, selected_mode == "main", bool(include_links)
    )
    if not isinstance(raw, dict):
        raise RuntimeError("Page text script returned an unexpected result")
    full_text = _normalize_text(str(raw.get("text") or ""))
    total = len(full_text)
    fallback_used = bool(raw.get("fallback_used"))
    text, truncated = _clip_on_boundary(full_text, limit)
    links: list[dict[str, Any]] = []
    if include_links:
        # The link index is part of what the caller receives, so it has to be paid
        # for out of the same budget instead of silently overflowing it. Both the
        # kept text and its index grow with the text budget, so the largest budget
        # that still fits is found by bisection.
        listing = ""
        low, high = 0, limit
        text = ""
        while low <= high:
            middle = (low + high) // 2
            candidate, _ = _clip_on_boundary(full_text, middle)
            candidate_listing, candidate_links = _link_listing(
                candidate, raw.get("links") or []
            )
            spent = len(candidate) + (len(candidate_listing) + 2 if candidate_listing else 0)
            if spent <= limit:
                text, listing, links = candidate, candidate_listing, candidate_links
                low = middle + 1
            else:
                high = middle - 1
        truncated = text != full_text
        if listing:
            text = f"{text}\n\n{listing}"
    return {
        "url": raw.get("url", ""),
        "title": raw.get("title", ""),
        "mode": selected_mode,
        "mode_used": "full" if fallback_used else selected_mode,
        "fallback_used": fallback_used,
        "root_tag": raw.get("root_tag", ""),
        "root_selector": raw.get("root_selector", ""),
        "root_reason": raw.get("root_reason", ""),
        "text": text,
        "chars": len(text),
        "total_chars": total,
        "truncated": truncated,
        "max_chars": limit,
        **({"links": links} if include_links else {}),
    }


# ---------------------------------------------------------------------------
# Semantic find
# ---------------------------------------------------------------------------

_FIND_SCRIPT = _JS_LIB + r"""
const query = arguments[0];
const roleFilter = arguments[1];
const limit = arguments[2];
const visibleOnly = arguments[3];
const includeOcclusion = arguments[4];
const registry = wsnRegistry();
wsnPruneRegistry(registry);

const WSN_ROLE_SYNONYMS = {
  button: ['button', 'knopka', 'кнопка', 'нажать', 'submit', 'apply'],
  link: ['link', 'ссылка', 'перейти', 'anchor'],
  textbox: ['textbox', 'input', 'field', 'поле', 'ввод', 'текст'],
  searchbox: ['searchbox', 'search', 'поиск', 'найти'],
  checkbox: ['checkbox', 'галочка', 'флажок', 'чекбокс'],
  radio: ['radio', 'переключатель'],
  combobox: ['combobox', 'select', 'dropdown', 'список', 'выбор'],
  listbox: ['listbox', 'список'],
  file: ['file', 'upload', 'файл', 'загрузка'],
  heading: ['heading', 'заголовок'],
  image: ['image', 'картинка', 'изображение'],
  tab: ['tab', 'вкладка'],
  dialog: ['dialog', 'modal', 'диалог', 'окно']
};
const WSN_ACTION_WORDS = new Set([
  'click', 'press', 'open', 'submit', 'send', 'search', 'find', 'login', 'log', 'sign',
  'buy', 'add', 'apply', 'continue', 'next', 'save', 'download', 'upload', 'accept',
  'нажать', 'нажми', 'открыть', 'открой', 'отправить', 'войти', 'вход', 'купить',
  'добавить', 'найти', 'поиск', 'продолжить', 'сохранить', 'скачать', 'кнопка', 'ссылка'
]);

function wsnCollect(root, offsets, out, depth) {
  if (depth > 6 || out.length > 4000) return;
  let elements;
  try {
    elements = root.querySelectorAll('*');
  } catch (error) {
    return;
  }
  for (const el of elements) {
    if (WSN_SKIP_TAGS.has(el.tagName)) continue;
    if (el.shadowRoot) wsnCollect(el.shadowRoot, offsets, out, depth + 1);
    if (el.tagName === 'IFRAME') {
      let doc = null;
      try {
        doc = el.contentDocument;
      } catch (error) {
        doc = null;
      }
      if (doc) {
        const box = el.getBoundingClientRect();
        const origin = wsnFrameOffset(el, {x: box.left, y: box.top});
        wsnCollect(doc, {x: offsets.x + origin.x, y: offsets.y + origin.y,
                         frame: wsnSelector(el)}, out, depth + 1);
      }
      continue;
    }
    let matched = false;
    try {
      matched = el.matches(WSN_CANDIDATE_SELECTOR);
    } catch (error) {
      matched = false;
    }
    if (matched) out.push({el: el, offsets: offsets});
  }
}

const normalizedQuery = wsnNorm(query);
const queryTokens = normalizedQuery ? normalizedQuery.split(' ') : [];
const wantedRole = roleFilter ? String(roleFilter).toLowerCase() : '';
const actionish = queryTokens.some(token => WSN_ACTION_WORDS.has(token));

const candidates = [];
wsnCollect(document, {x: 0, y: 0, frame: null}, candidates, 0);

const view = {width: window.innerWidth, height: window.innerHeight};
const scored = [];
let anyInteractive = false;

for (let index = 0; index < candidates.length; index += 1) {
  const el = candidates[index].el;
  const offsets = candidates[index].offsets;
  const role = wsnRole(el);
  if (role === 'hidden') continue;
  if (wsnAttr(el, 'aria-hidden') === 'true' || el.hasAttribute('hidden')) continue;
  const box = el.getBoundingClientRect();
  const width = Math.round(box.width);
  const height = Math.round(box.height);
  const visible = wsnVisible(el);
  if (visibleOnly && (!visible || (width === 0 && height === 0))) continue;
  const interactive = WSN_INTERACTIVE_ROLES.has(role);
  const name = wsnName(el, role);
  const value = wsnValue(el, role);
  const testId = wsnAttr(el, 'data-testid') || wsnAttr(el, 'data-qa') || wsnAttr(el, 'data-test');
  let hrefTail = '';
  if (role === 'link') {
    const href = wsnAttr(el, 'href');
    const parts = href.split(/[?#]/)[0].split('/').filter(Boolean);
    hrefTail = parts.length ? wsnSplitIdentifier(parts[parts.length - 1]) : '';
  }
  const synonyms = WSN_ROLE_SYNONYMS[role] || [role];
  const fields = [
    ['name', 1.00, name],
    ['placeholder', 0.80, wsnAttr(el, 'placeholder')],
    ['title', 0.65, wsnAttr(el, 'title')],
    ['testid', 0.60, wsnSplitIdentifier(testId)],
    ['name_id', 0.50, wsnSplitIdentifier(wsnAttr(el, 'name')) + ' ' + wsnSplitIdentifier(el.id)],
    ['role', 0.40, role + ' ' + synonyms.join(' ')],
    ['value', 0.40, value],
    ['href', 0.30, hrefTail]
  ];
  let score = 0;
  let matchedField = '';
  for (const field of fields) {
    const component = wsnFieldScore(normalizedQuery, queryTokens, field[2]);
    if (component <= 0) continue;
    const weighted = component * field[1];
    if (weighted > score) {
      score = weighted;
      matchedField = field[0];
    }
  }
  if (score <= 0) continue;
  const local = {x: Math.round(box.left), y: Math.round(box.top), w: width, h: height};
  const page = {
    x: Math.round(box.left + offsets.x),
    y: Math.round(box.top + offsets.y),
    w: width,
    h: height
  };
  const inViewport = width > 0 && height > 0 && page.x + page.w > 0 && page.y + page.h > 0
    && page.x < view.width && page.y < view.height;
  if (inViewport) score += 18;
  if (actionish && interactive) score += 12;
  if (wantedRole && role === wantedRole) score += 8;
  const disabled = el.disabled === true || wsnAttr(el, 'aria-disabled') === 'true';
  if (!disabled) score += 6;
  else score -= 15;
  const parent = el.parentElement;
  if (parent && (parent.tagName === 'MAIN' || parent.tagName === 'FORM')) score += 4;
  const occluded = includeOcclusion ? wsnOccluded(el, local) : false;
  if (occluded) score -= 25;
  if (interactive) anyInteractive = true;
  scored.push({
    el: el,
    index: index,
    role: role,
    name: name,
    score: score,
    matched_field: matchedField,
    rect: local,
    page_rect: page,
    center: {x: page.x + Math.round(page.w / 2), y: page.y + Math.round(page.h / 2)},
    visible: visible,
    occluded: occluded,
    interactive: interactive,
    frame: offsets.frame || null
  });
}

for (const item of scored) {
  if (!item.interactive && anyInteractive && item.role === 'generic') item.score -= 12;
  if (wantedRole && item.role !== wantedRole) item.score -= 20;
  item.score = Math.round(item.score * 10) / 10;
}

scored.sort((left, right) => (right.score - left.score) || (left.index - right.index));

let selected = scored.filter(item => item.score >= 25).slice(0, limit);
let lowConfidence = false;
if (!selected.length) {
  lowConfidence = true;
  selected = scored.slice(0, Math.min(3, limit));
}

const matches = selected.map(item => ({
  ref: wsnHandle(item.el, registry),
  role: item.role,
  name: item.name,
  score: item.score,
  matched_field: item.matched_field,
  rect: item.rect,
  page_rect: item.page_rect,
  center: item.center,
  visible: item.visible,
  occluded: item.occluded,
  frame: item.frame
}));

return {
  url: String(location.href),
  title: String(document.title || ''),
  dom_epoch: registry.epoch,
  candidates: candidates.length,
  scored: scored.length,
  matches: matches,
  low_confidence: lowConfidence
};
"""


def find(
    driver: Any,
    query: str,
    *,
    role: str | None = None,
    limit: int = 5,
    visible_only: bool = True,
) -> dict[str, Any]:
    """Find elements by meaning; all scoring happens inside the page in one round-trip."""
    text = str(query or "").strip()
    if not text:
        raise ValueError("query must not be empty")
    match_limit = max(1, min(int(limit), _MAX_FIND_MATCHES))
    wanted_role = str(role).strip().lower() if role else None
    raw = driver.execute_script(
        _FIND_SCRIPT, text, wanted_role, match_limit, bool(visible_only), True
    )
    if not isinstance(raw, dict):
        raise RuntimeError("Element find script returned an unexpected result")
    return {
        "query": text,
        "role": wanted_role,
        "url": raw.get("url", ""),
        "title": raw.get("title", ""),
        "dom_epoch": raw.get("dom_epoch", ""),
        "matches": raw.get("matches") or [],
        "low_confidence": bool(raw.get("low_confidence")),
        "candidates": int(raw.get("candidates") or 0),
    }


# ---------------------------------------------------------------------------
# Locator resolution
# ---------------------------------------------------------------------------


def split_piercing_path(selector: str) -> list[str] | None:
    """Split on `` ' >>> ' `` only where it is a separator, not selector payload.

    ``div[data-op='a >>> b']`` is a perfectly valid CSS selector, so the scan has to
    ignore the separator inside quotes and inside attribute brackets. Returns
    ``None`` when the string is not a piercing path at all, and raises when it is
    one but a segment is missing.
    """
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    index = 0
    length = len(selector)
    while index < length:
        character = selector[index]
        if character == "\\" and index + 1 < length:
            current.append(character)
            current.append(selector[index + 1])
            index += 2
            continue
        if quote:
            current.append(character)
            if character == quote:
                quote = None
            index += 1
            continue
        if character in "\"'":
            quote = character
            current.append(character)
            index += 1
            continue
        if character == "[":
            depth += 1
        elif character == "]":
            depth = max(0, depth - 1)
        if depth == 0 and selector.startswith(_PIERCING_SEPARATOR, index):
            parts.append("".join(current))
            current = []
            index += len(_PIERCING_SEPARATOR)
            continue
        current.append(character)
        index += 1
    parts.append("".join(current))
    if quote or depth:
        # Unbalanced input is not valid CSS either way, so the separator is taken
        # at face value rather than swallowing the whole locator into one part.
        parts = selector.split(_PIERCING_SEPARATOR)
    parts = [part.strip() for part in parts]
    if len(parts) > 1 and not all(parts):
        # Dropping the empty part would leave a single segment that then travels
        # on as plain CSS and fails deep inside the driver with nothing to act on.
        raise ValueError(_EMPTY_SEGMENT_HINT.format(selector=selector))
    return parts if len(parts) > 1 else None


def resolve_locator_expression(locator: str) -> str | None:
    """Translate a ref handle or a ``a >>> b`` piercing path into a JS expression.

    Returns ``None`` for plain CSS selectors so the caller keeps its existing
    ``find_element`` path, and raises for a locator that is meant as a ref or a
    piercing path but cannot address anything. `` ' >>> ' `` is only treated as a
    separator outside quotes and brackets, so neither form can steal a selector
    that already works today.
    """
    if not isinstance(locator, str):
        return None
    candidate = locator.strip()
    if not candidate:
        return None
    if REF_PATTERN.match(candidate):
        return ref_expression(candidate)
    if candidate.startswith(">>>") or candidate.endswith(">>>"):
        # Stripping already ate the space that made this a separator, and no CSS
        # selector begins or ends with a bare '>>>'.
        raise ValueError(_EMPTY_SEGMENT_HINT.format(selector=candidate))
    if _PIERCING_SEPARATOR not in candidate:
        return None
    parts = split_piercing_path(candidate)
    if not parts:
        return None
    encoded = json.dumps(parts, ensure_ascii=True)
    return (
        "(() => {"
        f"const parts = {encoded};"
        "let root = document, element = null;"
        "for (const part of parts) {"
        "if (!root || !root.querySelector) return null;"
        "element = root.querySelector(part);"
        "if (!element) return null;"
        "let next = element.shadowRoot;"
        "if (!next && element.tagName === 'IFRAME') {"
        "try { next = element.contentDocument; } catch (error) { next = null; }"
        "}"
        "root = next || element;"
        "}"
        "return element;"
        "})()"
    )
