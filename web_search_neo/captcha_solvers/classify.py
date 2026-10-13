"""Naming the captcha on the page: vendor family and challenge type.

The local slider solver only knows one shape - a picture with a hole and a
handle to drag into it. Everything else on the modern web (click the words in
order, tap the icon, tick the box, pick the buses) needs a person or a paid
service, and "no slider widget on the page" never said which of those was
actually there. This module names it from facts the harness already has -
visible text labels, class-name fragments, SDK/frame URLs, and whether
Tencent's ``aid`` parameter is present - so the detection path can say what
would be needed instead of only that the drag did not happen.

Pure function over strings: no browser, no network, no side effects, and no
imports beyond the standard library, which is what keeps ``captcha_solvers``
a leaf package. The page-side fact gathering is a data string below
(``CLASSIFY_FACTS_SCRIPT``); running it stays the caller's job, exactly like
the slider locate script.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

# Vendor family -> substring signals in class names, SDK/frame URLs and prose.
# Matched case-insensitively against the class-name and URL blobs; the text
# blob is weaker evidence (prose mentions vendors without showing one) and is
# only consulted when the markup says nothing.
_VENDOR_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tencent", (
        "tencent-captcha", "tcaptcha_transform_dy", "tcaptcha",
        "captcha_popup", "turing.captcha.qcloud.com", "captcha.gtimg.cn",
        "tencentcaptcha", "tjcaptcha",
    )),
    ("geetest", (
        "geetest_widget", "geetest_panel", "geetest_slider_button",
        "geetest_result", "geetest.com", "static.geetest.com", "geetest",
    )),
    ("recaptcha", (
        "g-recaptcha", "recaptcha/api2", "recaptcha/enterprise",
        "grecaptcha", "___grecaptcha_cfg", "recaptcha",
    )),
    ("hcaptcha", ("h-captcha", "hcaptcha.com", "hcaptcha")),
    ("turnstile", ("cf-turnstile", "challenges.cloudflare.com", "turnstile")),
)

# Harness shorthand for the vendor it already identified: the slider locate
# script names its own vendors, the sitekey probe names the paid ones.
_VENDOR_HINTS = {
    "tcaptcha": "tencent",
    "tencent": "tencent",
    "geetest4": "geetest",
    "geetest": "geetest",
    "recaptcha": "recaptcha",
    "hcaptcha": "hcaptcha",
    "turnstile": "turnstile",
}

VENDOR_LABELS = {
    "tencent": "Tencent TCaptcha",
    "geetest": "Geetest",
    "recaptcha": "reCAPTCHA",
    "hcaptcha": "hCaptcha",
    "turnstile": "Cloudflare Turnstile",
    "generic": "an image captcha",
    "unknown": "a captcha",
}

# Tencent word-click instructions read "Select in order: <chars>" (or the
# Chinese equivalent); the characters to click follow the colon.
_WORD_ORDER = re.compile(
    r"select[^.]{0,40}in order|in order[^.]{0,40}select"
    r"|按顺序|依次点击|顺序点击|按序点击",
)
_ICON_CLICK = re.compile(
    r"click[^.]{0,30}icon|tap[^.]{0,30}icon|点.{0,6}图标|选择.{0,6}图标",
)
# An image grid asks for a set ("select all ..."), a checkbox asks for one
# tick ("I'm not a robot").
_IMAGE_GRID = re.compile(
    r"select all|choose all|click all|select the|imageselect|选择.*图|勾选.*图",
)
_CHECKBOX = re.compile(r"not a robot|checkbox|tick the|打勾|复选框")

# Challenge shapes that are sliders by markup alone, whatever the vendor calls
# the pieces this release ("handler" becomes "knob" and back again).
_SLIDER_MARKS = (
    "slider", "slide-btn", "drag_button", "drag-button",
    "slider-block", "slider-groove", "slider-bar", "btn_slide",
)

# Only these (vendor, challenge) pairs can be attempted by the local solver,
# which measures a picture and drags a handle and does nothing else.
LOCALLY_SOLVABLE = frozenset({("tencent", "slider"), ("geetest", "slider")})

_NEEDS = {
    "slider": (
        "needs the local slider drag (measure the picture, drag the handle) "
        "or a person in the browser with mode='wait'"
    ),
    "word_click": (
        "word_click needs a click-order solver; the local solver only "
        "measures sliders - clear it with mode='wait'"
    ),
    "icon_click": (
        "icon_click needs an icon-click solver; the local solver only "
        "measures sliders - clear it with mode='wait'"
    ),
    "image_select": (
        "needs a person to pick the images with mode='wait', or a solving "
        "service with mode='solve' and WEB_SEARCH_NEO_CAPTCHA_KEY"
    ),
    "checkbox": (
        "needs a person to tick the box with mode='wait', or a solving "
        "service with mode='solve' and WEB_SEARCH_NEO_CAPTCHA_KEY"
    ),
    "unknown": (
        "needs a person in the browser with mode='wait'; the local solver "
        "only measures sliders"
    ),
}


def _blob(items: Any) -> str:
    """One lowercase blob out of a fact list, tolerating whatever the page gave."""
    if not isinstance(items, (list, tuple)):
        return ""
    return "\n".join(str(item).lower() for item in items)


def _evidence_picks(
    texts: list[str], class_names: list[str], sdk_urls: list[str],
    hits: list[str],
) -> list[str]:
    """Up to three short quotes of what actually matched, for the answer."""
    picks: list[str] = []
    for source, items in (("text", texts), ("class", class_names), ("sdk", sdk_urls)):
        for item in items:
            lowered = str(item).lower()
            if any(hit in lowered for hit in hits) and len(picks) < 3:
                quote = str(item).strip().replace("\n", " ")[:80]
                picks.append(f"{source} {quote!r}")
        if len(picks) >= 3:
            break
    return picks


def classify_captcha(
    texts: Sequence[str] | None = None,
    class_names: Sequence[str] | None = None,
    sdk_urls: Sequence[str] | None = None,
    aid_present: bool = False,
    vendor_hint: str | None = None,
) -> dict[str, Any]:
    """Name a captcha widget from DOM-ish facts the harness already collected.

    ``texts`` are visible instruction labels near the widget, ``class_names``
    are class/id fragments of its nodes, ``sdk_urls`` are script/frame URLs,
    ``aid_present`` is whether Tencent's ``aid`` parameter showed up, and
    ``vendor_hint`` is a vendor the harness already identified (sitekey probe
    or slider locate names). Returns ``vendor``, ``challenge``, ``evidence``,
    ``solver_available`` (whether the local slider solver can attempt it) and
    a ``needs`` sentence saying what would clear it.
    """
    text_list = [str(t) for t in (texts or [])]
    class_list = [str(c) for c in (class_names or [])]
    url_list = [str(u) for u in (sdk_urls or [])]
    markup = _blob(class_list) + "\n" + _blob(url_list)
    prose = _blob(text_list)

    vendor: str | None = None
    vendor_hits: list[str] = []
    for name, needles in _VENDOR_SIGNALS:
        hits = [n for n in needles if n in markup]
        if hits and vendor is None:
            vendor, vendor_hits = name, hits
    if vendor is None:
        for name, needles in _VENDOR_SIGNALS:
            hits = [n for n in needles if n in prose]
            if hits:
                vendor, vendor_hits = name, hits
                break
    hinted = _VENDOR_HINTS.get(str(vendor_hint or "").strip().lower(), "")
    if vendor is None:
        if aid_present:
            vendor, vendor_hits = "tencent", ["aid parameter"]
        elif hinted:
            vendor, vendor_hits = hinted, ["harness identification"]
    if vendor is None:
        if _IMAGE_GRID.search(prose) or "imageselect" in markup:
            return {
                "vendor": "generic", "challenge": "image_select",
                "evidence": _evidence_picks(text_list, class_list, url_list, ("select", "image")),
                "solver_available": False, "needs": _NEEDS["image_select"],
                "vendor_label": VENDOR_LABELS["generic"],
            }
        return {
            "vendor": "unknown", "challenge": "unknown", "evidence": [],
            "solver_available": False, "needs": _NEEDS["unknown"],
            "vendor_label": VENDOR_LABELS["unknown"],
        }

    slider_mark = any(mark in markup for mark in _SLIDER_MARKS)
    if vendor == "tencent":
        if _WORD_ORDER.search(prose):
            challenge = "word_click"
        elif _ICON_CLICK.search(prose) or "icon" in markup:
            challenge = "icon_click"
        elif slider_mark or hinted == "tencent":
            challenge = "slider"
        else:
            challenge = "unknown"
    elif vendor == "geetest":
        challenge = "slider" if slider_mark or hinted == "geetest" else "unknown"
    elif vendor in ("recaptcha", "hcaptcha"):
        if _IMAGE_GRID.search(prose) or "imageselect" in markup:
            challenge = "image_select"
        else:
            challenge = "checkbox"
    elif vendor == "turnstile":
        challenge = "image_select" if _IMAGE_GRID.search(prose) else "checkbox"
    else:  # pragma: no cover - vendor set is closed above
        challenge = "unknown"

    needles = list(vendor_hits)
    if aid_present and vendor == "tencent":
        needles.append("aid parameter")
    evidence = _evidence_picks(text_list, class_list, url_list, needles or (vendor,))
    if aid_present and vendor == "tencent":
        evidence = (evidence + ["aid parameter present"])[:3]
    if hinted == vendor and not evidence:
        evidence = ["harness identification"]
    return {
        "vendor": vendor,
        "challenge": challenge,
        "evidence": evidence[:3],
        "solver_available": (vendor, challenge) in LOCALLY_SOLVABLE,
        "needs": _NEEDS[challenge],
        "vendor_label": VENDOR_LABELS[vendor],
    }


# The facts classify_captcha reads, gathered in one round trip. Class
# fragments and instruction labels come off the widget families the harness
# already knows; script/frame URLs name the SDK; Tencent's ``aid`` is the app
# id its constructor takes, present in the SDK URL or on ``window``.
CLASSIFY_FACTS_SCRIPT = """
const out = {texts: [], class_names: [], sdk_urls: [], aid_present: false};
try {
  const families = [
    '[class*="tencent-captcha"]', '[id^="tCaptcha"]', '#tcaptcha_transform_dy',
    '#TCaptcha', '#captcha_popup', 'iframe[src*="turing.captcha.qcloud.com"]',
    '[class*="geetest"]', 'iframe[src*="geetest.com"]',
    'div.g-recaptcha', 'iframe[src*="recaptcha"]',
    'div.h-captcha', 'iframe[src*="hcaptcha.com"]',
    'div.cf-turnstile', 'iframe[src*="challenges.cloudflare.com"]'
  ];
  const seenClasses = {};
  for (const selector of families) {
    let elements = [];
    try { elements = Array.from(document.querySelectorAll(selector)); }
    catch (error) { continue; }
    for (const element of elements.slice(0, 3)) {
      const cls = (element.getAttribute && element.getAttribute('class')) || '';
      if (cls && !seenClasses[cls]) {
        seenClasses[cls] = true;
        out.class_names.push(String(cls).slice(0, 160));
      }
      const label = (element.innerText || '').trim();
      if (label) out.texts.push(label.slice(0, 200));
    }
  }
  const sources = [];
  try {
    for (const element of Array.from(
        document.querySelectorAll('script[src], iframe[src]')).slice(0, 60)) {
      const src = String(element.src || '');
      if (/captcha|recaptcha|hcaptcha|turnstile|geetest|tencent|qcloud|gtimg|cloudflare/i.test(src)) {
        sources.push(src.slice(0, 220));
        if (/[?&]aid=/.test(src)) out.aid_present = true;
      }
    }
  } catch (error) { /* a root that cannot be queried has nothing to add */ }
  out.sdk_urls = sources.slice(0, 12);
  out.texts = out.texts.slice(0, 4);
  out.class_names = out.class_names.slice(0, 10);
  if (!out.aid_present) {
    try { out.aid_present = (typeof TencentCaptcha !== 'undefined'); }
    catch (error) { out.aid_present = false; }
  }
} catch (error) { /* no facts is "unknown", never a failure */ }
return out;
"""


def classify_from_harness(
    found: Any = None,
    slider: Any = None,
    status: Any = None,
    facts: Any = None,
) -> dict[str, Any]:
    """Classify from what the detection path already holds, plus one fact probe.

    ``found`` is the sitekey identification, ``slider`` the slider locate
    outcome, ``status`` the challenge verdict, ``facts`` the
    ``CLASSIFY_FACTS_SCRIPT`` answer. Every argument tolerates junk - a probe
    that answered something unexpected degrades to ``unknown``, never to an
    exception, because this runs on the reporting path.
    """
    found = found if isinstance(found, dict) else {}
    slider = slider if isinstance(slider, dict) else {}
    status = status if isinstance(status, dict) else {}
    facts = facts if isinstance(facts, dict) else {}

    def strings(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item) for item in value if isinstance(item, (str, int, float))]

    texts = strings(facts.get("texts"))
    class_names = strings(facts.get("class_names"))
    sdk_urls = strings(facts.get("sdk_urls"))
    for key in ("captcha_widgets", "challenge_evidence"):
        for item in strings(status.get(key)):
            class_names.append(item)
            sdk_urls.append(item)
    invisible = status.get("invisible_challenge")
    invisible = invisible if isinstance(invisible, dict) else {}
    for item in strings(invisible.get("evidence")):
        sdk_urls.append(item)
    for selector in (slider.get("bg_selector"), slider.get("handle_selector")):
        if isinstance(selector, str) and selector:
            class_names.append(selector)

    hint = ""
    for candidate in (
        found.get("vendor"), invisible.get("vendor"), slider.get("vendor"),
    ):
        mapped = _VENDOR_HINTS.get(str(candidate or "").strip().lower(), "")
        if mapped:
            hint = mapped
            break
    return classify_captcha(
        texts=texts,
        class_names=class_names,
        sdk_urls=sdk_urls,
        aid_present=facts.get("aid_present") is True,
        vendor_hint=hint or None,
    )
