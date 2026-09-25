"""Load metrics from the page's own Performance API, rated against the Web Vitals thresholds.

These are lab numbers from the automation browser (its network, its CPU, no
throttling, usually a cold cache in an isolated session), not field data from
real visitors - good for before/after comparisons of your own changes.
"""
from __future__ import annotations

from typing import Any

from web_search_neo.audit.sites import host_of, is_third_party, site_of

# (good up to, poor above) - web.dev/vitals.
THRESHOLDS = {"ttfb_ms": (800, 1800), "fcp_ms": (1800, 3000), "lcp_ms": (2500, 4000), "cls": (0.1, 0.25)}
RESOURCE_LIMIT = 1000
RESOURCE_TIMING_BUFFER = 250  # Chrome's default; the page cannot be made to keep more before load

# An async function body (run_script semantics). Waits for load (bounded), then
# reads the buffered LCP and layout-shift entries through a PerformanceObserver.
PERF_SCRIPT = r"""
const waitMs = Math.max(0, Math.min(Number(arguments[0]) || 0, 30000));
if (document.readyState !== 'complete' && waitMs) {
  await new Promise(done => { addEventListener('load', done, {once: true}); setTimeout(done, waitMs); });
}
const observe = (type) => new Promise(resolve => {
  let seen = [];
  try {
    const observer = new PerformanceObserver(list => { seen = seen.concat(list.getEntries()); });
    observer.observe({type, buffered: true});
    setTimeout(() => { try { seen = seen.concat(observer.takeRecords()); observer.disconnect(); } catch (e) {} resolve(seen); }, 60);
  } catch (e) { resolve(null); }
});
const lcp = await observe('largest-contentful-paint');
const shifts = await observe('layout-shift');
const nav = performance.getEntriesByType('navigation')[0];
const paint = Object.fromEntries(performance.getEntriesByType('paint').map(p => [p.name, p.startTime]));
const lastLcp = lcp && lcp.length ? lcp[lcp.length - 1] : null;
const describe = (el) => el ? (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '')) : null;
const all = performance.getEntriesByType('resource');
const resources = all.slice(0, 1000).map(r => ({
  url: r.name, type: r.initiatorType, start: Math.round(r.startTime), duration: Math.round(r.duration),
  transfer: r.transferSize || 0, encoded: r.encodedBodySize || 0, decoded: r.decodedBodySize || 0,
  blocking: r.renderBlockingStatus || null, protocol: r.nextHopProtocol || '',
}));
const headBlocking = [...document.querySelectorAll('head script[src]:not([async]):not([defer]):not([type=module]), head link[rel~="stylesheet"]:not([media="print"])')]
  .map(el => el.src || el.href);
return {
  url: location.href, ready_state: document.readyState,
  navigation: nav ? {
    start: nav.startTime, response_start: nav.responseStart, request_start: nav.requestStart,
    dom_content_loaded: nav.domContentLoadedEventEnd, load: nav.loadEventEnd, duration: nav.duration,
    transfer: nav.transferSize || 0, encoded: nav.encodedBodySize || 0, decoded: nav.decodedBodySize || 0,
    protocol: nav.nextHopProtocol || '', type: nav.type, redirects: nav.redirectCount,
  } : null,
  fcp: paint['first-contentful-paint'] ?? null, fp: paint['first-paint'] ?? null,
  lcp: lastLcp ? {time: lastLcp.renderTime || lastLcp.loadTime || lastLcp.startTime, size: lastLcp.size,
                  element: describe(lastLcp.element), url: lastLcp.url || null} : null,
  lcp_supported: lcp !== null,
  shifts: shifts === null ? null : shifts.slice(0, 500).map(s => ({value: s.value, time: s.startTime, input: s.hadRecentInput})),
  resources, resources_total: all.length, head_blocking: headBlocking.slice(0, 100),
  dom_nodes: document.getElementsByTagName('*').length,
};
"""


def rate(metric: str, value: float | None) -> str | None:
    """good / needs-improvement / poor, or None when the value is unknown."""
    if value is None:
        return None
    good, poor = THRESHOLDS[metric]
    return "good" if value <= good else "poor" if value > poor else "needs-improvement"


def cumulative_layout_shift(shifts: list[dict[str, Any]] | None) -> float | None:
    """CLS as Chrome defines it: the largest session window (gap < 1 s, span <= 5 s)."""
    if shifts is None:
        return None
    best = current = 0.0
    window_start = last = None
    for shift in sorted((s for s in shifts if not s.get("input")), key=lambda s: float(s.get("time") or 0)):
        moment = float(shift.get("time") or 0)
        if window_start is None or moment - last >= 1000 or moment - window_start > 5000:
            window_start, current = moment, 0.0
        current += float(shift.get("value") or 0)
        last = moment
        best = max(best, current)
    return round(best, 4)


def _kb(value: float) -> float:
    return round(value / 1024, 1)


def _number(value: Any) -> float | None:
    """A finite number, else None (a page can replace the Performance API with anything)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (
            float("inf"), float("-inf")):
        return None
    return value


_NAV_NUMBERS = ("start", "response_start", "request_start", "dom_content_loaded", "load", "duration", "transfer",
                "encoded", "decoded", "redirects")
_RESOURCE_NUMBERS = ("start", "duration", "transfer", "encoded", "decoded")


def _clean(raw: Any) -> dict[str, Any]:
    """The script's answer with every field in the type ``shape`` expects; anything else is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    nav = raw.get("navigation") if isinstance(raw.get("navigation"), dict) else None
    lcp = raw.get("lcp") if isinstance(raw.get("lcp"), dict) else None
    shifts = raw.get("shifts") if isinstance(raw.get("shifts"), list) else None
    resources = []
    for item in raw.get("resources") if isinstance(raw.get("resources"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            resources.append({**{key: _number(item.get(key)) for key in _RESOURCE_NUMBERS},
                              "url": item["url"], "type": str(item.get("type") or "other")[:40],
                              "blocking": item.get("blocking") if isinstance(item.get("blocking"), str) else None,
                              "protocol": str(item.get("protocol") or "")[:20]})
    return {
        "url": raw.get("url") if isinstance(raw.get("url"), str) else "",
        "ready_state": raw.get("ready_state"),
        "navigation": ({**{key: _number(nav.get(key)) for key in _NAV_NUMBERS},
                        "protocol": str(nav.get("protocol") or "")[:20]} if nav is not None else None),
        "fcp": _number(raw.get("fcp")), "fp": _number(raw.get("fp")),
        "lcp": ({"time": _number(lcp.get("time")), "size": _number(lcp.get("size")),
                 "element": lcp.get("element") if isinstance(lcp.get("element"), str) else None,
                 "url": lcp.get("url") if isinstance(lcp.get("url"), str) else None} if lcp is not None else None),
        "lcp_supported": raw.get("lcp_supported") is True,
        "shifts": None if shifts is None else [
            {"value": _number(item.get("value")) or 0.0, "time": _number(item.get("time")) or 0.0,
             "input": item.get("input") is True} for item in shifts if isinstance(item, dict)],
        "resources": resources,
        "resources_total": raw.get("resources_total") if isinstance(raw.get("resources_total"), int)
        and not isinstance(raw.get("resources_total"), bool) else len(resources),
        "head_blocking": [u for u in raw.get("head_blocking") or [] if isinstance(u, str)]
        if isinstance(raw.get("head_blocking"), list) else [],
        "dom_nodes": raw.get("dom_nodes") if isinstance(raw.get("dom_nodes"), int) else None,
    }


def shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Metrics, ratings, resource totals and the recommendations that follow from them.

    The input is whatever the page's script returned; ``_clean`` coerces it
    first, so a page that broke or replaced the Performance API yields empty
    metrics instead of an exception.
    """
    raw = _clean(raw)
    nav = raw.get("navigation") or {}
    page_url = str(raw.get("url") or "")
    ttfb = round(nav["response_start"] - (nav.get("start") or 0)) if nav.get("response_start") else None
    fcp = round(raw["fcp"]) if isinstance(raw.get("fcp"), (int, float)) else None
    lcp = raw.get("lcp") or None
    lcp_ms = round(lcp["time"]) if lcp and isinstance(lcp.get("time"), (int, float)) else None
    cls = cumulative_layout_shift(raw.get("shifts"))
    resources = list(raw.get("resources") or [])
    by_type: dict[str, dict[str, float]] = {}
    for item in resources:
        bucket = by_type.setdefault(str(item.get("type") or "other"), {"count": 0, "kb": 0.0})
        bucket["count"] += 1
        bucket["kb"] = round(bucket["kb"] + _kb(item.get("transfer") or 0), 1)
    total_transfer = sum(item.get("transfer") or 0 for item in resources) + (nav.get("transfer") or 0)
    blocking = [item["url"] for item in resources if item.get("blocking") == "blocking"]
    if not blocking and not any(item.get("blocking") for item in resources):
        blocking = list(raw.get("head_blocking") or [])  # an older Chrome: the head's sync scripts/styles
    third = [item for item in resources if is_third_party(item.get("url", ""), page_url)]
    uncompressed = [item["url"] for item in resources
                    if item.get("type") in {"script", "link", "css", "fetch", "xmlhttprequest"}
                    and (item.get("encoded") or 0) > 50_000 and item.get("encoded") == item.get("decoded")]
    heavy = sorted((item for item in resources if (item.get("transfer") or 0) > 300_000),
                   key=lambda item: -(item.get("transfer") or 0))
    metrics = {
        "ttfb_ms": ttfb, "fcp_ms": fcp, "lcp_ms": lcp_ms, "cls": cls,
        "dom_content_loaded_ms": round(nav["dom_content_loaded"]) if nav.get("dom_content_loaded") else None,
        "load_ms": round(nav["load"]) if nav.get("load") else None,
        "document_kb": _kb(nav.get("transfer") or 0), "protocol": nav.get("protocol") or None,
        "redirects": nav.get("redirects"), "dom_nodes": raw.get("dom_nodes"),
    }
    ratings = {name: rate(name, metrics[name]) for name in THRESHOLDS}
    report: dict[str, Any] = {
        "url": page_url, "metrics": metrics, "ratings": ratings,
        "lcp_element": lcp.get("element") if lcp else None, "lcp_url": lcp.get("url") if lcp else None,
        "resources": {
            "count": int(raw.get("resources_total") or len(resources)), "listed": len(resources),
            "transfer_kb": _kb(total_transfer), "by_type": by_type,
            "third_party": {"count": len(third), "transfer_kb": _kb(sum(i.get("transfer") or 0 for i in third)),
                            "sites": sorted({site_of(host_of(i["url"])) for i in third})[:30]},
            "largest": [{"url": i["url"], "kb": _kb(i.get("transfer") or 0), "type": i.get("type")} for i in heavy[:10]],
        },
        "render_blocking": {"count": len(blocking), "urls": blocking[:20]},
        "recommendations": _recommend(metrics, ratings, blocking, uncompressed, heavy, total_transfer, resources),
        "note": "Lab numbers from the automation browser (no throttling, its own network and cache); "
                "compare runs of the same page rather than reading them as real-visitor data.",
    }
    total = raw.get("resources_total") if isinstance(raw.get("resources_total"), int) else len(resources)
    if total > len(resources):
        report["resources"]["truncated"] = True
    if total >= RESOURCE_TIMING_BUFFER:
        report["resources"]["buffer_note"] = (
            f"Chrome keeps {RESOURCE_TIMING_BUFFER} resource timings by default and the page reached that: "
            "later resources are missing from count, totals and render_blocking.")
    if raw.get("ready_state") != "complete":
        report["warning"] = "The page had not finished loading; load_ms and totals may grow."
    if not raw.get("lcp_supported"):
        report["lcp_note"] = "This browser does not report largest-contentful-paint."
    return report


def _recommend(metrics: dict[str, Any], ratings: dict[str, Any], blocking: list[str], uncompressed: list[str],
               heavy: list[dict[str, Any]], total_transfer: float, resources: list[dict[str, Any]]) -> list[str]:
    out = []
    if ratings["ttfb_ms"] in {"needs-improvement", "poor"}:
        out.append(f"TTFB {metrics['ttfb_ms']} ms: cache the HTML (CDN, server-side cache) or speed up the backend query path.")
    if ratings["lcp_ms"] in {"needs-improvement", "poor"}:
        out.append(f"LCP {metrics['lcp_ms']} ms: preload the LCP image/font (<link rel=preload>, fetchpriority=high), "
                   "serve it smaller (AVIF/WebP, srcset) and do not lazy-load it.")
    if ratings["cls"] in {"needs-improvement", "poor"}:
        out.append(f"CLS {metrics['cls']}: give images/ads/embeds width and height (or aspect-ratio) and avoid "
                   "inserting content above what is already shown.")
    if blocking:
        out.append(f"{len(blocking)} render-blocking resource(s): defer or async scripts, inline critical CSS, "
                   "load the rest with media or preload.")
    if uncompressed:
        out.append(f"{len(uncompressed)} large text resource(s) served uncompressed: enable Brotli or gzip.")
    if heavy:
        out.append(f"{len(heavy)} resource(s) over 300 KB (largest: {heavy[0]['url'][:120]}): compress, resize or split them.")
    if total_transfer > 3_000_000:
        out.append(f"The page transfers {_kb(total_transfer)} KB: aim for under 2-3 MB on first load.")
    if len(resources) > 150:
        out.append(f"{len(resources)} requests: bundle small files and drop unused third-party tags.")
    return out
