"""The init script behind the opt-in ``stealth`` tool.

It hides the usual automation tells before a page's scripts can read them. The
languages it reports follow the session's locale override, because a page that
sees ``navigator.language`` say one thing and ``navigator.languages`` another
has found a tell the override itself created.
"""
from __future__ import annotations

import json

_STEALTH_BASE = """
Object.defineProperty(navigator, 'webdriver', {get: () => false, configurable: true});
if (!window.chrome) { window.chrome = {runtime: {}}; }
try {
  const original = navigator.permissions && navigator.permissions.query;
  if (original) {
    navigator.permissions.query = (parameters) =>
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : original(parameters);
  }
} catch (error) { /* a locked-down permissions API is not worth failing over */ }
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5], configurable: true});
"""


def stealth_languages(locale: str | None) -> list[str]:
    """``['de-DE', 'de']`` for ``de-DE``; ``['en-US', 'en']`` when no locale is set."""
    tag = str(locale or "").strip().replace("_", "-") or "en-US"
    base = tag.split("-", 1)[0]
    return [tag] if base == tag else [tag, base]


def stealth_source(locale: str | None = None) -> str:
    languages = json.dumps(stealth_languages(locale))
    return _STEALTH_BASE + (
        f"Object.defineProperty(navigator, 'languages', {{get: () => {languages}, configurable: true}});\n"
    )


STEALTH_SOURCE = stealth_source()
