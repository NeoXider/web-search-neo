"""A local http.server whose page ships JS bundles with planted fake secrets.

One site (its own port): the page and its own bundle on 127.0.0.1, a vendor
bundle on ``localhost`` - another site by registrable domain, named but never
fetched - plus an API description the page references and a sign-in form, so
no test ever reaches the internet. Every secret value is an obvious fake.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any

APP_JS = """// fixture bundle: every secret below is fake.
const AWS_KEY = "AKIAIOSFODNN7EXAMPLE";
const apiKey = "ghp_fixturetoken0123456789abcdef";
const session = "Ab3dEf7hIj2kLm5nOp8Qr0sT3uV1wX4yZ6";
const INTERNAL = "http://10.0.0.5/internal/status";
fetch("/api/users/123").then(r => r.json()).catch(() => {});
const axios = {post: () => Promise.resolve({})};
axios.post("/api/login", {u: 1});
fetch("{third_party}/api/external").catch(() => {});
//# sourceMappingURL=/static/app.js.map
"""

VENDOR_JS = "// third-party bundle: named in reports, never fetched.\n"

OPENAPI = {"openapi": "3.0.0", "info": {"title": "fixture", "version": "1"},
           "paths": {"/api/users": {}, "/api/login": {}}}

APP_MAP = {"version": 3, "sources": ["src/app.ts", "src/secret.ts"],
           "sourcesContent": ["console.log(1)", "const fixtureSource = 1"],
           "mappings": ""}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>secret fixture</title>
<script src="/static/app.js"></script>
<script src="{third_party}/static/vendor.js"></script>
<link rel="openapi" href="/openapi.json">
</head><body><h1>secret fixture</h1>
<form action="/login" method="post"><input type="password" name="pw"><button type="submit" id="go">go</button></form>
</body></html>
"""


@dataclass
class FixtureSite:
    base_url: str
    third_party: str
    server: Any = None
    thread: Any = None
    paths: list[str] = field(default_factory=list)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _QuietServer(ThreadingHTTPServer):
    """Chrome resets keep-alive connections when a session closes; that is not an error here."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


def start() -> FixtureSite:
    site = FixtureSite(base_url="", third_party="")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> None:
            path = self.path.split("?", 1)[0]
            site.paths.append(f"{self.command} {self.path}")
            if path == "/":
                html = PAGE.replace("{third_party}", site.third_party)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/static/app.js":
                self._send(200, APP_JS.replace("{third_party}", site.third_party).encode("utf-8"),
                            "text/javascript; charset=utf-8")
            elif path == "/static/vendor.js":
                self._send(200, VENDOR_JS.encode("utf-8"), "text/javascript; charset=utf-8")
            elif path == "/openapi.json":
                self._send(200, json.dumps(OPENAPI).encode("utf-8"), "application/json")
            elif path == "/api/users/123":
                self._send(200, b'{"id": 123}', "application/json")
            elif path == "/favicon.ico":
                self._send(200, b"", "image/x-icon")
            elif path == "/static/app.js.map":
                self._send(200, json.dumps(APP_MAP).encode("utf-8"), "application/json")
            else:
                self._send(404, b"<html><body>not found</body></html>", "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._route()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            site.paths.append(f"POST {self.path}")
            if path == "/login" and b"pw=" in body:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", "fixture-session=logged-in; Path=/; SameSite=Lax")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/login":
                self._send(200, b'{"ok": true}', "application/json")
            elif path == "/api/login":
                self._send(200, b'{"ok": true}', "application/json")
            else:
                self._send(404, b"<html><body>not found</body></html>", "text/html; charset=utf-8")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _QuietServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_port}"
    site.third_party = f"http://localhost:{server.server_port}"
    site.server = server
    site.thread = threading.Thread(target=server.serve_forever, daemon=True)
    site.thread.start()
    return site
