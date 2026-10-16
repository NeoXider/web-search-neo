"""One request replayed with a single thing changed, and what that changed.

A replay answers "what does this endpoint say". A variant sweep answers "what does
it say when I change exactly one thing" - the header, the query parameter or the
body field a server may or may not honour - which is where access-control
("does this id belong to me?"), client-routing ("does this header pick a cheaper
channel?") and mass-assignment ("did my extra field reach the record?") questions
are actually settled.

Each mutation is its own request against the same baseline, and the answer is the
delta rather than a second full body to read: status, size change, which JSON
paths appeared or vanished, and which response headers appeared or vanished. Values
themselves are described by shape - ``string(24)``, ``null`` - never quoted, so a
diff cannot smuggle a token into the answer the way a body comparison would.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# A sweep is an experiment, not a fuzzing run: a dozen requests is enough to test
# the handful of fields one endpoint might honour, and past that the answer stops
# being readable.
MAX_VARIANTS = 12
# How many JSON paths a single delta names before it reports a count instead.
SAMPLE_PATHS = 16
MAX_DEPTH = 4
LIST_ITEMS = 4

_MUTABLE_KEYS = ("headers", "query", "body", "method")


def shape(value: Any) -> str:
    """How a value looks, never what it says."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"string({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    if isinstance(value, dict):
        return f"object({len(value)})"
    return "unknown"


def _flatten(value: Any) -> dict[str, Any]:
    """Leaf paths of a JSON document, bounded in depth, list width and total."""
    flat: dict[str, Any] = {}

    def walk(node: Any, path: str, depth: int) -> None:
        if len(flat) >= SAMPLE_PATHS * 6:
            return
        if isinstance(node, dict):
            if depth >= MAX_DEPTH or not node:
                flat[path] = node
                return
            for key, item in node.items():
                walk(item, f"{path}.{key}" if path else str(key), depth + 1)
        elif isinstance(node, list):
            if depth >= MAX_DEPTH or not node:
                flat[path] = node
                return
            for index, item in enumerate(node[:LIST_ITEMS]):
                walk(item, f"{path}[{index}]", depth + 1)
        else:
            flat[path] = node

    walk(value, "", 0)
    return flat


def _json_body(response: dict[str, Any]) -> Any:
    body = response.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _size(response: dict[str, Any]) -> int:
    body = response.get("body")
    return len(body) if isinstance(body, str) else 0


def _parent_path(path: str) -> str:
    cut = max(path.rfind("."), path.rfind("["))
    return path[:cut] if cut > 0 else ""


def _ancestors(paths: set[str]) -> set[str]:
    """Every container a side has, not just the leaves it holds.

    ``profile.name`` means ``profile`` exists: a new field under it is a change to
    ``profile.email``, while a whole object appearing is that object.
    """
    known: set[str] = set()
    for path in paths:
        current = path
        while current:
            known.add(current)
            current = _parent_path(current)
    return known


def _newest_branch(path: str, known_other_side: set[str]) -> str:
    """Walk a new leaf back to the branch that appeared.

    ``scope[0]`` is not a finding when nothing called ``scope`` existed before:
    what appeared was ``scope``. The walk stops at the first ancestor the other
    side already had, which is exactly the boundary of the change.
    """
    current = path
    while True:
        parent = _parent_path(current)
        if not parent or parent in known_other_side:
            return current
        current = parent


def _collapse(paths: list[str]) -> list[str]:
    """Name the shortest path that is new, not every leaf under it.

    A whole branch appearing is one fact - ``scope`` - and listing ``scope[0]``,
    ``scope[1]`` ... for it reads like several findings and hides which branch it
    was.
    """
    kept: list[str] = []
    for path in sorted(paths):
        if kept and (path == kept[-1] or path.startswith(kept[-1] + ".") or path.startswith(kept[-1] + "[")):
            continue
        kept.append(path)
    return kept


def structural_diff(baseline: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """What the variant says that the baseline did not, in paths and shapes only."""
    delta: dict[str, Any] = {}
    base_status = baseline.get("status")
    variant_status = variant.get("status")
    if base_status != variant_status:
        delta["status_changed"] = True
        delta["status_from"] = base_status
    delta["bytes_delta"] = _size(variant) - _size(baseline)

    base_headers = {str(name).lower() for name in (baseline.get("headers") or {})}
    variant_headers = {str(name).lower() for name in (variant.get("headers") or {})}
    delta["headers_added"] = sorted(variant_headers - base_headers)
    delta["headers_removed"] = sorted(base_headers - variant_headers)
    # A 405 names the verbs the route really serves: that list is the map of the
    # endpoint, so it is reported as its own field, not buried in headers_added.
    allow = (variant.get("headers") or {}).get("allow") or (variant.get("headers") or {}).get("Allow")
    if allow:
        delta["allow"] = str(allow)

    base_body, variant_body = _json_body(baseline), _json_body(variant)
    if base_body is None and variant_body is None:
        # Neither side parsed as JSON: only status and size say anything here, and
        # saying more would mean comparing text we cannot trust to be structured.
        delta["body_kind"] = "not_json"
    else:
        base_paths, variant_paths = _flatten(base_body), _flatten(variant_body)
        base_known, variant_known = _ancestors(base_paths), _ancestors(variant_paths)
        added = _collapse(
            [_newest_branch(path, base_known) for path in sorted(set(variant_paths) - set(base_paths))]
        )
        removed = _collapse(
            [_newest_branch(path, variant_known) for path in sorted(set(base_paths) - set(variant_paths))]
        )
        changed = [
            {"path": path, "from": shape(base_paths[path]), "to": shape(variant_paths[path])}
            for path in sorted(set(base_paths) & set(variant_paths))
            if base_paths[path] != variant_paths[path]
        ]
        delta["paths_added"] = added[:SAMPLE_PATHS]
        delta["paths_removed"] = removed[:SAMPLE_PATHS]
        delta["values_changed"] = changed[:SAMPLE_PATHS]
        for key, total in (("paths_added", added), ("paths_removed", removed), ("values_changed", changed)):
            if len(total) > SAMPLE_PATHS:
                delta[f"{key}_count"] = len(total)

    delta["same_as_baseline"] = not (
        delta.get("status_changed")
        or delta["bytes_delta"]
        or delta["headers_added"]
        or delta["headers_removed"]
        or delta.get("paths_added")
        or delta.get("paths_removed")
        or delta.get("values_changed")
    )
    return delta


def _apply_query(url: str, params: dict[str, str]) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({str(key): str(value) for key, value in params.items()})
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def _mutations(vary: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Turn the requested sweep into labelled single-change specs."""
    mutations: list[tuple[str, dict[str, Any]]] = []
    unsupported: list[str] = []
    for key in vary:
        if str(key) not in _MUTABLE_KEYS:
            unsupported.append(str(key))

    def pairs(name: str) -> list[dict[str, str]]:
        raw = vary.get(name) or []
        if isinstance(raw, dict):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        unsupported.append(f"{name} (needs a list of objects)")
        return []

    for item in pairs("headers"):
        for header, value in item.items():
            mutations.append((f"header {header}={value}", {"headers": {str(header): str(value)}}))
    for item in pairs("query"):
        for param, value in item.items():
            mutations.append((f"query {param}={value}", {"query": {str(param): str(value)}}))
    methods = vary.get("method") or []
    if isinstance(methods, str):
        methods = [methods]
    if isinstance(methods, list):
        for verb in methods:
            if isinstance(verb, str) and verb.strip():
                mutations.append((f"method {verb.strip().upper()}", {"method": verb.strip().upper()}))
    else:
        unsupported.append("method (needs a list of verbs)")

    bodies = vary.get("body") or []
    if isinstance(bodies, str):
        bodies = [bodies]
    if isinstance(bodies, list):
        for index, text in enumerate(item for item in bodies if isinstance(item, str)):
            mutations.append((f"body #{index + 1}", {"body": text}))
    else:
        unsupported.append("body (needs a list of strings)")
    return mutations, unsupported


def run_variants(
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    baseline_spec: dict[str, Any],
    vary: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    max_variants: int = MAX_VARIANTS,
) -> dict[str, Any]:
    """Run the baseline once, then one request per mutation, and report the deltas.

    ``execute`` is the caller's own way of sending a spec - this module never
    touches a browser, which is why the sweep can be tested without one.
    """
    if not isinstance(vary, dict) or not vary:
        return {"variants": [], "variant_note": "vary needs at least one mutation."}
    mutations, unsupported = _mutations(vary)
    baseline_response = baseline if isinstance(baseline, dict) else execute(baseline_spec)
    rows: list[dict[str, Any]] = []
    for label, change in mutations[:max_variants]:
        spec = dict(baseline_spec)
        if "headers" in change:
            merged = dict(spec.get("headers") or {})
            merged.update(change["headers"])
            spec["headers"] = merged
        if "query" in change:
            spec["url"] = _apply_query(str(spec.get("url") or ""), change["query"])
        if "method" in change:
            spec["method"] = change["method"]
        if "body" in change:
            spec["body"] = change["body"]
            # A body on a GET is a body most servers ignore, so a body mutation
            # is asked as a POST unless the baseline was already something else.
            if str(spec.get("method") or "GET").upper() == "GET":
                spec["method"] = "POST"
        response = execute(spec)
        row: dict[str, Any] = {"label": label, "status": response.get("status")}
        if not response.get("ok", True):
            row["error"] = response.get("error") or "the variant request failed"
        else:
            row.update(structural_diff(baseline_response, response))
        rows.append(row)

    answer: dict[str, Any] = {
        "baseline": {"status": baseline_response.get("status"), "bytes": _size(baseline_response)},
        "variants": rows,
        "variants_ran": len(rows),
    }
    if len(mutations) > max_variants:
        answer["variants_dropped"] = len(mutations) - max_variants
        answer["variants_limit"] = max_variants
    if unsupported:
        answer["vary_unsupported"] = unsupported
    answer["variant_note"] = (
        "Each variant is the baseline with exactly one thing changed, sent as its own request. "
        "Values are reported as shapes (string(24), null) and header names only: a delta names "
        "what appeared, it never quotes what a secret said."
    )
    return answer
