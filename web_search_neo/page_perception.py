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

# How many frames deep every topic walks. One number for all of them on purpose:
# when the outline reached deeper than find, page_elements and page_text, it
# handed out refs for elements the other topics denied existed, and the action
# tools - bounded lower still - refused them as gone. The locator resolver is
# bounded above this, never below, so a ref the outline minted can always be
# followed back to its document.
MAX_FRAME_DEPTH = 8


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
      closedShadowRoots: 0,
      closedShadowRootsByHost: new WeakMap()
    };
  }
  if (!window.__wsnRefs.closedShadowRootsByHost) {
    window.__wsnRefs.closedShadowRootsByHost = new WeakMap();
  }
  // Closed roots are only reachable through the value returned by attachShadow.
  // Keep that value keyed weakly by its host while retaining the existing count.
  const attachShadow = Element.prototype.attachShadow;
  if (attachShadow && !attachShadow.__wsnPatched) {
    const patched = function (init) {
      const root = attachShadow.apply(this, arguments);
      const registry = window.__wsnRefs;
      if (init && init.mode === 'closed' && registry) {
        registry.closedShadowRoots += 1;
        registry.closedShadowRootsByHost.set(this, root);
      }
      return root;
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
        # A ref outlives its element, and `isConnected` alone does not notice it:
        # a node inside an iframe that was removed stays connected to its own
        # orphaned document and would happily accept actions no user could
        # perform. The document's window is discarded with the browsing context,
        # so it is what actually says "this subtree left the page".
        "if (!node || !node.isConnected) return null;"
        "const doc = node.ownerDocument;"
        "let view = null;"
        "try { view = doc ? doc.defaultView : null; } catch (error) { view = null; }"
        "return view ? node : null;"
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
    f"const WSN_MAX_FRAME_DEPTH = {MAX_FRAME_DEPTH};\n"
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
    const shadow = wsnShadowRoot(hit);
    if (!shadow) break;
    root = shadow;
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

function wsnEscape(value) {
  return window.CSS && CSS.escape
    ? CSS.escape(value)
    : String(value).replace(/[^a-zA-Z0-9_-]/g, character => '\\' + character);
}

function wsnUnique(root, path, el) {
  // "Probably this element" is the failure this whole function exists to avoid:
  // a path that matches two nodes silently addresses whichever comes first.
  try {
    const found = root.querySelectorAll(path);
    return found.length === 1 && found[0] === el;
  } catch (error) {
    return false;
  }
}

function wsnSelector(el) {
  // Returns a path that resolves to this element and to nothing else, addressed
  // from the top document, or '' when no such path exists. A shadow root and a
  // frame both hide their contents from plain CSS, so both are crossed with the
  // 'outer >>> inner' piercing form the locator resolver understands. Reporting
  // the path relative to the inner document instead is worse than reporting
  // nothing: '#save' names one button in the frame and a different one on the
  // page, and the caller cannot tell which one it just clicked.
  if (!el || !el.tagName) return '';
  const root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
  if (!root || !root.querySelectorAll) return '';
  const parts = [];
  let node = el;
  let hops = 0;
  let local = '';
  while (node && node.nodeType === 1 && hops < 40) {
    hops += 1;
    let part = node.tagName.toLowerCase();
    if (node.id) {
      const byId = '#' + wsnEscape(node.id);
      if (wsnUnique(root, byId, node)) part = byId;
    }
    // A direct child of a shadow root has no parentElement, and skipping the
    // sibling count there left two identical buttons sharing one bare tag - and
    // therefore no usable path at all, though nth-of-type separates them.
    const parent = node.parentElement;
    const container = parent || node.parentNode;
    if (part.charAt(0) !== '#' && container && container.children) {
      const siblings = Array.prototype.filter.call(
        container.children, other => other.tagName === node.tagName
      );
      if (siblings.length > 1) {
        part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
    }
    parts.unshift(part);
    const candidate = parts.join(' > ');
    if (wsnUnique(root, candidate, el)) {
      local = candidate;
      break;
    }
    if (part.charAt(0) === '#') break;
    node = parent;
  }
  if (!local) return '';
  if (root.host) {
    const host = wsnSelector(root.host);
    return host ? host + ' >>> ' + local : '';
  }
  let owner = null;
  try {
    owner = root.defaultView ? root.defaultView.frameElement : null;
  } catch (error) {
    owner = null;  // cross-origin parent: nothing here is addressable from there
  }
  if (owner) {
    const host = wsnSelector(owner);
    return host ? host + ' >>> ' + local : '';
  }
  return local;
}

// Where a point inside a frame lands, one hop at a time. Both layers ask this:
// perception, to report a box in the top document's terms, and input, to aim an
// event at one. A second copy of this arithmetic drifts, and the two layers then
// disagree about the same pixel - which is exactly how a reported centre came to
// be a place the click never went.
//
// A frame's content is not merely offset from its host: a `transform` between
// the two rotates and scales everything inside it, so only a full affine map -
// an origin plus the images of the two unit vectors - can carry a point across.
//
// `transform` is not the whole story either. The CSS Transforms 2 individual
// properties - `rotate`, `scale`, `translate` - never appear in the computed
// `transform` (Chrome reports `transform: none` for `rotate: 20deg`), and `zoom`
// is not a transform at all, so a map that reads only `transform` comes out as
// the identity while the frame is turned and resized on screen. The individual
// properties are composed in the spec's order - translate, rotate, scale, then
// `transform` - and `zoom` multiplies in as a scalar, which commutes with the
// rest so the whole chain's factor can be applied once at the end. `translate`
// is left out on purpose: it is a pure translation, and every translation in the
// chain is recovered from the painted box below.
//
// A 3D/perspective chain is not affine and says so in `flat` instead of being
// quietly reported as something a caller can aim at.
const WSN_IDENTITY_MAP = {x: 0, y: 0, ax: 1, ay: 0, bx: 0, by: 1, flat: true};

function wsnRotateFunction(value) {
  const parts = value.trim().split(/\s+/);
  const angle = parts.pop();
  if (!parts.length || parts[0] === 'z') return 'rotate(' + angle + ')';
  const axis = parts.length === 1 ? {x: '1,0,0', y: '0,1,0'}[parts[0]] : parts.join(',');
  return axis ? 'rotate3d(' + axis + ',' + angle + ')' : 'rotate(' + angle + ')';
}

function wsnScaleFunction(value) {
  const parts = value.trim().split(/\s+/);
  const sx = parts[0];
  const sy = parts[1] || parts[0];
  const sz = parts[2] || '1';
  return parseFloat(sz) === 1
    ? 'scale(' + sx + ',' + sy + ')'
    : 'scale3d(' + sx + ',' + sy + ',' + sz + ')';
}

// `is2D` answers how a matrix was spelled, not what it does: a rotate3d about
// z is flagged 3D while projecting exactly like a 2D rotation. What matters is
// whether the x/y projection depends on z, so the numbers are asked directly.
function wsnProjectsFlat(matrix) {
  return !matrix.m13 && !matrix.m14 && !matrix.m23 && !matrix.m24 &&
    !matrix.m31 && !matrix.m32 && !matrix.m34;
}

function wsnFrameMap(el) {
  // The map from this frame's own viewport coordinates to those of the document
  // that holds it - one hop. Compose it with the host's map to reach the page.
  const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
  const styleOf = node => (view && view.getComputedStyle ? view.getComputedStyle(node) : null);
  let linear = new DOMMatrix();
  let flat = true;
  let zoom = 1;
  for (let node = el; node; node = node.parentElement) {
    const style = styleOf(node);
    if (!style) continue;
    const list = [];
    if (style.rotate && style.rotate !== 'none') list.push(wsnRotateFunction(style.rotate));
    if (style.scale && style.scale !== 'none') list.push(wsnScaleFunction(style.scale));
    if (style.transform && style.transform !== 'none') list.push(style.transform);
    if (list.length) {
      // Every entry is Chrome's own computed serialisation, in the same grammar
      // DOMMatrix parses, so a throw here would mean a spelling nobody knows
      // about yet - worth hearing about rather than quietly aiming through.
      const step = new DOMMatrix(list.join(' '));
      if (!wsnProjectsFlat(step)) flat = false;
      linear = step.multiply(linear);
    }
    // `zoom` is inherited by multiplication rather than by computed value, so the
    // effective factor is the product of the chain. It is a uniform scale, which
    // commutes with everything above, so applying it once at the end is exact.
    const nodeZoom = parseFloat(style.zoom);
    if (nodeZoom > 0 && nodeZoom !== 1) zoom *= nodeZoom;
  }
  if (zoom !== 1) linear = linear.scale(zoom, zoom);
  linear.e = 0;
  linear.f = 0;
  const width = el.offsetWidth;
  const height = el.offsetHeight;
  const corners = [[0, 0], [width, 0], [width, height], [0, height]].map(
    pair => linear.transformPoint(new DOMPoint(pair[0], pair[1]))
  );
  const rect = el.getBoundingClientRect();
  const shiftX = rect.left - Math.min.apply(null, corners.map(point => point.x));
  const shiftY = rect.top - Math.min.apply(null, corners.map(point => point.y));
  const own = styleOf(el);
  const insetX = own
    ? (parseFloat(own.borderLeftWidth) || 0) + (parseFloat(own.paddingLeft) || 0) : 0;
  const insetY = own
    ? (parseFloat(own.borderTopWidth) || 0) + (parseFloat(own.paddingTop) || 0) : 0;
  const origin = linear.transformPoint(new DOMPoint(insetX, insetY));
  const unitX = linear.transformPoint(new DOMPoint(insetX + 1, insetY));
  const unitY = linear.transformPoint(new DOMPoint(insetX, insetY + 1));
  return {
    x: origin.x + shiftX, y: origin.y + shiftY,
    ax: unitX.x - origin.x, ay: unitX.y - origin.y,
    bx: unitY.x - origin.x, by: unitY.y - origin.y,
    flat: flat
  };
}

function wsnComposeMap(outer, inner) {
  // `outer` after `inner`: a point in the inner frame goes through the inner map
  // into its host's coordinates, and through the outer map from there.
  return {
    x: outer.x + outer.ax * inner.x + outer.bx * inner.y,
    y: outer.y + outer.ay * inner.x + outer.by * inner.y,
    ax: outer.ax * inner.ax + outer.bx * inner.ay,
    ay: outer.ay * inner.ax + outer.by * inner.ay,
    bx: outer.ax * inner.bx + outer.bx * inner.by,
    by: outer.ay * inner.bx + outer.by * inner.by,
    flat: outer.flat && inner.flat
  };
}

function wsnMapPoint(map, x, y) {
  return {x: map.x + map.ax * x + map.bx * y, y: map.y + map.ay * x + map.by * y};
}

// A rotated box has no axis-aligned rectangle of its own, so `page_rect` is the
// bounding box of its mapped corners - the smallest rectangle that contains it -
// while `center` is the mapped centre, which is the point a caller aims at and
// is exact whatever the rotation.
function wsnMapBox(map, box) {
  const right = box.left + box.width;
  const bottom = box.top + box.height;
  const corners = [
    wsnMapPoint(map, box.left, box.top), wsnMapPoint(map, right, box.top),
    wsnMapPoint(map, right, bottom), wsnMapPoint(map, box.left, bottom)
  ];
  const xs = corners.map(point => point.x);
  const ys = corners.map(point => point.y);
  const left = Math.min.apply(null, xs);
  const top = Math.min.apply(null, ys);
  const middle = wsnMapPoint(map, box.left + box.width / 2, box.top + box.height / 2);
  return {
    page_rect: {
      x: Math.round(left),
      y: Math.round(top),
      w: Math.round(Math.max.apply(null, xs) - left),
      h: Math.round(Math.max.apply(null, ys) - top)
    },
    center: {x: Math.round(middle.x), y: Math.round(middle.y)}
  };
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

function wsnRegistryIn(win) {
  // Ref numbers restart at 1 in every document, so a handle only means anything
  // in the document that minted it - and an element handle only works while the
  // driver is in that same browsing context. A frame therefore gets its own
  // registry instead of borrowing the top one, which is what makes a ref read
  // inside a frame resolvable, and actionable, later.
  if (!win) return null;
  let current = null;
  try {
    current = win.__wsnRefs;
  } catch (error) {
    return null;  // cross-origin: nothing here can be addressed anyway
  }
  if (current && current.nodes && current.byNode) {
    if (!current.closedShadowRootsByHost) current.closedShadowRootsByHost = new WeakMap();
    return current;
  }
  const bytes = new Uint8Array(8);
  if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Math.floor(Math.random() * 256);
  }
  let epoch = '';
  for (const byte of bytes) epoch += byte.toString(16).padStart(2, '0');
  const created = {
    epoch: epoch, nodes: new Map(), next: 1, byNode: new WeakMap(), closedShadowRoots: 0,
    closedShadowRootsByHost: new WeakMap()
  };
  try {
    win.__wsnRefs = created;
  } catch (error) {
    return null;
  }
  return created;
}

const WSN_PRUNED = new Set();

function wsnPruneOnce(registry) {
  if (registry && !WSN_PRUNED.has(registry)) {
    WSN_PRUNED.add(registry);
    wsnPruneRegistry(registry);
  }
  return registry;
}

function wsnRegistryOf(el) {
  const doc = el ? el.ownerDocument : null;
  let win = null;
  try {
    win = doc ? doc.defaultView : null;
  } catch (error) {
    win = null;
  }
  return wsnPruneOnce(wsnRegistryIn(win) || wsnRegistry());
}

function wsnShadowRoot(el) {
  if (!el) return null;
  if (el.shadowRoot) return el.shadowRoot;
  const doc = el.ownerDocument;
  let registry = null;
  try {
    registry = doc && doc.defaultView ? doc.defaultView.__wsnRefs : null;
  } catch (error) {
    registry = null;
  }
  const roots = registry && registry.closedShadowRootsByHost;
  return roots && roots.get ? (roots.get(el) || null) : null;
}

function wsnHandleFor(el) {
  return wsnHandle(el, wsnRegistryOf(el));
}

function wsnPruneRegistry(registry) {
  // The registry holds strong references, so a page that rebuilds its DOM would
  // pile up detached nodes forever. Every fresh read drops what is already gone.
  for (const entry of Array.from(registry.nodes)) {
    const node = entry[1];
    if (!node || node.isConnected === false) {
      registry.nodes.delete(entry[0]);
      continue;
    }
    // A node inside a frame that was removed stays connected to its own orphaned
    // document; the window is what gets discarded with the browsing context.
    let view = null;
    try {
      view = node.ownerDocument ? node.ownerDocument.defaultView : null;
    } catch (error) {
      view = null;
    }
    if (!view) registry.nodes.delete(entry[0]);
  }
}

function wsnHiddenBy(el) {
  // The outline stops at the first aria-hidden or [hidden] ancestor, so anything
  // that reports elements has to use the same rule or the two topics disagree
  // about what is on the page. The walk crosses shadow and frame boundaries,
  // because both of those hide their contents from a plain parent walk.
  let node = el;
  let hops = 0;
  while (node && hops < 200) {
    hops += 1;
    if (node.nodeType === 1) {
      if (wsnAttr(node, 'aria-hidden') === 'true') return 'aria-hidden';
      if (node.hasAttribute && node.hasAttribute('hidden')) return 'hidden-attribute';
    }
    if (node.parentElement) {
      node = node.parentElement;
      continue;
    }
    const root = node.getRootNode ? node.getRootNode() : null;
    if (root && root.host) {
      node = root.host;
      continue;
    }
    let owner = null;
    try {
      owner = root && root.defaultView ? root.defaultView.frameElement : null;
    } catch (error) {
      owner = null;
    }
    node = owner;
  }
  return '';
}

function wsnHiddenReason(el) {
  const ancestor = wsnHiddenBy(el);
  if (ancestor) return ancestor;
  const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
  const style = view && view.getComputedStyle ? view.getComputedStyle(el) : null;
  if (style) {
    if (style.display === 'none') return 'display-none';
    if (style.visibility === 'hidden' || style.visibility === 'collapse') {
      return 'visibility-hidden';
    }
    if (parseFloat(style.opacity || '1') === 0) return 'transparent';
  }
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 && rect.height <= 0) return 'zero-size';
  return '';
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


# The element topics in ``browser_tools`` share these helpers, so that a
# selector, a visibility verdict and the aria-hidden rule mean the same thing
# whichever topic an agent happens to read.
JS_LIBRARY = _JS_LIB


_EPOCH_SCRIPT = _JS_LIB + "\nreturn wsnRegistry().epoch;\n"


# ---------------------------------------------------------------------------
# Outline
# ---------------------------------------------------------------------------

_OUTLINE_SCRIPT = _JS_LIB + r"""
const limit = arguments[0];
const includeOcclusion = arguments[1];
const registry = wsnPruneOnce(wsnRegistry());

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
  frames: {same_origin: 0, cross_origin: 0, unaddressable: 0, too_deep: 0},
  offsets: {map: WSN_IDENTITY_MAP, frame: null},
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
    if (!wsnShadowRoot(el)) continue;
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
  if (wsnShadowRoot(el)) return true;
  if (context.hosts && context.hosts.has(el)) return true;
  if (!el.firstElementChild) return false;
  try {
    return !!el.querySelector(WSN_CANDIDATE_SELECTOR);
  } catch (error) {
    return true;
  }
}

function wsnWalkRoot(root, depth, context, map, frameInfo) {
  const previousHosts = context.hosts;
  const previousOffsets = context.offsets;
  const previousRegistry = context.registry;
  context.hosts = wsnShadowAncestors(root);
  context.offsets = {map: map, frame: frameInfo || null};
  // A shadow root belongs to the document that hosts it; a frame document is its
  // own, and its refs have to be minted there to be resolvable there.
  if (root.nodeType === 9) {
    context.registry = wsnPruneOnce(wsnRegistryIn(root.defaultView) || previousRegistry);
  }
  const start = root.nodeType === 9 ? (root.body || root.documentElement) : root;
  if (start) wsnWalkChildren(start, depth, context);
  wsnFlushText(context);
  context.hosts = previousHosts;
  context.offsets = previousOffsets;
  context.registry = previousRegistry;
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
    const shadow = wsnShadowRoot(el);
    if (shadow) {
      wsnWalkRoot(shadow, depth, context, context.offsets.map,
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
  const shadow = wsnShadowRoot(el);
  if (shadow) {
    wsnWalkRoot(shadow, depth, context, context.offsets.map,
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
  const mapped = wsnMapBox(offsets.map, box);
  const page = mapped.page_rect;
  const tag = el.tagName.toLowerCase();
  const interactive = WSN_INTERACTIVE_ROLES.has(role);
  const node = {
    kind: 'node',
    depth: depth,
    ref: wsnHandle(el, context.registry || wsnRegistryOf(el)),
    tag: tag,
    role: role,
    name: wsnName(el, role),
    states: wsnStates(el),
    rect: local,
    page_rect: page,
    center: mapped.center,
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
  if (offsets.frame) {
    node.frame = offsets.frame.path;
    // A path that cannot be verified unique would send the caller into whichever
    // frame happens to match first, so it is handed over labelled, not silently.
    if (!offsets.frame.addressable) node.frame_addressable = false;
  }
  // A perspective chain does not project onto the page as an affine map, so the
  // box above is its best flat approximation and can be a few pixels out. Saying
  // so per node beats handing over a plausible number with nothing to doubt.
  if (!offsets.map.flat) node.page_rect_approximate = true;
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
    wsnDescendFrame(el, node, depth, context);
    return;
  }
  wsnDescend(el, depth + 1, context, node);
}

function wsnDescendFrame(el, node, depth, context) {
  let doc = null;
  try {
    doc = el.contentDocument;
  } catch (error) {
    doc = null;
  }
  node.src = wsnAttr(el, 'src');
  if (!doc) {
    node.same_origin = false;
    context.frames.cross_origin += 1;
    return;
  }
  node.same_origin = true;
  const parent = context.offsets.frame;
  if ((parent ? parent.depth : 0) >= WSN_MAX_FRAME_DEPTH) {
    // Every topic stops at the same depth, so none of them can report something
    // the others cannot reach - or hand out a ref the action tools cannot follow.
    context.frames.too_deep += 1;
    node.frame_too_deep = true;
    return;
  }
  context.frames.same_origin += 1;
  // wsnSelector is already absolute from the top document; composing it with the
  // parent's path again would name the outer frame twice. The bare tag name is
  // exactly the path that lands in the wrong document, so a frame whose selector
  // cannot be verified is carried as not addressable rather than as something the
  // caller may pass back as frame_selector.
  const selector = wsnSelector(el);
  const addressable = !!selector;
  const framePath = selector
    || ((parent ? parent.path + ' >>> ' : '') + el.tagName.toLowerCase());
  const frameInfo = {
    path: framePath,
    addressable: addressable,
    depth: (parent ? parent.depth : 0) + 1
  };
  node.frame_path = framePath;
  if (!addressable) {
    node.frame_addressable = false;
    context.frames.unaddressable += 1;
  }
  try {
    const frameWindow = el.contentWindow;
    if (frameWindow && frameWindow.__wsnRefs) {
      context.closed += frameWindow.__wsnRefs.closedShadowRoots || 0;
    }
  } catch (error) {
    // Ignore cross-origin probing failures.
  }
  // The frame's own map carries a point from its content out to this document;
  // composing it with the map already in hand carries it the rest of the way to
  // the page, however many transformed hops that is.
  const inner = wsnComposeMap(context.offsets.map, wsnFrameMap(el));
  wsnWalkRoot(doc, depth + 1, context, inner, frameInfo);
}

wsnWalkRoot(document, 0, ctx, WSN_IDENTITY_MAP, null);
wsnFlushText(ctx);

return {
  url: String(location.href),
  title: String(document.title || ''),
  dom_epoch: registry.epoch,
  closed_shadow_roots: ctx.closed,
  truncated: ctx.truncated,
  counts: ctx.counts,
  frames: ctx.frames,
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
                # The box is a flat approximation of a perspective projection, so
                # it is the one box on this page a caller should not aim at
                # without checking. Marked here rather than left to look exact.
                if node.get("page_rect_approximate"):
                    parts.append("approximate-box")
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

    A node inside a frame carries a ref minted in that frame's own document, so its
    epoch differs from the page's ``dom_epoch``; the action tools follow the ref
    into its document. Its ``frame`` path is verified to address exactly one frame,
    and is marked ``frame_addressable: false`` when no such path exists.

    ``rect`` is the element's box in its own document. ``page_rect`` and ``center``
    are that box carried into the top document through every frame hop, including
    any CSS transform, individual ``rotate``/``scale``/``translate`` property or
    ``zoom`` between the two - the same map the input tools aim through, so the
    two agree on where a thing is. A rotated box has no axis-aligned rectangle of
    its own, so ``page_rect`` is the bounding box of its mapped corners - the
    smallest rectangle containing it, which is wider than the element - while
    ``center`` is the mapped centre and is exact: it is the point to click.

    Under a 3D/perspective chain the projection is not affine, so both are the
    best flat approximation of it and can be a few pixels out; those nodes carry
    ``page_rect_approximate: true`` (``approximate-box`` in text) rather than a
    plausible number with nothing to doubt.
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
        "frames": raw.get("frames", {}),
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

function wsnTextLength(el) {
  if (!el) return 0;
  const text = el.innerText === undefined ? (el.textContent || '') : (el.innerText || '');
  return wsnClean(text, 0).length;
}

function wsnPickRoot(doc) {
  const bodyChars = wsnTextLength(doc.body);
  let articles;
  try {
    articles = doc.querySelectorAll('article');
  } catch (error) {
    articles = [];
  }
  const direct = doc.querySelector('main')
    || doc.querySelector('[role="main"]')
    // Several articles are an index page: taking the first one returns a single
    // post and drops every other, which reads exactly like a complete answer.
    || (articles.length === 1 ? articles[0] : null);
  // A landmark is only the main content while it holds most of the page's text.
  // An app shell whose <main> holds a spinner while the framework mounted
  // somewhere else looks identical from here, and reporting 'Loading...' as the
  // page is worse than reporting too much.
  if (direct && wsnTextLength(direct) * 2 >= bodyChars) {
    return {root: direct, reason: direct.tagName.toLowerCase(), body_chars: bodyChars};
  }
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
  return {
    root: best || doc.body,
    reason: best ? 'text-weight' : 'body',
    body_chars: bodyChars
  };
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
// `chars` is what the frames contributed, which the top document's own innerText
// knows nothing about: without it, "how much of the page is missing" compares two
// different universes and clamps to zero the moment a frame outweighs the loss.
const frames = {same_origin: 0, cross_origin: 0, not_loaded: 0, too_deep: 0, chars: 0};

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

function wsnWalkFrame(el, budget) {
  // An iframe renders as part of the page a person is reading, so leaving its
  // text out returns "" for a page whose whole content is one frame, and calls
  // that the page. What genuinely cannot be read - another origin - is counted
  // instead of being passed over in silence.
  let doc = null;
  try {
    doc = el.contentDocument;
  } catch (error) {
    doc = null;
  }
  if (!doc) {
    frames.cross_origin += 1;
    return;
  }
  if (!doc.body) {
    // Same origin, simply not parsed yet. Calling that "cannot be read from this
    // page at all" tells the caller to give up on text that is about to arrive.
    frames.not_loaded += 1;
    return;
  }
  if (budget.depth >= WSN_MAX_FRAME_DEPTH) {
    frames.too_deep += 1;
    return;
  }
  // Counted only once it is actually read, so the tally matches the text.
  frames.same_origin += 1;
  frames.chars += wsnTextLength(doc.body);
  budget.depth += 1;
  chunks.push('\n\n');
  wsnTextWalk(doc.body, budget);
  chunks.push('\n\n');
  budget.depth -= 1;
}

function wsnTextWalk(el, budget) {
  if (budget.nodes > 60000) return;
  budget.nodes += 1;
  const tag = el.tagName;
  if (WSN_SKIP_TAGS.has(tag)) return;
  if (wsnDropped(el)) return;
  if (!wsnDisplayed(el)) return;
  if (tag === 'IFRAME' || tag === 'FRAME') {
    wsnWalkFrame(el, budget);
    return;
  }
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
  const source = wsnShadowRoot(el) || el;
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

function wsnAppendDialogs(root) {
  // An open modal is the page as far as the person looking at it is concerned.
  // It usually lives outside the main landmark, so main mode would drop the one
  // thing standing between the caller and everything else.
  let dialogs;
  try {
    dialogs = document.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"]');
  } catch (error) {
    return 0;
  }
  const appended = [];
  for (const dialog of dialogs) {
    if (dialog.tagName === 'DIALOG' && !dialog.open) continue;
    if (root && (root === dialog || wsnContains(root, dialog))) continue;
    // An alertdialog inside a dialog is one overlay, not two. Document order puts
    // the outer one first, so anything already inside what was appended is
    // already in the text - emitting it again reads as two separate warnings.
    if (appended.some(other => other === dialog || wsnContains(other, dialog))) continue;
    if (!wsnDisplayed(dialog) || wsnHiddenBy(dialog)) continue;
    const restore = mainOnly;
    mainOnly = false;  // a login modal is a <form>, which main mode calls chrome
    chunks.push('\n\n');
    wsnTextWalk(dialog, {nodes: 0, depth: 0});
    mainOnly = restore;
    appended.push(dialog);
  }
  return appended.length;
}

// 'full' means the whole rendered body. Guessing a main-content sub-tree here is
// what let an app shell answer for a page it had nothing to do with.
const picked = mainOnly
  ? wsnPickRoot(document)
  : {root: document.body, reason: 'body', body_chars: wsnTextLength(document.body)};
let root = picked.root;
let reason = picked.reason;
let fallbackUsed = false;
if (root) wsnTextWalk(root, {nodes: 0, depth: 0});
let dialogsAppended = wsnAppendDialogs(root);
// Emptiness is the only reliable signal: on a page that is one big <form> the
// noise list eats everything, and an over-eager root guess does the same. Either
// way the caller must not be handed a blank page with no way to tell why.
if (!chunks.join('').trim() && document.body && (mainOnly || root !== document.body)) {
  chunks.length = 0;
  links.length = 0;
  frames.same_origin = 0;
  frames.cross_origin = 0;
  frames.not_loaded = 0;
  frames.too_deep = 0;
  frames.chars = 0;
  fallbackUsed = true;
  mainOnly = false;
  root = document.body;
  reason = 'body-fallback';
  wsnTextWalk(root, {nodes: 0, depth: 0});
  dialogsAppended = wsnAppendDialogs(root);
}

return {
  url: String(location.href),
  title: String(document.title || ''),
  root_tag: root ? root.tagName.toLowerCase() : '',
  root_selector: root ? wsnSelector(root) : '',
  root_reason: reason,
  fallback_used: fallbackUsed,
  body_chars: picked.body_chars || 0,
  frames: frames,
  dialogs_appended: dialogsAppended,
  text: chunks.join(''),
  links: links
};
"""


_MULTI_NEWLINE = re.compile(r"\n{3,}")
_WHITESPACE_RUN = re.compile(r"\s+")
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


def _text_exclusions(
    raw: dict[str, Any],
    full_text: str,
    mode: str,
    fallback_used: bool,
    truncated: bool,
    frames: dict[str, Any],
) -> tuple[int, list[str]]:
    """Say what this text does not contain, in the caller's own units.

    A sub-tree that reads like a whole page is the failure worth naming: the
    result is plausible, self-consistent, and wrong, and nothing in it hints that
    the rest of the page exists.

    Both sides are measured in one universe. ``body_chars`` is the top document's
    own rendered text and knows nothing about frames, while the text returned
    includes them, so subtracting one from the other clamped to zero the moment a
    frame outweighed what main mode had dropped - and reported "nothing is
    missing" for a page whose navigation and footer had both been thrown away.
    Link markers are removed before counting because they are this tool's own
    markup rather than the page's words; the count is otherwise exact to within
    the few characters of heading and cell punctuation that get inserted.
    """
    kept = len(_WHITESPACE_RUN.sub(" ", _MARKER_PATTERN.sub("", full_text)).strip())
    body_chars = int(raw.get("body_chars") or 0)
    frame_chars = int(frames.get("chars") or 0)
    rendered = body_chars + frame_chars
    missing = max(0, rendered - kept)
    reasons: list[str] = []
    if missing:
        reasons.append(
            f"{missing} of the {rendered} characters this page renders are not here"
        )
        if mode == "main" and not fallback_used:
            reasons.append(
                f"mode='main' kept only {raw.get('root_selector') or raw.get('root_tag') or 'a sub-tree'} "
                "and dropped nav/header/footer/aside/form chrome - call again with mode='full'"
            )
        else:
            reasons.append("aria-hidden, [hidden] and off-layout subtrees are never read")
    cross_origin = int(frames.get("cross_origin") or 0)
    if cross_origin:
        reasons.append(
            f"{cross_origin} cross-origin frame(s) cannot be read from this page at all"
        )
    not_loaded = int(frames.get("not_loaded") or 0)
    if not_loaded:
        reasons.append(
            f"{not_loaded} same-origin frame(s) had not parsed a body yet; read again in a moment"
        )
    if int(frames.get("too_deep") or 0):
        reasons.append(
            f"{frames['too_deep']} frame(s) nested deeper than {MAX_FRAME_DEPTH} were not entered"
        )
    if truncated:
        reasons.append("clipped at max_chars; raise max_chars for the rest")
    return missing, reasons


def page_text(
    driver: Any,
    *,
    max_chars: int = 20000,
    mode: str = "main",
    include_links: bool = False,
) -> dict[str, Any]:
    """Extract the readable text of the rendered page, keeping block structure.

    ``mode='full'`` is the whole rendered ``<body>``, including same-origin frames
    and open dialogs. ``mode='main'`` narrows to the main-content sub-tree and also
    drops navigation, header, footer, aside and form chrome; because that can drop a
    lot, ``excluded_chars`` says how much of the rendered body is not in the result
    and ``excluded`` says why, so a partial read is never handed over as the page.

    A page whose whole content is chrome - a login or checkout form - would come
    back empty, so an empty result is retried without the noise list and reported
    as ``fallback_used``.
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
    frames = raw.get("frames") or {}
    excluded_chars, excluded = _text_exclusions(
        raw, full_text, selected_mode, fallback_used, truncated, frames
    )
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
        "body_chars": int(raw.get("body_chars") or 0),
        "excluded_chars": excluded_chars,
        "excluded": excluded,
        "frames": frames,
        "dialogs_appended": int(raw.get("dialogs_appended") or 0),
        "truncated": truncated,
        "max_chars": limit,
        **({"links": links} if include_links else {}),
    }


# ---------------------------------------------------------------------------
# Single-element extraction
# ---------------------------------------------------------------------------

# ``innerText`` reports only the *rendered* text of an overflowing block: the
# scrolled-out tail of a chat code panel simply does not exist there, and the
# whole point of this topic is to hand over that tail. ``full_text`` switches
# to ``textContent``, which is not clipped by overflow - at the price of also
# including nodes hidden by ``display:none`` or ``visibility``, fine for code.
_ELEMENT_TEXT_SCRIPT = _JS_LIB + r"""
const el = arguments[0];
const mode = arguments[1];
const fullText = arguments[2];
if (!el) return {found: false};
const read = (element, forceFull) => {
  if (forceFull) return element.textContent || '';
  return element.innerText === undefined ? (element.textContent || '') : (element.innerText || '');
};
const rect = el.getBoundingClientRect();
const box = {
  x: rect.x, y: rect.y,
  width: rect.width, height: rect.height,
  scroll_height: el.scrollHeight || 0,
  client_height: el.clientHeight || 0
};
const own = fullText ? (el.textContent || '') : read(el, false);
const result = {
  found: true,
  url: location.href,
  title: document.title,
  tag: (el.tagName || '').toLowerCase(),
  text: own,
  box: box
};
if (mode === 'html' || mode === 'both') result.html = el.innerHTML || '';
if (mode === 'outer' || mode === 'both') result.outer_html = el.outerHTML || '';
return result;
"""


def element_text(
    driver: Any,
    element: Any,
    *,
    mode: str = "text",
    full_text: bool = False,
    max_chars: int = 20000,
) -> dict[str, Any]:
    """Extract one element's content instead of a clipped slice of the page.

    ``mode`` is ``text`` (rendered text), ``html`` (innerHTML), ``outer``
    (outerHTML) or ``both`` (text plus both markup forms). With ``full_text``
    the text comes from ``textContent`` rather than ``innerText``: overflow
    clipping (a scrolled code block, a collapsed accordion) stops hiding the
    tail, at the price of also counting ``display:none`` subtrees.
    """
    selected = str(mode or "text").strip().lower()
    if selected not in {"text", "html", "outer", "both"}:
        raise ValueError("mode must be 'text', 'html', 'outer' or 'both'")
    limit = max(200, min(int(max_chars), _MAX_TEXT_CHARS))
    raw = driver.execute_script(_ELEMENT_TEXT_SCRIPT, element, selected, bool(full_text))
    if not isinstance(raw, dict):
        raise RuntimeError("Element text script returned an unexpected result")
    if not raw.get("found"):
        return {"found": False, "mode": selected, "full_text": bool(full_text)}
    text, truncated = _clip_on_boundary(_normalize_text(str(raw.get("text") or "")), limit)
    html = str(raw.get("html") or "")
    outer = str(raw.get("outer_html") or "")
    clipped_html = (html[:limit] + ("…" if len(html) > limit else "")) if html else ""
    clipped_outer = (outer[:limit] + ("…" if len(outer) > limit else "")) if outer else ""
    return {
        "found": True,
        "mode": selected,
        "full_text": bool(full_text),
        "url": raw.get("url", ""),
        "title": raw.get("title", ""),
        "tag": raw.get("tag", ""),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "max_chars": limit,
        "box": raw.get("box") or {},
        **({"html": clipped_html} if selected in {"html", "both"} else {}),
        **({"outer_html": clipped_outer} if selected in {"outer", "both"} else {}),
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
const registry = wsnPruneOnce(wsnRegistry());

// A match at or above this is an answer; below it the tool is guessing. 25 sits
// just under "every token of the query appears in the element's own name" (34)
// and far above a bare role-synonym brush (about 7).
const WSN_MATCH_THRESHOLD = 25;

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

// `depth` counts frames only. Shadow nesting is finite by construction and
// costs nothing to follow, while a frame can embed itself forever - and the
// depth every topic stops at has to be the same one.
function wsnCollect(root, offsets, out, depth) {
  if (out.length > 4000) return;
  let elements;
  try {
    elements = root.querySelectorAll('*');
  } catch (error) {
    return;
  }
  for (const el of elements) {
    if (WSN_SKIP_TAGS.has(el.tagName)) continue;
    const shadow = wsnShadowRoot(el);
    if (shadow) wsnCollect(shadow, offsets, out, depth);
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
      if (doc) {
        // wsnSelector is absolute from the top document already; a frame with no
        // verifiable path is reported as having none rather than as a bare tag
        // that would address a different document. The map is the outline's:
        // this frame's own hop composed with everything above it, so a box comes
        // out where the page paints it and not merely where its host sits.
        wsnCollect(doc, {map: wsnComposeMap(offsets.map, wsnFrameMap(el)),
                         frame: wsnSelector(el) || null}, out, depth + 1);
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

let framesTooDeep = 0;
const candidates = [];
wsnCollect(document, {map: WSN_IDENTITY_MAP, frame: null}, candidates, 0);

const view = {width: window.innerWidth, height: window.innerHeight};
const scored = [];
let anyInteractive = false;
let hiddenSkipped = 0;

for (let index = 0; index < candidates.length; index += 1) {
  const el = candidates[index].el;
  const offsets = candidates[index].offsets;
  const role = wsnRole(el);
  if (role === 'hidden') continue;
  // The outline stops at the first aria-hidden or [hidden] ancestor. Checking
  // only the element itself let a hidden copy of a real control outrank it, so
  // the two topics reported different pages and neither said which was wrong.
  if (wsnHiddenBy(el)) {
    hiddenSkipped += 1;
    continue;
  }
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
  // An aria-label wins the accessible name and hides the element's own words from
  // the scorer, so a button reading "Send message" scored nothing against "Send
  // message" and the tool called itself unsure of the one right answer. What a
  // person can see is what they will ask for, so it is scored as its own field.
  let ownText = '';
  if (!WSN_STRUCTURAL_ROLES.has(role)) {
    const rendered = el.innerText === undefined ? el.textContent : el.innerText;
    const cleaned = wsnClean(rendered, WSN_NAME_LIMIT);
    if (cleaned !== name) ownText = cleaned;
  }
  const fields = [
    ['name', 1.00, name],
    ['text', 0.90, ownText],
    ['placeholder', 0.80, wsnAttr(el, 'placeholder')],
    ['title', 0.65, wsnAttr(el, 'title')],
    ['testid', 0.60, wsnSplitIdentifier(testId)],
    ['name_id', 0.50, wsnSplitIdentifier(wsnAttr(el, 'name')) + ' ' + wsnSplitIdentifier(el.id)],
    ['role', 0.40, role + ' ' + synonyms.join(' ')],
    ['value', 0.40, value],
    ['href', 0.30, hrefTail]
  ];
  // How well this element answers the query, and nothing else. Kept apart from
  // the ranking score below, which mixes in where the element sits and whether
  // it can be clicked - useful for ordering, worthless for deciding whether the
  // page holds an answer at all.
  let matchScore = 0;
  let matchedField = '';
  for (const field of fields) {
    const component = wsnFieldScore(normalizedQuery, queryTokens, field[2]);
    if (component <= 0) continue;
    const weighted = component * field[1];
    if (weighted > matchScore) {
      matchScore = weighted;
      matchedField = field[0];
    }
  }
  if (matchScore <= 0) continue;
  let score = matchScore;
  const local = {x: Math.round(box.left), y: Math.round(box.top), w: width, h: height};
  const mapped = wsnMapBox(offsets.map, box);
  const page = mapped.page_rect;
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
    match_score: matchScore,
    matched_field: matchedField,
    rect: local,
    page_rect: page,
    center: mapped.center,
    approximate: !offsets.map.flat,
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
  item.match_score = Math.round(item.match_score * 10) / 10;
}

scored.sort((left, right) => (right.score - left.score) || (left.index - right.index));

// The bar has to be applied to the match, not to the ranking score: being in the
// viewport, looking actionable and being enabled are worth 36 points between
// them, so on any actionish query every live control on the page cleared a bar
// of 25 without matching a single word of it. A role filter is a filter, too -
// asking for a button and being handed a link is not a confident answer.
const qualified = scored.filter(item => item.match_score >= WSN_MATCH_THRESHOLD
  && (!wantedRole || item.role === wantedRole));
let selected = qualified.slice(0, limit);
let lowConfidence = false;
if (!selected.length) {
  // Nothing here answers the query. The closest few are still returned - seeing
  // what the page does have beats an empty result - but as the guess they are.
  lowConfidence = true;
  selected = scored.slice(0, Math.min(3, limit));
}
// Neither the match nor the ranking could separate the top two, so which one
// came first was decided by document order. That is not a confidence problem -
// both are good matches - and it needs its own flag rather than sharing one.
// Read from the qualified pool rather than from what survived `limit`: the
// caller who asked for exactly one answer is the one who most needs to know the
// second one was just as good.
const ambiguous = !lowConfidence && qualified.length > 1
  && (qualified[0].match_score - qualified[1].match_score) < 5
  && (qualified[0].score - qualified[1].score) < 5;

const matches = selected.map(item => {
  const match = {
    // Minted in the element's own document: a handle from another browsing context
    // resolves to nothing the driver can act on.
    ref: wsnHandleFor(item.el),
    role: item.role,
    name: item.name,
    score: item.score,
    match_score: item.match_score,
    matched_field: item.matched_field,
    rect: item.rect,
    page_rect: item.page_rect,
    center: item.center,
    visible: item.visible,
    occluded: item.occluded,
    frame: item.frame
  };
  // A perspective chain projects onto the page without an affine map, so the box
  // is its flat approximation; the outline says the same thing on the same key.
  if (item.approximate) match.page_rect_approximate = true;
  return match;
});

return {
  url: String(location.href),
  title: String(document.title || ''),
  dom_epoch: registry.epoch,
  candidates: candidates.length,
  frames_too_deep: framesTooDeep,
  scored: scored.length,
  matched: qualified.length,
  returned: matches.length,
  truncated: qualified.length > matches.length,
  match_threshold: WSN_MATCH_THRESHOLD,
  matches: matches,
  aria_hidden_skipped: hiddenSkipped,
  ambiguous: ambiguous,
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
    """Find elements by meaning; all scoring happens inside the page in one round-trip.

    Elements under an ``aria-hidden="true"`` or ``[hidden]`` ancestor are skipped,
    exactly as the outline skips them, and counted in ``aria_hidden_skipped``: a
    hidden duplicate of a real control must never outrank the control.

    Every match carries two numbers, because one cannot say both things:

    ``match_score`` (0-100) is how well the query matched that element and nothing
    else - 100 the whole field, 62 a prefix, 45 a substring, 34 every query token
    present - multiplied by the field's weight (``name`` 1.0, the element's own
    visible ``text`` when an accessible name overrode it 0.9, ``placeholder`` 0.8,
    ``title`` 0.65, ``testid`` 0.6, ``name``/``id`` 0.5, ``role`` and ``value``
    0.4, ``href`` 0.3). ``matched_field`` names the field it came from.

    ``score`` is the ranking score: ``match_score`` plus where the element sits and
    whether it can be used (in the viewport, action-shaped query, requested role,
    enabled, not occluded). It orders the results; it says nothing about relevance,
    which is why ``low_confidence`` is derived from ``match_score`` against
    ``match_threshold`` and never from it.

    ``low_confidence`` means nothing on the page answered the query - the matches
    are the closest things found, offered as a guess. ``ambiguous`` is a different
    failure and has its own flag: the top two matched equally well *and* ranked
    equally, so which came first was decided by document order alone.

    ``rect``, ``page_rect``, ``center`` and ``page_rect_approximate`` mean exactly
    what they mean in ``outline``: ``center`` is the element's centre carried into
    the top document through every frame transform and is the point to click,
    ``page_rect`` is the bounding box of the mapped corners, and the flag marks a
    box that only approximates a perspective projection.

    ``candidates`` were examined, ``scored`` resembled the query at all (weak
    role-word brushes included), ``matched`` cleared ``match_threshold``, and
    ``returned`` fit inside ``limit``; ``truncated`` says ``matched`` did not.
    Under ``low_confidence`` nothing cleared the bar, so ``matched`` is 0 while
    ``returned`` counts the guesses handed over anyway - the one case where
    ``returned`` exceeds ``matched``. ``frames_too_deep`` counts frames nested
    past ``MAX_FRAME_DEPTH``, which no topic enters.
    """
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
        "ambiguous": bool(raw.get("ambiguous")),
        "match_threshold": int(raw.get("match_threshold") or 0),
        "candidates": int(raw.get("candidates") or 0),
        "scored": int(raw.get("scored") or 0),
        "matched": int(raw.get("matched") or 0),
        "frames_too_deep": int(raw.get("frames_too_deep") or 0),
        "returned": int(raw.get("returned") or len(raw.get("matches") or [])),
        "truncated": bool(raw.get("truncated")),
        "aria_hidden_skipped": int(raw.get("aria_hidden_skipped") or 0),
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


# Which browsing context minted an epoch. Element references belong to one
# context, so the answer is not "where is the node" but "which frame must the
# driver be in before the node can be handed over at all".
FRAME_FOR_EPOCH_SCRIPT = _JS_LIB + r"""
const wanted = String(arguments[0]).toLowerCase();
const targetNumber = Number(arguments[1] || 0);
const targetToken = String(arguments[2] || '');

function wsnMatches(win) {
  if (targetToken) {
    try {
      return win.document.__wsnActionRefTarget === targetToken;
    } catch (error) {
      return false;
    }
  }
  return wsnEpochOf(win) === wanted;
}

function wsnEpochOf(win) {
  try {
    const registry = win.__wsnRefs;
    return registry && registry.epoch ? String(registry.epoch).toLowerCase() : '';
  } catch (error) {
    return '';  // cross-origin: nothing in there was ever handed out
  }
}

function wsnFrameElements(root, out) {
  let frames;
  try {
    frames = root.querySelectorAll('iframe, frame');
  } catch (error) {
    return out;
  }
  for (const el of frames) out.push(el);
  let hosts;
  try {
    hosts = root.querySelectorAll('*');
  } catch (error) {
    return out;
  }
  for (const el of hosts) {
    const shadow = wsnShadowRoot(el);
    if (shadow) wsnFrameElements(shadow, out);
  }
  return out;
}

// Searched two levels deeper than any topic walks, so a ref that was minted can
// always be followed home. Hitting the bound is remembered, because "we did not
// look that far" and "it is gone" call for opposite things from the caller.
const WSN_SEARCH_DEPTH = WSN_MAX_FRAME_DEPTH + 2;
let depthLimited = false;

function wsnFindTargetDocument(win, depth) {
  if (!win) return null;
  if (depth > WSN_SEARCH_DEPTH) {
    depthLimited = true;
    return null;
  }
  let doc = null;
  try {
    doc = win.document;
    const registry = win.__wsnRefs;
    if (registry && registry.nodes
        && String(registry.epoch).toLowerCase() === wanted) {
      const node = registry.nodes.get(targetNumber);
      const owner = node && node.isConnected ? node.ownerDocument : null;
      if (owner && owner.defaultView) return owner;
    }
  } catch (error) {
    return null;
  }
  if (!doc) return null;
  for (const el of wsnFrameElements(doc, [])) {
    let child = null;
    try {
      child = el.contentWindow;
    } catch (error) {
      child = null;
    }
    const found = child ? wsnFindTargetDocument(child, depth + 1) : null;
    if (found) return found;
  }
  return null;
}

if (targetNumber) {
  const targetDocument = wsnFindTargetDocument(window, 0);
  if (!targetDocument) return {missing: true, depth_limited: depthLimited};
  try {
    targetDocument.__wsnActionRefTarget = targetToken;
  } catch (error) {
    return {missing: true, depth_limited: depthLimited};
  }
}

function wsnHolds(win, depth) {
  if (!win) return false;
  if (depth > WSN_SEARCH_DEPTH) {
    depthLimited = true;
    return false;
  }
  let doc = null;
  try {
    doc = win.document;
  } catch (error) {
    return false;
  }
  if (!doc) return false;
  if (wsnMatches(win)) return true;
  for (const el of wsnFrameElements(doc, [])) {
    let child = null;
    try {
      child = el.contentWindow;
    } catch (error) {
      child = null;
    }
    if (child && wsnHolds(child, depth + 1)) return true;
  }
  return false;
}

if (wsnMatches(window)) {
  if (targetToken) {
    try { delete document.__wsnActionRefTarget; } catch (error) {}
  }
  return {here: true};
}
for (const el of wsnFrameElements(document, [])) {
  let child = null;
  try {
    child = el.contentWindow;
  } catch (error) {
    child = null;
  }
  if (child && wsnHolds(child, 1)) return {frame: el, path: wsnSelector(el)};
}
return {missing: true, depth_limited: depthLimited};
"""


# One document's worth of a piercing path. A frame boundary is returned rather
# than crossed, because an element read through ``contentDocument`` from the
# parent document is not a handle any action can use.
PIERCING_STEP_SCRIPT = r"""
const parts = arguments[0];
let root = document;
let element = null;
function shadowRootOf(host) {
  if (host.shadowRoot) return host.shadowRoot;
  const registry = window.__wsnRefs;
  const roots = registry && registry.closedShadowRootsByHost;
  return roots && roots.get ? (roots.get(host) || null) : null;
}
for (let index = 0; index < parts.length; index += 1) {
  if (!root || !root.querySelector) return {missing: true, at: index};
  try {
    element = root.querySelector(parts[index]);
  } catch (error) {
    return {invalid: true, at: index};
  }
  if (!element) return {missing: true, at: index};
  if (index === parts.length - 1) return {element: element};
  const shadow = shadowRootOf(element);
  if (shadow) {
    root = shadow;
    continue;
  }
  if (element.tagName === 'IFRAME' || element.tagName === 'FRAME') {
    return {frame: element, rest: parts.slice(index + 1)};
  }
  root = element;
}
return {missing: true, at: 0};
"""


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
        "const registry = window.__wsnRefs;"
        "const roots = registry && registry.closedShadowRootsByHost;"
        "let next = element.shadowRoot || (roots && roots.get ? roots.get(element) : null);"
        "if (!next && element.tagName === 'IFRAME') {"
        "try { next = element.contentDocument; } catch (error) { next = null; }"
        "}"
        "root = next || element;"
        "}"
        "return element;"
        "})()"
    )
