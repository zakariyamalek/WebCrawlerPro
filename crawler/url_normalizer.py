"""URL normalisation used for duplicate detection.

Rules (see README "URL normalisation"):

* scheme and host lower-cased, default ports (80/443) removed, IDNA host kept
* fragments removed (``/page#top`` == ``/page``)
* percent-encoding normalised (unreserved characters decoded, hex upper-cased,
  non-ASCII characters encoded as UTF-8)
* dot-segments resolved (``/a/./b/../c`` -> ``/a/c``) and duplicate slashes collapsed
* trailing slash removed except for the root path
* query parameters: tracking/session parameters removed, exact duplicate
  ``key=value`` pairs removed, remaining parameters **sorted** (order never changes
  a page) but **never merged** - ``?id=1`` and ``?id=2`` stay different pages.
"""
from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

_UNRESERVED = re.compile(r"^[A-Za-z0-9\-._~]$")
_PCT = re.compile(r"%([0-9a-fA-F]{2})")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_PATH_SAFE = "/:@!$&'()*+,;=-._~%"


def _normalize_percent(path: str) -> str:
    """Decode %XX of unreserved chars, upper-case the rest, encode raw non-ASCII."""
    def repl(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if _UNRESERVED.match(char) else "%" + match.group(1).upper()

    path = _PCT.sub(repl, path)
    return quote(path, safe=_PATH_SAFE)


class UrlNormalizer:
    """Stateless URL normaliser configured with the parameters to strip."""

    def __init__(self, strip_params: list[str] | None = None) -> None:
        self.strip_params = {p.lower() for p in (strip_params or [])}

    def join(self, base: str, href: str) -> str | None:
        """Resolve *href* against *base* (relative -> absolute).  None if invalid."""
        href = (href or "").strip()
        if not href:
            return None
        try:
            return urljoin(base, href)
        except ValueError:
            return None

    def normalize(self, url: str) -> str:
        """Return the canonical form of *url* (raises ValueError if unparsable)."""
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower().rstrip(".")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        port = parts.port
        netloc = host
        if port and _DEFAULT_PORTS.get(scheme) != port:
            netloc = f"{host}:{port}"

        path = _normalize_percent(parts.path or "/")
        # Collapse duplicate slashes and resolve dot segments.
        path = re.sub(r"/{2,}", "/", path)
        # normpath also drops the trailing slash (root "/" is preserved).
        path = posixpath.normpath(path) if path != "/" else "/"
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
        if not path.startswith("/"):
            path = "/" + path

        query = self._normalize_query(parts.query)
        return urlunsplit((scheme, netloc, path, query, ""))

    def _normalize_query(self, query: str) -> str:
        if not query:
            return ""
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False)
        kept: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for key, value in pairs:
            if key.lower() in self.strip_params:
                continue
            pair = (key, value)
            if pair in seen:
                continue
            seen.add(pair)
            kept.append(pair)
        kept.sort()
        return urlencode(kept, doseq=False, quote_via=quote)

    def safe_normalize(self, url: str) -> str | None:
        try:
            return self.normalize(url)
        except (ValueError, UnicodeError):
            return None

    @staticmethod
    def decoded_path(url: str) -> str:
        return unquote(urlsplit(url).path)
