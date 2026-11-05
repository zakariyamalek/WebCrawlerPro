"""The Scrapy spider: recursive crawling, retries/back-off, sitemaps, JS rendering."""
from __future__ import annotations

import asyncio
import email.utils
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import scrapy
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response, TextResponse

from config import CrawlConfig
from crawler.duplicate_filter import DuplicateFilter
from crawler.javascript_renderer import JavaScriptRenderer, RenderError, needs_rendering
from crawler.page_extractor import empty_record, extract_page
from crawler.robots_handler import RobotsHandler
from crawler.sitemap_parser import parse_sitemap
from crawler.url_discovery import DomainPolicy, LinkClassifier
from crawler.url_normalizer import UrlNormalizer
from database.crawl_database import CrawlDatabase
from utils.helpers import image_filename, utc_now_iso
from utils.logger import get_logger

log = get_logger("spider")

RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 522, 524}
HTML_TYPES = ("text/html", "application/xhtml")
MAX_SITEMAP_LEVELS = 3


class CrawlProgress:
    """Live counters shown in the CLI/GUI."""

    def __init__(self, max_pages: int) -> None:
        self.max_pages = max_pages
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.current_depth = 0
        self.current_url = ""
        self.paused = False
        self.started = time.monotonic()

    def snapshot(self, spider: "WebsiteSpider") -> dict[str, Any]:
        crawled = self.success + self.failed
        queued = max(0, spider.scheduled - spider.finished - spider.baseline_done)
        known = crawled + queued
        # Estimated: the denominator grows as new links are discovered.
        denominator = max(1, min(self.max_pages, known))
        return {
            "discovered": len(spider.dupes), "queued": queued, "pages_crawled": crawled,
            "pages_remaining": queued, "success": self.success, "failed": self.failed,
            "skipped": self.skipped, "current_depth": self.current_depth,
            "current_url": self.current_url, "max_pages": self.max_pages,
            "progress_estimate": round(min(1.0, crawled / denominator), 4), "estimated": True,
            "paused": self.paused, "elapsed": round(time.monotonic() - self.started, 1),
        }


class WebsiteSpider(scrapy.Spider):
    name = "webcrawler_pro"

    def __init__(self, config: CrawlConfig, db: CrawlDatabase, session_id: int,
                 resume: bool = False, renderer: JavaScriptRenderer | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cfg = config
        self.db = db
        self.session_id = session_id
        self.resume = resume
        self.normalizer = UrlNormalizer(config.strip_params)
        self.policy = DomainPolicy(config.url, config.include_subdomains)
        self.classifier = LinkClassifier(config, self.policy, self.normalizer)
        self.dupes = DuplicateFilter()
        self.progress = CrawlProgress(config.max_pages)
        self.renderer = renderer if renderer is not None else (
            JavaScriptRenderer(config) if config.javascript != "never" else None)
        self.robots: RobotsHandler | None = None
        self.scheduled = 0          # requests handed to Scrapy (counts towards max_pages)
        self.baseline_done = 0      # pages finished in earlier runs (resume)
        self.finished = 0           # scheduled requests that have produced a final record
        self.cancelled = False
        self.current_delay = config.delay
        self._sitemaps_seen: set[str] = set()
        self._images_requested: set[str] = set()
        self._render_warned = False
        self._success_streak = 0

    # ------------------------------------------------------------------ helpers
    def _path(self, url: str) -> str:
        parts = urlsplit(url)
        return parts.path + (f"?{parts.query}" if parts.query else "") or "/"

    def _set_delay(self, value: float) -> None:
        self.current_delay = value
        self.download_delay = value
        try:
            for slot in self.crawler.engine.downloader.slots.values():
                slot.delay = value
        except Exception:  # engine not ready yet - spider attribute covers new slots
            pass

    def _make_request(self, url: str, depth: int, key: str, source: str | None = None,
                      is_seed: bool = False) -> scrapy.Request:
        self.scheduled += 1
        return scrapy.Request(
            url, callback=self.parse, errback=self.on_error, dont_filter=True, priority=-depth,
            meta={"crawl_depth": depth, "key": key, "source": source, "is_seed": is_seed,
                  "retry_count": 0, "download_timeout": self.cfg.timeout})

    @staticmethod
    def _retry_after_seconds(response: Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        text = value.decode("latin-1").strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(text) if text else None
            if parsed is not None:
                return max(0.0, parsed.timestamp() - time.time())
        return None

    @staticmethod
    def _retry_request(request: scrapy.Request, attempt: int) -> scrapy.Request:
        """Clone *request* for a retry with a clean meta (no copied internal keys)."""
        keep = ("crawl_depth", "key", "source", "is_seed", "download_timeout")
        meta = {k: request.meta[k] for k in keep if k in request.meta}
        meta["retry_count"] = attempt
        return request.replace(dont_filter=True, meta=meta)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = self.cfg.backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
        delay = max(base, retry_after or 0.0)
        return min(delay, self.cfg.backoff_max)

    # -------------------------------------------------------------- start / seeds
    async def start(self) -> AsyncIterator[Any]:
        cfg = self.cfg
        start_key = self.normalizer.safe_normalize(cfg.url) or cfg.url
        log.info("Starting crawler for %s", cfg.url)
        if self.resume:
            for key in self.db.all_url_keys(self.session_id):
                self.dupes.mark_seen(key)
            self.baseline_done = self.db.count_status(self.session_id, "crawled") + \
                self.db.count_status(self.session_id, "failed")
            self.scheduled = self.baseline_done
            rows = self.db.pending_urls(self.session_id, ("queued", "discovered"))
            resumed = 0
            for row in rows:
                if row["status"] == "discovered" and (
                        row["depth"] > cfg.max_depth or self.scheduled >= cfg.max_pages):
                    continue
                self.db.set_url_status(self.session_id, row["url"], "queued")
                resumed += 1
                yield self._make_request(row["url"], row["depth"], row["url"], row["discovered_from"],
                                         is_seed=(row["url"] == start_key))
            log.info("Resuming crawl: %d URLs re-queued, %d pages already done.", resumed, self.baseline_done)
            return
        self.dupes.add(start_key)
        self.db.add_url(self.session_id, start_key, "queued", 0, None, "seed", commit=True)
        yield self._make_request(cfg.url, 0, start_key, None, is_seed=True)
        if cfg.use_sitemaps or cfg.respect_robots:
            root = f"{urlsplit(cfg.url).scheme}://{urlsplit(cfg.url).netloc}"
            yield scrapy.Request(
                root + "/robots.txt", callback=self.parse_robots, errback=self.on_aux_error, priority=50,
                dont_filter=True, meta={"root": root, "download_timeout": cfg.timeout, "aux": "robots.txt"})

    # ------------------------------------------------------------ robots / sitemap
    async def parse_robots(self, response: Response) -> AsyncIterator[Any]:
        root = response.meta["root"]
        sitemap_urls: list[str] = []
        if response.status == 200 and isinstance(response, TextResponse):
            self.robots = RobotsHandler(response.url, response.text, self.cfg.user_agent)
            sitemap_urls = self.robots.sitemaps()
            delay = self.robots.crawl_delay()
            if delay and self.cfg.respect_robots and delay > self.current_delay:
                self._set_delay(min(delay, self.cfg.backoff_max))
                log.info("robots.txt Crawl-delay honoured: %.1fs between requests.", self.current_delay)
            log.info("robots.txt read (%d sitemap reference(s)).", len(sitemap_urls))
        else:
            log.info("No robots.txt found (HTTP %s).", response.status)
        if not self.cfg.use_sitemaps:
            return
        default = root + "/sitemap.xml"
        if default not in sitemap_urls:
            sitemap_urls.append(default)
        for url in sitemap_urls:
            req = self._sitemap_request(url, level=0)
            if req is not None:
                yield req

    def _sitemap_request(self, url: str, level: int) -> scrapy.Request | None:
        if url in self._sitemaps_seen or len(self._sitemaps_seen) >= self.cfg.max_sitemaps:
            return None
        if not self.policy.is_internal(url):
            log.warning("Ignoring off-site sitemap: %s", url)
            return None
        self._sitemaps_seen.add(url)
        return scrapy.Request(url, callback=self.parse_sitemap, errback=self.on_aux_error, priority=40,
                              dont_filter=True,
                              meta={"level": level, "aux": "sitemap", "download_timeout": self.cfg.timeout})

    async def parse_sitemap(self, response: Response) -> AsyncIterator[Any]:
        if response.status != 200:
            log.debug("Sitemap %s -> HTTP %s", response.url, response.status)
            return
        result = parse_sitemap(response.body)
        level = response.meta.get("level", 0)
        if result.is_index:
            log.info("Sitemap index %s lists %d sitemap(s).", response.url, len(result.sitemaps))
            if level + 1 < MAX_SITEMAP_LEVELS:
                for nested in result.sitemaps:
                    req = self._sitemap_request(nested, level + 1)
                    if req is not None:
                        yield req
            return
        if not result.urls:
            return
        log.info("Sitemap %s provided %d URL(s).", response.url, len(result.urls))
        for url in result.urls:
            for req in self._consider(url, 1, response.url, via="sitemap", text=""):
                yield req
        self.db.commit()

    # ----------------------------------------------------------- link evaluation
    def _consider(self, url: str, depth: int, source: str, via: str = "link", text: str = "") -> list[scrapy.Request]:
        """Apply scope/duplicate/limit rules to a discovered URL; return requests to schedule."""
        verdict = self.classifier.check(url)
        if verdict.reason in ("external", "unsupported scheme", "malformed URL") or verdict.normalized is None:
            return []
        key = verdict.normalized
        if not self.dupes.add(key):
            return []
        src_key = self.normalizer.safe_normalize(source) or source
        if not verdict.crawlable:
            self.db.add_url(self.session_id, key, "skipped", depth, src_key, via, verdict.reason)
            self.progress.skipped += 1
            return []
        if self.cfg.respect_robots and via == "sitemap" and self.robots and not self.robots.can_fetch(url):
            self.db.add_url(self.session_id, key, "skipped", depth, src_key, via, "disallowed by robots.txt")
            self.progress.skipped += 1
            return []
        if self.cancelled:
            # Stop requested: keep the URL as 'queued' so a later --resume continues from here.
            self.db.add_url(self.session_id, key, "queued", depth, src_key, via)
            return []
        if depth > self.cfg.max_depth or self.scheduled >= self.cfg.max_pages:
            self.db.add_url(self.session_id, key, "discovered", depth, src_key, via,
                            "depth limit" if depth > self.cfg.max_depth else "page limit")
            return []
        self.db.add_url(self.session_id, key, "queued", depth, src_key, via)
        return [self._make_request(url, depth, key, src_key)]

    # ------------------------------------------------------------------ parsing
    def _item(self, record: dict[str, Any], key: str | None, status: str) -> dict[str, Any]:
        record["status"] = status
        record["ok"] = status == "crawled"
        record["_key"] = key
        self.finished += 1
        if status == "crawled":
            self.progress.success += 1
        elif status == "failed":
            self.progress.failed += 1
        else:
            self.progress.skipped += 1
        return record

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        meta = response.meta
        depth, key = meta["crawl_depth"], meta["key"]
        requested = (meta.get("redirect_urls") or [response.url])[0]
        chain = list(meta.get("redirect_urls", []))
        self.progress.current_depth, self.progress.current_url = depth, response.url
        log.info("Crawling %s", self._path(response.url))
        status = response.status
        latency_ms = round(float(meta.get("download_latency", 0.0)) * 1000, 1)
        headers = {k.decode("latin-1"): b", ".join(v).decode("latin-1") for k, v in response.headers.items()}

        # -- transient errors: back off and retry (never hammer a blocking server) -----
        if status in RETRY_STATUSES:
            attempt = meta["retry_count"]
            retry_after = self._retry_after_seconds(response)
            if status in (429, 503):
                self._set_delay(min(max(self.current_delay * 2, 1.0, retry_after or 0.0), self.cfg.backoff_max))
                log.warning("HTTP %s received - slowing down to %.1fs between requests.", status, self.current_delay)
            if attempt < self.cfg.max_retries:
                wait = self._backoff(attempt, retry_after)
                log.warning("HTTP %s for %s - retry %d/%d in %.1fs", status, self._path(response.url),
                            attempt + 1, self.cfg.max_retries, wait)
                await asyncio.sleep(wait)
                yield self._retry_request(response.request, attempt + 1)
                return

        # -- HTTP errors ---------------------------------------------------------------
        if status >= 300:
            message = f"HTTP {status}"
            if status < 400:
                message += " - redirect loop or too many redirects" if meta.get("redirect_times", 0) >= 10 \
                    else " - redirect without a usable Location header"
            log.warning("%s encountered at %s", message, response.url)
            rec = empty_record(response.url, depth, requested_url=requested, status_code=status,
                               content_type=headers.get("Content-Type"), error_message=message,
                               response_time_ms=latency_ms,
                               technical={"response_headers": {k: v for k, v in headers.items()
                                                                if k.lower() != "set-cookie"},
                                          "redirect_chain": chain, "response_time_ms": latency_ms})
            yield self._item(rec, key, "failed")
            return

        # -- non-HTML content -----------------------------------------------------------
        ctype = headers.get("Content-Type", "").lower()
        if not isinstance(response, TextResponse) or (ctype and not any(t in ctype for t in HTML_TYPES)):
            rec = empty_record(response.url, depth, requested_url=requested, status_code=status,
                               content_type=ctype or None,
                               error_message=f"Unsupported content type: {ctype or 'unknown'}",
                               response_time_ms=latency_ms)
            yield self._item(rec, key, "skipped")
            return
        if not response.body.strip():
            rec = empty_record(response.url, depth, requested_url=requested, status_code=status,
                               content_type=ctype, error_message="Empty HTML response", response_time_ms=latency_ms)
            yield self._item(rec, key, "failed")
            return

        # -- duplicate landing pages (redirects to something we already know) ---------------
        final_key = self.normalizer.safe_normalize(response.url) or response.url
        if final_key != key:
            if final_key in self.dupes and not meta.get("is_seed"):
                rec = empty_record(requested, depth, requested_url=requested, status_code=status,
                                   error_message=f"Redirects to already-known URL {final_key}",
                                   technical={"redirect_chain": chain})
                self.dupes.add(final_key)  # count as a duplicate encounter
                yield self._item(rec, key, "skipped")
                return
            self.dupes.mark_seen(final_key)
            if self.policy.is_internal(response.url):
                self.db.add_url(self.session_id, final_key, "crawled", depth, key, "redirect")
        if meta.get("is_seed") and not self.policy.is_internal(response.url):
            self.policy.adopt(urlsplit(response.url).hostname or "")

        # -- extraction (CPU work runs in a thread so the crawl stays responsive) ---------------
        kwargs = dict(config=self.cfg, classifier=self.classifier, normalizer=self.normalizer,
                      status_code=status, headers=headers, response_time_ms=latency_ms, depth=depth,
                      requested_url=requested, redirect_chain=chain)
        try:
            page = await asyncio.to_thread(extract_page, response.text, response.url, **kwargs)
        except Exception as exc:  # parsing must never kill the crawl
            log.error("Extraction failed for %s: %s", response.url, exc)
            rec = empty_record(response.url, depth, requested_url=requested, status_code=status,
                               error_message=f"Extraction error: {type(exc).__name__}: {exc}",
                               response_time_ms=latency_ms)
            yield self._item(rec, key, "failed")
            return

        if self.renderer is not None and needs_rendering(page["js_signals"], self.cfg) and self.renderer.available:
            try:
                rendered = await self.renderer.render(response.url)
                kwargs["response_time_ms"] = round(latency_ms + rendered.elapsed_ms, 1)
                page = await asyncio.to_thread(extract_page, rendered.html, response.url,
                                               render_mode="rendered", **kwargs)
                log.info("Rendered %s with JavaScript.", self._path(response.url))
            except RenderError as exc:
                page["render_mode"] = "render_failed"
                page["technical"]["render_error"] = str(exc)
                if not self._render_warned:
                    self._render_warned = True
                    log.warning("JavaScript rendering unavailable: %s (continuing with static HTML)", exc)
        elif self.renderer is not None and not self.renderer.available and needs_rendering(page["js_signals"], self.cfg):
            page["render_mode"] = "render_failed"
            page["technical"]["render_error"] = self.renderer.failure_reason

        page["technical"]["retries"] = meta.get("retry_count", 0)
        if depth == 0:
            log.info("Homepage successfully retrieved.")
        log.info("Extracted %d headings.", len(page["headings"]))
        self._success_streak += 1
        if self.current_delay > self.cfg.delay and self._success_streak >= 10:
            self._success_streak = 0
            self._set_delay(max(self.cfg.delay, self.current_delay * 0.75))

        yield self._item(page, key, "crawled")

        # -- discover further links -----------------------------------------------------------------
        internal = [l for l in page["links"] if l["internal"] and l["type"] in ("page", "file")]
        if internal:
            log.info("%d internal links discovered.", len(internal))
        if page["follow_links"]:
            candidates = [(l["url"], l["text"]) for l in internal]
            if page.get("canonical_url"):
                candidates.append((page["canonical_url"], ""))
            for url, text in candidates:
                for req in self._consider(url, depth + 1, response.url, text=text):
                    yield req
        else:
            log.info("Page has a nofollow robots directive - links not followed: %s", response.url)

        if self.cfg.download_images:
            for img in page["images"]:
                if img["url"] in self._images_requested:
                    continue
                self._images_requested.add(img["url"])
                yield scrapy.Request(
                    img["url"], callback=self.save_image, errback=self.on_image_error, dont_filter=True,
                    priority=-depth - 5,
                    meta={"allow_offsite": True,
                          "download_maxsize": int(self.cfg.max_image_mb * 1024 * 1024),
                          "download_timeout": self.cfg.timeout})
        self.db.commit()

    # ------------------------------------------------------------------- images
    async def save_image(self, response: Response) -> AsyncIterator[Any]:
        ctype = response.headers.get("Content-Type", b"").decode("latin-1").lower()
        if response.status != 200 or not ctype.startswith("image/"):
            log.debug("Image skipped (%s, %s): %s", response.status, ctype, response.url)
            return
        directory = Path(self.cfg.output_dir) / "images" / f"session_{self.session_id}"
        directory.mkdir(parents=True, exist_ok=True)
        # Files are written only; nothing downloaded is ever opened, parsed or executed.
        (directory / image_filename(response.url)).write_bytes(response.body)
        return
        yield  # pragma: no cover  (makes this an async generator)

    async def on_image_error(self, failure) -> AsyncIterator[Any]:
        log.debug("Image download failed: %s (%s)", failure.request.url, failure.value)
        return
        yield  # pragma: no cover

    # -------------------------------------------------------------------- errors
    @staticmethod
    def describe_error(failure) -> tuple[str, bool]:
        """Return (friendly message, retryable)."""
        name = failure.type.__name__
        text = str(failure.value) or name
        retryable = {"TimeoutError", "TCPTimedOutError", "ConnectionRefusedError", "ConnectionDone",
                     "ConnectError", "ConnectionLost", "ResponseNeverReceived", "TunnelError",
                     "NoRouteError", "ResponseFailed"}
        lowered = text.lower()
        if name == "DNSLookupError" or "dns lookup failed" in lowered:
            return f"DNS resolution failed: {text}", False
        if "ssl" in lowered or "certificate" in lowered or name in ("SSLError", "CertificateError"):
            return f"SSL/TLS error: {text}", False
        if name in ("TimeoutError", "TCPTimedOutError"):
            return f"Request timed out: {text}", True
        if name == "ConnectionRefusedError":
            return f"Connection refused: {text}", True
        if name in ("DownloadCancelledError", "CancelledError") or "maxsize" in lowered:
            return f"Download cancelled (response too large?): {text}", False
        return f"{name}: {text}", name in retryable

    async def on_error(self, failure) -> AsyncIterator[Any]:
        request = failure.request
        meta = request.meta
        key, depth = meta.get("key"), meta.get("crawl_depth", 0)
        err = failure.value
        if isinstance(err, IgnoreRequest):
            reason = str(err) or "request ignored"
            if "max redirections" in reason.lower():
                msg = f"Redirect loop: more than 10 redirects starting at {request.url}"
                log.warning(msg)
                yield self._item(empty_record(request.url, depth, error_message=msg), key, "failed")
                return
            if "robots" in reason.lower():
                reason = "Blocked by robots.txt"
            log.warning("Skipped %s: %s", request.url, reason)
            rec = empty_record(request.url, depth, error_message=reason)
            yield self._item(rec, key, "skipped")
            return
        message, retryable = self.describe_error(failure)
        attempt = meta.get("retry_count", 0)
        if retryable and attempt < self.cfg.max_retries:
            wait = self._backoff(attempt, None)
            log.warning("%s - retry %d/%d in %.1fs (%s)", message, attempt + 1, self.cfg.max_retries, wait,
                        self._path(request.url))
            await asyncio.sleep(wait)
            yield self._retry_request(request, attempt + 1)
            return
        log.error("Failed %s: %s", request.url, message)
        rec = empty_record(request.url, depth, error_message=message)
        rec["technical"] = {"retries": attempt}
        yield self._item(rec, key, "failed")

    async def on_aux_error(self, failure) -> AsyncIterator[Any]:
        log.info("Could not fetch %s (%s) - continuing.", failure.request.url, self.describe_error(failure)[0])
        return
        yield  # pragma: no cover

    # --------------------------------------------------------------------- close
    def closed(self, reason: str) -> None:
        status = {"finished": "completed"}.get(reason, "cancelled" if reason in ("cancelled", "shutdown") else "failed")
        if self.cancelled:
            status = "cancelled"
        try:
            self.db.save_duplicate_hits(self.session_id, self.dupes.hit_counts())
            self.db.finish_session(self.session_id, status, self.dupes.duplicate_hits,
                                   notes=f"spider closed: {reason}")
        finally:
            if self.renderer is not None:
                self.renderer.close()
        log.info("Crawl %s (%d pages: %d ok, %d failed, %d skipped).", status,
                 self.progress.success + self.progress.failed, self.progress.success, self.progress.failed,
                 self.progress.skipped)
