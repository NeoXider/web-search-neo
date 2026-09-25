"""click_text's page-side matcher and the Python half of its answer.

``find`` and ``page_outline`` already walk open shadow roots (and closed ones the
ref registry captured) and same-origin frames; a click by text that stopped at the
top document could not reach what they had just reported. This matcher walks the
same way: every shadow root through ``wsnShadowRoot``, every same-origin frame
through ``contentDocument`` down to ``WSN_MAX_FRAME_DEPTH``, and it counts the
cross-origin frames it could not enter instead of pretending they are empty.

Strictness is unchanged: exact rendered text (or substring with ``exact=False``),
optional role, a candidate CSS selector applied inside every root, and anything
other than exactly one visible match is refused with the count. The click itself
stays the caller's verified pointer dispatch: the script answers with the target's
centre in the viewport of the document it ran in, carried out of every frame it
crossed by ``wsnFrameMap`` - the same map outline boxes and input use - and
hit-tested at every hop, so an overlay in the element's document or in any frame
above it is named rather than clicked.
"""

from __future__ import annotations

from typing import Any

from web_search_neo import page_perception


CLICK_TEXT_SCRIPT = page_perception.JS_LIBRARY + r"""
const wanted = String(arguments[0] || '');
const exact = !!arguments[1];
const wantedRole = String(arguments[2] || '').trim().toLowerCase();
const candidateSelector = String(arguments[3] || '').trim() ||
  'button, a[href], label, [role="button"], [role="link"], [role="option"], ' +
  '[role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"]';
const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
const needle = norm(wanted);
if (!needle) throw new Error('text must not be empty');
const reach = {frames: 0, shadow_roots: 0, cross_origin_frames: 0, frames_too_deep: 0};
const found = [];
// `frames` is the chain of frame elements crossed from the script's document,
// outermost first; `hosts` the shadow hosts crossed inside the innermost one.
function walk(root, frames, hosts, depth) {
  let nodes;
  try { nodes = root.querySelectorAll(candidateSelector); }
  catch (error) { throw new Error('Invalid selector: ' + candidateSelector); }
  for (const el of nodes) found.push({el, frames, hosts});
  let all;
  try { all = root.querySelectorAll('*'); } catch (error) { return; }
  for (const el of all) {
    const shadow = wsnShadowRoot(el);
    if (shadow) {
      reach.shadow_roots += 1;
      walk(shadow, frames, hosts.concat([el]), depth);
    }
    if (el.tagName !== 'IFRAME' && el.tagName !== 'FRAME') continue;
    let doc = null;
    try { doc = el.contentDocument; } catch (error) { doc = null; }
    if (!doc) { reach.cross_origin_frames += 1; continue; }
    if (depth >= WSN_MAX_FRAME_DEPTH) { reach.frames_too_deep += 1; continue; }
    reach.frames += 1;
    walk(doc, frames.concat([el]), [], depth + 1);
  }
}
walk(document, [], [], 0);
const implicitRole = el => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'button') return 'button';
  if (tag === 'a' && el.hasAttribute('href')) return 'link';
  if (tag === 'input') {
    const type = (el.type || '').toLowerCase();
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
  }
  return '';
};
const roleOf = el => (el.getAttribute('role') || implicitRole(el)).toLowerCase();
// Composed parent: through an assigned slot, a shadow host and a frame element, so
// aria-hidden on a host or an outer page hides what is inside it too.
const composedParent = node => {
  if (node.assignedSlot) return node.assignedSlot;
  if (node.parentElement) return node.parentElement;
  const root = node.getRootNode ? node.getRootNode() : null;
  if (root && root.host) return root.host;
  try {
    const view = node.ownerDocument ? node.ownerDocument.defaultView : null;
    return view && view !== window.top ? view.frameElement : null;
  } catch (error) { return null; }
};
const composedContains = (el, node) => {
  for (let current = node, hops = 0; current && hops < 400; hops += 1) {
    if (current === el) return true;
    current = composedParent(current);
  }
  return false;
};
const styleOf = el => {
  const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
  return view && view.getComputedStyle ? view.getComputedStyle(el) : null;
};
const visibleBox = el => {
  const style = styleOf(el);
  const rect = el.getBoundingClientRect();
  return !!(rect.width && rect.height && (!style || (style.display !== 'none' &&
    style.visibility !== 'hidden' && style.opacity !== '0')));
};
const ariaHidden = el => {
  for (let node = el, hops = 0; node && hops < 400; node = composedParent(node), hops += 1) {
    if (node.nodeType === 1 && node.getAttribute('aria-hidden') === 'true') return true;
  }
  return false;
};
// innerText does not follow a slot to what is assigned to it, nor into a host's
// shadow tree, so a control whose label is projected needs the flat-tree text.
const flatText = node => {
  if (node.nodeType === 3) return node.nodeValue || '';
  if (node.nodeType !== 1 && node.nodeType !== 11) return '';
  if (node.tagName === 'SLOT') {
    const assigned = node.assignedNodes({flatten: true});
    return (assigned.length ? assigned : Array.from(node.childNodes)).map(flatText).join(' ');
  }
  if (node.nodeType === 1 && WSN_SKIP_TAGS.has(node.tagName)) return '';
  const shadow = node.nodeType === 1 ? wsnShadowRoot(node) : null;
  return Array.from((shadow || node).childNodes).map(flatText).join(' ');
};
const name = el => {
  const own = el.getAttribute('aria-label') || el.innerText || el.value;
  if (own) return norm(own);
  const flat = (wsnShadowRoot(el) || el.querySelector('slot')) ? norm(flatText(el)) : '';
  return norm(flat || el.getAttribute('title') || el.textContent || '');
};
const seen = new Set();
const matches = found.filter(entry => {
  const el = entry.el;
  if (seen.has(el)) return false;
  seen.add(el);
  if (!visibleBox(el) || ariaHidden(el)) return false;
  if (entry.frames.some(frame => !visibleBox(frame))) return false;
  if (wantedRole && roleOf(el) !== wantedRole) return false;
  const label = name(el);
  return exact ? label === needle : label.includes(needle);
});
const where = entry => {
  const selector = wsnSelector(entry.el);
  const inner = entry.frames.length ? entry.frames[entry.frames.length - 1] : null;
  const frame = inner ? (wsnSelector(inner) || null) : null;
  let shadowPath = null;
  if (entry.hosts.length) {
    const prefix = frame ? frame + ' >>> ' : '';
    shadowPath = prefix && selector.startsWith(prefix) ? selector.slice(prefix.length) : selector;
  }
  return {
    selector, frame, frame_depth: entry.frames.length,
    shadow_path: shadowPath || null, shadow_depth: entry.hosts.length
  };
};
const describe = entry => ({text: name(entry.el), role: roleOf(entry.el), ...where(entry)});
if (matches.length !== 1) {
  return {ok: false, count: matches.length, reach, samples: matches.slice(0, 8).map(describe)};
}
const target = matches[0];
const el = target.el;
const frames = target.frames;
const pageMap = () => frames.reduce((map, frame) => wsnComposeMap(map, wsnFrameMap(frame)),
                                    WSN_IDENTITY_MAP);
const centre = () => {
  const rect = el.getBoundingClientRect();
  return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
};
const inView = (view, x, y) => x >= 0 && y >= 0 && x < view.innerWidth && y < view.innerHeight;
let local = centre();
let point = wsnMapPoint(pageMap(), local.x, local.y);
if (!inView(el.ownerDocument.defaultView, local.x, local.y) || !inView(window, point.x, point.y)) {
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  local = centre();
  point = wsnMapPoint(pageMap(), local.x, local.y);
}
// What the browser would hit at a point of one document, followed down through
// the shadow roots on the way; null means nothing answered (an unpainted tab).
const deepHit = (doc, x, y) => {
  let root = doc;
  let hit = null;
  for (let depth = 0; depth < 32; depth += 1) {
    const next = root.elementFromPoint ? root.elementFromPoint(x, y) : null;
    if (!next || next === hit) break;
    hit = next;
    const shadow = wsnShadowRoot(hit);
    if (!shadow) break;
    root = shadow;
  }
  return hit;
};
const label = node => name(node) || wsnSelector(node) || node.tagName.toLowerCase();
const refusal = blocker => ({
  ok: false, count: 1, occluded: true, blocker, reach, samples: [describe(target)]
});
// A text node slotted straight into a host is hit as the host itself: hit-testing
// retargets it there and its shadow root answers with the host again. That host
// is the target's own composed ancestor, not something painted over it.
const reaches = hit => composedContains(el, hit) ||
  (!!wsnShadowRoot(hit) && composedContains(hit, el));
let unavailable = false;
const own = deepHit(el.ownerDocument, local.x, local.y);
if (!own) unavailable = true;
else if (!reaches(own)) return refusal(label(own));
let hop = {x: local.x, y: local.y};
for (let index = frames.length - 1; index >= 0; index -= 1) {
  const frame = frames[index];
  hop = wsnMapPoint(wsnFrameMap(frame), hop.x, hop.y);
  const holder = frame.ownerDocument;
  if (!inView(holder.defaultView, hop.x, hop.y)) {
    return refusal('the edge of ' + (wsnSelector(frame) || 'its frame') + ' (the point is scrolled out of it)');
  }
  const hit = deepHit(holder, hop.x, hop.y);
  if (!hit) { unavailable = true; continue; }
  if (hit !== frame) return refusal(label(hit));
}
return {
  ok: true, count: 1, x: point.x, y: point.y, text: name(el), role: roleOf(el),
  ...where(target), reach, hit_test_unavailable: unavailable,
  approximate: frames.length ? !pageMap().flat : false
};
"""


def _reach_note(reach: dict[str, Any]) -> str:
    """Say which documents the uniqueness check could not see, or ``''``."""
    parts = []
    hidden = int(reach.get("cross_origin_frames") or 0)
    if hidden:
        parts.append(
            f"{hidden} cross-origin frame(s) were not searched: a script of this page "
            "cannot read them. Pass frame_selector naming one to match inside it."
        )
    deep = int(reach.get("frames_too_deep") or 0)
    if deep:
        parts.append(
            f"{deep} frame(s) nested deeper than {page_perception.MAX_FRAME_DEPTH} "
            "levels were not searched."
        )
    return " ".join(parts)


def refusal(match: dict[str, Any]) -> ValueError:
    """The error for anything other than one reachable, unobstructed match."""
    note = _reach_note(match.get("reach") or {})
    if match.get("occluded"):
        return ValueError(
            f"The unique text match is covered by {match.get('blocker')!r}; "
            "inspect the page again before clicking."
        )
    return ValueError(
        f"Expected exactly one visible text match, found {int(match.get('count', 0))}. "
        f"Matches: {match.get('samples') or []}. Narrow with role or selector."
        + (f" {note}" if note else "")
    )


def answer_fields(match: dict[str, Any], frame_selector: str | None) -> dict[str, Any]:
    """Where the clicked element was, for the click_text answer.

    ``matched_selector`` is the absolute piercing path (``host >>> button``,
    ``iframe#f >>> button``) that ``click``/``find`` accept back; ``frame`` is the
    frame that holds it (``frame_selector`` when the match sits directly in the
    document that was named), and ``shadow_path`` its path inside that document
    when it lives in a shadow tree.
    """
    reach = match.get("reach") or {}
    fields: dict[str, Any] = {
        "matched_text": match.get("text", ""),
        "matched_role": match.get("role", ""),
        "matched_selector": match.get("selector", ""),
        "hit_test_unavailable": bool(match.get("hit_test_unavailable")),
        "frame": match.get("frame") or frame_selector or None,
        "shadow_path": match.get("shadow_path") or None,
        "frame_selector": frame_selector,
    }
    if match.get("approximate"):
        fields["point_approximate"] = True
    if reach.get("frames") or reach.get("shadow_roots"):
        fields["searched"] = {
            "frames": int(reach.get("frames") or 0),
            "shadow_roots": int(reach.get("shadow_roots") or 0),
        }
    note = _reach_note(reach)
    if note:
        fields["cross_origin_frames"] = int(reach.get("cross_origin_frames") or 0)
        fields["frames_note"] = "The match is unique among the documents searched. " + note
    return fields
