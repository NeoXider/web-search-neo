"""A local http.server whose pages carry chosen security headers, cookies and markup.

Two independent sites (their own ports, their own well-known files) let one test
compare a weak and a strong configuration. The "third party" is the same server
addressed as ``localhost`` while the page is ``127.0.0.1`` - another site by
registrable domain - so no test ever reaches the internet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "security"

WEAK_HEADERS = [
    ("X-Powered-By", "PHP/8.1.2"),
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Credentials", "true"),
    ("Referrer-Policy", "unsafe-url"),
    ("Set-Cookie", "sessionid=abc123; Path=/"),
    ("Set-Cookie", "prefs=dark; Path=/; Domain=127.0.0.1; SameSite=None"),
]
STRONG_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
              "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
STRONG_HEADERS = [
    ("Content-Security-Policy", STRONG_CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Set-Cookie", "__Host-sid=xyz; Path=/; Secure; HttpOnly; SameSite=Strict"),
]
SECURITY_TXT = "Contact: mailto:security@example.test\nExpires: 2099-01-01T00:00:00Z\nPreferred-Languages: en, ru\n"
ROBOTS_TXT = "User-agent: *\nDisallow: /admin\nDisallow: /private\nSitemap: /sitemap.xml\n"


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


def start(page: str, headers: list[tuple[str, str]], *, well_known: bool) -> FixtureSite:
    site = FixtureSite(base_url="", third_party="")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: bytes, content_type: str, extra: list[tuple[str, str]] = ()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0]
            site.paths.append(path)
            if path in {"/", "/page"}:
                html = (FIXTURES / page).read_text(encoding="utf-8").replace("{third_party}", site.third_party)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", headers)
            elif path == "/perf":
                self._send(200, (FIXTURES / "perf.html").read_bytes(), "text/html; charset=utf-8")
            elif path.startswith("/fixtures/security/") and (FIXTURES / path.rsplit("/", 1)[-1]).is_file():
                name = path.rsplit("/", 1)[-1]
                kind = {"js": "text/javascript", "css": "text/css", "html": "text/html"}[name.rsplit(".", 1)[-1]]
                self._send(200, (FIXTURES / name).read_bytes(), kind + "; charset=utf-8")
            elif well_known and path == "/.well-known/security.txt":
                self._send(200, SECURITY_TXT.encode(), "text/plain; charset=utf-8")
            elif well_known and path == "/robots.txt":
                self._send(200, ROBOTS_TXT.encode(), "text/plain; charset=utf-8")
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
