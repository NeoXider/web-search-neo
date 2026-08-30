"""Extensible, API-key-free metasearch with caching and provider cooldowns."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import math
import os
import re
import threading
import time
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from ddgs import DDGS

from web_search_neo.web_client import request


DEFAULT_ENGINE = "duckduckgo"
STATUS_CACHE_TTL_SECONDS = 300
SEARCH_CACHE_TTL_SECONDS = 120
PROVIDER_COOLDOWN_SECONDS = 180
CHALLENGE_COOLDOWN_SECONDS = 600
MIN_PROVIDER_INTERVAL_SECONDS = 0.75
MAX_PROVIDER_ATTEMPT_SECONDS = 4.0

SearchResult = dict[str, str]


class SearchProviderError(RuntimeError):
    """Provider failure with a machine-readable category."""

    def __init__(self, message: str, kind: str = "provider_error") -> None:
        super().__init__(message)
        self.kind = kind


# "Nothing matched" is an answer, not a failure: ddgs reports it by raising
# DDGSException("No results found.") when no backend produced a row and no
# backend reported an error either.
_EMPTY_ANSWER_RE = re.compile(r"^\W*no results(?: found)?\W*$", re.IGNORECASE)

# A status code only counts when it is written as one. Matching the bare digits
# would turn every message that happens to contain the current year, a byte
# count or a URL path segment into a rate-limit report.
_STATUS_CODE_RE = re.compile(
    r"\b(?:https?|http/\d(?:\.\d)?|status(?:\s*code)?|code|error)(?:\s*[:=]\s*|\s+)(\d{3})\b",
    re.IGNORECASE,
)
_CHALLENGE_STATUS_CODES = frozenset({"202", "401", "403", "429"})

_TIMEOUT_TYPE_NAMES = frozenset(
    {"TimeoutError", "TimeoutException", "Timeout", "ReadTimeout", "ConnectTimeout"}
)
_CHALLENGE_TYPE_NAMES = frozenset({"RatelimitException", "ConversationLimitException"})
_TRANSPORT_TYPE_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "ConnectionAbortedError",
        "ProxyError",
        "SSLError",
        "OSError",
        "socket.error",
    }
)

_TIMEOUT_RE = re.compile(r"\b(?:timeout|timeouts|timed[\s-]?out|deadline exceeded)\b", re.I)
_TRANSPORT_RE = re.compile(
    r"\b(?:connection (?:reset|refused|aborted|closed|error)|reset by peer|broken pipe"
    r"|connectionerror|connection_error|getaddrinfo|name or service not known"
    r"|nodename nor servname|temporary failure in name resolution|dns"
    r"|ssl|tls|certificate|proxy error|network is unreachable|no route to host"
    r"|remote end closed|incomplete read|unexpected end of stream)\b",
    re.I,
)
_CHALLENGE_RE = re.compile(
    r"\b(?:captcha|ratelimit|rate[\s-]?limit(?:ed|ing)?|too many requests|unusual traffic"
    r"|verify (?:you|that you|your)|are you (?:a )?human|human verification"
    r"|access denied|forbidden|blocked|challenge)\b",
    re.I,
)


def classify_provider_error(exc: Exception) -> str:
    """Categorise a provider failure as empty/timeout/challenge/provider_error.

    Checks run type-first and transport-before-challenge so that a reset
    connection or a read timeout can never be reported as a CAPTCHA the caller
    is then told to go and solve in a browser.
    """
    if isinstance(exc, SearchProviderError):
        # "empty_results" is the legacy spelling third-party providers may still
        # raise; it means the same "answered with nothing" as "empty".
        return "empty" if exc.kind in {"empty", "empty_results"} else exc.kind
    message = str(exc)
    type_names = {klass.__name__ for klass in type(exc).__mro__}
    if _EMPTY_ANSWER_RE.match(message.strip()):
        return "empty"
    if type_names & _TIMEOUT_TYPE_NAMES or _TIMEOUT_RE.search(message):
        return "timeout"
    if type_names & _CHALLENGE_TYPE_NAMES:
        return "challenge"
    if type_names & _TRANSPORT_TYPE_NAMES or _TRANSPORT_RE.search(message):
        return "provider_error"
    if _CHALLENGE_RE.search(message):
        return "challenge"
    if any(
        code in _CHALLENGE_STATUS_CODES for code in _STATUS_CODE_RE.findall(message)
    ):
        return "challenge"
    return "provider_error"


class SearchProvider(ABC):
    """Implement this small interface and register the instance to add an engine."""

    name: str

    @abstractmethod
    def search(
        self, query: str, num: int, timeout_seconds: float
    ) -> list[SearchResult]:
        """Return normalized title/url/snippet results or raise on failure."""

    @abstractmethod
    def browser_url(self, query: str) -> str:
        """Return a human-openable results URL for challenge handoff."""


@dataclass(frozen=True)
class FunctionSearchProvider(SearchProvider):
    """Convenient adapter for project-local or test provider functions."""

    name: str
    implementation: object
    url_template: str = "https://duckduckgo.com/?q={query}"

    def search(
        self, query: str, num: int, timeout_seconds: float
    ) -> list[SearchResult]:
        return self.implementation(query, num, timeout_seconds)  # type: ignore[operator]

    def browser_url(self, query: str) -> str:
        return self.url_template.format(query=quote_plus(query))


@dataclass(frozen=True)
class DdgsSearchProvider(SearchProvider):
    """Adapter over the maintained DDGS metasearch transport."""

    name: str
    backend: str
    url_template: str

    def search(
        self, query: str, num: int, timeout_seconds: float
    ) -> list[SearchResult]:
        proxy = os.getenv("WEB_SEARCH_NEO_PROXY") or None
        region = os.getenv("WEB_SEARCH_NEO_REGION", "us-en")
        try:
            raw = DDGS(
                proxy=proxy,
                timeout=max(1, int(math.ceil(timeout_seconds))),
            ).text(
                query,
                region=region,
                safesearch="moderate",
                max_results=num,
                backend=self.backend,
            )
        except Exception as exc:
            kind = classify_provider_error(exc)
            if kind == "empty":
                # The backend answered: this query simply has no hits.
                return []
            raise SearchProviderError(str(exc) or type(exc).__name__, kind) from exc

        results: list[SearchResult] = []
        for item in raw or []:
            url = str(item.get("href") or item.get("url") or "").strip()
            title = " ".join(str(item.get("title") or "").split())
            if not url or not title:
                continue
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": " ".join(
                        str(item.get("body") or item.get("snippet") or "").split()
                    ),
                }
            )
            if len(results) >= num:
                break
        # An empty list is an answer ("no hits"), never a provider failure.
        return results

    def browser_url(self, query: str) -> str:
        return self.url_template.format(query=quote_plus(query))


@dataclass(frozen=True)
class BingHtmlSearchProvider(SearchProvider):
    """Use Bing's public HTML route without pretending another backend is Bing."""

    name: str = "bing"

    def search(
        self, query: str, num: int, timeout_seconds: float
    ) -> list[SearchResult]:
        response = request(
            "https://www.bing.com/search",
            timeout_seconds=timeout_seconds,
            max_response_bytes=2_000_000,
            params={"q": query, "count": min(max(num, 1), 20)},
        )
        body = response.text
        lower = body.lower()
        if any(
            marker in lower
            for marker in (
                "b_captcha",
                "captcha challenge",
                "please solve the challenge below",
                "verify you are human",
                "unusual traffic",
            )
        ):
            raise SearchProviderError("Bing returned a human verification challenge", "challenge")
        soup = BeautifulSoup(body, "html.parser")
        results: list[SearchResult] = []
        for item in soup.select("li.b_algo"):
            anchor = item.select_one("h2 a[href]")
            if anchor is None:
                continue
            url = str(anchor.get("href") or "").strip()
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if not url.startswith(("http://", "https://")) or not title:
                continue
            snippet_node = item.select_one(".b_caption p") or item.select_one("p")
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": " ".join(
                        snippet_node.get_text(" ", strip=True).split()
                        if snippet_node is not None
                        else ""
                    ),
                }
            )
            if len(results) >= num:
                break
        # Bing answered without a challenge page; zero rows means zero hits.
        return results

    def browser_url(self, query: str) -> str:
        return f"https://www.bing.com/search?q={quote_plus(query)}"


SEARCH_PROVIDERS: dict[str, SearchProvider] = {}
ENGINE_ORDER: list[str] = []
_provider_locks: dict[str, threading.Lock] = {}
_provider_last_request: dict[str, float] = {}
_provider_state: dict[str, dict] = {}
_runtime_lock = threading.RLock()
_status_cache: tuple[float, dict] | None = None
_search_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


def register_search_provider(provider: SearchProvider) -> None:
    """Register or replace a provider; all status/fallback logic updates automatically."""
    global _status_cache
    if not provider.name or not provider.name.replace("_", "").isalnum():
        raise ValueError("Provider name must contain only letters, digits, and underscores")
    with _runtime_lock:
        if provider.name not in SEARCH_PROVIDERS:
            ENGINE_ORDER.append(provider.name)
        SEARCH_PROVIDERS[provider.name] = provider
        _provider_locks.setdefault(provider.name, threading.Lock())
        _provider_state.setdefault(provider.name, {})
        _status_cache = None


for _provider in (
    DdgsSearchProvider("duckduckgo", "duckduckgo", "https://duckduckgo.com/?q={query}"),
    DdgsSearchProvider("brave", "brave", "https://search.brave.com/search?q={query}"),
    DdgsSearchProvider("mojeek", "mojeek", "https://www.mojeek.com/search?q={query}"),
    DdgsSearchProvider("yahoo", "yahoo", "https://search.yahoo.com/search?p={query}"),
    BingHtmlSearchProvider(),
    DdgsSearchProvider("startpage", "startpage", "https://www.startpage.com/do/search?q={query}"),
):
    register_search_provider(_provider)


def _error_kind(exc: Exception) -> str:
    """Return the category of a raised provider failure.

    ``empty`` means the provider answered and the answer was "no hits"; it is
    the one category that must never be treated as a failure.
    """
    return classify_provider_error(exc)


def _set_provider_result(name: str, error: Exception | None) -> None:
    now = time.monotonic()
    with _runtime_lock:
        state = _provider_state.setdefault(name, {})
        if error is None:
            state.update(last_success=now, last_error=None, error_kind=None, cooldown_until=0.0)
            return
        kind = _error_kind(error)
        cooldown = CHALLENGE_COOLDOWN_SECONDS if kind == "challenge" else PROVIDER_COOLDOWN_SECONDS
        state.update(
            last_error=f"{type(error).__name__}: {error}",
            error_kind=kind,
            cooldown_until=now + cooldown,
        )


def _cooldown_remaining(name: str) -> int:
    with _runtime_lock:
        until = float(_provider_state.get(name, {}).get("cooldown_until", 0.0))
    return max(0, math.ceil(until - time.monotonic()))


def _call_provider(
    name: str,
    query: str,
    num: int,
    timeout_seconds: float,
    record_state: bool = True,
) -> list[SearchResult]:
    """Query one provider.

    Diagnostic probes pass ``record_state=False`` so that a status check neither
    puts a healthy provider into cooldown nor clears a cooldown that a real
    search just set.
    """
    provider = SEARCH_PROVIDERS[name]
    lock = _provider_locks[name]
    with lock:
        since = time.monotonic() - _provider_last_request.get(name, 0.0)
        if since < MIN_PROVIDER_INTERVAL_SECONDS:
            time.sleep(MIN_PROVIDER_INTERVAL_SECONDS - since)
        _provider_last_request[name] = time.monotonic()
        try:
            results = provider.search(query, num, timeout_seconds)
        except Exception as exc:
            if _error_kind(exc) != "empty":
                if record_state:
                    _set_provider_result(name, exc)
                raise
            # The provider answered "nothing matched" by raising. It answered
            # correctly, so it counts as a success with zero results.
            results = []
        if record_state:
            _set_provider_result(name, None)
        return results


def _recovery(name: str, query: str) -> dict:
    browser_url = SEARCH_PROVIDERS[name].browser_url(query)
    return {
        "provider": name,
        "challenge": True,
        "browser_url": browser_url,
        "next_tool": "web_action",
        "suggested_arguments": {
            "actions": [
                {
                    "action": "open",
                    "url": browser_url,
                    "session_id": f"search-{name}",
                    "headless": False,
                }
            ]
        },
        "note": "Open visibly for manual challenge completion, or let fallback providers continue.",
    }


def _probe_engine(name: str, timeout_seconds: float, force: bool) -> dict:
    started = time.perf_counter()
    remaining = _cooldown_remaining(name)
    if remaining and not force:
        state = _provider_state.get(name, {})
        return {
            "name": name,
            "available": False,
            "state": "cooldown",
            "latency_ms": 0,
            "cooldown_seconds": remaining,
            "error_kind": state.get("error_kind"),
            "error": state.get("last_error"),
        }
    try:
        results = _call_provider(name, "python", 1, timeout_seconds, record_state=False)
        return {
            "name": name,
            # A probe that comes back empty is answering, not failing: it is
            # reported as such and never as an error or a cooldown.
            "available": bool(results),
            "state": "available" if results else "no_results",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "cooldown_seconds": 0,
            "error_kind": None,
            "error": None,
        }
    except Exception as exc:
        return {
            "name": name,
            "available": False,
            "state": "challenge" if _error_kind(exc) == "challenge" else "unavailable",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "cooldown_seconds": _cooldown_remaining(name),
            "error_kind": _error_kind(exc),
            "error": f"{type(exc).__name__}: {exc}",
        }


def get_search_engines_status(
    check_live: bool = True,
    timeout_seconds: float = 6.0,
    force_refresh: bool = False,
) -> dict:
    """List engines and optionally probe up to three providers concurrently."""
    global _status_cache
    if not check_live:
        return {
            "default_engine": DEFAULT_ENGINE,
            "checked_live": False,
            "configured": list(ENGINE_ORDER),
            "available": [],
            "engines": [
                {
                    "name": name,
                    "available": None,
                    "state": "configured",
                    "latency_ms": None,
                    "cooldown_seconds": _cooldown_remaining(name),
                    "error_kind": None,
                    "error": None,
                }
                for name in ENGINE_ORDER
            ],
        }

    with _runtime_lock:
        if (
            not force_refresh
            and _status_cache is not None
            and time.monotonic() - _status_cache[0] < STATUS_CACHE_TTL_SECONDS
        ):
            return {**copy.deepcopy(_status_cache[1]), "cached": True}

    timeout = max(1.0, float(timeout_seconds))
    by_name: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(ENGINE_ORDER))) as executor:
        futures = {
            executor.submit(_probe_engine, name, timeout, force_refresh): name
            for name in ENGINE_ORDER
        }
        for future in as_completed(futures):
            item = future.result()
            by_name[item["name"]] = item
    engines = [by_name[name] for name in ENGINE_ORDER]
    status = {
        "default_engine": DEFAULT_ENGINE,
        "checked_live": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
        "cache_ttl_seconds": STATUS_CACHE_TTL_SECONDS,
        "configured": list(ENGINE_ORDER),
        "available": [item["name"] for item in engines if item["available"]],
        "engines": engines,
    }
    with _runtime_lock:
        _status_cache = (time.monotonic(), copy.deepcopy(status))
    return status


def search_web(
    query: str,
    num: int = 5,
    engine: str = DEFAULT_ENGINE,
    fallback: bool = True,
    timeout_seconds: float = 10.0,
    fresh: bool = False,
) -> dict:
    """Search with DuckDuckGo by default, skipping challenged providers on cooldown.

    A provider that answers "no hits" is not a failed provider: the answer is
    passed on as a successful, empty result set (``result_status`` says
    ``empty``) and the provider stays usable for the next query.
    """
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    if engine not in SEARCH_PROVIDERS:
        raise ValueError(f"Unknown engine '{engine}'. Choose from: {', '.join(ENGINE_ORDER)}")
    num = max(1, min(int(num), 20))
    timeout = max(1.0, float(timeout_seconds))
    started = time.perf_counter()
    cache_key = (query.casefold(), num, engine, bool(fallback))
    with _runtime_lock:
        cached = _search_cache.get(cache_key)
        if not fresh and cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL_SECONDS:
            _search_cache.move_to_end(cache_key)
            return {
                **copy.deepcopy(cached[1]),
                "cached": True,
                "cache_age_seconds": round(time.monotonic() - cached[0], 1),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                # The stored run's handoffs describe a challenge that was live
                # minutes ago; replaying them would send the caller to a browser
                # for a result it already has. ``errors`` is kept as the record
                # of why the cached run fell back, dated by cache_age_seconds.
                "challenge_recoveries": [],
            }

    candidates = [engine]
    if fallback:
        candidates.extend(name for name in ENGINE_ORDER if name != engine)
    errors: dict[str, dict] = {}
    recoveries: list[dict] = []
    empty_answers: list[str] = []
    deadline = time.monotonic() + timeout
    for attempt_index, candidate in enumerate(candidates):
        budget = deadline - time.monotonic()
        if attempt_index > 0 and budget < 1.0:
            errors["overall_deadline"] = {
                "kind": "timeout",
                "message": f"Overall search deadline of {timeout:g} seconds was reached",
                "cooldown_seconds": 0,
            }
            break
        remaining = _cooldown_remaining(candidate)
        if remaining:
            state = _provider_state.get(candidate, {})
            errors[candidate] = {
                "kind": state.get("error_kind") or "cooldown",
                "message": state.get("last_error") or "Provider is cooling down",
                "cooldown_seconds": remaining,
            }
            if state.get("error_kind") == "challenge":
                recoveries.append(_recovery(candidate, query))
            continue
        try:
            attempt_timeout = max(1.0, min(MAX_PROVIDER_ATTEMPT_SECONDS, budget))
            results = _call_provider(candidate, query, num, attempt_timeout)
        except Exception as exc:
            kind = _error_kind(exc)
            errors[candidate] = {
                "kind": kind,
                "message": f"{type(exc).__name__}: {exc}",
                "cooldown_seconds": _cooldown_remaining(candidate),
            }
            if kind == "challenge":
                recoveries.append(_recovery(candidate, query))
            continue
        if not results:
            # Answered, but this engine knows nothing about the query. Ask the
            # next one; the provider itself is fine and stays uncooled.
            empty_answers.append(candidate)
            continue
        return _successful_search(
            cache_key,
            query=query,
            engine=engine,
            candidate=candidate,
            num=num,
            results=results,
            errors=errors,
            recoveries=recoveries,
            empty_answers=empty_answers,
            started=started,
        )

    if empty_answers:
        # Every engine that could be asked answered, and the honest answer is
        # that this query has no hits. That is a successful search.
        return _successful_search(
            cache_key,
            query=query,
            engine=engine,
            candidate=empty_answers[0],
            num=num,
            results=[],
            errors=errors,
            recoveries=recoveries,
            empty_answers=empty_answers,
            started=started,
        )

    return {
        "success": False,
        "query": query,
        "requested_engine": engine,
        "engine_used": None,
        "fallback_used": False,
        "cached": False,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "requested_num": num,
        "result_count": 0,
        "result_status": "failed",
        "results": [],
        "engines_without_results": empty_answers,
        "errors": errors,
        "challenge_recoveries": recoveries,
    }


def _successful_search(
    cache_key: tuple,
    *,
    query: str,
    engine: str,
    candidate: str,
    num: int,
    results: list[SearchResult],
    errors: dict[str, dict],
    recoveries: list[dict],
    empty_answers: list[str],
    started: float,
) -> dict:
    """Build, cache and return a successful response.

    ``result_status`` tells the caller how complete the answer is without
    making them compare lengths: ``ok`` for a full set, ``partial`` when the
    engine had fewer than ``num`` hits, ``empty`` when it had none.
    """
    if not results:
        status = "empty"
    elif len(results) < num:
        status = "partial"
    else:
        status = "ok"
    response = {
        "success": True,
        "query": query,
        "requested_engine": engine,
        "engine_used": candidate,
        "fallback_used": candidate != engine,
        "cached": False,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "requested_num": num,
        "result_count": len(results),
        "result_status": status,
        "results": results,
        "engines_without_results": empty_answers,
        "errors": errors,
        "challenge_recoveries": recoveries,
    }
    if results or not errors:
        # "No hits anywhere" is worth caching, but not when an engine could not
        # be asked: that empty answer is incomplete, and repeating it for two
        # minutes would hide the retry that fixes it.
        with _runtime_lock:
            _search_cache[cache_key] = (time.monotonic(), copy.deepcopy(response))
            while len(_search_cache) > 64:
                _search_cache.popitem(last=False)
    return response


def _search_one(name: str, query: str, num: int, timeout_seconds: float) -> list[SearchResult]:
    return _call_provider(name, query, max(1, min(int(num), 20)), timeout_seconds)


def search_duckduckgo(query: str, num: int = 5, timeout_seconds: float = 10.0) -> list[SearchResult]:
    return _search_one("duckduckgo", query, num, timeout_seconds)


def search_bing(query: str, num: int = 5, timeout_seconds: float = 10.0) -> list[SearchResult]:
    return _search_one("bing", query, num, timeout_seconds)


def search_yandex(query: str, num: int = 5, timeout_seconds: float = 10.0) -> list[SearchResult]:
    raise SearchProviderError(
        "Yandex is not enabled because it consistently requires SmartCaptcha; use search_web fallback providers",
        "challenge",
    )
