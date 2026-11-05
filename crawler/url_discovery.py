"""Domain policy and link eligibility decisions."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from config import BINARY_EXTENSIONS, CrawlConfig
from crawler.url_normalizer import UrlNormalizer


class DomainPolicy:
    """Decides whether a host belongs to the target website.

    * Default: the start host and its ``www.`` / non-``www.`` twin.
    * ``include_subdomains``: the start host (minus ``www.``) and every subdomain.
    """

    def __init__(self, start_url: str, include_subdomains: bool = False) -> None:
        host = (urlsplit(start_url).hostname or "").lower().rstrip(".")
        self.base = host[4:] if host.startswith("www.") else host
        self.include_subdomains = include_subdomains
        self._extra_hosts: set[str] = set()

    def adopt(self, host: str) -> None:
        """Accept an additional host (used when the start URL redirects to another host)."""
        self._extra_hosts.add(host.lower().rstrip("."))

    def is_internal_host(self, host: str | None) -> bool:
        if not host:
            return False
        host = host.lower().rstrip(".")
        if host in self._extra_hosts or host == self.base or host == "www." + self.base:
            return True
        return self.include_subdomains and host.endswith("." + self.base)

    def is_internal(self, url: str) -> bool:
        try:
            return self.is_internal_host(urlsplit(url).hostname)
        except ValueError:
            return False


@dataclass(frozen=True)
class Eligibility:
    """Outcome of :meth:`LinkClassifier.check`."""
    crawlable: bool
    reason: str = ""
    normalized: str | None = None


class LinkClassifier:
    """Applies scope, pattern, binary-file and URL-trap rules to a URL."""

    def __init__(self, config: CrawlConfig, policy: DomainPolicy, normalizer: UrlNormalizer) -> None:
        self.config = config
        self.policy = policy
        self.normalizer = normalizer
        self._include = [re.compile(p) for p in config.include_patterns]
        self._exclude = [re.compile(p) for p in config.exclude_patterns]

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def extension(url: str) -> str:
        path = urlsplit(url).path
        ext = posixpath.splitext(path)[1].lstrip(".").lower()
        return ext

    @staticmethod
    def link_type(href: str, url: str | None) -> str:
        """Classify an anchor: page, anchor, mailto, tel, javascript, file, other."""
        h = (href or "").strip().lower()
        if h.startswith("#"):
            return "anchor"
        if h.startswith("mailto:"):
            return "mailto"
        if h.startswith("tel:") or h.startswith("sms:"):
            return "tel"
        if h.startswith("javascript:"):
            return "javascript"
        if url is None:
            return "other"
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            return "other"
        return "file" if LinkClassifier.extension(url) in BINARY_EXTENSIONS else "page"

    def is_trap(self, url: str) -> str | None:
        """Heuristics for infinite/unbounded URL spaces.  Returns a reason or None."""
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) > self.config.max_path_depth:
            return f"path deeper than {self.config.max_path_depth} segments"
        # Repeating path loops such as /a/b/a/b/a/b or /x/x/x
        if len(segments) >= 6:
            for size in (1, 2, 3):
                chunk = segments[-size:]
                if len(segments) >= size * 3 and segments[-size * 3:] == chunk * 3:
                    return "repeating path segments (possible crawl loop)"
        n_params = len([p for p in parts.query.split("&") if p])
        if n_params > self.config.max_query_params:
            return f"more than {self.config.max_query_params} query parameters"
        return None

    # -- main decision ------------------------------------------------------------
    def check(self, url: str, is_start: bool = False) -> Eligibility:
        """Decide whether *url* may be queued for crawling."""
        try:
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            return Eligibility(False, "malformed URL")
        if scheme not in ("http", "https"):
            return Eligibility(False, "unsupported scheme")
        norm = self.normalizer.safe_normalize(url)
        if norm is None:
            return Eligibility(False, "malformed URL")
        if not self.policy.is_internal(norm):
            return Eligibility(False, "external", norm)
        if is_start:
            return Eligibility(True, "", norm)
        if not self.config.include_binary_files and self.extension(norm) in BINARY_EXTENSIONS:
            return Eligibility(False, "binary/non-HTML file", norm)
        for rx in self._exclude:
            if rx.search(norm):
                return Eligibility(False, f"excluded by pattern {rx.pattern}", norm)
        if self._include and not any(rx.search(norm) for rx in self._include):
            return Eligibility(False, "does not match include patterns", norm)
        trap = self.is_trap(norm)
        if trap:
            return Eligibility(False, f"URL trap: {trap}", norm)
        return Eligibility(True, "", norm)
