from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class RecordedRequest:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class LocalSite:
    base_url: str
    requests: list[RecordedRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, request: RecordedRequest) -> None:
        with self.lock:
            self.requests.append(request)


@pytest.fixture(scope="session")
def local_site() -> LocalSite:
    site = LocalSite(base_url="")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_html(self, body: str, status: int = 200) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path == "/page":
                self._send_html(
                    """<!doctype html>
                    <html><head><title>Fixture page</title>
                    <style>.hidden { display: none; }</style>
                    <script>window.secret = 'script must not be returned';</script></head>
                    <body><h1>Local fixture</h1><p>Visible body text.</p>
                    <a href="/relative">Relative</a>
                    <a href="/relative">Duplicate</a>
                    <a href="https://example.test/absolute">Absolute</a>
                    <a href="mailto:test@example.test">Mail</a>
                    <noscript>hidden noscript</noscript></body></html>"""
                )
                return
            if parsed.path == "/relative":
                self._send_html("<html><title>Relative</title><body>relative target</body></html>")
                return
            if parsed.path == "/form":
                marker = parse_qs(parsed.query).get("session", ["default"])[0]
                self._send_html(
                    f"""<!doctype html>
                    <html><head><title>Form {marker}</title></head><body>
                    <a id="fixture-link" href="/relative">Fixture link</a>
                    <p id="session-marker">{marker}</p>
                    <p id="click-state">not clicked</p>
                    <button id="action-button" type="button"
                      onclick="document.getElementById('click-state').textContent='clicked'">
                      Run action
                    </button>
                    <form id="application" action="/submit" method="post"
                          enctype="multipart/form-data">
                      <label for="candidate-name">Candidate name</label>
                      <input id="candidate-name" name="candidate_name" required>
                      <label for="cover-letter">Cover letter</label>
                      <textarea id="cover-letter" name="cover_letter"></textarea>
                      <label for="role">Role</label>
                      <select id="role" name="role">
                        <option value="python">Python</option>
                        <option value="unity">Unity Developer</option>
                      </select>
                      <label><input id="remote" name="remote" type="checkbox">Remote</label>
                      <label for="resume">Resume</label>
                      <input id="resume" name="resume" type="file">
                      <button id="submit-button" type="submit">Apply</button>
                    </form></body></html>"""
                )
                return
            if parsed.path == "/slow":
                delay = min(float(parse_qs(parsed.query).get("delay", ["0"])[0]), 1.0)
                time.sleep(max(delay, 0.0))
                marker = parse_qs(parsed.query).get("session", ["unknown"])[0]
                self._send_html(
                    f"<html><head><title>Slow {marker}</title></head>"
                    f"<body><p id='session-marker'>{marker}</p></body></html>"
                )
                return
            if parsed.path == "/error":
                self._send_html("<html><body>intentional error</body></html>", status=503)
                return
            self._send_html("<html><body>not found</body></html>", status=404)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            site.record(
                RecordedRequest(
                    path=self.path,
                    headers={key: value for key, value in self.headers.items()},
                    body=body,
                )
            )
            self._send_html(
                "<html><head><title>Submitted</title></head>"
                "<body><h1 id='result'>Application submitted</h1></body></html>"
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clean_browser_sessions():
    """Never leak Chrome processes or session state between tests."""
    try:
        import browser_tools
    except ImportError:
        yield
        return
    browser_tools.close_all_sessions()
    yield
    browser_tools.close_all_sessions()
