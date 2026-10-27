"""What a plain fetch could not see, said plainly, with the browser route named.

Three live sessions taught this file what it knows:

- Cyrillic queries come back as junk from most engines (Latin ones work through
  the brave/yahoo fallback), so the search side transliterates as a last try;
  operators should still feed it Latin or English first when they can.
- Direct fetches to google.com, duckduckgo.com and wikipedia.org are cut at the
  network level here (DNS), so their content is reached through a search engine.
- tips.gg answers a plain fetch with 403 (it wants browser-like headers),
  melbet.com answers 451, ligastavok.ru answers 401 to python fetches and works
  through driven Chrome, and stavka.tv / 1xstavka.ru / pin-up.com / tspula.ru
  are unreachable by plain fetch at all (JS-rendered or geo-blocked shells).

So the rule this module encodes: when a fetch lands on a gate status or reads
back a consent/challenge/JS shell, say so in one line and point at the Chrome
path that already works, instead of returning a title-only page as content.

The solver hook is OPT-IN and ships OFF: registering one changes nothing until
WEB_SEARCH_NEO_FETCH_SOLVER names it. A 2captcha-like service plugs in later as
a function ``solve(page_url, challenge) -> token | None``; this module only
carries the registry and the gate, no accounts and no call-out of its own.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Protocol

# Statuses that mean "you were answered, but not as a visitor yet".
GATE_STATUSES = frozenset({401, 403, 429, 451})
# The status that means "asked to come back", with its own clock.
RATE_STATUSES = frozenset({429})

SOLVER_ENV = "WEB_SEARCH_NEO_FETCH_SOLVER"

# Visible text under this line, together with a marker below, is what a shell
# looks like: a wall or a JS app with nothing rendered in it.
SHELL_TEXT_LIMIT = 200

# Substrings that name a consent wall or a bot challenge in the markup itself.
CHALLENGE_MARKERS = (
    "cf_chl", "challenges.cloudflare", "g-recaptcha", "recaptcha/api", "hcaptcha",
    "needtoverify", "just a moment", "enable javascript and cookies",
    "unusual traffic", "aptashield", "captchacode", "security check",
)
CONSENT_MARKERS = (
    "consentmanager", "cookieconsent", "cookie-consent", "onetrust", "cdn.cmp",
    "gdpr", "accept cookies", "accept all cookies", "cookie-policy-modal",
    "принять cookies", "принять cookies".upper(),
)
# Observed hosts whose plain-fetch answer this server already knows, from live
# sessions. The text is what the operator should do, not a promise about the site.
HOST_NOTES = {
    "tips.gg": "answers 403 to a plain fetch without browser-like headers; the builder covers this, otherwise use the browser route",
    "melbet.com": "answers 451 (legal restriction) to plain fetches; use the browser route",
    "ligastavok.ru": "answers 401 to python fetches but works through driven Chrome; use the browser route",
    "stavka.tv": "unreachable by plain fetch (JS shell); use the browser route",
    "1xstavka.ru": "unreachable by plain fetch (JS shell); use the browser route",
    "pin-up.com": "unreachable by plain fetch (JS shell); use the browser route",
    "tspula.ru": "unreachable by plain fetch (geo-blocked shell); use the browser route",
    "google.com": "DNS-blocked at the network level here; reach this content through a search engine",
    "duckduckgo.com": "DNS-blocked at the network level here; reach this content through a search engine",
    "wikipedia.org": "DNS-blocked at the network level here; reach this content through a search engine",
}


class SolverLike(Protocol):
    """A pluggable captcha solver: name it, and answer a challenge with a token."""

    name: str

    def solve(self, page_url: str, challenge: dict[str, Any]) -> str | None: ...


_solvers: dict[str, Callable[..., Any]] = {}


def register_solver(name: str, fn: Callable[..., Any]) -> None:
    """Add one solver under a name; it stays unused until WEB_SEARCH_NEO_FETCH_SOLVER names it."""
    _solvers[str(name).strip()] = fn


def registered_solvers() -> list[str]:
    """Names that could be picked (nothing is running them yet)."""
    return sorted(_solvers)


def configured_solver() -> Callable[..., Any] | None:
    """The registered solver WEB_SEARCH_NEO_FETCH_SOLVER names; None (OFF) by default."""
    pick = os.getenv(SOLVER_ENV, "").strip()
    return _solvers.get(pick) if pick else None


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(str(url or "")).hostname or "").lower()


def note_for_host(url: str) -> str | None:
    """What this server already knows about this host's plain-fetch answer."""
    host = host_of(url)
    if not host:
        return None
    for name, note in HOST_NOTES.items():
        if host == name or host.endswith("." + name):
            return f"{name}: {note}"
    return None


def classify_body(html: str, text: str, status: int | None = None) -> str | None:
    """What the page looks like when it only answers with a wall: kind or None.

    A marker alone is not enough - a real article can link a captcha script -
    so this fires only when the visible text is also under SHELL_TEXT_LIMIT,
    which is what an unanswered wall or an unrendered shell actually looks like.
    """
    lowered = str(html or "").lower()
    if not lowered or len(str(text or "").strip()) >= SHELL_TEXT_LIMIT:
        return None
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        return "challenge"
    if any(marker in lowered for marker in CONSENT_MARKERS):
        return "consent wall"
    if status in RATE_STATUSES:
        return "rate limited"
    if status in GATE_STATUSES:
        return "gate"
    return None


def browser_route(url: str, reason: str) -> str:
    """The one-line pointer appended when a fetch landed on a wall it cannot pass."""
    note = f" ({note_for_host(url)})" if note_for_host(url) else ""
    solver = configured_solver()
    hook = f" Registered solver '{solver}' is available for this." if solver else ""
    return (
        f"\n\n[fetch_hint] {reason}{note}{hook} - the static fetch cannot pass it; render it "
        f"in a browser session instead: web_action [{{\"action\":\"open\",\"url\":\"{url}\","
        "\"session_id\":\"<id>\"}}] then web_info(topic='page_text', params={'session_id':'<id>'})."
    )


def hint_for_status(status: int | None, url: str, headers: Any = None) -> str | None:
    """The plain hint for a gate answer, or None when the status needs none."""
    if status is None or status not in GATE_STATUSES:
        return None
    reason = {
        401: "the host answered 401 (it wants authenticated or browser-shaped requests)",
        403: "the host answered 403 (bot guard or header check)",
        429: "the host answered 429 (asked for a pause; Retry-After is honoured)",
        451: "the host answered 451 (served for legal reasons only)",
    }[status]
    return browser_route(url, reason)
