"""A local http.server with CORS, methods, redirects and reflections to probe.

One site (its own port): the page on 127.0.0.1, "elsewhere" on ``localhost`` -
another site by registrable domain - so the open-redirect chain really leaves
the site without ever reaching the internet. ``/api/echo-cors`` reflects the
request's Origin like a misconfigured API would; ``/redir`` bounces to its
``to`` parameter; ``/search`` mirrors the query back into the page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any
from urllib.parse import parse_qsl, urlsplit

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>active fixture</title></head>
<body><h1>active fixture</h1>
<a id="away" href="/redir?to={third_party}/landing">away</a>
<a id="local" href="/page2">local</a>
<a id="query" href="/search?q=hello">query</a>
<form action="/search" method="get"><input name="q" value="hi"></form>
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
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            site.paths.append(f"{self.command} {self.path}")
            if path == "/":
                self._send(200, PAGE.replace("{third_party}", site.third_party).encode("utf-8"),
                            "text/html; charset=utf-8")
            elif path == "/page2":
                self._send(200, b"<html><body>two</body></html>", "text/html; charset=utf-8")
            elif path == "/landing":
                self._send(200, b"<html><body>elsewhere</body></html>", "text/html; charset=utf-8")
            elif path == "/redir":
                target = dict(parse_qsl(query)).get("to", site.third_party + "/landing")
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/search":
                params = dict(parse_qsl(query))
                seen = " ".join(f"{name}={value}" for name, value in params.items())
                body = (f"<html><body><p>results for {seen}</p>"
                        f'<a href="/search?q={params.get("q", "")}">more</a></body></html>').encode()
                self._send(200, body, "text/html; charset=utf-8")
            elif path == "/api/echo-cors":
                self._send(200, b'{"ok": true}', "application/json")
            else:
                self._send(404, b"<html><body>not found</body></html>", "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._route()

        def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            site.paths.append(f"OPTIONS {self.path}")
            origin = self.headers.get("Origin", "")
            path = self.path.split("?", 1)[0]
            extra = [("Allow", "GET, POST, OPTIONS")]
            if path == "/api/echo-cors" and origin:
                extra += [("Access-Control-Allow-Origin", origin),
                          ("Access-Control-Allow-Credentials", "true"),
                          ("Access-Control-Allow-Methods", "*"),
                          ("Access-Control-Allow-Headers", "*"),
                          ("Access-Control-Max-Age", "100")]
            self._send(200, b"", "text/plain", extra)

        def do_TRACE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            site.paths.append(f"TRACE {self.path}")
            self._send(200, f"TRACE {self.path}".encode(), "message/http")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _QuietServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_port}"
    site.third_party = f"http://localhost:{server.server_port}"
    site.server = server
    site.thread = threading.Thread(target=server.serve_forever, daemon=True)
    site.thread.start()
    return site
