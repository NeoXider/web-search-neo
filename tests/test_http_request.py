"""Canned tests for the browserless http_request action (no Chrome)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import requests

import web_search_neo.fetch.api as api
from web_search_neo import main

URL = "https://api.example.test/items"


class _FakeResponse:
    def __init__(
        self,
        url: str = URL,
        status: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.url = url
        self.status_code = status
        self.headers = dict(headers or {"Content-Type": "application/json"})
        self._text = text
        self.content = text.encode("utf-8")

    @property
    def text(self) -> str:
        return self._text


def _http_error(response: _FakeResponse) -> requests.HTTPError:
    error = requests.HTTPError(f"{response.status_code} error for {response.url}")
    error.response = response  # type: ignore[attr-defined]
    return error


def test_get_200_envelope(monkeypatch):
    seen: dict[str, Any] = {}

    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        seen["url"] = url
        seen.update(kwargs)
        return _FakeResponse(text='{"ok": true}')

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    result = api.http_request(URL)

    assert result["success"] is True
    assert result["url"] == URL
    assert result["status"] == 200
    assert result["headers"]["Content-Type"] == "application/json"
    assert result["body"] == '{"ok": true}'
    assert result["truncated"] is False
    assert result["size_bytes"] == len('{"ok": true}'.encode("utf-8"))
    assert seen["method"] == "GET"


def test_post_body_json_adds_json_content_type(monkeypatch):
    seen: dict[str, Any] = {}

    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        seen.update(kwargs)
        return _FakeResponse(status=201, text='{"id": 1}')

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    result = api.http_request(URL, method="post", body_json={"name": "neo"})

    assert result["status"] == 201
    assert seen["method"] == "POST"
    assert seen["json"] == {"name": "neo"}
    assert seen["headers"]["Content-Type"] == "application/json"


def test_query_maps_to_request_params(monkeypatch):
    seen: dict[str, Any] = {}

    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        seen.update(kwargs)
        return _FakeResponse(text="[]")

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    api.http_request(URL, query={"q": "kittens", "page": "2"})

    assert seen["params"] == {"q": "kittens", "page": "2"}


def test_body_and_body_json_together_raise():
    with pytest.raises(ValueError, match="mutually exclusive"):
        api.http_request(URL, body="raw", body_json={"a": 1})


def test_invalid_method_raises():
    with pytest.raises(ValueError, match="method must be"):
        api.http_request(URL, method="FETCH")


def test_404_returns_success_envelope_with_status(monkeypatch):
    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        raise _http_error(_FakeResponse(status=404, text="not found"))

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    result = api.http_request(URL)

    assert result["success"] is True
    assert result["status"] == 404
    assert result["body"] == "not found"


def test_max_chars_truncates_body(monkeypatch):
    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(text="x" * 100)

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    result = api.http_request(URL, max_chars=10)

    assert result["body"] == "x" * 10
    assert result["truncated"] is True
    assert result["size_bytes"] == 100


def test_save_to_writes_file(monkeypatch, tmp_path):
    target = tmp_path / "resp.bin"

    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        response = _FakeResponse(text="binary-data")
        response.content = b"binary-data"
        return response

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    monkeypatch.setenv("WEB_SEARCH_NEO_DOWNLOAD_DIR", str(tmp_path))
    result = api.http_request(URL, save_to=str(target))

    assert target.read_bytes() == b"binary-data"
    assert result["success"] is True
    assert result["status"] == 200
    assert result["saved_to"] == str(target.resolve())
    assert result["size_bytes"] == len(b"binary-data")
    assert "body" not in result


def test_transport_error_raises_like_fetch_text(monkeypatch):
    def fake(url: str, **kwargs: Any) -> _FakeResponse:
        raise requests.ConnectionError("DNS failure")

    monkeypatch.setattr("web_search_neo.fetch.api.request", fake)
    with pytest.raises(requests.ConnectionError):
        api.http_request(URL)


def test_registered_in_main_actions_with_fetch_group():
    spec = main._ACTIONS["http_request"]
    assert spec.group == "fetch"
    assert "\n" not in spec.summary
    required, _ = main._parameter_names(spec.tool_name)
    assert required == ["url"]


def test_action_schema_contains_http_request():
    schema = asyncio.run(main.web_info("action_schema", {"action": "http_request"}))
    assert schema["action"] == "http_request"
    properties = schema["input_schema"]["properties"]
    assert {"url", "method", "headers", "query", "body", "body_json"} <= set(properties)
    assert "url" in schema["input_schema"]["required"]
    assert schema["notes"]["method"].startswith("GET/POST")
    assert "replay_request" in schema["notes"]["response"]
