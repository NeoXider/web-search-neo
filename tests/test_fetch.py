from __future__ import annotations

import asyncio
import time

import pytest
import requests

import main
from web_client import request, validate_http_url


def test_fetch_url_text_uses_local_http_and_strips_non_visible_content(local_site):
    text = asyncio.run(main.fetch_url_text(f"{local_site.base_url}/page"))

    assert "Local fixture" in text
    assert "Visible body text." in text
    assert "script must not be returned" not in text
    assert "hidden noscript" not in text


def test_fetch_url_text_honors_max_chars(local_site):
    text = asyncio.run(main.fetch_url_text(f"{local_site.base_url}/page", max_chars=12))

    assert len(text) == 12


def test_fetch_page_links_resolves_relative_urls_and_deduplicates(local_site):
    links = asyncio.run(main.fetch_page_links(f"{local_site.base_url}/page"))

    assert links == [
        f"{local_site.base_url}/relative",
        "https://example.test/absolute",
    ]


def test_fetch_urls_text_runs_independent_requests_concurrently(local_site):
    urls = [
        f"{local_site.base_url}/slow?delay=0.5&session=one",
        f"{local_site.base_url}/slow?delay=0.5&session=two",
    ]

    started = time.perf_counter()
    results = asyncio.run(main.fetch_urls_text(urls))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.9, f"bulk fetch appears serial: {elapsed:.3f}s"
    assert [result["url"] for result in results] == urls
    assert all(result["success"] for result in results)
    assert "Slow one" in results[0]["text"]
    assert "Slow two" in results[1]["text"]


@pytest.mark.parametrize(
    "value",
    ["", "relative/path", "ftp://example.test/file", "file:///etc/passwd"],
)
def test_validate_http_url_rejects_non_http_absolute_urls(value):
    with pytest.raises(ValueError, match="absolute http"):
        validate_http_url(value)


def test_request_raises_for_http_error(local_site):
    # Retried status codes surface as RetryError; non-retried 4xx/5xx as HTTPError.
    with pytest.raises(requests.RequestException):
        request(f"{local_site.base_url}/error", timeout_seconds=1)
