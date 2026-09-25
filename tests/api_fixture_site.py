"""A local http.server whose page calls its own JSON API on load.

One site (its own port): the page is served on 127.0.0.1 while the "third
party" is the same server addressed as ``localhost`` - another site by
registrable domain - so no test ever reaches the internet. Every API route
carries chosen response headers, cookies and bodies, so the api_report checks
have something to find: Chrome-backed tests read them from a real isolated
load, the rest use the same routes over plain HTTP.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any

CRASH_BODY = (
    "Traceback (most recent call last):\n"
    '  File "/var/www/app/views.py", line 42, in profile\n'
    "    raise ValueError('boom')\n"
    "ValueError: boom\n"
    "Werkzeug/2.3.4 (Python/3.11.2)"
)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>API fixture</title></head>
<body><h1>api fixture</h1>
<script>
function get(url) { return fetch(url).then(function (r) { return r.text(); }).catch(function () {}); }
get("/api/users/123");
get("/api/session");
get("/api/profile");
get("/api/crash");
get("/api/search?token=fixture-secret-token-value&email=qa%40example.test&sid=ABCDEF123456");
get("{third_party}/api/external");
fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
                     body: JSON.stringify({login: "qa", csrf_token: "fixture-csrf"})}).catch(function () {});
fetch("{third_party}/api/external", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({login: "qa"})}).catch(function () {});
</script>
</body></html>
"""


def _json(body: object) -> bytes:
    return json.dumps(body).encode("utf-8")


API_ROUTES: dict[str, tuple[int, bytes, str, list[tuple[str, str]]]] = {
    # path -> (status, body, content_type, extra_headers)
    "/api/users/123": (200, _json({"id": 123, "name": "Ada"}), "application/json", [
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Credentials", "true"),
        ("Access-Control-Allow-Methods", "GET, POST"),
        ("Access-Control-Max-Age", "600"),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]),
    "/api/session": (200, _json({"session": True}), "application/json", [
        ("Set-Cookie", "sid=fixture-sid-value; Path=/"),
        ("Cache-Control", "public, max-age=60"),
        ("X-Content-Type-Options", "nosniff"),
    ]),
    "/api/profile": (200, _json({"login": "qa"}), "application/json", [
        ("Cache-Control", "no-store"),
    ]),
    "/api/crash": (500, CRASH_BODY.encode("utf-8"), "text/plain; charset=utf-8", []),
    "/api/login": (200, _json({"ok": True}), "application/json", [
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]),
    "/api/external": (200, _json({"ok": True}), "application/json", [
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]),
}


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

        def _send(self, status: int, body: bytes, content_type: str,
                  extra: list[tuple[str, str]] = ()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> None:
            path = self.path.split("?", 1)[0]
            site.paths.append(f"{self.command} {self.path}")
            if path == "/":
                html = PAGE.replace("{third_party}", site.third_party)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif path in API_ROUTES:
                status, body, content_type, extra = API_ROUTES[path]
                self._send(status, body, content_type, extra)
            elif path == "/api/search":
                self._send(200, _json([]), "application/json", [
                    ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                ])
            else:
                self._send(404, b"<html><body>not found</body></html>", "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._route()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._route()

        def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            site.paths.append(f"OPTIONS {self.path}")
            self._send(200, b"", "text/plain", [
                ("Access-Control-Allow-Origin", "http://127.0.0.1"),
                ("Access-Control-Allow-Methods", "GET, POST"),
                ("Access-Control-Allow-Headers", "Content-Type"),
                ("Access-Control-Max-Age", "600"),
            ])

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _QuietServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_port}"
    site.third_party = f"http://localhost:{server.server_port}"
    site.server = server
    site.thread = threading.Thread(target=server.serve_forever, daemon=True)
    site.thread.start()
    return site
