// Pure helpers for the companion event pipeline: CDP payloads in, agent-facing
// records out. Nothing here touches chrome.*, so it can be unit-tested with node.

export const MAX_ENTRIES = 500;
export const MAX_BYTES = 512 * 1024;
export const MAX_PENDING = 1000;

const ARG_LIMIT = 200;
const TEXT_LIMIT = 2000;
const EXCEPTION_LIMIT = 4000;
const STACK_LIMIT = 10;
const DEFAULT_LIMIT = 200;

const LEVEL_RANK = {debug: 0, info: 1, warn: 2, error: 3};

const CONSOLE_LEVELS = {
  debug: "debug",
  log: "info",
  info: "info",
  dir: "info",
  dirxml: "info",
  table: "info",
  trace: "info",
  count: "info",
  timeEnd: "info",
  group: "info",
  groupCollapsed: "info",
  groupEnd: "info",
  startGroup: "info",
  startGroupCollapsed: "info",
  endGroup: "info",
  warning: "warn",
  error: "error",
  assert: "error",
};

const BROWSER_LEVELS = {verbose: "debug", info: "info", warning: "warn", error: "error"};

const TEXTUAL_MIME = /^text\/|^application\/(json|javascript|xml|xhtml\+xml|x-www-form-urlencoded)|\+json$|\+xml$/i;

export function consoleLevel(type) {
  return CONSOLE_LEVELS[String(type)] || "info";
}

export function browserLevel(level) {
  return BROWSER_LEVELS[String(level)] || "info";
}

export function truncate(value, limit) {
  const text = value === undefined || value === null ? "" : String(value);
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function stringifyValue(value) {
  if (value === null || typeof value !== "object") return String(value);
  try {
    return JSON.stringify(value);
  } catch (_error) {
    return String(value);
  }
}

function formatPreview(preview) {
  const isArray = preview.subtype === "array";
  const parts = (preview.properties || []).map(property => {
    const rendered = property.value === undefined ? property.type || "" : String(property.value);
    return isArray ? rendered : `${property.name}: ${rendered}`;
  });
  if (preview.overflow) parts.push("…");
  const body = parts.join(", ");
  if (isArray) return `[${body}]`;
  const label = preview.description && preview.description !== "Object" ? `${preview.description} ` : "";
  return `${label}{${body}}`;
}

export function formatArg(arg) {
  if (arg === null || typeof arg !== "object") return truncate(arg, ARG_LIMIT);
  if (arg.value !== undefined) return truncate(stringifyValue(arg.value), ARG_LIMIT);
  if (arg.preview) return truncate(formatPreview(arg.preview), ARG_LIMIT);
  return truncate(arg.description || arg.type || "", ARG_LIMIT);
}

export function formatStack(stackTrace, limit = STACK_LIMIT) {
  const frames = stackTrace?.callFrames || [];
  return frames.slice(0, limit).map(frame => ({
    fn: frame.functionName || "(anonymous)",
    url: frame.url || "",
    line: (frame.lineNumber ?? -1) + 1,
    col: (frame.columnNumber ?? -1) + 1,
  }));
}

function baseEntry(context, kind, level) {
  return {
    seq: context.seq ?? 0,
    ts: context.ts ?? 0,
    kind,
    level,
    text: "",
    args: [],
    source: "",
    url: "",
    line: 0,
    col: 0,
    stack: [],
    frame: context.frame ?? null,
  };
}

export function consoleEntry(params, context = {}) {
  const level = consoleLevel(params.type);
  const args = (params.args || []).map(formatArg);
  const top = params.stackTrace?.callFrames?.[0] || {};
  const entry = baseEntry(context, "console", level);
  entry.text = truncate(args.join(" "), TEXT_LIMIT);
  entry.args = args;
  entry.source = "console-api";
  entry.url = top.url || "";
  entry.line = (top.lineNumber ?? -1) + 1;
  entry.col = (top.columnNumber ?? -1) + 1;
  // Only failures are worth the extra frames.
  entry.stack = level === "error" ? formatStack(params.stackTrace) : [];
  return entry;
}

export function exceptionEntry(params, context = {}) {
  const details = params.exceptionDetails || {};
  const entry = baseEntry(context, "exception", "error");
  entry.text = truncate(
    details.exception?.description || details.text || "Uncaught exception",
    EXCEPTION_LIMIT,
  );
  entry.source = "javascript";
  entry.url = details.url || details.stackTrace?.callFrames?.[0]?.url || "";
  entry.line = (details.lineNumber ?? -1) + 1;
  entry.col = (details.columnNumber ?? -1) + 1;
  entry.stack = formatStack(details.stackTrace);
  return entry;
}

export function browserEntry(params, context = {}) {
  const logged = params.entry || {};
  const entry = baseEntry(context, "browser", browserLevel(logged.level));
  entry.text = truncate(logged.text || "", TEXT_LIMIT);
  entry.source = logged.source || "other";
  entry.url = logged.url || "";
  entry.line = (logged.lineNumber ?? -1) + 1;
  entry.stack = formatStack(logged.stackTrace);
  if (logged.networkRequestId) entry.network_request_id = logged.networkRequestId;
  return entry;
}

export function navigationEntry(url, context = {}) {
  const entry = baseEntry(context, "navigation", "info");
  entry.text = `→ ${truncate(url, TEXT_LIMIT)}`;
  entry.source = "page";
  entry.url = url || "";
  return entry;
}

export function networkLevel(row) {
  if (row.failed) return "error";
  const status = Number(row.status || 0);
  if (status >= 400) return "error";
  if (status >= 300) return "warn";
  return "info";
}

export function networkRow(params, context = {}) {
  const request = params.request || {};
  const row = {
    seq: 0,
    kind: "network",
    level: "info",
    id: String(params.requestId || ""),
    ts: context.ts ?? 0,
    method: request.method || "GET",
    url: request.url || "",
    type: params.type || "Other",
    initiator: params.initiator?.type || "",
    doc: params.documentURL || "",
    frame: params.frameId || context.frame || null,
  };
  // Monotonic CDP timestamp, kept off the wire but needed for the duration.
  Object.defineProperty(row, "t0", {value: Number(params.timestamp) || 0, enumerable: false});
  return row;
}

export function applyResponse(row, params, includeHeaders = false) {
  const response = params.response || {};
  row.status = Number(response.status || 0);
  row.mime = response.mimeType || "";
  row.from_cache = Boolean(
    response.fromDiskCache || response.fromPrefetchCache || response.fromServiceWorker,
  );
  row.remote = response.remoteIPAddress
    ? `${response.remoteIPAddress}:${response.remotePort ?? 0}`
    : "";
  if (includeHeaders) row.headers = response.headers || {};
  row.level = networkLevel(row);
  return row;
}

function durationMs(row, params) {
  const finished = Number(params.timestamp) || 0;
  if (!row.t0 || !finished) return 0;
  return Math.max(0, Math.round((finished - row.t0) * 1000));
}

export function applyFinished(row, params) {
  row.size = Math.round(Number(params.encodedDataLength) || row.size || 0);
  row.ms = durationMs(row, params);
  row.done = true;
  row.level = networkLevel(row);
  return row;
}

export function applyFailed(row, params) {
  row.failed = true;
  row.error = params.errorText || "";
  row.canceled = Boolean(params.canceled);
  if (params.blockedReason) row.blocked_reason = params.blockedReason;
  row.ms = durationMs(row, params);
  row.done = true;
  row.level = networkLevel(row);
  return row;
}

export function applyRedirect(row, params) {
  const response = params.redirectResponse || {};
  row.status = Number(response.status || 0);
  row.mime = response.mimeType || "";
  row.redirected_to = params.request?.url || "";
  row.ms = durationMs(row, params);
  row.done = true;
  row.level = networkLevel(row);
  return row;
}

export function isTextualMime(mime) {
  return TEXTUAL_MIME.test(String(mime || ""));
}

export function sameFrameUrl(left, right) {
  if (left === right) return true;
  if (!left || !right) return false;
  try {
    const a = new URL(left);
    const b = new URL(right);
    return a.origin === b.origin && a.pathname === b.pathname;
  } catch (_error) {
    return false;
  }
}

export function createBuffer(startedAt = 0) {
  return {
    seq: 0,
    startedAt,
    console: [],
    network: [],
    pending: new Map(),
    dropped: {console: 0, network: 0},
    bytes: {console: 0, network: 0},
  };
}

export function nextSeq(buffer) {
  buffer.seq += 1;
  return buffer.seq;
}

function entrySize(entry) {
  if (entry.bytes) return entry.bytes;
  let size = 256;
  try {
    size = JSON.stringify(entry).length;
  } catch (_error) {
    size = 256;
  }
  Object.defineProperty(entry, "bytes", {value: size, enumerable: false});
  return size;
}

function dropOldest(buffer, stream) {
  const removed = buffer[stream].shift();
  if (!removed) return false;
  buffer.bytes[stream] = Math.max(0, buffer.bytes[stream] - entrySize(removed));
  buffer.dropped[stream] += 1;
  return true;
}

export function pushEntry(buffer, stream, entry) {
  if (!entry.seq) entry.seq = nextSeq(buffer);
  buffer[stream].push(entry);
  buffer.bytes[stream] += entrySize(entry);
  while (buffer[stream].length > MAX_ENTRIES) {
    if (!dropOldest(buffer, stream)) break;
  }
  while (buffer.bytes.console + buffer.bytes.network > MAX_BYTES) {
    const victim = buffer.bytes.console >= buffer.bytes.network ? "console" : "network";
    if (!dropOldest(buffer, victim)) break;
  }
  return entry;
}

export function trackPending(buffer, row) {
  buffer.pending.set(row.id, row);
  while (buffer.pending.size > MAX_PENDING) {
    const oldest = buffer.pending.keys().next();
    if (oldest.done) break;
    buffer.pending.delete(oldest.value);
    buffer.dropped.network += 1;
  }
  return row;
}

export function clearBuffer(buffer, streams) {
  const wanted = normalizeKinds(streams).streams;
  for (const stream of wanted) {
    buffer[stream] = [];
    buffer.bytes[stream] = 0;
    buffer.dropped[stream] = 0;
    if (stream === "network") buffer.pending.clear();
  }
  return [...wanted];
}

// "console"/"network" pick a stream, any other value narrows the entry kind.
export function normalizeKinds(kinds) {
  const streams = new Set();
  const entryKinds = new Set();
  for (const raw of Array.isArray(kinds) ? kinds : kinds ? [kinds] : []) {
    const name = String(raw).toLowerCase();
    if (name === "console" || name === "network") {
      streams.add(name);
      continue;
    }
    entryKinds.add(name);
    if (["exception", "browser", "navigation"].includes(name)) streams.add("console");
  }
  if (!streams.size) {
    streams.add("console");
    streams.add("network");
  }
  return {streams, entryKinds};
}

function matchesLevel(entry, level) {
  if (Array.isArray(level)) return level.map(String).includes(entry.level);
  const wanted = LEVEL_RANK[String(level)];
  if (wanted === undefined) return true;
  return (LEVEL_RANK[entry.level] ?? 1) >= wanted;
}

function matchesEntry(entry, options) {
  if (options.entryKinds.size && !options.entryKinds.has(entry.kind)) return false;
  if (options.level && !matchesLevel(entry, options.level)) return false;
  if (options.onlyErrors && entry.level !== "error") return false;
  if (options.contains) {
    const haystack = `${entry.text || ""} ${entry.url || ""}`.toLowerCase();
    if (!haystack.includes(options.contains)) return false;
  }
  if (options.urlPattern && !options.urlPattern.test(entry.url || "")) return false;
  if (options.types) {
    if (entry.kind !== "network") return false;
    if (!options.types.has(String(entry.type || "").toLowerCase())) return false;
  }
  return true;
}

function compilePattern(pattern) {
  if (!pattern) return null;
  try {
    return new RegExp(String(pattern), "i");
  } catch (_error) {
    const literal = String(pattern).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(literal, "i");
  }
}

export function collectEvents(buffer, options = {}) {
  const requestedSeq = Math.max(0, Number(options.since_seq) || 0);
  // An evicted worker restarts the counter; replay instead of going silent.
  const reset = requestedSeq > buffer.seq;
  const sinceSeq = reset ? 0 : requestedSeq;
  const limit = Math.min(Math.max(Number(options.limit) || DEFAULT_LIMIT, 1), 2000);
  const {streams, entryKinds} = normalizeKinds(options.kinds);
  const filter = {
    entryKinds,
    level: options.level || null,
    onlyErrors: Boolean(options.only_errors),
    contains: options.contains ? String(options.contains).toLowerCase() : "",
    urlPattern: compilePattern(options.url_pattern),
    types: Array.isArray(options.types) && options.types.length
      ? new Set(options.types.map(item => String(item).toLowerCase()))
      : null,
  };

  const pool = [];
  for (const stream of streams) {
    for (const entry of buffer[stream]) {
      if (entry.seq > sinceSeq && matchesEntry(entry, filter)) pool.push(entry);
    }
  }
  pool.sort((left, right) => left.seq - right.seq);
  const entries = pool.slice(0, limit);
  const truncated = pool.length > entries.length;
  return {
    entries,
    next_seq: truncated ? entries[entries.length - 1].seq : buffer.seq,
    dropped: {...buffer.dropped},
    started_at: buffer.startedAt,
    truncated,
    reset,
    pending: buffer.pending.size,
  };
}

// Legacy shape kept for ChromeBridgeDriver.get_log("browser").
const LEGACY_LEVELS = {debug: "DEBUG", info: "INFO", warn: "WARNING", error: "SEVERE"};

export function legacyConsole(buffer) {
  return buffer.console.map(entry => ({
    level: LEGACY_LEVELS[entry.level] || "INFO",
    message: entry.text,
    timestamp: entry.ts,
  }));
}
