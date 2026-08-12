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
FIXTURE_ROOT = (PROJECT_ROOT / "tests" / "fixtures").resolve()
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

        def _send_file(self, relative: str) -> None:
            candidate = (FIXTURE_ROOT / relative).resolve()
            if not candidate.is_file() or FIXTURE_ROOT not in candidate.parents:
                self._send_html("<html><body>fixture not found</body></html>", status=404)
                return
            payload = candidate.read_bytes()
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(candidate.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_redirect(self, location: str, status: int) -> None:
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path.startswith("/fixtures/"):
                self._send_file(parsed.path[len("/fixtures/") :])
                return
            if parsed.path == "/redirect-loop":
                self._send_redirect("/redirect-loop", 302)
                return
            if parsed.path == "/redirect":
                query = parse_qs(parsed.query)
                self._send_redirect(
                    query.get("to", ["/relative"])[0],
                    int(query.get("status", ["302"])[0]),
                )
                return
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
            if parsed.path == "/game":
                self._send_html(
                    """<!doctype html>
                    <html><head><title>Canvas game fixture</title></head><body>
                    <canvas id="game-canvas" tabindex="0" width="640" height="360"
                      style="display:block;width:640px;height:360px"></canvas>
                    <script>
                    const canvas = document.getElementById('game-canvas');
                    const context = canvas.getContext('2d');
                    context.fillStyle = '#0b1739'; context.fillRect(0, 0, 640, 360);
                    window.gameEvents = [];
                    for (const name of ['pointerdown', 'pointerup', 'pointermove']) {
                      canvas.addEventListener(name, event => window.gameEvents.push({
                        type: name, x: Math.round(event.offsetX), y: Math.round(event.offsetY),
                        frame: window.frameCount
                      }));
                    }
                    for (const name of ['keydown', 'keyup']) {
                      document.addEventListener(name, event => window.gameEvents.push({
                        type: name, key: event.key, code: event.code,
                        frame: window.frameCount
                      }), true);
                    }
                    window.frameCount = 0;
                    const cachedRequestAnimationFrame = window.requestAnimationFrame;
                    function animate() { window.frameCount += 1; cachedRequestAnimationFrame(animate); }
                    cachedRequestAnimationFrame(animate);
                    </script></body></html>"""
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
