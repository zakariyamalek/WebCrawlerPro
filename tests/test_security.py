"""SSRF protection and validation tests (no real network needed)."""
from __future__ import annotations

import pytest
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request

from crawler.scrapy_components import DomainRestrictionMiddleware, SSRFProtectionMiddleware, SafeResolver
from crawler.url_discovery import DomainPolicy
from utils.validators import check_url_safe, ip_is_public, validate_start_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8000/", "http://[::1]/", "http://10.1.2.3/", "http://192.168.0.10/",
    "http://172.16.5.5/", "http://169.254.169.254/latest/meta-data/", "http://100.64.0.1/", "http://0.0.0.0/",
    "http://2130706433/", "http://0x7f.1/", "http://[::ffff:127.0.0.1]/", "http://metadata.google.internal/",
    "http://printer.local/", "ftp://example.com/", "file:///etc/passwd", "http://user:pw@example.com/", "",
])
def test_blocked_destinations(url):
    assert not check_url_safe(url).ok


def test_public_ip_accepted_and_private_flag():
    assert check_url_safe("http://8.8.8.8/").ok
    assert check_url_safe("http://127.0.0.1:8000/", allow_private=True).ok
    assert ip_is_public("1.1.1.1") and not ip_is_public("fe80::1") and not ip_is_public("garbage")


def test_start_url_normalisation():
    url, check = validate_start_url("8.8.8.8")
    assert url == "https://8.8.8.8" and check.ok


def test_middleware_blocks_every_request_including_redirect_targets():
    mw = SSRFProtectionMiddleware(allow_private=False)
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("http://169.254.169.254/latest/meta-data/"))   # e.g. a redirect target
    assert mw.process_request(Request("http://8.8.8.8/")) is None
    assert mw.blocked == 1


def test_resolver_rejects_rebinding_to_private_ip():
    with pytest.raises(IgnoreRequest):
        SafeResolver._check_ip("127.0.0.1", "rebind.example.com")
    assert SafeResolver._check_ip("8.8.8.8", "ok.example.com") == "8.8.8.8"


def test_offsite_requests_blocked_but_seed_redirect_allowed():
    class Crawler:  # minimal stand-in
        class spider:  # noqa: N801
            policy = DomainPolicy("https://example.com")
    mw = DomainRestrictionMiddleware(Crawler)
    assert mw.process_request(Request("https://example.com/a")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://other.org/"))
    assert mw.process_request(Request("https://brand-new.org/", meta={"is_seed": True})) is None
