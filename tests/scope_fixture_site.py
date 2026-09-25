"""Small local servers with fixed routes, for scope, crawl and dev-mode tests.

Each ``RouteSite`` is its own ``http.server`` on its own port (a different
"host" to the checks), optionally over TLS with a self-signed certificate for
localhost and 127.0.0.1 that is generated once per test run (``cryptography``,
which the MCP SDK already depends on) - no private key lives in the repository.
Every request path is recorded so a test can prove what was, and was not,
requested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import ssl
import tempfile
import threading
from typing import Any

Route = tuple[int, list[tuple[str, str]], str]


@functools.lru_cache(maxsize=1)
def self_signed_pair() -> tuple[Path, Path]:
    """(certificate, key) files of a fresh self-signed localhost certificate, made once per run."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    folder = Path(tempfile.mkdtemp(prefix="wsn-test-tls-"))
    cert_path, key_path = folder / "localhost.crt", folder / "localhost.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        return


@dataclass
class RouteSite:
    routes: dict[str, Route]
    tls: bool = False
    base_url: str = ""
    port: int = 0
    seen: list[str] = field(default_factory=list)
    server: Any = None
    thread: Any = None

    def start(self) -> "RouteSite":
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                site.seen.append(self.path)
                status, headers, body = site.routes.get(self.path.split("?", 1)[0], (404, [], "not found"))
                payload = body.replace("{base}", site.base_url).encode("utf-8")
                self.send_response(status)
                names = {name.lower() for name, _ in headers}
                if "content-type" not in names:
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in headers:
                    self.send_header(name, value.replace("{base}", site.base_url))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        if self.tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*self_signed_pair())
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self.base_url = f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def page(links: list[str] = (), extra: str = "") -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return f"<!doctype html><html><head><title>t</title></head><body>{anchors}{extra}</body></html>"


HARDENED = [
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Embedder-Policy", "require-corp"),
]
