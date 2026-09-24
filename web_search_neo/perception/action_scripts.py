"""Page-side JavaScript the action tools send (pure sources, no runtime imports).

Moved out of ``browser_tools.py`` unchanged; re-exported there under the same names.
``_replay_window_note`` is the one helper: the note that goes with ``_REPLAY_SCRIPT``.
"""

import json
from typing import Any

from web_search_neo import page_perception


_CLICK_TEXT_SCRIPT = page_perception.JS_LIBRARY + r"""
const wanted = String(arguments[0] || '');
const exact = !!arguments[1];
const wantedRole = String(arguments[2] || '').trim().toLowerCase();
const candidateSelector = String(arguments[3] || '').trim() ||
  'button, a[href], label, [role="button"], [role="link"], [role="option"], ' +
  '[role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"]';
const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
const needle = norm(wanted);
if (!needle) throw new Error('text must not be empty');
let nodes;
try { nodes = Array.from(document.querySelectorAll(candidateSelector)); }
catch (error) { throw new Error('Invalid selector: ' + candidateSelector); }
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
const visible = el => {
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return !!(rect.width && rect.height && style.display !== 'none' &&
    style.visibility !== 'hidden' && style.opacity !== '0' &&
    !el.closest('[aria-hidden="true"]'));
};
const name = el => norm(
  el.getAttribute('aria-label') || el.innerText || el.value ||
  el.getAttribute('title') || el.textContent || ''
);
const matches = nodes.filter(el => {
  if (!visible(el)) return false;
  const role = (el.getAttribute('role') || implicitRole(el)).toLowerCase();
  if (wantedRole && role !== wantedRole) return false;
  const label = name(el);
  return exact ? label === needle : label.includes(needle);
});
if (matches.length !== 1) {
  return {
    ok: false,
    count: matches.length,
    samples: matches.slice(0, 8).map(el => ({
      text: name(el), role: (el.getAttribute('role') || implicitRole(el)).toLowerCase(),
      selector: wsnSelector(el)
    }))
  };
}
const el = matches[0];
let rect = el.getBoundingClientRect();
if (rect.left < 0 || rect.top < 0 || rect.right > window.innerWidth || rect.bottom > window.innerHeight) {
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  rect = el.getBoundingClientRect();
}
const x = rect.left + rect.width / 2;
const y = rect.top + rect.height / 2;
const top = document.elementFromPoint(x, y);
if (top && !(top === el || el.contains(top))) {
  return {
    ok: false, count: 1, occluded: true,
    blocker: name(top) || top.tagName.toLowerCase(),
    samples: [{text: name(el), role: (el.getAttribute('role') || implicitRole(el)).toLowerCase(), selector: wsnSelector(el)}]
  };
}
return {
  ok: true, count: 1, x, y, text: name(el),
  role: (el.getAttribute('role') || implicitRole(el)).toLowerCase(),
  selector: wsnSelector(el), hit_test_unavailable: !top
};
"""


# Re-issue a captured request from inside the page, so its cookies and origin are
# the page's own. Returning status, headers and a clipped body is what makes it a
# probe - resend the login POST, see whether the token still works - rather than
# a blind fire-and-forget.
_REPLAY_SCRIPT = """
// Returns a Promise, which execute_js(await_promise=True) resolves at the CDP
// layer. The script runs in a plain (non-async) function wrapper, so a top-level
// `await` here would be a sloppy-mode identifier, not a keyword - the await lives
// inside this async IIFE instead, and its result is what the caller receives.
const spec = arguments[0];
const started = performance.now();
return (async () => {
  try {
    const noBody = spec.method === 'GET' || spec.method === 'HEAD';
    const response = await fetch(spec.url, {
      method: spec.method || 'GET',
      headers: spec.headers || {},
      body: (spec.body != null && !noBody) ? spec.body : undefined,
      credentials: spec.credentials || 'include',
      redirect: 'follow',
    });
    const text = await response.text();
    const headers = {};
    response.headers.forEach((value, key) => { headers[key] = value; });
    return {
      ok: response.ok,
      status: response.status,
      url: response.url,
      redirected: response.redirected,
      headers: headers,
      body: text.length > __BODY_CHARS__ ? text.slice(0, __BODY_CHARS__) : text,
      truncated: text.length > __BODY_CHARS__,
      body_chars: text.length,
      ms: Math.round(performance.now() - started),
      body_ignored: spec.body != null && noBody,
    };
  } catch (error) {
    return {ok: false, status: 0, error: String(error && error.message || error)};
  }
})();
"""
REPLAY_BODY_CHARS = 20_000
_REPLAY_SCRIPT = _REPLAY_SCRIPT.replace("__BODY_CHARS__", str(REPLAY_BODY_CHARS))


def _replay_window_note(spec: dict[str, Any], carried: dict[str, Any]) -> dict[str, Any]:
    """``{"window_note": ...}`` when a replayed response came back cut, else ``{}``.

    Two cuts can happen - the body past ``REPLAY_BODY_CHARS`` (``response.truncated``)
    and the whole answer windowed by the script-result budget (``truncated``) - and
    neither could be read further from ``replay_request`` itself. The note spells
    out the same fetch as a ``run_script`` with ``save_to``, which returns all of it.
    """
    response = carried.get("response")
    body_cut = isinstance(response, dict) and bool(response.get("truncated"))
    if not (body_cut or carried.get("truncated")):
        return {}
    options: dict[str, Any] = {"method": spec.get("method") or "GET",
                               "credentials": spec.get("credentials") or "include"}
    if spec.get("headers"):
        options["headers"] = spec["headers"]
    if spec.get("body") is not None and options["method"] not in {"GET", "HEAD"}:
        options["body"] = spec["body"]
    script = (f"const r = await fetch({json.dumps(spec.get('url'))}, {json.dumps(options, ensure_ascii=False)}); "
              "return {status: r.status, headers: Object.fromEntries(r.headers), body: await r.text()};")
    what = (f"the body was cut at {REPLAY_BODY_CHARS} characters (response.body_chars has its length)"
            if body_cut else "the answer is a window of the response")
    return {"window_note": (f"Part of the response: {what}. For all of it, send the same request as "
                            f"run_script with save_to (it writes the whole value to a file): "
                            f"script={json.dumps(script, ensure_ascii=False)}, save_to='replay-response.json'.")}


# ``scrollIntoView`` brings the target into view first, so the wheel point below
# is reachable even when the element lives inside a tall scroll container. The
# call runs in the element's own document (``_resolve_element`` left the driver
# there); ``wsnFrameMap`` then reports where the element's centre lands on the
# top-level page, the same mapping outline and find boxes go through.
_SCROLL_INTO_VIEW_SCRIPT = page_perception.JS_LIBRARY + """
const element = arguments[0];
if (!element || element.scrollIntoView !== undefined) {
  element.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
}
const rect = element.getBoundingClientRect();
const local_x = rect.x + rect.width / 2;
const local_y = rect.y + rect.height / 2;
const mapped = wsnFrameMap(element);
return {
  cx: local_x,
  cy: local_y,
  frame: {
    x: mapped.x, y: mapped.y,
    ax: mapped.ax, ay: mapped.ay,
    bx: mapped.bx, by: mapped.by,
    page_width: window.innerWidth, page_height: window.innerHeight
  }
};
"""


# CDP input is addressed in top-level page pixels, so a point inside a frame has
# to be carried through everything that stands between the two: the origin of the
# frame's *content* box - the border box misses by exactly the border and padding
# - and any CSS transform, individual `rotate`/`scale`/`translate` property or
# `zoom` on the frame or on an ancestor. `wsnFrameMap` in the shared page-side
# library is that map, and the outline and find report their boxes through the
# very same function: two implementations of "where is this frame-local point on
# the page" drift apart, and then the centre a caller is told to click is not the
# pixel this module aims at.
_FRAME_MAP_SCRIPT = page_perception.JS_LIBRARY + """
const mapped = wsnFrameMap(arguments[0]);
return {
  x: mapped.x, y: mapped.y,
  ax: mapped.ax, ay: mapped.ay,
  bx: mapped.bx, by: mapped.by,
  flat: mapped.flat,
  page_width: window.innerWidth, page_height: window.innerHeight
};
"""


# Being inside the window is not the same as being reachable. A frame clipped by
# an `overflow: hidden` ancestor, or with a fixed header painted over it, answers
# every question about its own viewport as if it were whole, and the mapped point
# is a perfectly ordinary page coordinate - it just belongs to something else.
# Chrome hit-tests the top document before it delivers, so the same question is
# asked here, and the answer names what is in the way.
_FRAME_HIT_SCRIPT = """
const frame = arguments[0];
const hit = document.elementFromPoint(arguments[1], arguments[2]);
if (hit && (hit === frame || frame.contains(hit))) return null;
if (!hit) return 'nothing this document paints';
const classes = Array.from(hit.classList).map(name => '.' + name).join('');
return hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '') + classes;
"""


_GAME_PROBE_SCRIPT = r"""
function selector(el) {
  if (el.id) return '#' + CSS.escape(el.id);
  const nodes = Array.from(document.querySelectorAll(el.tagName.toLowerCase()));
  return el.tagName.toLowerCase() + ':nth-of-type(' + (nodes.indexOf(el) + 1) + ')';
}
const canvases = Array.from(document.querySelectorAll('canvas')).map(canvas => {
  const rect = canvas.getBoundingClientRect();
  let context = 'unknown';
  for (const kind of ['webgl2', 'webgl', '2d']) {
    try {
      if (canvas.getContext(kind)) { context = kind; break; }
    } catch (_) {}
  }
  return {
    selector: selector(canvas), context,
    width: canvas.width, height: canvas.height,
    client_width: canvas.clientWidth, client_height: canvas.clientHeight,
    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
    visible: !!(rect.width && rect.height)
  };
});
const navigation = performance.getEntriesByType('navigation')[0];
return {
  ready_state: document.readyState,
  visibility_state: document.visibilityState,
  document_has_focus: document.hasFocus(),
  canvas_count: canvases.length,
  canvases,
  iframe_count: document.querySelectorAll('iframe').length,
  iframes: Array.from(document.querySelectorAll('iframe')).map(frame => ({
    selector: selector(frame), src: frame.src || '', title: frame.title || ''
  })),
  navigation_ms: navigation ? Math.round(navigation.duration) : null
};
"""


_RENDER_STEP_SCRIPT = r"""
const count = arguments[0];
const done = arguments[arguments.length - 1];
const state = window.__webSearchNeoRenderControl;
if (!state) {
  done({success: false, error: 'missing_bootstrap'});
  return;
}
if (state.mode !== 'step') {
  done({success: false, error: 'not_step_mode', mode: state.mode});
  return;
}
state.step(count, done);
"""


# The submit event is dispatched, and then the navigation it starts throws the
# whole window away - counter included. sessionStorage survives a same-origin
# load, and the token on the window says whether this is still the document the
# form was submitted from, which is the only way a POST back onto the same URL
# under the same title can be told apart from nothing happening at all.
#
# The listener goes on in the capture phase, because a framework that owns the
# form calls stopImmediatePropagation in its own handler: every listener added to
# the form after it was invisible, so a submit that had worked came back as one
# that never happened, and the caller sent it a second time.
#
# Nothing branded is left behind either. The key is this call's own token, read
# once and removed again, so a site that goes looking for an automation
# fingerprint finds a random string that is already gone; anything an earlier
# call could not clean up - its document was replaced before the read - is swept
# away here.
_SUBMIT_WATCH_SCRIPT = """
const form = arguments[0];
const token = arguments[1];
const state = {token: token, fired: 0, prevented: false};
window[token] = state;
try {
  for (const key of Object.keys(sessionStorage)) {
    if (key !== token && /^sf-[0-9]+$/.test(key)) sessionStorage.removeItem(key);
  }
} catch (error) { /* denied */ }
form.addEventListener('submit', (event) => {
  state.fired += 1;
  try { sessionStorage.setItem(token, '1'); } catch (error) { /* denied */ }
  // defaultPrevented is only final once every listener has run.
  queueMicrotask(() => { state.prevented = event.defaultPrevented; });
}, {capture: true, once: true});
// A form that delivers its result somewhere else leaves this document with
// nothing to say about what happened.
return String(form.target || '');
"""


_SUBMIT_RESULT_SCRIPT = """
const token = arguments[0];
const state = window[token];
let stored = false;
try {
  stored = sessionStorage.getItem(token) !== null;
  sessionStorage.removeItem(token);
} catch (error) { stored = false; }
try { delete window[token]; } catch (error) { window[token] = undefined; }
return {
  same_document: !!state && state.token === token,
  fired: !!state && state.fired > 0,
  prevented: !!state && !!state.prevented,
  stored: stored
};
"""


# The write is rehearsed on a throwaway control of the same type first, because
# these controls do not refuse a value they cannot parse - they replace it. A
# range takes its midpoint, a colour takes black and a date empties itself, so a
# failed fill used to leave the form holding a plausible wrong answer.
_SET_VALUE_SCRIPT = """
const element = arguments[0];
const wanted = String(arguments[1]);
const doc = element.ownerDocument || document;
const probe = doc.createElement('input');
probe.type = element.type;
if (element.min) probe.min = element.min;
if (element.max) probe.max = element.max;
if (element.step) probe.step = element.step;
probe.value = wanted;
const outcome = String(probe.value || '');
// A control may shorten what it is given - a datetime-local drops the seconds it
// does not carry - but not answer with something else entirely.
const usable = outcome !== '' &&
  wanted.toLowerCase().indexOf(outcome.toLowerCase()) === 0;
if (!usable) return {taken: false, value: String(element.value || ''), expected: outcome};
element.value = wanted;
element.dispatchEvent(new Event('input', {bubbles: true}));
element.dispatchEvent(new Event('change', {bubbles: true}));
return {taken: true, value: String(element.value || ''), expected: outcome};
"""


_FILE_INPUT_STATE_SCRIPT = """
const element = arguments[0];
return {
  type: String(element.type || '').toLowerCase(),
  multiple: !!element.multiple,
  names: Array.from(element.files || []).map(file => file.name)
};
"""


# A Dropzone-style widget takes the file off the input the instant it gets it and
# uploads it itself, so the input is empty a millisecond later. Reading the input
# back is still the right first question - it is exact when it answers - but an
# empty one is not an answer at all, and calling it a failure sent a resume that
# was already on S3 back round the loop as "upload failed".
_UPLOAD_TRACE_SCRIPT = """
const names = arguments[0];
const text = ((document.body && document.body.innerText) || '');
const values = Array.from(document.querySelectorAll('input, textarea'))
  .map(node => String(node.value || '')).join(' ');
const attributes = Array.from(document.querySelectorAll('[title], [data-filename], [aria-label]'))
  .slice(0, 400)
  .map(node => [node.getAttribute('title'), node.getAttribute('data-filename'),
                node.getAttribute('aria-label')].join(' ')).join(' ');
const haystack = text + ' ' + values + ' ' + attributes;
const seen = [];
for (const name of names) {
  if (name && haystack.indexOf(name) >= 0) seen.push(name);
}
return seen;
"""


_SCROLL_METRICS_SCRIPT = """
const root = document.scrollingElement || document.documentElement;
const width = window.innerWidth;
const height = window.innerHeight;
const pageWidth = Math.max(root ? root.scrollWidth : 0, document.documentElement.scrollWidth);
const pageHeight = Math.max(root ? root.scrollHeight : 0, document.documentElement.scrollHeight);
return {
  scroll_x: window.scrollX,
  scroll_y: window.scrollY,
  max_scroll_x: Math.max(0, pageWidth - width),
  max_scroll_y: Math.max(0, pageHeight - height),
  viewport_width: width,
  viewport_height: height,
  page_width: pageWidth,
  page_height: pageHeight,
  at_top: window.scrollY <= 0,
  at_bottom: window.scrollY >= Math.max(0, pageHeight - height) - 1
};
"""


_POINTER_LOCK_SCRIPT = """
const selector = arguments[0];
const wanted = arguments[1];
const target = selector ? document.querySelector(selector)
                        : (document.querySelector('canvas') || document.body);
if (!target) return {success: false, error: 'No pointer lock target on this page'};
if (wanted === 'release') {
  document.exitPointerLock();
  return {success: true, locked: false, element: null};
}
try { target.requestPointerLock(); } catch (error) {
  return {success: false, error: String(error)};
}
return {success: true, requested: true};
"""


_POINTER_LOCK_STATUS_SCRIPT = """
const locked = document.pointerLockElement;
return {
  locked: !!locked,
  element: locked ? (locked.id || locked.tagName.toLowerCase()) : null
};
"""
