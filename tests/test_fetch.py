from __future__ import annotations

import asyncio
import time
from unittest.mock import Mock

import pytest
import requests

from web_search_neo import main
from web_search_neo.fetch import content as fetch_content
from web_search_neo.web_client import request, validate_http_url


def test_fetch_url_text_uses_local_http_and_strips_non_visible_content(local_site):
    text = asyncio.run(main.fetch_url_text(f"{local_site.base_url}/page"))

    assert "Local fixture" in text
    assert "Visible body text." in text
    assert "script must not be returned" not in text
    assert "hidden noscript" not in text


def test_fetch_url_text_honors_max_chars(local_site):
    text = asyncio.run(main.fetch_url_text(f"{local_site.base_url}/page", max_chars=12))

    # 1.19: the cut is always stated, with where to continue.
    content, marker = text.split("\n\n[truncated=true", 1)
    assert len(content) == 12 and "next_offset=12]" in marker
    page = asyncio.run(main.fetch_url_text(f"{local_site.base_url}/page", max_chars=12,
                                           offset=12, output="json"))
    assert page["offset"] == 12 and page["text"] and page["total_chars"] > 12


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


# --- Bug #4: fetch_text on JS-rendered SPA pages ------------------------------
# A static fetch cannot run JS, so an SPA arrives as its shell (title, empty
# mount node, bundled scripts). The fetch must say so explicitly instead of
# silently returning title-only content as if it were the page.


def _fake_html_response(html: str, url: str = "https://example.test/app",
                        content_type: str = "text/html; charset=utf-8") -> Mock:
    return Mock(status_code=200, url=url, headers={"Content-Type": content_type},
                text=html, content=html.encode("utf-8"))


def _fetch_html(html: str, **kwargs) -> str:
    client = Mock(return_value=_fake_html_response(html))
    return fetch_content._fetch_url_text("https://example.test/app", request_client=client, **kwargs)


VITE_SHELL = (
    "<html><head><title>TokenForge</title>"
    '<script type="module" src="/src/main.tsx"></script></head>'
    '<body><div id="root"></div></body></html>'
)
CRA_SHELL = (
    "<html><head><title>TokenForge sign up</title></head><body>"
    '<div id="root"></div><noscript>You need JavaScript.</noscript>'
    '<script src="/static/js/main.ab12cd34.js"></script></body></html>'
)
NEXT_SHELL = (
    "<html><head><title>octocat - GitHub</title></head><body>"
    '<div id="__next"></div>'
    '<script src="/_next/static/chunks/pages/profile-abc123.js"></script></body></html>'
)
VUE_SHELL = (
    "<html><head><title>App</title></head><body>"
    '<div id="app"></div><script src="/assets/index-xyz.js"></script></body></html>'
)


@pytest.mark.parametrize(
    ("html", "text"),
    [
        (VITE_SHELL, "TokenForge"),
        (CRA_SHELL, "TokenForge sign up"),
        (NEXT_SHELL, "octocat - GitHub"),
        (VUE_SHELL, "App"),
        # A shell without even a title is still a shell.
        ('<html><body><div id="root"></div>'
         '<script src="/assets/index-xyz.js"></script></body></html>', ""),
    ],
)
def test_spa_shell_detection_flags_mount_and_bundle_markers(html, text):
    assert fetch_content.is_spa_shell(html, text) is True


@pytest.mark.parametrize(
    ("html", "text"),
    [
        # Long bundled page: real content, not a shell.
        ('<html><head><title>Guide</title></head><body><div id="root">'
         + "<p>Real paragraph. </p>" * 60 +
         '</div><script src="/static/js/main.ab12.js"></script></body></html>',
         "Guide\n" + "Real paragraph. \n" * 60),
        # Tiny static page with no app markers: short, but not a shell.
        ("<html><title>Relative</title><body>relative target</body></html>",
         "Relative\nrelative target"),
        # Inline scripts and styles alone are not an app bundle.
        ("<html><head><script>window.secret = 'x';</script></head>"
         "<body>hi</body></html>", "hi"),
        # A tiny jQuery page is still a page, not an app shell.
        ('<html><head><title>Club</title>'
         '<script src="https://cdn.example.test/jquery.min.js"></script></head>'
         "<body><p>Chess club meets Fridays.</p></body></html>",
         "Club\nChess club meets Fridays."),
        # A mount node carrying real text is server-rendered content.
        ('<html><body><div id="root"><article><h1>Notes</h1><p>'
         + "Word. " * 40 + "</p></article></div></body></html>",
         "Notes\n" + "Word. " * 40),
        # Not HTML at all (JSON echo, plain probe bodies).
        ('{"ok": true}', '{"ok": true}'),
        ("ok", "ok"),
    ],
)
def test_spa_shell_detection_ignores_normal_pages(html, text):
    assert fetch_content.is_spa_shell(html, text) is False


@pytest.mark.parametrize("html", [VITE_SHELL, CRA_SHELL, NEXT_SHELL, VUE_SHELL])
def test_fetch_url_text_marks_spa_shell_instead_of_returning_title_only(html):
    text = _fetch_html(html)

    # The shell text stays first (backward compatible), the pointer is added.
    assert text.startswith(("TokenForge", "octocat", "App"))
    assert "spa_suspected=true" in text
    assert "SPA: use browser session" in text
    assert "browser session" in text


def test_fetch_url_text_leaves_plain_pages_unchanged():
    html = ("<html><head><title>Plain</title></head>"
            "<body><p>Hello world.</p></body></html>")

    assert _fetch_html(html) == "Plain\nHello world."


def test_fetch_url_text_truncation_does_not_fake_an_spa_signal():
    # WHY this guard: max_chars applies to page content, so detecting on the
    # truncated slice would flag every long page fetched with a small budget.
    html = ("<html><head><title>Guide</title></head><body>"
            + "<p>Real paragraph. </p>" * 200 +
            '<script src="/static/js/main.ab12.js"></script></body></html>')

    text = _fetch_html(html, max_chars=20)

    assert "spa_suspected" not in text
    assert "browser session" not in text
    assert len(text.split("\n\n[truncated=true", 1)[0]) == 20


def test_fetch_url_text_raw_mode_keeps_source_without_spa_notice():
    text = _fetch_html(VITE_SHELL, mode="raw")

    assert "main.tsx" in text
    assert "spa_suspected" not in text


def test_fetch_url_text_skips_spa_check_for_json_payloads():
    client = Mock(return_value=_fake_html_response(
        '{"root": "short"}', content_type="application/json"))
    text = fetch_content._fetch_url_text(
        "https://example.test/api", request_client=client)

    assert "spa_suspected" not in text
