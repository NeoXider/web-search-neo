"""Content-Security-Policy: Mozilla HTTP Observatory's one verdict, plus advice.

The verdict and the policy merge reproduce the behaviour of Observatory v1.7.1's
CSP test and ``cspParser.js`` (MPL-2.0); the code here is written anew:

* policies: every header value and every ``<meta http-equiv>`` value, each split
  on commas; a policy that is empty or shorter than six characters (a trailing
  comma) makes the whole CSP invalid, and so does a directive repeated inside one
  policy - except ``report-uri`` / ``report-to``, whose repetition only turns a
  passing verdict into "duplicate directives" (0 points);
* sources are lowercased; a ``*-src`` directive with no sources means ``'none'``;
  several policies are merged so that, for a directive the later policy names,
  only sources both policies allow survive (nothing surviving means ``'none'``);
* a nonce or hash drops ``'unsafe-inline'`` from script-src and style-src; with a
  nonce or hash, ``'strict-dynamic'`` drops every scheme or wildcard source,
  ``'self'`` and ``'unsafe-inline'`` - without one it is invalid (-25);
* first match wins: no policy -25 (Report-Only alone -25); ``'unsafe-inline'``,
  ``data:`` or a broad source (``*``, ``http:``, ``https:``, ``ftp:``, ``http(s)://*``,
  ``http(s)://*.*``) in script-src, or a broad source in object-src (object-src
  falls back to default-src, then ``*``) -20; on an https page an ``http:``/``ftp:``
  source in any directive but img-src/media-src -20; ``'unsafe-eval'`` in script or
  style sources -10; ``http:``/``ftp:`` images or media on https -10; unsafe style
  sources only 0; ``default-src 'none'`` +10; otherwise +5.

Advice about object-src, base-uri, header ``frame-ancestors`` and form-action
carries 0 points.
"""
from __future__ import annotations

import re
from typing import Any

from web_search_neo.audit.findings import finding, info, passed

CATEGORY = "csp"
DANGEROUSLY_BROAD = frozenset({"ftp:", "http:", "https:", "*", "http://*", "http://*.*", "https://*", "https://*.*"})
UNSAFE_INLINE = frozenset({"'unsafe-inline'", "data:"})
BROAD_OR_INLINE = DANGEROUSLY_BROAD | UNSAFE_INLINE
META_IGNORED = ("frame-ancestors", "report-uri", "sandbox")
REPEATABLE = frozenset({"report-uri", "report-to"})
PASSIVE = ("img-src", "media-src")
HASHES = ("'sha256-", "'sha384-", "'sha512-")
NONCE_OR_HASH = HASHES + ("'nonce-",)
MIN_POLICY_CHARS = 6  # len("img-src") - 1: the shortest policy Observatory accepts
DUPLICATES = "__duplicates__"


class InvalidPolicy(ValueError):
    """The CSP cannot be parsed the way Observatory parses it (-25)."""


def split_policies(values: list[str]) -> list[str]:
    """Every header or meta value, split on commas into single policies."""
    return [part.strip() for value in values for part in str(value).split(",")]


_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
# The order in which an English-locale collator (what JavaScript's Intl.Collator
# and so Observatory use) ranks printable ASCII: punctuation before digits before
# letters, not code-point order. Sources are lowercased before they are sorted.
_ASCII_ORDER = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789abcdefghijklmnopqrstuvwxyz"
_RANK = {char: rank for rank, char in enumerate(_ASCII_ORDER)}


def _collation_key(text: str) -> tuple[int, ...]:
    """Sort key giving ASCII sources the collator's order (other characters after, by code point)."""
    return tuple(_RANK.get(char, len(_RANK) + ord(char)) for char in text)


def _decoded(segment: str) -> str:
    """A URL path segment with its %XX escapes resolved; a broken escape is an invalid policy.

    A browser (and Observatory, which decodes with ``decodeURIComponent``) cannot
    read a source such as ``https://a.test/%zz``, so neither can this parser.
    """
    if "%" not in segment:
        return segment
    if segment.count("%") != len(_PERCENT.findall(segment)):
        raise InvalidPolicy(f"source path {segment!r} has a broken %-escape")
    raw = bytearray()
    for piece in re.split(r"(%[0-9A-Fa-f]{2})", segment):
        raw += bytes([int(piece[1:], 16)]) if _PERCENT.fullmatch(piece) else piece.encode("utf-8")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidPolicy(f"source path {segment!r} is not UTF-8 once decoded") from None


def _covers(outer: str, inner: str) -> bool:
    """Whether source ``outer`` also allows ``inner``, compared segment by segment of the "/"-split text.

    An ``outer`` ending in "/" is a directory and covers anything below it; any
    other ``outer`` must have exactly as many segments as ``inner``.
    """
    if outer == "" or (outer == "/" and inner == ""):
        return True
    wanted, given = outer.split("/"), inner.split("/")
    if outer.endswith("/"):
        wanted = wanted[:-1]
        if len(wanted) + 1 > len(given):
            return False
    elif len(wanted) != len(given):
        return False
    return all(_decoded(a) == _decoded(b) for a, b in zip(wanted, given))


class _Merged:
    """The sources of one directive while the policies are read one after another.

    ``items`` holds (source, policy number) pairs in the order the next merge
    sorts them; ``live`` marks which of them still count. The first policy fills
    it; each later policy that names the directive adds its own sources, and a
    source survives only where a neighbour of the other policy covers it.
    """

    def __init__(self, sources: list[str], policy: int) -> None:
        self.items = [(source, policy) for source in sorted(sources, key=_collation_key)]
        # Within one policy a source is dropped when the one sorted just before it
        # is its prefix (https://a.test/x after https://a.test adds nothing).
        self.live = [not (n and self.items[n][0].startswith(self.items[n - 1][0])) for n in range(len(self.items))]

    def keep_live(self) -> None:
        self.items = [item for item, alive in zip(self.items, self.live) if alive]
        self.live = [True] * len(self.items)

    def forget_confirmation(self) -> None:
        """Before a second policy is read, every source of the first has to be confirmed again."""
        self.live = [False] * len(self.items)

    def meet(self, sources: list[str], policy: int) -> None:
        """Fold a later policy's sources in: only what both sides allow stays live."""
        pool = sorted([(item, alive) for item, alive in zip(self.items, self.live)]
                      + [((source, policy), False) for source in sources],
                      key=lambda entry: _collation_key(entry[0][0]))
        items = [item for item, _alive in pool]
        live = [alive for _item, alive in pool]
        for n in range(1, len(items)):
            (before, before_policy), (source, source_policy) = items[n - 1], items[n]
            if before_policy == source_policy:
                if source.startswith(before):
                    live[n] = False
            elif _covers(before, source):
                live[n] = True
        self.items, self.live = items, live
        self.keep_live()

    def sources(self) -> set[str]:
        return {source for source, _policy in self.items} or {"'none'"}


def _directives(policy: str) -> list[tuple[str, list[str]]]:
    """(directive, sources) pairs of one policy in the order written; names and sources lowercased."""
    out = []
    for chunk in policy.split(";"):
        words = chunk.split()
        if words:
            name = words[0].lower()
            sources = [word.lower() for word in words[1:]]
            if not sources and name.endswith("-src"):
                sources = ["'none'"]  # a source list left empty allows nothing
            out.append((name, sources))
    return out


def parse(policies: list[str], *, meta: bool = False) -> dict[str, set[str]]:
    """Directive -> sources for the merged policies; raises ``InvalidPolicy``.

    Every policy must be at least six characters long and name each directive
    once (report-uri and report-to may repeat; the repetition is recorded under
    ``DUPLICATES``). A directive named in several policies keeps only the
    sources every policy allows; ``meta=True`` drops what a ``<meta>`` policy
    cannot set.
    """
    texts = [p.replace("\r", "").replace("\n", "").strip() for p in policies]
    if not texts:
        return {}
    short = [text for text in texts if len(text) < MIN_POLICY_CHARS]
    if short:
        raise InvalidPolicy(f"invalid policy {short[0]!r} (empty, or a trailing comma)")
    merged: dict[str, _Merged] = {}
    repeated: set[str] = set()
    for number, text in enumerate(texts):
        named: set[str] = set()
        for name, sources in _directives(text):
            if name in named and name not in REPEATABLE:
                raise InvalidPolicy(f"directive {name} repeats in policy {number + 1}")
            if name in named:
                repeated.add(name)
            named.add(name)
            if number == 0:
                merged[name] = _Merged(sources, 0)  # a repeated reporting directive: the last one stands
                merged[name].keep_live()
            else:
                if name not in merged:
                    merged[name] = _Merged([], number)  # first seen in a later policy: nothing confirms it
                merged[name].meet(sources, number)
        if number == 0 and len(texts) > 1:
            for state in merged.values():
                state.forget_confirmation()
    result = {name: state.sources() for name, state in merged.items()}
    if meta:
        for name in META_IGNORED:
            result.pop(name, None)
    if repeated:
        result[DUPLICATES] = repeated
    return result


def _nonce_or_hash(sources: set[str]) -> bool:
    return any(s.startswith(NONCE_OR_HASH) for s in sources)


def verdict(parsed: dict[str, set[str]], https: bool) -> tuple[str, dict[str, Any]]:
    """Observatory's CSP result for a parsed, non-empty CSP, plus what it computed on the way.

    script-src, style-src and object-src fall back to the very same source set
    of default-src, and the nonce and 'strict-dynamic' rules edit that set in
    place: with ``default-src 'nonce-a' 'strict-dynamic' https:`` the removal of
    ``https:`` is seen by style-src and by the active/passive source lists too.
    The edits work on a copy, never on the caller's policy.
    """
    csp = {directive: set(sources) for directive, sources in parsed.items()}
    fallback = csp.get("default-src")
    script = csp.get("script-src") or fallback or {"*"}
    style = csp.get("style-src") or fallback or {"*"}
    objects = csp.get("object-src") or fallback or {"*"}
    for sources in (script, style):
        if _nonce_or_hash(sources):
            sources.discard("'unsafe-inline'")
    result = None
    if _nonce_or_hash(script) and "'strict-dynamic'" in script:
        for source in [s for s in script if any(s.startswith(b) for b in DANGEROUSLY_BROAD)
                       or s in {"'self'", "'unsafe-inline'"}]:
            script.discard(source)
    elif "'strict-dynamic'" in script:
        result = "csp-invalid"
    active = [s for d, sources in csp.items() if d not in PASSIVE and d != "script-src" for s in sources] + list(script)
    passive = [s for d in PASSIVE for s in (csp.get(d) or fallback or set())]
    insecure = ("http:", "ftp:")
    checks = (
        ("csp-unsafe-scripts", bool(script & BROAD_OR_INLINE or objects & DANGEROUSLY_BROAD)),
        ("csp-insecure-scheme", https and any(s.startswith(insecure) for s in active)),
        ("csp-unsafe-eval", "'unsafe-eval'" in script | style),
        ("csp-insecure-passive", https and any(s.startswith(insecure) for s in passive)),
        ("csp-unsafe-style-only", bool(style & BROAD_OR_INLINE)),
        ("csp-default-none", csp.get("default-src") == {"'none'"}),
        ("csp-no-unsafe", True),
    )
    for fid, applies in checks:
        if result is None and applies:
            result = fid
    if result in {"csp-no-unsafe", "csp-default-none", "csp-unsafe-style-only", "csp-insecure-passive"} \
            and DUPLICATES in csp:
        result = "csp-duplicate-report"
    return result, {"effective_script_src": sorted(script)}


POINTS = {"csp-missing": -25, "csp-report-only-only": -25, "csp-invalid": -25, "csp-unsafe-scripts": -20,
          "csp-insecure-scheme": -20, "csp-unsafe-eval": -10, "csp-insecure-passive": -10,
          "csp-unsafe-style-only": 0, "csp-duplicate-report": 0, "csp-default-none": 10, "csp-no-unsafe": 5}

_TEXT = {
    "csp-invalid": ("fail", "high", "The CSP is invalid",
                    "Fix the policy: no empty policy (trailing comma), no repeated directive, and a nonce or "
                    "hash next to 'strict-dynamic'."),
    "csp-unsafe-scripts": ("fail", "high", "CSP does not stop injected scripts",
                           "Remove 'unsafe-inline', data:, '*' and scheme-only sources from script-src, set "
                           "object-src 'none', and allow inline code with a per-response 'nonce-...' (plus "
                           "'strict-dynamic') or a 'sha256-...' hash."),
    "csp-insecure-scheme": ("fail", "high", "CSP allows active content over http",
                            "Replace http: and ftp: sources with https: ones."),
    "csp-unsafe-eval": ("fail", "medium", "CSP allows 'unsafe-eval'",
                        "Remove 'unsafe-eval'; replace eval/new Function/string timers in your code and libraries."),
    "csp-insecure-passive": ("fail", "medium", "CSP allows images or media over http",
                             "Serve images and media over https and drop http: from img-src/media-src."),
    "csp-unsafe-style-only": ("warn", "low", "CSP is safe for scripts; style sources stay unsafe",
                              "Move inline styles to files or nonce them to drop 'unsafe-inline' from style-src."),
    "csp-duplicate-report": ("warn", "low", "report-uri or report-to repeats in one policy",
                             "List each reporting directive once (the bonus returns)."),
}


def _missing(report_only: str | None) -> dict[str, Any]:
    fid = "csp-report-only-only" if report_only else "csp-missing"
    return finding(
        fid, CATEGORY, "fail", "high",
        "Only Content-Security-Policy-Report-Only is set" if report_only else "No Content-Security-Policy",
        detail="Without an enforced CSP any injected script runs with the page's full rights"
               + (" (Report-Only reports violations and blocks nothing)" if report_only else "") + ".",
        fix="Send Content-Security-Policy, for example: default-src 'self'; script-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'. "
            "Start with Content-Security-Policy-Report-Only to find what breaks.",
        points=-25)


def summarize(csp: dict[str, set[str]], header_csp: dict[str, set[str]], flags: dict[str, Any]) -> dict[str, Any]:
    """What the policy means in the browser, for the page checks (inline code, framing)."""
    script = set(flags.get("effective_script_src") or [])
    attr = csp.get("script-src-attr") or csp.get("script-src") or csp.get("default-src")
    handlers_allowed = attr is None or ("'unsafe-inline'" in attr and not _nonce_or_hash(set(attr))) \
        or "'unsafe-hashes'" in attr
    listed = csp.get("script-src") or csp.get("default-src") or set()
    return {
        "inline_scripts_blocked": bool(csp) and "'unsafe-inline'" not in script and "*" not in script,
        "inline_handlers_blocked": bool(csp) and not handlers_allowed,
        "uses_nonce": any(s.startswith("'nonce-") for s in listed),
        "uses_hashes": any(s.startswith(HASHES) for s in listed),
        # The browser honours frame-ancestors only from the header.
        "frame_ancestors": sorted(header_csp["frame-ancestors"]) if "frame-ancestors" in header_csp else None,
        # Observatory credits X-Frame-Options for frame-ancestors anywhere in the merged CSP.
        "frame_ancestors_any": "frame-ancestors" in csp,
    }


def _advice(csp: dict[str, set[str]], header_csp: dict[str, set[str]], meta_only: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if meta_only:
        out.append(info("csp-meta-only", CATEGORY, "CSP is set only in <meta>",
                        detail="A <meta> policy applies only after the parser reaches it; frame-ancestors, "
                               "report-uri and sandbox are ignored there.",
                        fix="Send the policy as a Content-Security-Policy response header."))
    if (csp.get("object-src") or csp.get("default-src")) != {"'none'"}:
        out.append(finding("csp-object-src", CATEGORY, "warn", "low", "object-src is not 'none'",
                           detail="<object>/<embed> plugins can load active content.", fix="Add object-src 'none'."))
    if "base-uri" not in csp:
        out.append(finding("csp-base-uri", CATEGORY, "warn", "low", "No base-uri directive",
                           detail="An injected <base href> can redirect every relative script URL.",
                           fix="Add base-uri 'self' (or 'none')."))
    if "frame-ancestors" not in header_csp:
        in_meta = "frame-ancestors" in csp
        out.append(finding(
            "csp-frame-ancestors-missing", CATEGORY, "warn", "low", "No frame-ancestors in the CSP header",
            detail="frame-ancestors is honoured only from the header"
                   + ("; browsers ignore the one in <meta> (Observatory still credits it)" if in_meta else "") + ".",
            fix="Add frame-ancestors 'self' (or 'none') to the header policy."))
    if "form-action" not in csp:
        out.append(info("csp-form-action", CATEGORY, "No form-action directive",
                        fix="Add form-action 'self' so injected forms cannot post elsewhere."))
    if "upgrade-insecure-requests" in csp:
        out.append(passed("csp-upgrade-insecure", CATEGORY, "upgrade-insecure-requests is set"))
    return out


def _empty_summary(header_policies: list[str], meta_policies: list[str]) -> dict[str, Any]:
    return {"inline_scripts_blocked": False, "inline_handlers_blocked": False, "uses_nonce": False,
            "uses_hashes": False, "frame_ancestors": None, "frame_ancestors_any": False, "directives": {},
            "strict_scripts": False,
            "source": ("header" if header_policies else "")
                      + ("+meta" if header_policies and meta_policies else "meta" if meta_policies else "")}


def analyze(header: str | None, report_only: str | None = None, meta: Any = None,
            *, https: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Findings for the page's CSP plus the summary the page and header checks use."""
    header_policies = split_policies([header]) if isinstance(header, str) else []  # an empty header is invalid
    meta_values = [m for m in meta if isinstance(m, str)] if isinstance(meta, list) else []
    meta_policies = split_policies(meta_values)
    summary = _empty_summary(header_policies, meta_policies)
    findings: list[dict[str, Any]] = []
    if report_only:
        findings.append(info("csp-report-only", CATEGORY, "Content-Security-Policy-Report-Only is set",
                             detail="Report-Only policies only report violations; they block nothing.",
                             evidence=str(report_only)[:500]))
    try:
        csp = parse(header_policies + meta_policies)
    except InvalidPolicy as exc:
        summary["verdict"] = "csp-invalid"
        findings.append(finding("csp-invalid", CATEGORY, "fail", "high", "The CSP is invalid", detail=str(exc),
                                fix=_TEXT["csp-invalid"][3], points=-25))
        return findings, summary
    try:
        header_csp = parse(header_policies)
    except InvalidPolicy:
        header_csp = {}
    try:
        meta_csp = parse(meta_policies, meta=True)
    except InvalidPolicy:
        meta_csp = {}
    # Observatory counts the keys of both maps, its duplicate-warning key included.
    if not header_csp and not meta_csp:
        summary["verdict"] = "csp-report-only-only" if report_only else "csp-missing"
        findings.append(_missing(report_only))
        return findings, summary
    fid, flags = verdict(csp, https)
    summary.update(summarize(csp, header_csp, flags))
    summary.update(verdict=fid, strict_scripts=fid not in {"csp-unsafe-scripts", "csp-invalid"},
                   directives={d: " ".join(sorted(v)) for d, v in csp.items() if d != DUPLICATES})
    evidence = summary["directives"]
    if fid in _TEXT:
        status, severity, title, fix = _TEXT[fid]
        findings.append(finding(fid, CATEGORY, status, severity, title, fix=fix, evidence=evidence, points=POINTS[fid]))
    else:
        title = "CSP uses default-src 'none'" if fid == "csp-default-none" else "CSP has no unsafe script or object sources"
        findings.append(passed(fid, CATEGORY, title, evidence=evidence, points=POINTS[fid]))
    findings += _advice(csp, header_csp, bool(meta_policies and not header_policies))
    return findings, summary
