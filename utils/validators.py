"""Input validation and SSRF protection.

``check_url_safe`` resolves the hostname and rejects any destination that is not
a public, globally routable address (localhost, RFC1918, link-local such as the
cloud metadata address 169.254.169.254, carrier-grade NAT, multicast, reserved,
IPv4-mapped IPv6 pointing at those ranges, ...).  It is applied

* to the start URL,
* to every discovered URL (downloader middleware),
* to every redirect hop (redirects re-enter the downloader middleware chain),
* to the resolved IP at connection time (``SafeResolver``), which defeats DNS
  rebinding, and
* to every sub-request made by the Playwright browser.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa")
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata.google.internal"}
_DNS_TTL_SECONDS = 60.0

_dns_cache: dict[str, tuple[float, list[str]]] = {}
_dns_lock = threading.Lock()


@dataclass(frozen=True)
class UrlCheck:
    """Result of a URL safety check."""
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:  # allows ``if check_url_safe(...):``
        return self.ok


def ip_is_public(ip: str) -> bool:
    """Return True only for globally routable unicast addresses."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr.teredo is not None:
            return False
    if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified):
        return False
    # ``is_global`` also excludes carrier-grade NAT 100.64.0.0/10 and similar.
    return bool(addr.is_global)


def _resolve(host: str) -> list[str]:
    """Resolve *host* to all of its IPs (cached briefly)."""
    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(host)
        if cached and now - cached[0] < _DNS_TTL_SECONDS:
            return cached[1]
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips = sorted({info[4][0] for info in infos})
    with _dns_lock:
        _dns_cache[host] = (now, ips)
    return ips


def clear_dns_cache() -> None:
    with _dns_lock:
        _dns_cache.clear()


def validate_url_syntax(url: str) -> UrlCheck:
    """Validate scheme/host syntax only (no DNS)."""
    if not isinstance(url, str) or not url.strip():
        return UrlCheck(False, "URL is empty")
    if len(url) > 4096:
        return UrlCheck(False, "URL is too long")
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        _ = parts.port  # raises ValueError on a bad port
    except ValueError as exc:
        return UrlCheck(False, f"malformed URL ({exc})")
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return UrlCheck(False, f"unsupported scheme {parts.scheme!r} (only http/https)")
    if not host:
        return UrlCheck(False, "URL has no host")
    if parts.username or parts.password:
        return UrlCheck(False, "URLs containing credentials are not allowed")
    return UrlCheck(True)


def check_url_safe(url: str, allow_private: bool = False, resolve: bool = True) -> UrlCheck:
    """Full validation: syntax + SSRF protection (host name and resolved IPs)."""
    syntax = validate_url_syntax(url)
    if not syntax.ok:
        return syntax
    if allow_private:
        return UrlCheck(True)
    host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return UrlCheck(False, f"blocked internal host name {host!r}")
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        if not ip_is_public(host):
            return UrlCheck(False, f"blocked non-public IP address {host}")
        return UrlCheck(True)
    if not resolve:
        return UrlCheck(True)
    try:
        ips = _resolve(host)
    except socket.gaierror as exc:
        return UrlCheck(False, f"DNS resolution failed for {host!r} ({exc})")
    except OSError as exc:
        return UrlCheck(False, f"DNS error for {host!r} ({exc})")
    if not ips:
        return UrlCheck(False, f"{host!r} did not resolve to any address")
    bad = [ip for ip in ips if not ip_is_public(ip)]
    if bad:
        return UrlCheck(False, f"{host!r} resolves to non-public address {bad[0]}")
    return UrlCheck(True)


def normalize_start_url(raw: str) -> str:
    """Add ``https://`` to bare domains (``example.com`` -> ``https://example.com``)."""
    raw = (raw or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    return raw


def validate_start_url(raw: str, allow_private: bool = False) -> tuple[str, UrlCheck]:
    """Normalise and fully validate the user-supplied start URL."""
    url = normalize_start_url(raw)
    return url, check_url_safe(url, allow_private=allow_private)
