"""Where a site check may send requests: the hosts the caller named, nothing else.

``Scope`` is validated before anything is sent. An entry is an exact host
(``example.com``, ``api.example.com``, ``localhost``, ``127.0.0.1``), optionally
with a port (``localhost:8080``) and a scheme (``http://localhost:8080``); a
host without a port covers its default ports (80, 443) only.
Wildcards, public suffixes (``com``, ``co.uk``, ``github.io``), paths, userinfo
and more than ``MAX_HOSTS`` entries are refused. ``include_subdomains`` extends
each named domain to its subdomains (``cdn.example.com`` under ``example.com``).

``Budget`` counts every request a check sends and refuses the one past the
limit, so a crawl or a list of hosts can never turn into a sweep.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from web_search_neo.actions.cookie_scope import is_public_suffix
from web_search_neo.fetch.safety import redact_url

MAX_HOSTS = 10
MAX_PAGES = 50
DEFAULT_PAGES = 10
MAX_REQUESTS = 200
_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


class ScopeError(ValueError):
    """A scope entry that must not be checked; raised before any request."""


@dataclass(frozen=True)
class Target:
    """One named origin: scheme (None = https, then what the host answers), host, port."""

    host: str
    port: int | None = None
    scheme: str | None = None

    def root(self, default_scheme: str = "https") -> str:
        scheme = self.scheme or default_scheme
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}" + (f":{self.port}" if self.port else "") + "/"

    def label(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host  # an IPv6 literal needs its brackets back
        return (f"{self.scheme}://" if self.scheme else "") + host + (f":{self.port}" if self.port else "")


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host.split(".")
    return bool(host) and len(host) <= 253 and all(_LABEL.fullmatch(label) for label in labels)


def parse_target(entry: Any) -> Target:
    """One scope entry, or ``ScopeError`` naming what is wrong with it."""
    if not isinstance(entry, str) or not entry.strip():
        raise ScopeError("a scope host must be a non-empty string such as 'example.com' or 'localhost:8080'")
    text = entry.strip()
    if "*" in text:
        raise ScopeError(f"{text!r}: wildcards are not allowed; name exact hosts (include_subdomains covers subdomains)")
    has_scheme = "://" in text
    parts = urlsplit(text if has_scheme else "//" + text)
    if has_scheme and parts.scheme not in {"http", "https"}:
        raise ScopeError(f"{text!r}: only http:// and https:// are allowed")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ScopeError(f"{text!r}: give a host (optionally scheme and port), not a URL with a path or credentials")
    try:
        host, port = (parts.hostname or "").rstrip(".").lower(), parts.port
    except ValueError as exc:
        raise ScopeError(f"{text!r}: {exc}") from None
    if not _valid_host(host):
        raise ScopeError(f"{text!r}: not a valid host name or IP address")
    if "." in host and not _is_ip(host) and is_public_suffix(host):
        raise ScopeError(f"{text!r}: a public suffix is a registry, not a site; name your own domain")
    if "." not in host and host != "localhost" and not _is_ip(host):
        raise ScopeError(f"{text!r}: a bare name is not a site; use localhost, an IP or a full domain")
    return Target(host=host, port=port, scheme=parts.scheme if has_scheme else None)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@dataclass
class Scope:
    """The origins a check may contact."""

    targets: tuple[Target, ...]
    include_subdomains: bool = False

    @classmethod
    def build(cls, entries: list[Any], *, include_subdomains: bool = False) -> "Scope":
        if not isinstance(entries, list) or not entries:
            raise ScopeError("scope needs at least one host")
        if len(entries) > MAX_HOSTS:
            raise ScopeError(f"at most {MAX_HOSTS} hosts per check ({len(entries)} given)")
        targets: list[Target] = []
        for entry in entries:
            target = parse_target(entry)
            if target not in targets:
                targets.append(target)
        return cls(tuple(targets), bool(include_subdomains))

    def allows(self, url: str) -> bool:
        """``url`` is on a named host (or a subdomain of one, when allowed) and a matching port."""
        try:
            parts = urlsplit(str(url))
            host, port = (parts.hostname or "").rstrip(".").lower(), parts.port
        except ValueError:
            return False
        if parts.scheme not in {"http", "https"} or not host:
            return False
        for target in self.targets:
            same = host == target.host or (self.include_subdomains and not _is_ip(host)
                                           and host.endswith("." + target.host))
            # A host named without a port covers the default ports only: a link to
            # another port of the same machine is another service, not in scope.
            # A URL without a port means its scheme's port (https:// is 443).
            effective = port if port is not None else (443 if parts.scheme == "https" else 80)
            if same and (effective in (80, 443) if target.port is None else effective == target.port):
                return True
        return False

    def describe(self) -> dict[str, Any]:
        return {"hosts": [t.label() for t in self.targets], "include_subdomains": self.include_subdomains}


@dataclass
class Budget:
    """Every request a check sends, redacted, and the ceiling it may not pass."""

    limit: int = MAX_REQUESTS
    made: list[str] = field(default_factory=list)
    refused: int = 0

    def take(self, label: str) -> bool:
        if len(self.made) >= self.limit:
            self.refused += 1
            return False
        self.made.append(label)
        return True

    def record(self, label: str) -> None:
        """A hop the transport already sent (a followed redirect): always recorded."""
        self.made.append(label)


def redacted(url: str) -> str:
    return redact_url(url)


# A URL inside free text (an exception message, a Location value), or the
# path?query part requests puts into "Max retries exceeded with url: /x?token=...".
_URL_IN_TEXT = re.compile(r"""(?:https?://(?:[^\s'"<>()\[\]{}@/]*@)?(?:\[[0-9A-Za-z:.%_-]+\])?[^\s'"<>()\[\]{}]*"""
                          r"""|(?<=url: )/[^\s'"<>()]*)""", re.IGNORECASE)


def redact_text(text: str) -> str:
    """``text`` with every URL in it redacted: no userinfo, sensitive query values masked."""
    def one(match: re.Match[str]) -> str:
        found = match.group(0)
        tail = found[len(found.rstrip(".,;:!")):]
        found = found[:len(found) - len(tail)]
        if found.startswith("/"):
            return redact_url("http://x" + found)[len("http://x"):] + tail
        return redact_url(found) + tail
    return _URL_IN_TEXT.sub(one, str(text))


def scrub(value: Any) -> Any:
    """A deep copy of a report with every string passed through ``redact_text``.

    The last line of defence: whatever a finding quotes (a redirect route, an
    error message, a Location header) never carries a password or a token out.
    """
    if isinstance(value, str):
        return redact_text(value) if ("://" in value or "url: /" in value) else value
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value
