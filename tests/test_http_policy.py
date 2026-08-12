"""Plain-HTTP policy: public hosts require https, local networks stay reachable."""

from __future__ import annotations

import pytest

import web_client


@pytest.fixture(autouse=True)
def clear_plain_http_optin(monkeypatch):
    monkeypatch.delenv(web_client.ALLOW_PLAIN_HTTP_ENV, raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/page",
        "http://127.0.0.1:8188/prompt",
        "http://localhost:3000/",
        "http://192.168.1.10/api",
        "http://10.0.0.5/",
        "http://172.16.4.4/",
        "http://[::1]:7860/",
        "http://comfyui.local/queue",
        "http://box.internal/status",
    ],
)
def test_local_and_https_urls_stay_allowed(url):
    assert web_client.validate_http_url(url) == url


@pytest.mark.parametrize(
    "url",
    ["http://example.test/page", "http://93.184.216.34/", "http://[2001:4860:4860::8888]/"],
)
def test_plain_http_to_public_hosts_is_blocked(url):
    with pytest.raises(ValueError, match="Unencrypted http"):
        web_client.validate_http_url(url)


def test_error_names_the_environment_override():
    with pytest.raises(ValueError, match=web_client.ALLOW_PLAIN_HTTP_ENV):
        web_client.validate_http_url("http://example.test/")


def test_environment_override_reenables_plain_http(monkeypatch):
    monkeypatch.setenv(web_client.ALLOW_PLAIN_HTTP_ENV, "1")
    assert web_client.validate_http_url("http://example.test/") == "http://example.test/"


def test_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv(web_client.ALLOW_PLAIN_HTTP_ENV, "1")
    with pytest.raises(ValueError):
        web_client.validate_http_url("http://example.test/", allow_plain_http=False)


@pytest.mark.parametrize(
    "url", ["ftp://example.test/file", "javascript:alert(1)", "file:///c:/secret", "notaurl"]
)
def test_non_http_schemes_stay_rejected(url):
    with pytest.raises(ValueError, match="absolute http"):
        web_client.validate_http_url(url)


def test_redirects_are_followed_and_recorded(local_site):
    response = web_client.request(f"{local_site.base_url}/redirect?to=/relative")
    assert response.status_code == 200
    assert "relative target" in response.text
    assert [item.status_code for item in response.history] == [302]
    assert response.url.endswith("/relative")


def test_redirect_to_public_plain_http_is_refused(local_site):
    with pytest.raises(ValueError, match="Unencrypted http"):
        web_client.request(f"{local_site.base_url}/redirect?to=http://example.test/")


def test_redirect_chain_is_bounded(local_site):
    with pytest.raises(ValueError, match="redirects"):
        web_client.request(f"{local_site.base_url}/redirect-loop")


def test_local_host_classification():
    assert web_client.is_local_host("127.0.0.1")
    assert web_client.is_local_host("LOCALHOST")
    assert web_client.is_local_host("169.254.1.1")
    assert not web_client.is_local_host("example.test")
    assert not web_client.is_local_host("")
