"""Scrapy plug-ins: SSRF guard, domain guard, safe DNS resolver, DB pipeline,
progress reporting and stdin control (pause / resume / stop for the GUI)."""
from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

from scrapy import signals
from scrapy.exceptions import IgnoreRequest, NotConfigured
from scrapy.resolver import CachingThreadedResolver
from twisted.internet import defer
from twisted.internet.task import LoopingCall

from utils.logger import get_logger
from utils.validators import check_url_safe, ip_is_public

log = get_logger("scrapy")


# --------------------------------------------------------------------- SSRF / scope
class SSRFProtectionMiddleware:
    """Downloader middleware: validate *every* request, including redirect hops.

    Redirects are re-scheduled as new requests, so they pass through here again
    before any connection is opened.
    """

    def __init__(self, allow_private: bool) -> None:
        self.allow_private = allow_private
        self.blocked = 0

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings.getbool("WCP_ALLOW_PRIVATE_NETWORK"))

    def process_request(self, request):
        check = check_url_safe(request.url, allow_private=self.allow_private)
        if not check.ok:
            self.blocked += 1
            log.warning("Blocked request (SSRF protection): %s - %s", request.url, check.reason)
            raise IgnoreRequest(f"SSRF protection: {check.reason}")
        return None


class DomainRestrictionMiddleware:
    """Downloader middleware: keep the crawl on the target site, even across redirects.

    Only the *start URL* may redirect to a different host (e.g. a rebranded domain);
    the spider then adopts that host.  Everything else off-site is dropped.
    """

    def __init__(self, crawler) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_request(self, request):
        spider = self.crawler.spider
        policy = getattr(spider, "policy", None)
        if policy is None or request.meta.get("allow_offsite"):
            return None
        if policy.is_internal(request.url):
            return None
        if request.meta.get("is_seed"):
            log.warning("Start URL redirected to another host: %s (it will be adopted as the target site)",
                        request.url)
            return None
        raise IgnoreRequest(f"off-site request blocked: {request.url}")


class SafeResolver(CachingThreadedResolver):
    """DNS resolver that refuses non-public addresses at connection time.

    Checking the IP that is *actually used* for the connection defeats DNS
    rebinding (a host name that resolved to a public IP during validation but to
    127.0.0.1 when connecting).
    """

    def __init__(self, reactor, cache_size: int, timeout: float, allow_private: bool) -> None:
        super().__init__(reactor, cache_size, timeout)
        self.allow_private = allow_private

    @classmethod
    def from_crawler(cls, crawler, reactor):
        cache = crawler.settings.getint("DNSCACHE_SIZE") if crawler.settings.getbool("DNSCACHE_ENABLED") else 0
        return cls(reactor, cache, crawler.settings.getfloat("DNS_TIMEOUT"),
                   crawler.settings.getbool("WCP_ALLOW_PRIVATE_NETWORK"))

    def getHostByName(self, name, timeout=()):
        d = super().getHostByName(name, timeout)
        if not self.allow_private:
            d.addCallback(self._check_ip, name)
        return d

    @staticmethod
    def _check_ip(ip: str, name: str) -> str:
        if not ip_is_public(ip):
            raise IgnoreRequest(f"SSRF protection: {name!r} resolved to non-public address {ip}")
        return ip


# ------------------------------------------------------------------------ pipeline
class DatabasePipeline:
    """Stores every page record (success or failure) and finalises the URL row."""

    def __init__(self, crawler) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_item(self, item):
        spider = self.crawler.spider
        db, session = spider.db, spider.session_id
        key = item.pop("_key", None)
        db.add_page(session, item, commit=False)
        if key:
            status = item.get("status", "crawled" if item.get("ok") else "failed")
            db.set_url_status(session, key, status, item.get("status_code"), item.get("error_message"), commit=True)
        return item


# ------------------------------------------------------------------ extensions
class ProgressExtension:
    """Periodic progress line.  ``--json-progress`` emits machine-readable lines for the GUI."""

    def __init__(self, crawler, json_mode: bool, interval: float) -> None:
        self.crawler = crawler
        self.json_mode = json_mode
        self.interval = interval
        self.loop: LoopingCall | None = None

    @classmethod
    def from_crawler(cls, crawler):
        json_mode = crawler.settings.getbool("WCP_JSON_PROGRESS")
        interval = crawler.settings.getfloat("WCP_PROGRESS_INTERVAL", 1.0)
        ext = cls(crawler, json_mode, interval)
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_opened(self, spider):
        self.loop = LoopingCall(self.emit, spider)
        self.loop.start(self.interval, now=False)

    def spider_closed(self, spider):
        if self.loop and self.loop.running:
            self.loop.stop()
        self.emit(spider, final=True)

    def emit(self, spider, final: bool = False):
        snap = spider.progress.snapshot(spider)
        if self.json_mode:
            sys.stdout.write("@@PROGRESS " + json.dumps(snap) + "\n")
            sys.stdout.flush()
        elif final or snap["pages_crawled"] % 25 == 0 and snap["pages_crawled"]:
            if snap["pages_crawled"] != getattr(self, "_last", -1):
                self._last = snap["pages_crawled"]
                log.info("Progress: %d crawled, %d queued, %d discovered, %d failed",
                         snap["pages_crawled"], snap["queued"], snap["discovered"], snap["failed"])


class StdinControlExtension:
    """Reads ``pause`` / ``resume`` / ``stop`` commands from stdin (used by the GUI)."""

    def __init__(self, crawler) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        if not crawler.settings.getbool("WCP_CONTROL_STDIN"):
            raise NotConfigured
        ext = cls(crawler)
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        return ext

    def spider_opened(self, spider):
        threading.Thread(target=self._read, args=(spider,), daemon=True, name="stdin-control").start()

    def _read(self, spider):
        from twisted.internet import reactor
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd == "pause":
                reactor.callFromThread(self._pause, spider)
            elif cmd == "resume":
                reactor.callFromThread(self._resume, spider)
            elif cmd == "stop":
                reactor.callFromThread(self._stop, spider)
                return

    def _pause(self, spider):
        self.crawler.engine.pause()
        spider.progress.paused = True
        log.info("Crawl paused.")

    def _resume(self, spider):
        self.crawler.engine.unpause()
        spider.progress.paused = False
        log.info("Crawl resumed.")

    def _stop(self, spider):
        log.info("Stop requested - finishing in-flight requests and exporting partial results.")
        spider.cancelled = True
        try:
            self.crawler.engine.unpause()
        except Exception:
            pass
        d = defer.maybeDeferred(self.crawler.engine.close_spider, spider, "cancelled")
        d.addErrback(lambda f: None)
