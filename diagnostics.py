"""Console and network capture shared by the Selenium and companion backends.

The companion buffers CDP events inside the extension. Selenium cannot subscribe
to CDP events at all, so the same information is recovered from a page-level
console hook plus Chrome's performance log, and both paths are normalised into
one record shape.
"""

from __future__ import annotations

import json
import re
from typing import Any


LEVELS = ("debug", "info", "warn", "error")
_SELENIUM_LEVELS = {
    "SEVERE": "error",
    "WARNING": "warn",
    "INFO": "info",
    "DEBUG": "debug",
    "VERBOSE": "debug",
}

# Captures console output and unhandled failures in the page itself. Chrome only
# reports console.log through CDP's Runtime domain, which Selenium cannot read,
# so the hook is what makes the Selenium path see anything at all.
CONSOLE_HOOK_SCRIPT = r"""
(() => {
const key = '__wsnConsole';
if (window[key]) return;
const state = {seq: 0, dropped: 0, items: [], limit: 500};
const clip = (value, max) => {
  // Strings go through verbatim; quoting them would make every log line noisy.
  let text;
  if (typeof value === 'string') text = value;
  else if (value === undefined) text = 'undefined';
  else {
    try { text = JSON.stringify(value); } catch (_) { text = String(value); }
    if (text === undefined) text = String(value);
  }
  return text.length > max ? text.slice(0, max) + '...' : text;
};
const push = (level, args, extra) => {
  state.seq += 1;
  if (state.items.length >= state.limit) { state.items.shift(); state.dropped += 1; }
  state.items.push(Object.assign({
    seq: state.seq,
    ts: Date.now(),
    kind: 'console',
    level: level,
    source: 'console-api',
    args: args.map(item => clip(item, 200)),
    text: args.map(item => clip(item, 200)).join(' ').slice(0, 2000)
  }, extra || {}));
};
const levels = {log: 'info', info: 'info', debug: 'debug', trace: 'info',
                warn: 'warn', error: 'error', assert: 'error', table: 'info', dir: 'info'};
for (const name of Object.keys(levels)) {
  const original = console[name];
  if (typeof original !== 'function') continue;
  console[name] = function (...args) {
    try { push(levels[name], args); } catch (_) {}
    return original.apply(console, args);
  };
}
addEventListener('error', event => push('error', [event.message], {
  kind: 'exception', source: 'javascript', url: event.filename,
  line: event.lineno, col: event.colno,
  stack: event.error && event.error.stack ? String(event.error.stack).split('\n').slice(0, 10) : []
}));
addEventListener('unhandledrejection', event => push('error',
  ['Unhandled promise rejection: ' + clip(event.reason, 500)],
  {kind: 'exception', source: 'javascript'}));
window[key] = state;
})();
"""

_CONSOLE_READ_SCRIPT = """
const since = arguments[0];
const state = window.__wsnConsole;
if (!state) return {entries: [], next_seq: 0, dropped: 0, installed: false};
const entries = state.items.filter(item => item.seq > since);
if (arguments[1]) { state.items = []; }
return {
  entries: entries,
  next_seq: state.seq,
  dropped: state.dropped,
  installed: true
};
"""


def read_page_console(driver: Any, since_seq: int = 0, clear: bool = False) -> dict[str, Any]:
    """Drain the in-page console hook, installing it first if needed."""
    result = driver.execute_script(_CONSOLE_READ_SCRIPT, int(since_seq), bool(clear))
    if not result.get("installed"):
        driver.execute_script(CONSOLE_HOOK_SCRIPT)
        return {"entries": [], "next_seq": 0, "dropped": 0}
    return result


def selenium_browser_log(driver: Any) -> list[dict[str, Any]]:
    """Read Chrome's own browser log: network failures, CSP, deprecations."""
    try:
        raw = driver.get_log("browser")
    except Exception:
        return []
    entries = []
    for item in raw:
        message = str(item.get("message", ""))
        location = re.match(r"^(\S+) (\d+):(\d+)\s+(.*)$", message, re.DOTALL)
        entries.append(
            {
                "seq": 0,
                "ts": int(item.get("timestamp", 0)),
                "kind": "browser",
                "level": _SELENIUM_LEVELS.get(str(item.get("level", "INFO")), "info"),
                "source": item.get("source") or "other",
                "text": location.group(4) if location else message,
                "url": location.group(1) if location else None,
                "line": int(location.group(2)) if location else None,
                "col": int(location.group(3)) if location else None,
                "args": [],
                "stack": [],
            }
        )
    return entries


def _performance_messages(driver: Any) -> list[dict[str, Any]]:
    try:
        raw = driver.get_log("performance")
    except Exception:
        return []
    messages = []
    for item in raw:
        try:
            payload = json.loads(item["message"])["message"]
        except (KeyError, ValueError, TypeError):
            continue
        messages.append(payload)
    return messages


def selenium_network_rows(driver: Any, pending: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold Chrome's performance log into finished request rows.

    ``pending`` carries partial rows between calls, because a request's start,
    response, and completion arrive as three separate log entries.
    """
    finished: list[dict[str, Any]] = []
    for message in _performance_messages(driver):
        method = message.get("method", "")
        params = message.get("params", {})
        request_id = params.get("requestId")
        if not request_id or not method.startswith("Network."):
            continue
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            pending[request_id] = {
                "kind": "network",
                "id": request_id,
                "ts": int(float(params.get("wallTime", 0)) * 1000),
                "started": float(params.get("timestamp", 0)),
                "method": request.get("method", "GET"),
                "url": request.get("url", ""),
                "type": params.get("type", "Other"),
                "initiator": (params.get("initiator") or {}).get("type"),
                "doc": params.get("documentURL"),
                "status": None,
                "done": False,
            }
        elif method == "Network.responseReceived":
            row = pending.get(request_id)
            if row is None:
                continue
            response = params.get("response", {})
            row.update(
                status=response.get("status"),
                mime=response.get("mimeType"),
                from_cache=bool(response.get("fromDiskCache") or response.get("fromServiceWorker")),
                remote=response.get("remoteIPAddress"),
                type=params.get("type", row.get("type")),
            )
        elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
            row = pending.pop(request_id, None)
            if row is None:
                continue
            row["ms"] = round(
                (float(params.get("timestamp", 0)) - row.pop("started", 0)) * 1000
            )
            if method == "Network.loadingFinished":
                row.update(size=int(params.get("encodedDataLength", 0)), done=True)
            else:
                row.update(
                    failed=True,
                    done=True,
                    error=params.get("errorText"),
                    canceled=bool(params.get("canceled")),
                    blocked_reason=params.get("blockedReason"),
                )
            status = row.get("status") or 0
            row["level"] = (
                "error"
                if row.get("failed") or status >= 400
                else ("warn" if 300 <= status < 400 else "info")
            )
            row["text"] = f"{row['method']} {status or '---'} {row['url']}"
            finished.append(row)
    return finished


def dedupe_console(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop browser-log copies of messages the in-page hook already captured.

    Chrome reports the same ``console.error`` through both channels, and showing
    it twice makes an agent think it happened twice.
    """
    hooked = {
        (str(entry.get("level")), str(entry.get("text", "")).strip())
        for entry in entries
        if entry.get("source") == "console-api"
    }
    result = []
    for entry in entries:
        if entry.get("source") == "console-api":
            result.append(entry)
            continue
        key = (str(entry.get("level")), str(entry.get("text", "")).strip())
        if key in hooked:
            continue
        result.append(entry)
    return result


def filter_console(
    entries: list[dict[str, Any]],
    levels: list[str] | None = None,
    contains: str | None = None,
    kinds: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Apply the level, kind, and substring filters agents actually ask for."""
    wanted = {level.lower() for level in levels} if levels else None
    kind_filter = {kind.lower() for kind in kinds} if kinds else None
    needle = contains.lower() if contains else None
    selected = []
    for entry in entries:
        if wanted and str(entry.get("level", "")).lower() not in wanted:
            continue
        if kind_filter and str(entry.get("kind", "")).lower() not in kind_filter:
            continue
        if needle:
            haystack = f"{entry.get('text', '')} {entry.get('url') or ''}".lower()
            if needle not in haystack:
                continue
        selected.append(entry)
    return selected[-max(1, limit):]


def filter_network(
    rows: list[dict[str, Any]],
    url_pattern: str | None = None,
    types: list[str] | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    only_errors: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter finished request rows; only_errors keeps failures and 4xx/5xx."""
    matcher = None
    if url_pattern:
        try:
            matcher = re.compile(url_pattern, re.IGNORECASE)
        except re.error:
            matcher = re.compile(re.escape(url_pattern), re.IGNORECASE)
    wanted_types = {item.lower() for item in types} if types else None
    selected = []
    for row in rows:
        status = row.get("status") or 0
        if matcher and not matcher.search(row.get("url", "")):
            continue
        if wanted_types and str(row.get("type", "")).lower() not in wanted_types:
            continue
        if status_min is not None and status < status_min:
            continue
        if status_max is not None and status > status_max:
            continue
        if only_errors and not (row.get("failed") or status >= 400):
            continue
        selected.append(row)
    return selected[-max(1, limit):]


def format_network(rows: list[dict[str, Any]]) -> list[str]:
    """Render rows as one compact line each, which is what an agent reads."""
    lines = []
    for row in rows:
        status = row.get("status")
        size = row.get("size")
        lines.append(
            " ".join(
                part
                for part in (
                    f"{row.get('method', 'GET'):<4}",
                    f"{status if status else 'ERR':>3}",
                    f"{str(row.get('type', 'Other')):<10}",
                    f"{row.get('ms', 0):>5}ms",
                    f"{round(size / 1024, 1)}KB" if size else "",
                    row.get("error") or "",
                    row.get("url", ""),
                )
                if part
            )
        )
    return lines
