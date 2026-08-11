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
import threading
import time
from urllib.parse import quote_plus

from ddgs import DDGS


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
            message = str(exc)
            lower = message.lower()
            if any(token in lower for token in ("captcha", "ratelimit", "rate limit", "202", "403", "429")):
                kind = "challenge"
            elif "timeout" in lower or "timed out" in lower:
                kind = "timeout"
            else:
                kind = "provider_error"
            raise SearchProviderError(message or type(exc).__name__, kind) from exc

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
        if not results:
            raise SearchProviderError(
                f"{self.name} returned no usable results", "empty_results"
            )
        return results

    def browser_url(self, query: str) -> str:
        return self.url_template.format(query=quote_plus(query))


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
    DdgsSearchProvider("bing", "bing", "https://www.bing.com/search?q={query}"),
    DdgsSearchProvider("startpage", "startpage", "https://www.startpage.com/do/search?q={query}"),
):
    register_search_provider(_provider)


def _error_kind(exc: Exception) -> str:
    return exc.kind if isinstance(exc, SearchProviderError) else "provider_error"


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
    name: str, query: str, num: int, timeout_seconds: float
) -> list[SearchResult]:
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
            _set_provider_result(name, exc)
            raise
        _set_provider_result(name, None)
        return results


def _recovery(name: str, query: str) -> dict:
    return {
        "provider": name,
        "challenge": True,
        "browser_url": SEARCH_PROVIDERS[name].browser_url(query),
        "next_tool": "browser_open_page",
        "suggested_arguments": {
            "url": SEARCH_PROVIDERS[name].browser_url(query),
            "session_id": f"search-{name}",
            "headless": False,
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
        results = _call_provider(name, "python", 1, timeout_seconds)
        return {
            "name": name,
            "available": bool(results),
            "state": "available",
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

    timeout = max(1.0, min(float(timeout_seconds), 15.0))
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
    """Search with DuckDuckGo by default, skipping challenged providers on cooldown."""
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    if engine not in SEARCH_PROVIDERS:
        raise ValueError(f"Unknown engine '{engine}'. Choose from: {', '.join(ENGINE_ORDER)}")
    num = max(1, min(int(num), 20))
    timeout = max(1.0, min(float(timeout_seconds), 20.0))
    cache_key = (query.casefold(), num, engine, bool(fallback))
    with _runtime_lock:
        cached = _search_cache.get(cache_key)
        if not fresh and cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL_SECONDS:
            _search_cache.move_to_end(cache_key)
            return {**copy.deepcopy(cached[1]), "cached": True}

    candidates = [engine]
    if fallback:
        candidates.extend(name for name in ENGINE_ORDER if name != engine)
    errors: dict[str, dict] = {}
    recoveries: list[dict] = []
    started = time.perf_counter()
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
            response = {
                "success": True,
                "query": query,
                "requested_engine": engine,
                "engine_used": candidate,
                "fallback_used": candidate != engine,
                "cached": False,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "results": results,
                "errors": errors,
                "challenge_recoveries": recoveries,
            }
            with _runtime_lock:
                _search_cache[cache_key] = (time.monotonic(), copy.deepcopy(response))
                while len(_search_cache) > 64:
                    _search_cache.popitem(last=False)
            return response
        except Exception as exc:
            kind = _error_kind(exc)
            errors[candidate] = {
                "kind": kind,
                "message": f"{type(exc).__name__}: {exc}",
                "cooldown_seconds": _cooldown_remaining(candidate),
            }
            if kind == "challenge":
                recoveries.append(_recovery(candidate, query))

    return {
        "success": False,
        "query": query,
        "requested_engine": engine,
        "engine_used": None,
        "fallback_used": False,
        "cached": False,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "results": [],
        "errors": errors,
        "challenge_recoveries": recoveries,
    }


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
