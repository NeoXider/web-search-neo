"""Regression tests for the fetch/http hardening pass (no Chrome needed)."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import Mock

import pytest
import requests
from mcp.types import CallToolResult

from web_search_neo import log_setup, main, mcp_compat, msp_date_time, plugins, web_client
from web_search_neo.fetch import api, content, safety

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class _ErrorHandler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.hits.append(self.path)
        if self.path.startswith("/missing"):
            self._reply(404, {"error": "no such item", "id": 7})
        else:
            self._reply(500, {"error": "database down"})

    do_POST = do_GET

    def log_message(self, *args):
        pass


# --- 1. error bodies ----------------------------------------------------------


def test_http_request_returns_404_json_body_from_a_real_server():
    with serve(_ErrorHandler) as base:
        result = api.http_request(f"{base}/missing?token=abc")
    assert result["success"] is True
    assert result["status"] == 404
    assert json.loads(result["body"]) == {"error": "no such item", "id": 7}
    assert result["size_bytes"] == len(result["body"])


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_http_request_returns_500_body_even_after_the_get_retry(method):
    with serve(_ErrorHandler) as base:
        result = api.http_request(f"{base}/boom", method=method)
    assert result["status"] == 500
    assert json.loads(result["body"]) == {"error": "database down"}


def test_request_still_raises_for_error_status_with_the_body_attached():
    with serve(_ErrorHandler) as base:
        with pytest.raises(requests.HTTPError) as caught:
            web_client.request(f"{base}/missing", max_response_bytes=10_000)
    assert caught.value.response.json()["error"] == "no such item"


class _BigBodyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 502 if self.path.startswith("/error") else 200
        body = b"E" * 3_000_000
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # the client stopped reading at its byte limit

    def log_message(self, *args):
        pass


def test_an_oversized_error_body_still_reports_the_status_truncated():
    with serve(_BigBodyHandler) as base:
        result = api.http_request(f"{base}/error", max_chars=10)
        with pytest.raises(requests.HTTPError) as caught:
            web_client.request(f"{base}/error", max_response_bytes=2048)
        # 1.19: a success body over the limit is cut and flagged, not refused.
        cut = web_client.request(f"{base}/ok", max_response_bytes=2048)
        assert cut.wsn_truncated is True and len(cut.content) == 2048
    assert result["success"] is True and result["status"] == 502
    assert result["truncated"] is True
    assert result["body"] == "E" * 10
    assert result["size_bytes"] == 1_000_000  # the byte budget for max_chars=10
    assert caught.value.response.status_code == 502
    assert caught.value.response.content == b"E" * 2048
    assert caught.value.response.wsn_truncated is True


def test_an_oversized_error_body_is_flagged_even_when_it_fits_max_chars(monkeypatch):
    class Cut:
        url, status_code, headers, text, content = URL_BIG, 500, {}, "short", b"short"
        wsn_truncated = True

    def fake(url, **kwargs):
        error = requests.HTTPError("500")
        error.response = Cut()
        raise error

    result = api.http_request(URL_BIG, request_client=fake)
    assert result["status"] == 500 and result["body"] == "short"
    assert result["truncated"] is True


URL_BIG = "https://api.example.test/big"


# --- 2. log location ----------------------------------------------------------


def test_log_file_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom" / "server.log"
    monkeypatch.setenv(log_setup.LOG_FILE_ENV, str(target))
    assert log_setup.default_log_file() == target


def test_default_log_file_is_per_user_not_next_to_the_package(monkeypatch, tmp_path):
    monkeypatch.delenv(log_setup.LOG_FILE_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = log_setup.default_log_file()
    assert path.is_relative_to(tmp_path)
    assert path.parts[-3:] == ("web-search-neo", "logs", "msp_server.log")
    assert not path.is_relative_to(PROJECT_ROOT)


def test_log_handler_creates_its_directory_lazily(tmp_path):
    target = tmp_path / "a" / "b" / "server.log"
    handler = log_setup.LazyRotatingFileHandler(target)
    try:
        assert not target.parent.exists()
        handler.emit(logging.makeLogRecord({"msg": "hello"}))
        assert "hello" in target.read_text(encoding="utf-8")
    finally:
        handler.close()


def test_unwritable_log_location_never_raises(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    handler = log_setup.LazyRotatingFileHandler(blocker / "sub" / "server.log")
    try:
        handler.emit(logging.makeLogRecord({"msg": "one"}))
        handler.emit(logging.makeLogRecord({"msg": "two"}))
    finally:
        handler.close()
    assert handler.disabled_reason
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib needs Python 3.11")
def test_packaged_list_matches_the_source_tree():
    import tomllib

    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    listed = set(config["tool"]["setuptools"]["packages"])
    discovered = {
        ".".join(init.parent.relative_to(PROJECT_ROOT).parts)
        for init in (PROJECT_ROOT / "web_search_neo").rglob("__init__.py")
        if "__pycache__" not in init.parts
    }
    assert listed - {"web_search_neo.chrome_extension"} == discovered
    mapped = config["tool"]["setuptools"]["package-dir"]["web_search_neo.chrome_extension"]
    assert (PROJECT_ROOT / mapped / "manifest.json").is_file()
    assert "bridge-token.js" in config["tool"]["setuptools"]["exclude-package-data"][
        "web_search_neo.chrome_extension"
    ]


# --- 3. cross-origin header allowlist ----------------------------------------


def test_cross_origin_hop_keeps_only_safe_headers():
    first = Mock(is_redirect=True, url="http://127.0.0.1:1/start",
                 headers={"location": "http://127.0.0.1:2/next"})
    final = Mock(is_redirect=False)
    session = Mock()
    session.request.return_value = final
    web_client._follow_redirects(
        session, first, method="GET", timeout=(1, 1),
        headers={"X-Api-Key": "k", "X-Custom-Token": "t", "Accept": "a/b",
                 "User-Agent": "ua", "Authorization": "Bearer x"},
    )
    sent = session.request.call_args.kwargs["headers"]
    assert sent["Accept"] == "a/b" and sent["User-Agent"] == "ua"
    assert "X-Api-Key" not in sent and "X-Custom-Token" not in sent
    assert sent["Authorization"] is None and sent["Cookie"] == ""


# --- 4. SSRF ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[fe80::1]/",
        "http://100.100.100.200/latest/meta-data/",
        "http://2852039166/",  # 169.254.169.254 as a decimal literal
    ],
)
def test_metadata_and_link_local_are_always_blocked(url):
    with pytest.raises(ValueError, match="blocked"):
        web_client.request(url)


@pytest.mark.parametrize(
    "literal",
    [
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "64:ff9b::169.254.169.254",
        "64:ff9b::a9fe:1",  # NAT64 of link-local 169.254.0.1
        "2002:a9fe:a9fe::",  # 6to4 of 169.254.169.254
        "2002:a9fe:a9fe:1::5",
        "64:ff9b::6464:64c8",  # NAT64 of Alibaba's 100.100.100.200
    ],
)
def test_ipv6_forms_embedding_a_metadata_address_are_blocked(literal, monkeypatch):
    with pytest.raises(ValueError, match="blocked"):
        web_client.classify_destination(f"http://[{literal}]/latest/meta-data/")
    monkeypatch.setattr(
        web_client.socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (literal, 0, 0, 0))],
    )
    with pytest.raises(ValueError, match="blocked"):
        web_client.classify_destination("https://evil.example/")


def test_ipv6_forms_embedding_a_public_address_are_not_blocked():
    assert web_client._embedded_ipv4(web_client._ip_literal("64:ff9b::808:808")) is not None
    assert not web_client._address_blocked(web_client._ip_literal("64:ff9b::808:808"))
    assert not web_client._address_blocked(web_client._ip_literal("2002:808:808::"))
    assert web_client._address_private(web_client._ip_literal("64:ff9b::7f00:1"))


def test_public_name_resolving_to_loopback_counts_as_private(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(web_client.socket, "getaddrinfo", fake_getaddrinfo)
    assert web_client.classify_destination("https://evil.example/") == "private"


def test_public_name_resolving_to_metadata_is_blocked(monkeypatch):
    monkeypatch.setattr(
        web_client.socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))],
    )
    with pytest.raises(ValueError, match="blocked"):
        web_client.classify_destination("https://evil.example/")


def test_explicit_local_urls_stay_allowed():
    assert web_client.classify_destination("http://127.0.0.1:8000/") == "private"
    assert web_client.classify_destination("http://localhost:3000/") == "private"
    assert web_client.classify_destination("http://192.168.1.10/") == "private"


def test_redirect_from_public_to_private_is_refused(monkeypatch):
    response = Mock(is_redirect=True, url="https://public.example/start",
                    headers={"location": "http://127.0.0.1:8080/admin"})
    session = Mock()
    with pytest.raises(ValueError, match="public host to the private"):
        web_client._follow_redirects(
            session, response, method="GET", timeout=(1, 1), origin_class="public"
        )
    response.close.assert_called_once()
    session.request.assert_not_called()


def test_redirect_between_private_hosts_is_allowed():
    response = Mock(is_redirect=True, url="http://127.0.0.1:1/start",
                    headers={"location": "http://localhost:2/next"})
    session = Mock()
    session.request.return_value = Mock(is_redirect=False)
    web_client._follow_redirects(
        session, response, method="GET", timeout=(1, 1), origin_class="private"
    )
    session.request.assert_called_once()


# --- 5. save_to confinement ---------------------------------------------------


@pytest.fixture
def download_root(monkeypatch, tmp_path):
    root = tmp_path / "downloads"
    monkeypatch.setenv(safety.DOWNLOAD_DIR_ENV, str(root))
    return root


def test_relative_save_to_lands_in_the_download_root(download_root):
    path = safety.write_download("sub/file.bin", b"data")
    assert path == (download_root / "sub" / "file.bin").resolve()
    assert path.read_bytes() == b"data"


@pytest.mark.parametrize("escape", ["../outside.bin", "sub/../../outside.bin"])
def test_save_to_cannot_escape_the_root(download_root, escape):
    with pytest.raises(ValueError, match="inside the download directory"):
        safety.write_download(escape, b"x")
    assert not (download_root.parent / "outside.bin").exists()


def test_absolute_save_to_outside_the_root_is_refused(download_root, tmp_path):
    with pytest.raises(ValueError, match="inside the download directory"):
        safety.write_download(str(tmp_path / "elsewhere.bin"), b"x")


def test_existing_file_needs_overwrite(download_root):
    safety.write_download("a.txt", b"first")
    with pytest.raises(ValueError, match="overwrite"):
        safety.write_download("a.txt", b"second")
    assert (download_root / "a.txt").read_bytes() == b"first"
    safety.write_download("a.txt", b"second", overwrite=True)
    assert (download_root / "a.txt").read_bytes() == b"second"


def test_default_download_root_is_cwd_downloads(monkeypatch, tmp_path):
    monkeypatch.delenv(safety.DOWNLOAD_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert safety.download_root() == (tmp_path / "downloads").resolve()


def test_http_request_and_fetch_honor_overwrite(download_root):
    response = Mock(status_code=200, url="https://x.example/", headers={},
                    text="body", content=b"body")
    client = Mock(return_value=response)
    api.http_request("https://x.example/", save_to="r.bin", request_client=client)
    with pytest.raises(ValueError, match="overwrite"):
        api.http_request("https://x.example/", save_to="r.bin", request_client=client)
    api.http_request("https://x.example/", save_to="r.bin", overwrite=True,
                     request_client=client)
    with pytest.raises(ValueError, match="overwrite"):
        content._fetch_url_text("https://x.example/", save_to="r.bin", request_client=client)
    message = content._fetch_url_text(
        "https://x.example/", save_to="r.bin", overwrite=True, request_client=client
    )
    assert message.startswith("Saved 4 bytes")


# --- 6. timeouts --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1e9, 120.0), (0, 1.0), (-5, 1.0), (float("nan"), 20.0), ("junk", 20.0), (None, 20.0), (30, 30.0)],
)
def test_clamp_timeout(value, expected):
    assert web_client.clamp_timeout(value) == expected


def test_tools_clamp_the_timeout_they_pass_on():
    response = Mock(status_code=200, url="https://x.example/", headers={},
                    text="ok", content=b"ok")
    client = Mock(return_value=response)
    api.http_request("https://x.example/", timeout_seconds=10_000, request_client=client)
    assert client.call_args.kwargs["timeout_seconds"] == web_client.MAX_TIMEOUT_SECONDS
    content._fetch_url_text("https://x.example/", timeout_seconds=10_000, request_client=client)
    assert client.call_args.kwargs["timeout_seconds"] == web_client.MAX_TIMEOUT_SECONDS
    content._fetch_page_links("https://x.example/", timeout_seconds=10_000, request_client=client)
    assert client.call_args.kwargs["timeout_seconds"] == web_client.MAX_TIMEOUT_SECONDS


class _TrickleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        try:
            for _ in range(40):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def test_streaming_read_has_a_total_deadline():
    with serve(_TrickleHandler) as base:
        started = time.monotonic()
        with pytest.raises(requests.Timeout, match="not received within"):
            web_client.request(f"{base}/", timeout_seconds=1, max_response_bytes=10_000)
        elapsed = time.monotonic() - started
    # budget = 1 s * TOTAL_DEADLINE_FACTOR; the trickle alone would take 8 s.
    assert elapsed < 5


# --- 7. web_action failures are MCP errors ------------------------------------


def test_failed_web_action_is_an_mcp_error_with_its_payload():
    result = asyncio.run(
        main.mcp.call_tool("web_action", {"actions": [{"action": "no_such_action"}]})
    )
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["results"][0]["action"] == "no_such_action"


def test_successful_web_action_is_not_an_error(monkeypatch):
    async def fake(**kwargs):
        return {"success": True}

    spec = main._ACTIONS["fetch_text"]
    monkeypatch.setitem(
        main._ACTIONS, "fetch_text",
        main.ActionSpec(spec.name, fake, spec.tool_name, spec.group, spec.summary),
    )
    result = asyncio.run(
        main.mcp.call_tool(
            "web_action", {"actions": [{"action": "fetch_text", "url": "https://x.example/"}]}
        )
    )
    assert not isinstance(result, CallToolResult)


# --- 8. FastMCP internals behind one helper -----------------------------------


def test_tool_registry_fails_loudly_when_internals_move():
    with pytest.raises(RuntimeError, match="_tool_manager._tools"):
        mcp_compat.tool_registry(object())


def test_registered_tool_finds_known_tools_and_rejects_unknown():
    assert mcp_compat.registered_tool(main.mcp, "web_action").name == "web_action"
    with pytest.raises(KeyError):
        mcp_compat.registered_tool(main.mcp, "definitely_missing")


# --- 9. time zone region ------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "name", "expected"),
    [
        (datetime.timedelta(hours=5, minutes=30), "IST", "IST +05:30"),
        (datetime.timedelta(hours=-3, minutes=-30), "NST", "NST -03:30"),
        (datetime.timedelta(hours=-4), "EDT", "EDT -04:00"),
        (datetime.timedelta(0), "UTC", "UTC +00:00"),
        (datetime.timedelta(hours=5, minutes=45), "NPT", "NPT +05:45"),
    ],
)
def test_format_region_keeps_minutes_and_sign(offset, name, expected):
    moment = datetime.datetime(2026, 7, 1, 12, tzinfo=datetime.timezone(offset, name))
    assert msp_date_time.format_region(moment) == expected


def test_current_region_matches_the_aware_local_offset():
    region = msp_date_time.get_current_time_and_region()["region"]
    assert region.rsplit(" ", 1)[-1][0] in "+-"
    assert region.rsplit(" ", 1)[-1][3] == ":"


# --- 10. log redaction --------------------------------------------------------


def test_redact_url_masks_sensitive_parameters_and_userinfo():
    redacted = safety.redact_url(
        "https://user:pw@api.example/x?q=cats&api_key=K1&access_token=T&sig=S"
        "&code=C&page=2#frag"
    )
    assert "K1" not in redacted and "T&" not in redacted and "pw" not in redacted
    assert "S&" not in redacted and "C&" not in redacted and "frag" not in redacted
    assert "q=cats" in redacted and "page=2" in redacted
    assert redacted.startswith("https://api.example/x?")


def test_fetch_logs_do_not_contain_secrets(caplog):
    response = Mock(status_code=200, url="https://x.example/", headers={},
                    text="ok", content=b"ok")
    client = Mock(return_value=response)
    with caplog.at_level(logging.INFO, logger="web_search_neo"):
        api.http_request("https://x.example/?token=SECRET1", request_client=client)
        content._fetch_url_text("https://x.example/?password=SECRET2", request_client=client)
        content._fetch_page_links("https://x.example/?key=SECRET3", request_client=client)
    assert "SECRET" not in caplog.text
    assert "x.example" in caplog.text


# --- 11. the bridge daemon does not load plugins ------------------------------


def test_bridge_mode_skips_plugins(monkeypatch):
    def refuse():
        raise AssertionError("plugins must not load in bridge mode")

    monkeypatch.setattr(plugins, "load_plugins", refuse)
    monkeypatch.setattr(main.bridge_daemon, "run_forever", lambda version: 0)
    monkeypatch.setattr(sys, "argv", ["wsn", "--bridge"])
    with pytest.raises(SystemExit) as stopped:
        main.main()
    assert stopped.value.code == 0
