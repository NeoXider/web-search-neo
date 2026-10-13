"""The page-side challenge probe run inside every page summary (pure JavaScript source)."""

# A challenge is a live widget, not the word "captcha" in prose. Matching text
# alone flags every article about CAPTCHAs and every search result for the word,
# and then the agent waits three minutes for a human who is not needed.
#
# The probe therefore gathers three things and leaves the verdict to
# _classify_challenge: which provider widgets are on the page, wherever they are -
# shadow roots and same-origin frames included, because half of them live one
# document down - which provider SDKs the markup loads, and whether any of it is
# lying over the middle of the viewport rather than sitting inside a form.
_CHALLENGE_WIDGET_SCRIPT = """
const WIDGETS = [
  'iframe[src*="recaptcha/api2"]', 'iframe[src*="recaptcha/enterprise"]',
  'iframe[src*="hcaptcha.com"]', 'iframe[src*="challenges.cloudflare.com"]',
  'iframe[src*="captcha-api.yandex"]', 'iframe[src*="captcha-delivery.com"]',
  'iframe[src*="captcha.awswaf.com"]', 'iframe[title*="captcha" i]',
  'div.g-recaptcha', 'div.h-captcha', 'div.cf-turnstile', 'div#cf-challenge-running',
  'form#challenge-form', '#px-captcha', '.smart-captcha', '.datadome-captcha',
  'awswaf-captcha', '[data-sitekey]',
  // Tencent TCaptcha has no sitekey and no iframe of its own in the current SDK:
  // TJCaptcha.js builds the widget into the page as #tcaptcha_transform_dy with
  // tencent-captcha-dy__* parts around it, and the older one is a frame from
  // turing.captcha.qcloud.com. The class family is matched by substring because
  // Tencent renames these pieces between releases.
  '#tcaptcha_transform_dy', '#TCaptcha', '#captcha_popup', '[id^="tCaptcha"]',
  'iframe[src*="turing.captcha.qcloud.com"]', 'iframe[src*="captcha.gtimg.cn"]',
  '[class*="tencent-captcha-dy__"]',
  // Geetest v4 slider: same family of substring selectors for the same reason.
  '.geetest_widget', '[class*="geetest_widget"]', 'iframe[src*="geetest.com"]'
];
// A script tag has no box of its own, so these are counted by presence. Both
// hosts only serve the SDK that asks a human to solve something; the tags that
// merely score a request quietly are deliberately not here.
const MARKERS = [
  'script[src*="captcha-sdk.awswaf.com"]', 'script[src*="captcha.awswaf.com"]',
  'script[src*="turing.captcha.qcloud.com"]', 'script[src*="captcha.gtimg.cn"]',
  'script[src*="static.geetest.com"]'
];
// A widget of one of these kinds is a real gate even with no box on the screen:
// the invisible modes of Turnstile, reCAPTCHA and Smart CAPTCHA render exactly
// this markup and nothing else, and the page's own submit handler waits on the
// token they are going to mint. The rest of WIDGETS is left out on purpose -
// a hidden challenge-form or a display:none article widget says nothing.
const INVISIBLE_CAPABLE = [
  'div.cf-turnstile', 'iframe[src*="challenges.cloudflare.com"]',
  'div.g-recaptcha', 'iframe[src*="recaptcha/api2"]',
  'iframe[src*="recaptcha/enterprise"]',
  'div.h-captcha', 'iframe[src*="hcaptcha.com"]',
  '.smart-captcha', 'iframe[src*="captcha-api.yandex"]'
];
// The hidden field every vendor mints its token into. It exists only because the
// widget script ran, and while it is empty the challenge is unsolved - which is
// the whole state an invisible widget ever shows.
const TOKEN_FIELDS = [
  {selector: 'input[name="cf-turnstile-response"]', vendor: 'turnstile'},
  {selector: 'textarea[name="g-recaptcha-response"], textarea#g-recaptcha-response', vendor: 'recaptcha'},
  {selector: 'textarea[name="h-captcha-response"], textarea#h-captcha-response', vendor: 'hcaptcha'},
  {selector: 'input[name="smart-token"], input[name="smartToken"]', vendor: 'smartcaptcha'}
];
const found = [];
const markers = [];
const hidden = [];
const tokens = [];
// The walk used to stop after 8000 nodes, which a marketplace listing page eats
// before the first shadow root is entered - and nothing said it had stopped, so
// "no captcha here" and "gave up looking" read the same. Walking 60000 elements
// costs 10ms, and the pages most likely to gate are the ones with that many.
const NODE_BUDGET = 50000;
const DEPTH_LIMIT = 8;
// A widget over the middle of the viewport is only in the way when the layer it
// sits in seals off enough of the page that there is nothing else to read.
const BLOCKING_COVER = 0.5;
const budget = {nodes: 0, truncated: false};
let blocking = false;

function isVisible(element) {
  const rect = element.getBoundingClientRect();
  if (rect.width < 20 || rect.height < 20) return false;
  // Computed opacity is not inherited: a widget inside a layer the provider faded
  // out reports opacity 1 while being as invisible as its parent. TCaptcha does
  // exactly this when it auto-passes - the whole #tcaptcha_transform_dy subtree
  // stays in the document at opacity 0, and reading only its own style made a
  // solved challenge look like an unsolved one. visibility and display do inherit
  // their effect, so checking each layer is what makes this say "not on screen".
  let node = element;
  for (let step = 0; step < 8 && node; step += 1) {
    const view = (node.ownerDocument && node.ownerDocument.defaultView) || window;
    if (!view) break;
    const style = view.getComputedStyle(node);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const opacity = parseFloat(style.opacity);
    if (!isNaN(opacity) && opacity <= 0.05) return false;
    node = node.parentElement || (node.parentNode && node.parentNode.host) || null;
  }
  return true;
}

// data-sitekey alone says nothing: chat, payment and analytics widgets mint one
// too, and treating every one of them as a gate stopped the agent on pages that
// were never blocking it.
function isCaptchaSitekey(element) {
  const name = String(element.getAttribute('class') || '') + ' ' + String(element.id || '');
  if (/captcha|turnstile|challenge/i.test(name)) return true;
  return !!element.querySelector('iframe[src*="captcha"], iframe[src*="turnstile"]');
}

function matchedSelector(element, selectors) {
  for (const selector of selectors) {
    try { if (element.matches(selector)) return selector; } catch (error) { continue; }
  }
  return null;
}

function viewOf(node) {
  const doc = node.ownerDocument;
  return (doc && doc.defaultView) || null;
}

function coverOfRect(rect, view) {
  const area = view.innerWidth * view.innerHeight;
  if (area <= 0) return 0;
  const width = Math.min(rect.right, view.innerWidth) - Math.max(rect.left, 0);
  const height = Math.min(rect.bottom, view.innerHeight) - Math.max(rect.top, 0);
  return width <= 0 || height <= 0 ? 0 : (width * height) / area;
}

function overPoint(rect, x, y) {
  return rect.left <= x && rect.right >= x && rect.top <= y && rect.bottom >= y;
}

// Zero unless the widget is over the centre of its own viewport; otherwise the
// share of that viewport the widget - or the positioned layer it is painted in -
// covers. It is that layer, the scrim of a modal, that actually stops the page
// being used; the widget itself is far too small to.
//
// Boxes, not a hit test: elementFromPoint retargets shadow content to its host,
// so a widget inside a web component could never be the node at the centre, and
// the veil a provider paints over its own widget while it verifies wins that hit
// test while blocking the page just as thoroughly.
function coverOverCenter(element) {
  const view = viewOf(element);
  if (!view) return 0;
  const x = view.innerWidth / 2;
  const y = view.innerHeight / 2;
  const rect = element.getBoundingClientRect();
  if (!overPoint(rect, x, y)) return 0;
  let cover = coverOfRect(rect, view);
  let node = element;
  for (let step = 0; step < 40 && node; step += 1) {
    // parentNode.host is the step out of a shadow tree, which parentElement -
    // and every hit test - stops dead at.
    const parent = node.parentElement || (node.parentNode && node.parentNode.host) || null;
    if (!parent) break;
    node = parent;
    let position = '';
    let ancestorView = null;
    try {
      ancestorView = viewOf(node);
      position = ancestorView.getComputedStyle(node).position;
    } catch (error) { position = ''; }
    if (!ancestorView) break;
    if (position !== 'fixed' && position !== 'absolute' && position !== 'sticky') continue;
    const box = node.getBoundingClientRect();
    if (overPoint(box, x, y)) cover = Math.max(cover, coverOfRect(box, ancestorView));
  }
  return cover;
}

function scan(root, where, atCenter, depth, outerCover) {
  if (found.length >= 3) return;
  if (depth > DEPTH_LIMIT || budget.nodes > NODE_BUDGET) { budget.truncated = true; return; }
  const doc = root.ownerDocument || root;
  let matches = [];
  try { matches = Array.from(root.querySelectorAll(WIDGETS.join(','))); } catch (error) { matches = []; }
  for (const element of matches) {
    const selector = matchedSelector(element, WIDGETS);
    if (!selector) continue;
    if (selector === '[data-sitekey]' && !isCaptchaSitekey(element)) continue;
    if (!isVisible(element)) {
      // An invisible widget used to be dropped here, and with it the only trace
      // of the challenge holding a submit: no box, no error, no report.
      if (INVISIBLE_CAPABLE.indexOf(selector) >= 0 && hidden.indexOf(selector + where) < 0) {
        hidden.push(selector + where);
      }
      continue;
    }
    found.push(selector + where);
    const cover = atCenter ? coverOverCenter(element) : 0;
    if (cover > 0 && Math.max(cover, outerCover) >= BLOCKING_COVER) blocking = true;
    if (found.length >= 3) break;
  }
  for (const spec of TOKEN_FIELDS) {
    let fields = [];
    try { fields = Array.from(root.querySelectorAll(spec.selector)); } catch (error) { fields = []; }
    for (const field of fields) {
      const key = spec.vendor + where;
      if (tokens.some(token => token.key === key)) continue;
      tokens.push({
        key: key,
        vendor: spec.vendor,
        where: where.trim(),
        field: String(field.getAttribute('name') || field.id || spec.vendor),
        filled: String(field.value || '').trim().length > 0
      });
    }
  }
  try {
    for (const element of root.querySelectorAll(MARKERS.join(','))) {
      const selector = matchedSelector(element, MARKERS);
      if (selector && markers.indexOf(selector + where) < 0) markers.push(selector + where);
    }
  } catch (error) { /* a root that cannot be queried has nothing to add */ }
  if (found.length >= 3) return;
  // Shadow roots and same-origin frames are where the rest of the challenges
  // live: a top-level querySelector never looks inside either one. A TreeWalker
  // stops the moment the budget runs out, where querySelectorAll('*') would have
  // built the whole element list of a huge page before anyone could check.
  let walker = null;
  try { walker = doc.createTreeWalker(root, NodeFilter.SHOW_ELEMENT); } catch (error) { walker = null; }
  while (walker) {
    const element = walker.nextNode();
    if (!element || found.length >= 3) return;
    if (++budget.nodes > NODE_BUDGET) { budget.truncated = true; return; }
    if (element.shadowRoot) {
      // A shadow tree is painted in its host's layer, so it stands over the page
      // exactly as far as the host does.
      scan(element.shadowRoot, ' (in shadow DOM)', atCenter, depth + 1, outerCover);
    } else if (element.tagName === 'IFRAME') {
      let inner = null;
      try { inner = element.contentDocument; } catch (error) { inner = null; }
      if (!inner) continue;
      const frameCover = atCenter ? coverOverCenter(element) : 0;
      scan(inner, ' (in a frame)', frameCover > 0, depth + 1, Math.max(outerCover, frameCover));
    }
  }
}

scan(document, '', true, 0, 0);
const heading = (document.title || '') + ' ' +
  Array.from(document.querySelectorAll('h1, h2')).slice(0, 3)
    .map(node => node.innerText || '').join(' ');
const body = (document.body && document.body.innerText) || '';
return {
  widgets: found,
  markers: markers,
  hidden_widgets: hidden,
  tokens: tokens,
  blocking: blocking,
  truncated: budget.truncated,
  heading: heading.slice(0, 400),
  body: body.slice(0, 2000),
  body_length: body.length
};
"""
