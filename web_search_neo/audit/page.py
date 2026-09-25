"""The page itself: third-party code, SRI, forms, password fields, links, inline code, mixed content.

Facts come from one of two places with the same shape: ``SNAPSHOT_SCRIPT`` run
in the rendered page (the DOM after scripts ran, plus the resources the browser
actually loaded), or ``snapshot_from_html`` over the served HTML when no browser
is available. ``analyze`` turns a snapshot into findings.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from web_search_neo.audit.findings import clip_list, finding, info, passed
from web_search_neo.audit.sites import host_of, is_third_party, site_of

LIST_LIMIT = 200
# Chrome's default Resource Timing buffer; a page that fills it hides later entries.
RESOURCE_TIMING_BUFFER = 250

# Runs as an async function body (run_script semantics). Every list is capped and
# its full length reported, so nothing is cut silently.
SNAPSHOT_SCRIPT = r"""
const LIMIT = 200;
const cap = (list) => ({items: list.slice(0, LIMIT), total: list.length});
const abs = (value) => { try { return new URL(value, document.baseURI).href; } catch (e) { return String(value || ''); } };
const inHead = (el) => !!(document.head && document.head.contains(el));
const scripts = [...document.scripts].map(s => ({
  src: s.src ? abs(s.getAttribute('src')) : null, integrity: s.integrity || null,
  crossorigin: s.getAttribute('crossorigin'), async: s.async, defer: s.defer,
  type: s.type || '', in_head: inHead(s), nonce: !!(s.nonce || s.getAttribute('nonce')),
  inline_chars: s.src ? 0 : (s.textContent || '').length,
}));
const styles = [...document.querySelectorAll('link[rel~="stylesheet"]')].map(l => ({
  href: abs(l.getAttribute('href')), integrity: l.integrity || null,
  crossorigin: l.getAttribute('crossorigin'), media: l.media || '', in_head: inHead(l),
}));
const forms = [...document.forms].map(f => ({
  action: abs(f.getAttribute('action') || location.href), method: (f.getAttribute('method') || 'get').toLowerCase(),
  has_password: !!f.querySelector('input[type=password]'),
}));
const passwords = [...document.querySelectorAll('input[type=password]')].map(i => ({
  name: i.name || i.id || '', autocomplete: i.getAttribute('autocomplete'),
  form_action: i.form ? abs(i.form.getAttribute('action') || location.href) : null,
  form_method: i.form ? (i.form.getAttribute('method') || 'get').toLowerCase() : null,
}));
const blank = [...document.querySelectorAll('a[target=_blank], area[target=_blank], form[target=_blank]')].map(a => ({
  href: abs(a.getAttribute('href') || a.getAttribute('action') || ''), rel: (a.getAttribute('rel') || '').toLowerCase(),
}));
const handlers = [];
for (const el of document.querySelectorAll('*')) {
  for (const attr of el.attributes) {
    if (attr.name.startsWith('on')) handlers.push(`${el.tagName.toLowerCase()}[${attr.name}]`);
  }
}
const jsUrls = [...document.querySelectorAll('a[href^="javascript:" i], iframe[src^="javascript:" i]')].length;
const frames = [...document.querySelectorAll('iframe, frame')].map(f => ({
  src: abs(f.getAttribute('src') || 'about:blank'), sandbox: f.hasAttribute('sandbox'),
}));
const media = [...document.querySelectorAll('img[src], audio[src], video[src], source[src], track[src], embed[src], object[data]')]
  .map(e => ({tag: e.tagName.toLowerCase(), url: abs(e.getAttribute('src') || e.getAttribute('data'))}));
const resources = performance.getEntriesByType('resource').map(r => ({url: r.name, type: r.initiatorType}));
const metaCsp = [...document.querySelectorAll('meta[http-equiv]')]
  .filter(m => (m.getAttribute('http-equiv') || '').toLowerCase() === 'content-security-policy')
  .map(m => m.getAttribute('content') || '');
return {
  url: location.href, title: document.title, secure_context: window.isSecureContext,
  scripts: cap(scripts), styles: cap(styles), forms: cap(forms), passwords: cap(passwords),
  blank_links: cap(blank), inline_handlers: cap(handlers), javascript_urls: jsUrls,
  frames: cap(frames), media: cap(media), resources: cap(resources), meta_csp: metaCsp,
  source: 'browser',
};
"""


def _cap(items: list[Any]) -> dict[str, Any]:
    return {"items": items[:LIST_LIMIT], "total": len(items)}


class _Collector(HTMLParser):
    """The same facts from static HTML (what the server sent, before any script ran)."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.in_head = True
        self.scripts: list[dict[str, Any]] = []
        self.styles: list[dict[str, Any]] = []
        self.forms: list[dict[str, Any]] = []
        self.passwords: list[dict[str, Any]] = []
        self.blank: list[dict[str, Any]] = []
        self.handlers: list[str] = []
        self.frames: list[dict[str, Any]] = []
        self.media: list[dict[str, Any]] = []
        self.meta_csp: list[str] = []
        self.meta_referrer: str | None = None
        self.js_urls = 0
        self.links: list[str] = []
        self.title = ""
        self._script: dict[str, Any] | None = None
        self._in_title = False

    def _abs(self, value: str | None) -> str:
        return urljoin(self.base, value or "")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): (value or "") for name, value in attrs}
        names = {name.lower() for name, _ in attrs}
        if tag == "body":
            self.in_head = False
        if tag == "base" and attr.get("href"):
            self.base = self._abs(attr["href"])
        self.handlers.extend(f"{tag}[{name}]" for name in names if name.startswith("on"))
        if tag == "script":
            self._script = {
                "src": self._abs(attr["src"]) if attr.get("src") else None,
                "raw_src": attr.get("src") or None,
                "integrity": attr.get("integrity") or None, "crossorigin": attr.get("crossorigin"),
                "async": "async" in names, "defer": "defer" in names, "type": attr.get("type", ""),
                "in_head": self.in_head, "nonce": "nonce" in names, "inline_chars": 0,
            }
            self.scripts.append(self._script)
        elif tag == "link" and "stylesheet" in attr.get("rel", "").lower().split():
            self.styles.append({"href": self._abs(attr.get("href")), "integrity": attr.get("integrity") or None,
                                "crossorigin": attr.get("crossorigin"), "media": attr.get("media", ""),
                                "in_head": self.in_head})
        elif tag == "form":
            self.forms.append({"action": self._abs(attr.get("action")) if attr.get("action") else self.base,
                               "method": (attr.get("method") or "get").lower(), "has_password": False})
            if attr.get("target") == "_blank":
                self.blank.append({"href": self._abs(attr.get("action")), "rel": attr.get("rel", "").lower()})
        elif tag == "input" and attr.get("type", "").lower() == "password":
            form = self.forms[-1] if self.forms else None
            if form:
                form["has_password"] = True
            self.passwords.append({"name": attr.get("name") or attr.get("id") or "",
                                   "autocomplete": attr.get("autocomplete") if "autocomplete" in names else None,
                                   "form_action": form["action"] if form else None,
                                   "form_method": form["method"] if form else None})
        elif tag in {"a", "area"}:
            href = attr.get("href", "")
            if href and not href.strip().lower().startswith(("javascript:", "mailto:", "tel:", "#")):
                self.links.append(self._abs(href))
            if href.strip().lower().startswith("javascript:"):
                self.js_urls += 1
            if attr.get("target") == "_blank":
                self.blank.append({"href": self._abs(href), "rel": attr.get("rel", "").lower()})
        elif tag in {"iframe", "frame"}:
            self.frames.append({"src": self._abs(attr.get("src") or "about:blank"), "sandbox": "sandbox" in names})
        elif tag in {"img", "audio", "video", "source", "track", "embed"} and attr.get("src"):
            self.media.append({"tag": tag, "url": self._abs(attr["src"])})
        elif tag == "object" and attr.get("data"):
            self.media.append({"tag": tag, "url": self._abs(attr["data"])})
        elif tag == "meta" and attr.get("http-equiv", "").strip().lower() == "content-security-policy":
            if attr.get("content"):
                self.meta_csp.append(attr["content"])
        elif tag == "meta" and attr.get("name", "").strip().lower() == "referrer" and attr.get("content"):
            self.meta_referrer = attr["content"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script = None
        elif tag == "title":
            self._in_title = False
        elif tag == "head":
            self.in_head = False

    def handle_data(self, data: str) -> None:
        if self._script is not None and not self._script["src"]:
            self._script["inline_chars"] += len(data)
        if self._in_title:
            self.title += data


def snapshot_from_html(html: str, base_url: str) -> dict[str, Any]:
    """A snapshot of the served HTML; resources are unknown without a browser."""
    collector = _Collector(base_url)
    try:
        collector.feed(html or "")
        collector.close()
    except Exception:
        pass  # a broken document still yields what was parsed before the break
    return {
        "url": base_url, "title": collector.title.strip(), "secure_context": base_url.startswith("https:"),
        "scripts": _cap(collector.scripts), "styles": _cap(collector.styles), "forms": _cap(collector.forms),
        "passwords": _cap(collector.passwords), "blank_links": _cap(collector.blank),
        "inline_handlers": _cap(collector.handlers), "javascript_urls": collector.js_urls,
        "frames": _cap(collector.frames), "media": _cap(collector.media), "resources": _cap([]),
        "meta_csp": collector.meta_csp, "meta_referrer": collector.meta_referrer, "source": "static-html",
        "links": _cap(collector.links),
    }


def _items(snapshot: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The dict entries of one snapshot list; anything else a hostile page returned is dropped."""
    value = snapshot.get(key) if isinstance(snapshot, dict) else None
    items = value.get("items") if isinstance(value, dict) else value
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _strings(snapshot: dict[str, Any], key: str) -> list[str]:
    value = snapshot.get(key) if isinstance(snapshot, dict) else None
    items = value.get("items") if isinstance(value, dict) else value
    return [item for item in items if isinstance(item, str)] if isinstance(items, list) else []


def _total(snapshot: dict[str, Any], key: str) -> int:
    value = snapshot.get(key) if isinstance(snapshot, dict) else None
    total = value.get("total") if isinstance(value, dict) else None
    return total if isinstance(total, int) and total >= 0 else len(_items(snapshot, key) or _strings(snapshot, key))


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def usable(snapshot: Any) -> bool:
    """A browser snapshot shaped like ours (a page may have broken the script's globals)."""
    return isinstance(snapshot, dict) and isinstance(snapshot.get("scripts"), dict) and isinstance(snapshot.get("url"), str)


def third_parties(snapshot: dict[str, Any], page_url: str, network: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Every other site the page loads from, with the scripts listed one by one."""
    scripts = [
        {"src": _text(s, "src"), "site": site_of(host_of(_text(s, "src"))), "sri": bool(s.get("integrity")),
         "crossorigin": s.get("crossorigin")}
        for s in _items(snapshot, "scripts") if _text(s, "src") and is_third_party(_text(s, "src"), page_url)
    ]
    urls = [_text(r, "url") for r in _items(snapshot, "resources")]
    urls += [str(row.get("url") or "") for row in network or [] if isinstance(row, dict)]
    urls += [_text(s, "href") for s in _items(snapshot, "styles")] + [_text(f, "src") for f in _items(snapshot, "frames")]
    urls += [s["src"] for s in scripts]
    counts: dict[str, int] = {}
    for url in dict.fromkeys(urls):  # one count per distinct URL, whichever source saw it
        if is_third_party(url, page_url):
            site = site_of(host_of(url))
            counts[site] = counts.get(site, 0) + 1
    out: dict[str, Any] = {"scripts": scripts, "sites": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))}
    if _total(snapshot, "resources") >= RESOURCE_TIMING_BUFFER:
        out["resource_timing_note"] = (f"Chrome keeps {RESOURCE_TIMING_BUFFER} resource timings by default; the page "
                                       "loaded at least that many, so later resources may be missing here.")
    return out


def _mixed(snapshot: dict[str, Any], network: list[dict[str, Any]] | None,
           graded_scripts: set[str]) -> tuple[list[str], list[str]]:
    active = [_text(s, "src") for s in _items(snapshot, "scripts")
              if _text(s, "src").startswith("http:") and _text(s, "src") not in graded_scripts]
    active += [_text(s, "href") for s in _items(snapshot, "styles") if _text(s, "href").startswith("http:")]
    active += [_text(f, "src") for f in _items(snapshot, "frames") if _text(f, "src").startswith("http:")]
    passive = [_text(m, "url") for m in _items(snapshot, "media") if _text(m, "url").startswith("http:")]
    for row in network or []:
        url = str(row.get("url") or "") if isinstance(row, dict) else ""
        if url.startswith("http:") and url not in active and url not in passive and url not in graded_scripts:
            (passive if row.get("type") in {"Image", "Media"} else active).append(url)
    return sorted(set(active)), sorted(set(passive))


HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def observatory_mime(content_type: str | None) -> str:
    """The media type as Observatory compares it: the text before the first ';', neither trimmed nor lowercased."""
    return str(content_type or "").split(";", 1)[0]
_SRI_ORDER = ("sri-all-secure", "sri-external-secure", "sri-insecure", "sri-missing", "sri-missing-insecure",
              "sri-not-html")
_SRI = {
    "sri-all-secure": (5, "pass", "info", "Scripts with SRI, loaded securely", ""),
    "sri-external-secure": (5, "pass", "info", "Every third-party script has SRI and loads over https", ""),
    "sri-insecure": (-20, "fail", "high", "Third-party scripts with SRI load over http or a protocol-relative URL",
                     "Load them with an explicit https:// URL."),
    "sri-missing": (-5, "fail", "medium", "Third-party scripts without SRI",
                    'Pin each file with integrity="sha384-..." crossorigin="anonymous" (or self-host it).'),
    "sri-missing-insecure": (-50, "fail", "high", "Third-party scripts without SRI load over http or protocol-relative",
                             "Load them over https:// and pin each with integrity=\"sha384-...\" crossorigin=\"anonymous\"."),
    "sri-not-html": (0, "info", "info", "The response is not HTML; SRI does not apply", ""),
    "sri-no-scripts": (0, "pass", "info", "The page loads no scripts", ""),
    "sri-own-scripts": (0, "pass", "info", "Scripts come from the page's own site", ""),
}


def sri(served: dict[str, Any], named_host: str, https: bool, content_type: str | None) -> dict[str, Any]:
    """Observatory's SRI test on the served HTML (the scripts the server sent, not the live DOM).

    ``//host/x.js`` counts as foreign and insecure (no explicit scheme), a full
    URL (``http://`` / ``https://``, lowercase as Observatory matches it) on the
    named host's registrable domain as the page's own, anything else as a
    relative URL, secure only on an https page. Results only ever get worse, in
    Observatory's order; own scripts with SRI earn the bonus when nothing worse
    was found.
    """
    mime = observatory_mime(content_type)
    if mime and mime not in HTML_TYPES:
        fid, evidence = "sri-not-html", None
    else:
        scripts = _items(served, "scripts")
        result: str | None = None
        foreign_any, evidence = False, []
        own_site = site_of(named_host)
        for script in scripts:
            raw = _text(script, "raw_src") or ""
            if not raw:
                continue
            integrity = bool(script.get("integrity"))
            relative_protocol = len(raw) > 2 and raw.startswith("//") and raw[2] != "/"
            full = not relative_protocol and raw.startswith(("http://", "https://"))
            relative = not relative_protocol and not full
            same = relative_protocol or relative or site_of(host_of(raw)) == own_site
            own = relative or (same and not relative_protocol)
            secure = raw.startswith("https://") or (relative and https)
            if not own:
                foreign_any = True
                evidence.append({"src": raw[:300], "integrity": integrity})
                if integrity and not secure:
                    new = "sri-insecure"
                elif not integrity and secure:
                    new = "sri-missing"
                elif not integrity:
                    new = "sri-missing-insecure"
                else:
                    new = None
                if new and (result is None or _SRI_ORDER.index(new) > _SRI_ORDER.index(result)):
                    result = new
            elif integrity and secure and result is None:
                result = "sri-all-secure"
        if not scripts:
            fid = "sri-no-scripts"
        else:
            fid = result or ("sri-external-secure" if foreign_any else "sri-own-scripts")
    points, status, severity, title, fix = _SRI[fid]
    return finding(fid, "page", status, severity, title, fix=fix, points=points,
                   evidence=clip_list(evidence, 20) if evidence else None,
                   detail="Judged on the HTML the server sent, as Observatory does; scripts added at runtime are "
                          "listed under third_parties.")


def _forms(snapshot: dict[str, Any], https: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for form in _items(snapshot, "forms"):
        if _text(form, "action").startswith("http:") and https:
            out.append(finding("form-insecure-action", "page", "fail", "high", "A form submits over http",
                               fix="Point the form action at https://.", evidence=_text(form, "action"),
                               extra_points=-20))
            break
    passwords = _items(snapshot, "passwords")
    if passwords and not https:
        out.append(finding("password-over-http", "page", "fail", "high", "Password field on an http page",
                           detail="Browsers mark the page Not secure; the password travels in clear text.",
                           fix="Serve the page (and the form target) over https.", extra_points=-20))
    if any(_text(p, "form_method") == "get" for p in passwords):
        out.append(finding("password-get-form", "page", "fail", "medium", "A password form uses GET",
                           detail="The password ends up in the URL, history, server logs and Referer.",
                           fix='Use method="post".', extra_points=-10))
    bad_autocomplete = [p for p in passwords if _text(p, "autocomplete").strip().lower() not in
                        {"current-password", "new-password", "one-time-code"}]
    if bad_autocomplete:
        out.append(finding(
            "password-autocomplete", "page", "warn", "low", "Password fields without a password autocomplete token",
            detail="autocomplete=\"off\" is ignored by browsers and only hinders password managers; "
                   "without a token managers may fill the wrong field.",
            fix='Use autocomplete="current-password" on sign-in and "new-password" on sign-up/change forms.',
            evidence=[{"name": p.get("name"), "autocomplete": p.get("autocomplete")} for p in bad_autocomplete[:20]]))
    return out


def _inline(snapshot: dict[str, Any], csp_summary: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    inline = [s for s in _items(snapshot, "scripts") if not _text(s, "src") and _text(s, "type") not in {
        "application/json", "application/ld+json", "importmap", "speculationrules", "text/template"}]
    handler_total = _total(snapshot, "inline_handlers")
    unnonced = [s for s in inline if not s.get("nonce")]
    if csp_summary.get("inline_scripts_blocked") and unnonced and csp_summary.get("uses_hashes"):
        out.append(info("inline-scripts-hashes", "page", f"{len(unnonced)} inline script(s) without a nonce",
                        detail="The CSP allows inline scripts by hash; those whose hash is listed run, the rest "
                               "are blocked - check the console for CSP violations.",
                        evidence={"inline_scripts": len(unnonced)}))
    elif csp_summary.get("inline_scripts_blocked") and unnonced:
        out.append(finding(
            "inline-scripts-blocked", "page", "fail", "medium", f"{len(unnonced)} inline script(s) blocked by your CSP",
            detail="The policy forbids inline code, so these scripts do not run - the page may be broken.",
            fix="Move them into files, or add the response's nonce (or a sha256 hash) to each.",
            evidence={"inline_scripts": len(unnonced)}))
    if csp_summary.get("inline_handlers_blocked") and handler_total:
        out.append(finding(
            "inline-handlers-blocked", "page", "fail", "medium", f"{handler_total} inline event handler(s) blocked by your CSP",
            detail="onclick= and similar attributes never run under this policy.",
            fix="Attach handlers with addEventListener in a script file.",
            evidence=clip_list(_strings(snapshot, "inline_handlers"), 20)))
    js_urls = snapshot.get("javascript_urls") if isinstance(snapshot.get("javascript_urls"), int) else 0
    if not csp_summary.get("strict_scripts") and (inline or handler_total or js_urls):
        out.append(info(
            "inline-code", "page", "Inline code keeps the site from a strict CSP",
            detail=f"{len(inline)} inline script(s), {handler_total} inline handler(s), {js_urls} javascript: URL(s).",
            fix="Move inline code into files (or nonce it) so script-src can drop 'unsafe-inline'."))
    return out


def analyze(snapshot: dict[str, Any], page_url: str, csp_summary: dict[str, Any],
            network: list[dict[str, Any]] | None = None, *, served: dict[str, Any] | None = None,
            named_host: str | None = None, content_type: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Findings for the page plus the third-party overview.

    SRI is Observatory's test and carries ``points``; mixed content and the form
    checks are this report's own and carry ``extra_points``.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    out: list[dict[str, Any]] = []
    https = urlsplit(page_url).scheme == "https"
    parties = third_parties(snapshot, page_url, network)
    third = parties["scripts"]
    out.append(sri(served if isinstance(served, dict) else snapshot, named_host or host_of(page_url), https,
                   content_type))
    if https:
        active, passive = _mixed(snapshot, network, {s["src"] for s in third})
        if active:
            out.append(finding(
                "mixed-content-active", "page", "fail", "high", "Scripts, styles or frames load over http",
                detail="Browsers block them (the page breaks) or, where allowed, a network attacker controls the page.",
                fix="Load every subresource over https (and add CSP upgrade-insecure-requests).",
                evidence=clip_list(active, 20), extra_points=-50))
        if passive:
            out.append(finding(
                "mixed-content-passive", "page", "fail", "medium", "Images or media load over http",
                detail="Browsers upgrade or block them; they can be swapped on the network and mark the page not secure.",
                fix="Serve these files over https.", evidence=clip_list(passive, 20), extra_points=-10))
        if not active and not passive:
            out.append(passed("mixed-content-none", "page", "No mixed content"))
    no_cors = [s["src"] for s in third if s["sri"] and not s.get("crossorigin")]
    if no_cors:
        out.append(finding("sri-no-crossorigin", "page", "fail", "medium", "SRI without crossorigin",
                           detail="A cross-origin script with integrity but no crossorigin attribute fails to load.",
                           fix='Add crossorigin="anonymous".', evidence=clip_list(no_cors, 20)))
    out += _forms(snapshot, https)
    unsafe_blank = [_text(b, "href") for b in _items(snapshot, "blank_links")
                    if "noopener" not in _text(b, "rel") and "noreferrer" not in _text(b, "rel")
                    and is_third_party(_text(b, "href"), page_url)]
    if unsafe_blank:
        out.append(finding(
            "blank-without-noopener", "page", "warn", "low", "target=_blank links without rel=noopener",
            detail="Current browsers imply noopener; older ones let the opened site navigate this tab (tabnabbing).",
            fix='Add rel="noopener noreferrer" to external target=_blank links.', evidence=clip_list(unsafe_blank, 20)))
    out += _inline(snapshot, csp_summary)
    frames = [_text(f, "src") for f in _items(snapshot, "frames")
              if not f.get("sandbox") and is_third_party(_text(f, "src"), page_url)]
    if frames:
        out.append(info("third-party-frames", "page", f"{len(frames)} third-party frame(s) without sandbox",
                        fix="Add sandbox with only the allow-* tokens the widget needs.", evidence=clip_list(frames, 20)))
    if parties["sites"]:
        out.append(info("third-party-sites", "page", f"The page loads from {len(parties['sites'])} other site(s)",
                        detail="Each is code or content you do not control; review that every one is still needed.",
                        evidence=parties["sites"]))
    return out, parties
