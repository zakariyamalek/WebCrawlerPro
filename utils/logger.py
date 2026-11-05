"""Logging setup: console + rotating-free per-session log file."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

APP_LOGGER_NAME = "webcrawler"


class _AppFilter(logging.Filter):
    """Show application messages at the chosen level; third-party loggers only warnings+."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(APP_LOGGER_NAME) or record.levelno >= logging.WARNING


class _ConsoleFormatter(logging.Formatter):
    """Console format used by the CLI and parsed by the GUI: ``[LEVEL] message``."""

    def format(self, record: logging.LogRecord) -> str:
        return f"[{record.levelname}] {record.getMessage()}"


def setup_logging(level: str = "INFO", log_file: str | Path | None = None,
                  console: bool = True) -> logging.Logger:
    """Configure application logging (idempotent) and return the app logger.

    Scrapy/Twisted loggers are kept at WARNING so the activity log stays readable.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)

    app_level = getattr(logging, level.upper(), logging.INFO)
    if console:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(app_level)
        handler.setFormatter(_ConsoleFormatter())
        handler.addFilter(_AppFilter())
        root.addHandler(handler)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        fh.addFilter(_AppFilter())
        root.addHandler(fh)

    for noisy in ("scrapy", "twisted", "asyncio", "httpx", "httpcore", "urllib3", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Console handler filters by level; the app logger itself passes everything.
    logging.getLogger(APP_LOGGER_NAME).setLevel(logging.DEBUG)
    return logging.getLogger(APP_LOGGER_NAME)


def get_logger(suffix: str = "") -> logging.Logger:
    """Return a child of the application logger."""
    return logging.getLogger(f"{APP_LOGGER_NAME}.{suffix}" if suffix else APP_LOGGER_NAME)
