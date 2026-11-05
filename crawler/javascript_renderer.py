"""Optional Playwright integration for JavaScript-rendered pages.

Strategy (``--javascript auto``):

1. The page is fetched by Scrapy as usual.
2. ``needs_rendering`` inspects cheap signals computed by the extractor (little
   visible text plus scripts, an empty ``#root``/``#app`` mount node, or a
   "please enable JavaScript" notice).
3. Only then is the page opened in a headless browser; the rendered HTML goes
   through the *same* extractor and its links are discovered like any other page.

The renderer never fills in forms, never solves CAPTCHAs and never logs in.  All
browser traffic is checked by the same SSRF validator as normal requests, and
images/media/fonts are blocked to save resources.

Playwright's sync API is bound to the thread that created it, so a dedicated
single-worker executor owns the browser for its whole lifetime.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from config import CrawlConfig
from utils.logger import get_logger
from utils.validators import check_url_safe

log = get_logger("renderer")

_BLOCKED_RESOURCES = {"image", "media", "font"}


class RenderError(RuntimeError):
    """Rendering failed (browser missing, timeout, navigation error...)."""


@dataclass
class RenderResult:
    html: str
    final_url: str
    status: int | None
    elapsed_ms: float


def needs_rendering(signals: dict[str, Any], config: CrawlConfig) -> bool:
    """Decide from extractor signals whether JavaScript rendering is worthwhile."""
    if config.javascript == "never":
        return False
    if config.javascript == "always":
        return True
    if signals.get("noscript_warning") or signals.get("empty_root"):
        return True
    return signals.get("text_chars", 0) < config.js_min_text_chars and signals.get("scripts", 0) >= 1


class JavaScriptRenderer:
    """Lazily started headless browser used to render single pages."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._failed: str | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._failed is None

    @property
    def failure_reason(self) -> str | None:
        return self._failed

    # -- runs in the playwright thread ------------------------------------------------
    def _start(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RenderError("Playwright is not installed (pip install playwright)") from exc
        self._playwright = sync_playwright().start()
        launcher = getattr(self._playwright, self.config.browser)
        try:
            self._browser = launcher.launch(headless=self.config.headless)
        except Exception as exc:
            self._playwright.stop()
            self._playwright = None
            raise RenderError(
                f"Could not launch {self.config.browser} (run 'playwright install {self.config.browser}'): "
                f"{str(exc).splitlines()[0] if str(exc) else exc}") from exc
        self._context = self._browser.new_context(
            user_agent=self.config.user_agent, java_script_enabled=True, ignore_https_errors=False,
            accept_downloads=False, service_workers="block")
        self._context.set_default_timeout(self.config.render_timeout * 1000)
        log.info("Headless %s started for JavaScript rendering.", self.config.browser)

    def _route_guard(self, route: Any) -> None:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCES:
            route.abort()
            return
        if not check_url_safe(request.url, allow_private=self.config.allow_private_network):
            route.abort()
            return
        route.continue_()

    def _render_sync(self, url: str) -> RenderResult:
        import time
        if self._failed:
            raise RenderError(self._failed)
        try:
            self._start()
        except RenderError as exc:
            self._failed = str(exc)
            raise
        page = self._context.new_page()
        started = time.monotonic()
        try:
            page.route("**/*", self._route_guard)
            response = page.goto(url, wait_until="domcontentloaded",
                                 timeout=self.config.render_timeout * 1000)
            try:  # let XHR-driven content settle, but never wait the whole timeout for it
                page.wait_for_load_state("networkidle", timeout=min(8000, self.config.render_timeout * 1000))
            except Exception:
                pass
            html = page.content()
            return RenderResult(html=html, final_url=page.url,
                                status=response.status if response else None,
                                elapsed_ms=(time.monotonic() - started) * 1000)
        except RenderError:
            raise
        except Exception as exc:
            raise RenderError(f"render failed: {str(exc).splitlines()[0] if str(exc) else exc}") from exc
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _close_sync(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._context = self._browser = self._playwright = None

    # -- async API used by the spider ----------------------------------------------------
    async def render(self, url: str) -> RenderResult:
        future = self._executor.submit(self._render_sync, url)
        return await asyncio.wrap_future(future)

    def close(self) -> None:
        """Release the browser (safe to call more than once)."""
        try:
            self._executor.submit(self._close_sync).result(timeout=20)
        except Exception:
            pass
        self._executor.shutdown(wait=False)
