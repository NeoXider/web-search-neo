"""Transport: the page's own response, http->https redirection, the TLS certificate.

Every request here is one an ordinary visitor's browser makes too: a GET of the
page, a GET of ``http://host/`` (typing the domain without https), one TLS
handshake with the default client settings, and GETs of the two public
well-known files. No protocol or cipher enumeration, no other paths.

Redirection is judged the way Mozilla HTTP Observatory (v1.7.1) judges it, on
the route of the ``http://host/`` request: no answer 0, a certificate error on
the way -20, a single hop (no redirect) -20, a final URL that is not https -20,
a first redirect to another http URL -10, a first redirect from http to https
on another host -5, otherwise 0. The preload-list result is not available (the
list is not looked up).
"""
from __future__ import annotations

import socket
import ssl
import time
import warnings
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from web_search_neo.audit.findings import finding, info, passed
from web_search_neo.audit.scope import redact_text
from web_search_neo.fetch.decoding import decode_response
from web_search_neo.web_client import classify_destination, is_local_host, request

PAGE_BYTES = 2_000_000
SMALL_BYTES = 65_536
Fetcher = Callable[..., Any]


def _lower_headers(response: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in dict(getattr(response, "headers", None) or {}).items()}


def _set_cookie_lines(response: Any) -> list[str]:
    raw = getattr(getattr(response, "raw", None), "headers", None)
    listed = raw.getlist("Set-Cookie") if hasattr(raw, "getlist") else None
    if isinstance(listed, (list, tuple)):
        return [str(item) for item in listed]
    value = _lower_headers(response).get("set-cookie")
    return [value] if value else []


def is_certificate_error(error: str | None) -> bool:
    """A fetch that failed on certificate verification rather than on the network."""
    text = str(error or "")
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text.lower() or (
        "SSLError" in text and "certificate" in text.lower())


def fetch(url: str, timeout: float, *, allow_plain_http: bool = False, max_bytes: int = PAGE_BYTES,
          plain_http_hosts: Any = frozenset(), follow: Callable[[str], bool] | None = None,
          verify: bool = True, client: Fetcher = request) -> dict[str, Any]:
    """GET one URL (redirects followed and validated hop by hop); errors come back as data.

    ``route`` is every URL requested, the first included; ``not_followed`` is a
    redirect target that was not requested - refused by ``follow`` (outside the
    scope or the budget) or by the client's own rules (``not_followed_reason``:
    plain http to a public host, a public-to-private hop), the chain up to it kept.
    ``verify=False`` reads an https origin whose certificate already failed a
    verified request (the retry and every later read of that origin, see
    ``report.Checker``), so its pages can still be judged; ``verified`` says which.
    """
    started = time.monotonic()
    extra: dict[str, Any] = {}
    if plain_http_hosts:
        extra["plain_http_hosts"] = plain_http_hosts
    if follow is not None:
        extra["follow"] = follow
    if not verify:
        extra["verify"] = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the unverified retry is deliberate and reported
            response = client(url, method="GET", timeout_seconds=timeout, max_response_bytes=max_bytes,
                              allow_plain_http=allow_plain_http, **extra)
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is None or not isinstance(getattr(response, "status_code", None), int):
            return {"url": url, "route": [url], "error": redact_text(f"{type(exc).__name__}: {exc}")[:400],
                    "verified": verify,
                    "ms": round((time.monotonic() - started) * 1000)}
    history = list(getattr(response, "history", None) or [])
    hops = [{"url": str(getattr(hop, "url", "")), "status": int(getattr(hop, "status_code", 0) or 0),
             "location": _lower_headers(hop).get("location")} for hop in history]
    cookies = [line for hop in history for line in _set_cookie_lines(hop)] + _set_cookie_lines(response)
    try:
        body, _charset = decode_response(response)
    except Exception:
        body = ""
    final = str(getattr(response, "url", "") or url)
    out = {
        "url": url, "final_url": final, "route": [hop["url"] for hop in hops] + [final],
        "status": int(response.status_code), "headers": _lower_headers(response),
        "set_cookies": cookies, "final_set_cookies": _set_cookie_lines(response), "hops": hops, "body": body,
        "verified": verify,
        "truncated": getattr(response, "wsn_truncated", False) is True,
        "ms": round((time.monotonic() - started) * 1000),
    }
    stopped = getattr(response, "wsn_not_followed", None)
    if isinstance(stopped, str):
        out["not_followed"] = stopped
        reason = getattr(response, "wsn_not_followed_reason", None)
        if isinstance(reason, str):
            out["not_followed_reason"] = redact_text(reason)[:300]
    return out


def http_variant(url: str) -> str | None:
    """``http://host/`` for an https URL on the default port, else None.

    Only the bare host is probed: the caller's path and query (which may carry a
    token) are never sent over plain http. Observatory requests the site's path;
    for a site's front page the two are the same request.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.port not in (None, 443):
        return None
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit(("http", netloc, "/", "", ""))


def https_variant(url: str) -> str | None:
    """``https://host/`` for an http URL on the default port (the HSTS read), else None."""
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.port not in (None, 80):
        return None
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", netloc, "/", "", ""))


def _redirect(fid: str, status: str, severity: str, title: str, points: int, *, detail: str = "",
              fix: str = "", evidence: Any = None) -> list[dict[str, Any]]:
    return [finding(fid, "transport", status, severity, title, detail=detail, fix=fix, evidence=evidence,
                    points=points)]


def analyze_redirect(target: str, probe: dict[str, Any] | None, *, requested_host: str | None = None) -> list[dict[str, Any]]:
    """Observatory's redirection result for the ``http://host/`` route (``probe``)."""
    parts = urlsplit(target)
    host = (requested_host or parts.hostname or "").lower()
    local = is_local_host(host)
    out: list[dict[str, Any]] = []
    if parts.scheme == "http":
        out.append(finding(
            "https-missing", "transport", "fail", "high", "The page is served over plain http",
            detail="Everything - pages, forms, cookies - can be read and changed on the network"
                   + (" (a local development address: judge the production deployment)" if local else "") + ".",
            fix="Serve the site over https (a free certificate from Let's Encrypt) and redirect http to it."))
    if probe is None:
        return out + [info("redirect-not-checked", "transport", "http->https redirect not checked",
                           detail="The URL uses a custom port, so there is no matching plain-http address.")]
    if not probe.get("error") and (probe.get("verified") is False or probe.get("certificate_error")):
        return out + _redirect("redirect-invalid-cert", "fail", "high",
                               "The http:// redirect ends on an untrusted certificate", -20,
                               fix="Fix the certificate of the https host the redirect goes to.",
                               evidence=str(probe.get("certificate_error") or "read without verification")[:300])
    if probe.get("error") and not probe.get("hops"):
        if is_certificate_error(probe["error"]):
            return out + _redirect("redirect-invalid-cert", "fail", "high",
                                   "The http:// redirect ends on an untrusted certificate", -20,
                                   fix="Fix the certificate of the https host the redirect goes to.",
                                   evidence=probe["error"][:300])
        return out + _redirect("redirect-no-http", "pass", "info", "Plain http is not served", 0,
                               detail="The http:// address did not answer, so nothing is served unencrypted.",
                               evidence=probe["error"][:200])
    route = list(probe.get("route") or [probe.get("url")])
    if probe.get("not_followed"):
        route.append(probe["not_followed"])
    chain = {"route": route, **({"not_followed": probe["not_followed"]} if probe.get("not_followed") else {})}
    if len(route) == 1:
        return out + _redirect("redirect-missing", "fail", "medium", "http:// serves the page without redirecting", -20,
                               detail="A visitor who types the domain stays on unencrypted http.",
                               fix="Answer every http:// request with 301 to the same https:// URL.", evidence=chain)
    first, second, last = (urlsplit(u) for u in (route[0], route[1], route[-1]))
    cut = bool(probe.get("not_followed")) and last.scheme != "https"
    if cut:
        # The chain stops at a hop the report does not request (outside the scope, over the
        # budget, refused by the client): where it would end is unknown, so "never reaches
        # https" cannot be claimed. What is known - the hops so far - is graded.
        chain["cut"] = ("chain cut at the scope boundary: the hop to " + route[-1]
                        + " was not requested, so the rest of the chain is not known")
        if second.scheme == "http":
            return out + _redirect("redirect-initial-http", "fail", "low", "The first redirect stays on http", -10,
                                   detail="The hop before https is readable and changeable on the network. The "
                                          "chain was cut at the scope boundary; add its hosts to the scope (hosts, "
                                          "include_subdomains) to see where it ends.",
                                   fix="Make the first redirect go straight to https:// on the same host.",
                                   evidence=chain)
        route = route[:-1]
        last = urlsplit(route[-1])
    if last.scheme != "https" and not cut:
        return out + _redirect("redirect-not-https", "fail", "medium", "The http:// redirect chain never reaches https",
                               -20, fix="Redirect http:// to https:// on the same host (301).", evidence=chain)
    if second.scheme == "http":
        return out + _redirect("redirect-initial-http", "fail", "low", "The first redirect stays on http", -10,
                               detail="The hop before https is readable and changeable on the network.",
                               fix="Make the first redirect go straight to https:// on the same host.",
                               evidence=chain)
    if first.scheme == "http" and second.scheme == "https" and (first.hostname or "") != (second.hostname or ""):
        return out + _redirect("redirect-off-host", "warn", "low",
                               "The first redirect leaves the host while switching to https", -5,
                               detail="The HSTS header of this exact host is then never set by the redirect.",
                               fix="Redirect http://host to https://host first, then elsewhere.", evidence=chain)
    return out + [passed("redirect-ok", "transport", "http:// redirects to https://", evidence=chain)]


def _name(parts: Any) -> dict[str, str]:
    flat: dict[str, str] = {}
    for group in parts or ():
        for key, value in group:
            flat[key] = value
    return {key: flat[key] for key in ("commonName", "organizationName", "countryName") if key in flat}


def certificate(host: str, port: int, timeout: float) -> dict[str, Any]:
    """One ordinary, verifying TLS handshake - what a browser sees; no scanning."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                protocol = tls.version()
                cipher = (tls.cipher() or (None,))[0]
    except ssl.SSLCertVerificationError as exc:
        message = str(getattr(exc, "verify_message", "") or exc)[:300]
        return {"valid": False, "error": message, "self_signed": "self-signed" in message or "self signed" in message}
    except (OSError, ssl.SSLError) as exc:
        return {"valid": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    not_after = cert.get("notAfter")
    days_left = None
    if not_after:
        try:
            days_left = int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)
        except ValueError:
            days_left = None
    alt = [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"]
    return {"valid": True, "protocol": protocol, "cipher": cipher, "issuer": _name(cert.get("issuer")),
            "subject": _name(cert.get("subject")), "not_before": cert.get("notBefore"),
            "not_after": not_after, "days_left": days_left, "san": alt[:20], "san_total": len(alt)}


def analyze_certificate(cert: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Certificate findings; like Observatory, the certificate itself is not scored.

    An untrusted one costs its points through HSTS and redirection instead.
    """
    if not cert:
        return []
    if cert.get("valid") is False:
        kind = "self-signed" if cert.get("self_signed") else "not trusted"
        return [finding("tls-invalid", "transport", "fail", "high", f"The TLS certificate is {kind}",
                        detail="Browsers show a full-page warning instead of the site. The rest of this report "
                               "was read over one unverified retry, as Observatory does.",
                        fix="Install a certificate for this exact host name from a public CA, with the full chain, "
                            "and automate renewal.", evidence=cert.get("error"))]
    if cert.get("valid") is None:
        return [info("tls-unread", "transport", "The certificate could not be read", evidence=cert.get("error"))]
    days = cert.get("days_left")
    evidence = {key: cert.get(key) for key in ("issuer", "subject", "not_after", "days_left", "protocol")}
    if isinstance(days, int) and days < 14:
        return [finding("tls-expiring", "transport", "fail", "high", f"The certificate expires in {days} day(s)",
                        fix="Renew it now and automate renewal (certbot / ACME client with a timer).",
                        evidence=evidence)]
    if isinstance(days, int) and days < 30:
        return [finding("tls-expiring-soon", "transport", "warn", "medium",
                        f"The certificate expires in {days} days",
                        fix="Check that automatic renewal runs.", evidence=evidence)]
    return [passed("tls-ok", "transport", "The certificate is trusted and not near expiry", evidence=evidence)]


def destination_error(url: str) -> str | None:
    """Metadata and link-local addresses are refused before anything is sent."""
    try:
        classify_destination(url)
    except ValueError as exc:
        return str(exc)


def options(url: str, timeout: float, method: str = "OPTIONS",
            headers: dict[str, str] | None = None, *,
            allow_plain_http: bool = False, plain_http_hosts: Any = frozenset(),
            verify: bool = True, client: Fetcher = request) -> dict[str, Any]:
    """One non-GET request (an OPTIONS preflight, a TRACE probe); errors come back as data.

    ``method`` is "OPTIONS" or "TRACE" - the only two ``active_probe`` sends.
    Only status, response headers and timing are kept: no cookies, no body.
    """
    started = time.monotonic()
    extra: dict[str, Any] = {}
    if plain_http_hosts:
        extra["plain_http_hosts"] = plain_http_hosts
    if not verify:
        extra["verify"] = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the unverified retry is deliberate and reported
            response = client(url, method=method, timeout_seconds=timeout, max_response_bytes=0,
                              allow_plain_http=allow_plain_http,
                              headers=headers or {}, **extra)
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is None or not isinstance(getattr(response, "status_code", None), int):
            return {"url": url, "error": redact_text(f"{type(exc).__name__}: {exc}")[:400],
                    "verified": verify, "ms": round((time.monotonic() - started) * 1000)}
    return {
        "url": url, "final_url": str(getattr(response, "url", "") or url),
        "status": int(response.status_code), "headers": _lower_headers(response),
        "verified": verify, "ms": round((time.monotonic() - started) * 1000),
    }
    return None
