"""What this computer looks like from the network: LAN addresses, public IP, location.

The LAN side is read locally and costs nothing. The public side - the address
the internet sees and the coarse location registries attach to it - can only be
learned by asking a service, so it is fetched from keyless public lookup
endpoints, one after another until one answers, and cached: a popup that polls
every second must not turn into a request per second. ``WEB_SEARCH_NEO_GEOIP=0``
turns the lookup off entirely; the location is that of the IP block (city level
at best), never the device's.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import socket
import threading
import time
from typing import Any, Callable
import urllib.request

from web_search_neo.nodes import config

GEOIP_ENV = "WEB_SEARCH_NEO_GEOIP"
CACHE_SECONDS = 15 * 60
FAILURE_CACHE_SECONDS = 60
LOOKUP_TIMEOUT_SECONDS = 4.0
MAX_LOOKUP_BYTES = 64 * 1024


def _num(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _from_ipwho(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("success") is False:
        return None
    connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    timezone = data.get("timezone") if isinstance(data.get("timezone"), dict) else {}
    return {"ip": data.get("ip"), "country": data.get("country"),
            "country_code": data.get("country_code"), "region": data.get("region"),
            "city": data.get("city"), "latitude": _num(data.get("latitude")),
            "longitude": _num(data.get("longitude")), "timezone": timezone.get("id"),
            "isp": connection.get("isp") or connection.get("org")}


def _from_ipapi_co(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("error"):
        return None
    return {"ip": data.get("ip"), "country": data.get("country_name"),
            "country_code": data.get("country_code"), "region": data.get("region"),
            "city": data.get("city"), "latitude": _num(data.get("latitude")),
            "longitude": _num(data.get("longitude")), "timezone": data.get("timezone"),
            "isp": data.get("org")}


def _from_ip_api(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("status") != "success":
        return None
    return {"ip": data.get("query"), "country": data.get("country"),
            "country_code": data.get("countryCode"), "region": data.get("regionName"),
            "city": data.get("city"), "latitude": _num(data.get("lat")),
            "longitude": _num(data.get("lon")), "timezone": data.get("timezone"),
            "isp": data.get("isp") or data.get("org")}


# Keyless endpoints, most complete first. Each answer is normalised to one shape.
PROVIDERS: tuple[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any] | None]], ...] = (
    ("ipwho.is", "https://ipwho.is/", _from_ipwho),
    ("ipapi.co", "https://ipapi.co/json/", _from_ipapi_co),
    ("ip-api.com", "http://ip-api.com/json/", _from_ip_api),
)


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "web-search-neo-node/1"})
    with urllib.request.urlopen(request, timeout=LOOKUP_TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = json.loads(response.read(MAX_LOOKUP_BYTES).decode("utf-8", "replace"))
    if not isinstance(payload, dict):
        raise ValueError("lookup answered with something other than an object")
    return payload


def flag(country_code: object) -> str:
    """The regional-indicator flag for a two-letter country code, else ''."""
    code = str(country_code or "").upper()
    if len(code) != 2 or not code.isalpha() or not code.isascii():
        return ""
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)


class PublicLookup:
    """The cached public-IP/location answer, refreshed at most every few minutes."""

    def __init__(self, fetch: Callable[[str], dict[str, Any]] = _fetch_json) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None
        self._expires = 0.0

    def get(self, refresh: bool = False) -> dict[str, Any]:
        if os.getenv(GEOIP_ENV, "1").strip().lower() in {"0", "false", "no", "off"}:
            return {"disabled": True, "reason": f"{GEOIP_ENV}=0"}
        with self._lock:
            if self._value is not None and not refresh and time.monotonic() < self._expires:
                return dict(self._value)
            errors = []
            for name, url, parse in PROVIDERS:
                try:
                    parsed = parse(self._fetch(url))
                except Exception as exc:  # network, TLS, JSON: try the next provider
                    errors.append(f"{name}: {type(exc).__name__}")
                    continue
                if parsed and parsed.get("ip"):
                    value = {key: item for key, item in parsed.items() if item not in (None, "")}
                    value.update(source=name, flag=flag(value.get("country_code")),
                                 checked_at=round(time.time()))
                    self._value, self._expires = value, time.monotonic() + CACHE_SECONDS
                    return dict(value)
                errors.append(f"{name}: no address in the answer")
            self._value = {"error": "public IP lookup failed", "tried": errors}
            self._expires = time.monotonic() + FAILURE_CACHE_SECONDS
            return dict(self._value)


PUBLIC = PublicLookup()


def _usable(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified)


def primary_lan_address() -> str | None:
    """The source address of the default route; a UDP connect sends nothing."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routed like the internet, never reached
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return address if _usable(address) else None


def _lan_rank(address: str) -> int:
    """Home and office networks first: the address another computer most likely shares.

    The default route is often a VPN adapter, and virtual switches (WSL, Hyper-V,
    overlay VPNs) add private-looking addresses nobody else is on, so the order
    is by the kind of network rather than by which adapter routes the internet.
    """
    if address.startswith("192.168."):
        return 0
    if address.startswith("10."):
        return 1
    if re.match(r"172\.(1[6-9]|2\d|3[01])\.", address):
        return 2
    return 3


def lan_addresses() -> list[str]:
    """Every IPv4 address of this machine other than loopback, likeliest LAN first."""
    found: list[str] = []
    primary = primary_lan_address()
    if primary:
        found.append(primary)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        address = str(info[4][0])
        if _usable(address) and address not in found:
            found.append(address)
    return sorted(found, key=_lan_rank)  # stable: the default route leads its own rank


def local_identity(version: str = "", include_public: bool = True,
                   refresh: bool = False) -> dict[str, Any]:
    """This machine as a node: name, OS, LAN addresses, and (cached) public IP/location."""
    addresses = lan_addresses()
    identity: dict[str, Any] = {
        "name": config.node_name(),
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "lan_ips": addresses,
        "primary_ip": addresses[0] if addresses else None,
        "default_route_ip": primary_lan_address(),
        "node_port": config.listen_port(),
    }
    if version:
        identity["version"] = version
    if include_public:
        identity["public"] = PUBLIC.get(refresh=refresh)
    return identity


def place(public: dict[str, Any] | None) -> str:
    """"City, Region, Country" from a public lookup, skipping what is missing."""
    if not isinstance(public, dict):
        return ""
    parts = []
    for key in ("city", "region", "country"):
        value = str(public.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)
