"""Shared fixtures: a local test website and a helper that runs the CLI in a subprocess.

Each crawl runs in its own process because Twisted's reactor cannot be restarted.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.crawl_database import CrawlDatabase  # noqa: E402
from tests.fixtures.site_server import TestSite  # noqa: E402


@pytest.fixture(scope="session")
def site():
    s = TestSite().start()
    yield s
    s.stop()


def cli(out: Path, *args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "main.py", "--accept-authorization", "--output-dir", str(out),
           "--db", str(out / "db.sqlite3"), *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def full_crawl(site, tmp_path_factory):
    """One complete crawl of the test site (JS rendering off), shared by many tests."""
    out = tmp_path_factory.mktemp("full")
    site.hits.clear()
    proc = cli(out, "--url", site.base_url, "--allow-private-network", "--delay", "0.05",
               "--javascript", "never", "--depth", "5", "--download-images")
    hits = dict(site.hits)
    db = CrawlDatabase(out / "db.sqlite3")
    yield {"out": out, "proc": proc, "db": db, "hits": hits, "base": site.base_url, "session": 1}
    db.close()
