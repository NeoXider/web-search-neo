"""Redirect credential isolation and response lifecycle regression tests."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import Mock

import pytest
import requests

from web_search_neo import web_client


@contextmanager
def origin():
    received = []
    redirects = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            target = redirects.get(self.path)
            self.send_response(302 if target else 200)
            if target:
                self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received, redirects
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("credential_source", ["headers", "session", "auth_and_cookies"])
def test_cross_origin_redirect_does_not_leak_credentials(monkeypatch, credential_source):
    with requests.Session() as session, origin() as first, origin() as second:
        session.trust_env = False
        monkeypatch.setattr(web_client, "_session", lambda: session)
        first_url, first_received, first_redirects = first
        second_url, second_received, second_redirects = second
        first_redirects["/start"] = second_url + "/away"
        second_redirects["/away"] = first_url + "/return"
        credentials = {"aUtHoRiZaTiOn": "Bearer private", "cOoKiE": "token=private",
                       "pRoXy-AuThOrIzAtIoN": "Basic private"}
        kwargs = {"headers": {"X-Public": "keep"}}
        if credential_source == "headers":
            kwargs["headers"].update(credentials)
        elif credential_source == "session":
            session.headers.update(credentials)
            session.auth = ("private", "password")
            session.cookies.set("jar", "private", domain="127.0.0.1", path="/")
        else:
            kwargs.update(auth=("private", "password"), cookies={"token": "private"})
        original_headers = dict(kwargs["headers"])
        response = web_client.request(first_url + "/start", **kwargs)
        assert response.status_code == 200
        assert len(response.history) == 2
        assert requests.structures.CaseInsensitiveDict(first_received[0])["Authorization"]
        for received in (second_received[0], first_received[1]):
            headers = requests.structures.CaseInsensitiveDict(received)
            assert not headers.get("Authorization")
            assert not headers.get("Cookie")
            assert not headers.get("Proxy-Authorization")
            assert headers["X-Public"] == "keep"
        assert kwargs["headers"] == original_headers


def test_same_origin_redirect_preserves_custom_credentials(monkeypatch):
    with requests.Session() as session, origin() as site:
        session.trust_env = False
        monkeypatch.setattr(web_client, "_session", lambda: session)
        base_url, received, redirects = site
        redirects["/start"] = "/end"
        headers = {"Authorization": "Bearer private", "Cookie": "token=private",
                   "Proxy-Authorization": "Basic private"}
        web_client.request(base_url + "/start", headers=headers)
        assert len(received) == 2
        for name, value in headers.items():
            assert received[1][name] == value


def test_rejected_redirect_closes_response():
    response = Mock(is_redirect=True, url="http://localhost/start",
                    headers={"location": "file:///secret"})
    with pytest.raises(ValueError, match="absolute http"):
        web_client._follow_redirects(Mock(), response, method="GET", timeout=(1, 1))
    response.close.assert_called_once()


@pytest.mark.parametrize("failure", ["http", "stream", "limit"])
def test_failed_request_closes_response(monkeypatch, failure):
    response = Mock(is_redirect=False)
    response.raise_for_status.return_value = None
    response.iter_content.return_value = iter([b"ok"])
    if failure == "http":
        response.raise_for_status.side_effect = requests.HTTPError("bad status")
    elif failure == "stream":
        response.iter_content.side_effect = requests.ConnectionError("broken stream")
    else:
        response.iter_content.return_value = iter([b"x" * 1025])
    session = Mock()
    session.request.return_value = response
    monkeypatch.setattr(web_client, "_session", lambda: session)
    with pytest.raises((requests.RequestException, ValueError)):
        web_client.request("http://localhost/file", max_response_bytes=1024)
    response.close.assert_called_once()


def test_origin_normalizes_default_ports():
    assert web_client._origin("https://example.test/a") == web_client._origin("https://example.test:443/b")
    assert web_client._origin("http://example.test/a") != web_client._origin("https://example.test/a")
