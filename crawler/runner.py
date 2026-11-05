"""Glue code: sessions, Scrapy settings, running a crawl and exporting results."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import APP_VERSION, CrawlConfig
from database.crawl_database import CrawlDatabase
from exporters.csv_exporter import export_csv
from exporters.excel_exporter import export_excel
from utils.logger import setup_logging
from utils.validators import validate_start_url

log = logging.getLogger("webcrawler.runner")


@dataclass
class CrawlOutcome:
    session_id: int
    status: str
    summary: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)


def _dns_setting_name() -> str:
    """Scrapy >= 2.13 renamed DNS_RESOLVER to TWISTED_DNS_RESOLVER."""
    from scrapy.settings import default_settings
    return "TWISTED_DNS_RESOLVER" if hasattr(default_settings, "TWISTED_DNS_RESOLVER") else "DNS_RESOLVER"


def build_scrapy_settings(cfg: CrawlConfig, json_progress: bool = False, control_stdin: bool = False) -> dict[str, Any]:
    """Translate :class:`CrawlConfig` into Scrapy settings."""
    downloader_mw: dict[str, int | None] = {
        "crawler.scrapy_components.SSRFProtectionMiddleware": 50,
        "crawler.scrapy_components.DomainRestrictionMiddleware": 55,
        "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,   # retries/back-off handled by the spider
    }
    if not cfg.use_system_proxy:
        downloader_mw["scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware"] = None
    from scrapy.settings import default_settings
    jitter = {"DOWNLOAD_DELAY_JITTER": 0.5} if hasattr(default_settings, "DOWNLOAD_DELAY_JITTER") \
        else {"RANDOMIZE_DOWNLOAD_DELAY": True}
    return {
        **jitter,
        "BOT_NAME": "webcrawler_pro",
        "USER_AGENT": cfg.user_agent,
        "ROBOTSTXT_OBEY": cfg.respect_robots,
        "CONCURRENT_REQUESTS": cfg.concurrency,
        "CONCURRENT_REQUESTS_PER_DOMAIN": cfg.concurrency,
        "DOWNLOAD_DELAY": cfg.delay,
        "DOWNLOAD_TIMEOUT": cfg.timeout,
        "DOWNLOAD_MAXSIZE": int(cfg.max_response_mb * 1024 * 1024),
        "DOWNLOAD_WARNSIZE": 0,
        "RETRY_ENABLED": False,
        "REDIRECT_MAX_TIMES": 10,                 # redirect loops end here
        "COOKIES_ENABLED": False,                 # no session cookies are stored or replayed
        "HTTPERROR_ALLOW_ALL": True,
        "TELNETCONSOLE_ENABLED": False,
        "AUTOTHROTTLE_ENABLED": False,
        _dns_setting_name(): "crawler.scrapy_components.SafeResolver",
        "LOG_INSTALL_ROOT_HANDLER": False,
        "DOWNLOADER_MIDDLEWARES": downloader_mw,
        "ITEM_PIPELINES": {"crawler.scrapy_components.DatabasePipeline": 100},
        "EXTENSIONS": {
            "crawler.scrapy_components.ProgressExtension": 10,
            "crawler.scrapy_components.StdinControlExtension": 11,
            "scrapy.extensions.telnet.TelnetConsole": None,
        },
        # Breadth-first: shallow pages first, FIFO within a depth level.
        "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
        "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
        "LOG_LEVEL": "WARNING",
        "WCP_ALLOW_PRIVATE_NETWORK": cfg.allow_private_network,
        "WCP_JSON_PROGRESS": json_progress,
        "WCP_CONTROL_STDIN": control_stdin,
    }


def export_outputs(cfg: CrawlConfig, db: CrawlDatabase, session_id: int,
                   output_dir: str | Path | None = None, fmt: str | None = None) -> dict[str, str]:
    """Export a stored session to CSV and/or XLSX.  Returns ``{kind: path}``."""
    base = Path(output_dir or cfg.output_dir)
    fmt = fmt or cfg.output_format
    files: dict[str, str] = {}
    if fmt in ("csv", "both"):
        path = base / "csv" / "website_data.csv"
        export_csv(db.iter_pages(session_id), path)
        files["csv"] = str(path)
    if fmt in ("xlsx", "both"):
        path = base / "excel" / "website_data.xlsx"
        export_excel(db.iter_pages(session_id), db.summary(session_id), db.url_status_map(session_id), path,
                     output_dir=base, config_snapshot=cfg.to_dict())
        files["xlsx"] = str(path)
    return files


def run_crawl(cfg: CrawlConfig, resume_session: int | None = None, json_progress: bool = False,
              control_stdin: bool = False, renderer: Any = None) -> CrawlOutcome:
    """Run one crawl (new or resumed) to completion and export the results.

    Must be called at most once per process: Twisted's reactor cannot be restarted.
    The GUI therefore runs each crawl in its own subprocess.
    """
    url, check = validate_start_url(cfg.url, allow_private=cfg.allow_private_network)
    db = CrawlDatabase(cfg.db_path)
    try:
        resume = resume_session is not None
        if resume:
            session = db.get_session(resume_session)
            if session is None:
                raise ValueError(f"No crawl session with id {resume_session}")
            stored = CrawlConfig.from_dict(json.loads(session["config_json"]))
            # Presentation settings come from this invocation; crawl semantics from the stored session.
            stored.output_dir, stored.output_format = cfg.output_dir, cfg.output_format
            stored.log_level, stored.db_path = cfg.log_level, cfg.db_path
            cfg = stored
            url = cfg.url
            session_id = resume_session
            db.reopen_session(session_id)
        else:
            if not check.ok:
                raise ValueError(f"Start URL rejected: {check.reason}")
            cfg.url = url
            cfg.validate()
            host = urlsplit(url).hostname or ""
            session_id = db.create_session(url, host, cfg.to_json())

        log_file = Path(cfg.output_dir) / "logs" / f"crawl_session_{session_id}.log"
        setup_logging(cfg.log_level, log_file, console=True)
        log.info("WebCrawler Pro %s - session %d (%s)", APP_VERSION, session_id, "resumed" if resume else "new")
        if not cfg.respect_robots:
            log.warning("robots.txt is NOT being respected for this crawl (you confirmed you are authorised).")

        from scrapy.crawler import CrawlerProcess
        from crawler.spider import WebsiteSpider

        process = CrawlerProcess(build_scrapy_settings(cfg, json_progress, control_stdin))
        setup_logging(cfg.log_level, log_file, console=True)  # re-apply: Scrapy resets logger levels on start-up
        process.crawl(WebsiteSpider, config=cfg, db=db, session_id=session_id, resume=resume, renderer=renderer)
        try:
            process.start()  # blocks until the spider is closed
        except KeyboardInterrupt:  # pragma: no cover - second Ctrl-C forces shutdown
            db.finish_session(session_id, "cancelled", notes="forced shutdown")

        files = export_outputs(cfg, db, session_id)
        summary = db.summary(session_id)
        log.info("Export completed.")
        for kind, path in files.items():
            log.info("Output %s: %s", kind.upper(), path)
        return CrawlOutcome(session_id, summary["status"], summary, files)
    finally:
        db.close()
