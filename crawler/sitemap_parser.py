"""Sitemap and sitemap-index parsing (plain XML and .gz)."""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET


@dataclass
class SitemapResult:
    """Parsed content of one sitemap document."""
    is_index: bool = False
    urls: list[str] = field(default_factory=list)       # page URLs (<urlset>)
    sitemaps: list[str] = field(default_factory=list)   # nested sitemaps (<sitemapindex>)


def _maybe_gunzip(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError):
            return b""
    return body


def parse_sitemap(body: bytes, max_bytes: int = 50 * 1024 * 1024) -> SitemapResult:
    """Parse a sitemap body.  Malformed or non-XML input yields an empty result.

    DOCTYPE/ENTITY declarations are rejected to avoid XML entity-expansion attacks.
    """
    body = _maybe_gunzip(body)
    if not body or len(body) > max_bytes:
        return SitemapResult()
    head = body[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        return SitemapResult()
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return SitemapResult()

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    result = SitemapResult()
    root_name = local(root.tag)
    if root_name == "sitemapindex":
        result.is_index = True
        for node in root:
            if local(node.tag) == "sitemap":
                for child in node:
                    if local(child.tag) == "loc" and child.text and child.text.strip():
                        result.sitemaps.append(child.text.strip())
    elif root_name == "urlset":
        for node in root:
            if local(node.tag) == "url":
                for child in node:
                    if local(child.tag) == "loc" and child.text and child.text.strip():
                        result.urls.append(child.text.strip())
    return result


_SITEMAP_PATH = re.compile(r"sitemap.*\.(xml|xml\.gz)$", re.I)


def looks_like_sitemap(url: str) -> bool:
    return bool(_SITEMAP_PATH.search(url.split("?", 1)[0]))
