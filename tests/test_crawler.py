"""End-to-end crawl tests against the controlled local test site."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import time

import pytest

from tests.conftest import ROOT, browser_available, cli
from database.crawl_database import CrawlDatabase


def url_rows(fc):
    c = fc["db"]._conn
    return {r["url"].replace(fc["base"], ""): dict(r) for r in c.execute("SELECT * FROM urls WHERE session_id=1")}


def test_crawl_completes_and_exports(full_crawl):
    assert full_crawl["proc"].returncode == 0, full_crawl["proc"].stdout[-2000:]
    assert (full_crawl["out"] / "csv" / "website_data.csv").exists()
    assert (full_crawl["out"] / "excel" / "website_data.xlsx").exists()
    assert full_crawl["db"].get_session(1)["status"] == "completed"


def test_homepage_and_recursive_discovery(full_crawl):
    rows = url_rows(full_crawl)
    assert rows["/"]["status"] == "crawled" and rows["/"]["depth"] == 0
    for path, depth in [("/about", 1), ("/services/service-1", 2), ("/blog/article-1", 2), ("/blog/article-3", 3)]:
        assert rows[path]["status"] == "crawled", path
        assert rows[path]["depth"] == depth, path


def test_relative_url_conversion(full_crawl):
    assert url_rows(full_crawl)["/products"]["status"] == "crawled"   # from href="products/"


def test_domain_restriction(full_crawl):
    keys = " ".join(url_rows(full_crawl))
    assert "external.example.org" not in keys and "other-site.example.net" not in keys
    assert "external.example.org" not in " ".join(full_crawl["hits"])


def test_duplicates_are_filtered(full_crawl):
    rows = url_rows(full_crawl)
    about = [k for k in rows if k.startswith("/about")]
    assert about == ["/about"]                       # slash, #fragment, utm_ variants merged
    assert rows["/about"]["duplicate_hits"] >= 3
    pages = [p["url"] for p in full_crawl["db"].iter_pages(1)]
    assert len(pages) == len(set(pages))


def test_query_parameters_not_merged(full_crawl):
    rows = url_rows(full_crawl)
    assert rows["/products/widget?id=1"]["status"] == "crawled"
    assert rows["/products/widget?id=2"]["status"] == "crawled"


def test_robots_txt_respected(full_crawl):
    assert full_crawl["hits"].get("/private/secret", 0) == 0
    assert url_rows(full_crawl)["/private/secret"]["status"] == "skipped"


def test_url_traps_and_files_excluded(full_crawl):
    assert full_crawl["hits"].get("/search", 0) == 0 and full_crawl["hits"].get("/calendar", 0) == 0
    assert full_crawl["hits"].get("/files/report.pdf", 0) == 0


def test_sitemap_discovery(full_crawl):
    r = url_rows(full_crawl)["/sitemap-only"]
    assert r["status"] == "crawled" and r["discovered_via"] == "sitemap"


def test_error_handling_does_not_stop_crawl(full_crawl):
    rows = url_rows(full_crawl)
    assert rows["/missing"]["status"] == "failed" and rows["/missing"]["http_status"] == 404
    assert rows["/server-error"]["status"] == "failed"
    assert full_crawl["hits"]["/server-error"] == 3         # 1 try + 2 retries, then give up
    assert rows["/empty"]["status"] == "failed" and "Empty" in rows["/empty"]["error"]
    assert rows["/loop-a"]["status"] == "failed" and "loop" in rows["/loop-a"]["error"].lower()
    assert rows["/plain"]["status"] == "skipped"
    assert rows["/caf%C3%A9"]["status"] == "crawled"


def test_429_retry_after_is_respected(full_crawl):
    assert url_rows(full_crawl)["/rate-limited"]["status"] == "crawled"
    assert full_crawl["hits"]["/rate-limited"] == 2


def test_redirect_followed_without_duplicate_page(full_crawl):
    assert url_rows(full_crawl)["/old-page"]["status"] == "skipped"
    about_pages = [p for p in full_crawl["db"].iter_pages(1) if p["url"].endswith("/about")]
    assert len(about_pages) == 1


def test_sensitive_data_not_collected(full_crawl):
    blob = json.dumps(list(full_crawl["db"].iter_pages(1)))
    assert "SECRET123" not in blob and "TOPSECRETTOKEN" not in blob


def test_images_downloaded_only_when_enabled(full_crawl):
    files = list((full_crawl["out"] / "images").rglob("*.png"))
    assert files, "image download was enabled for this crawl"


def test_ssrf_blocks_private_start_url(site, tmp_path):
    proc = cli(tmp_path, "--url", site.base_url)            # no --allow-private-network
    assert proc.returncode == 2
    assert "non-public" in proc.stderr or "blocked" in proc.stderr


def test_authorization_required_when_not_interactive(site, tmp_path):
    cmd = [sys.executable, "main.py", "--url", site.base_url, "--allow-private-network",
           "--output-dir", str(tmp_path), "--db", str(tmp_path / "d.sqlite3")]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 2 and "accept-authorization" in proc.stderr


def test_max_pages_and_depth_limits(site, tmp_path):
    proc = cli(tmp_path, "--url", site.base_url, "--allow-private-network", "--delay", "0",
               "--javascript", "never", "--depth", "1", "--max-pages", "5", "--no-sitemaps", "--format", "csv")
    assert proc.returncode == 0
    db = CrawlDatabase(tmp_path / "db.sqlite3")
    s = db.summary(1)
    assert s["pages_crawled"] <= 5 and s["not_queued_limits"] > 0
    assert max(p["depth"] for p in db.iter_pages(1)) <= 1
    db.close()


def test_cancellation_and_resume(site, tmp_path):
    site.slow_pages = 12
    cmd = [sys.executable, "main.py", "--accept-authorization", "--allow-private-network", "--url",
           site.base_url + "/slow/1", "--delay", "0", "--concurrency", "1", "--javascript", "never",
           "--no-sitemaps", "--depth", "20", "--include", "/slow/", "--output-dir", str(tmp_path),
           "--db", str(tmp_path / "db.sqlite3"), "--json-progress", "--control-stdin"]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    deadline, crawled = time.time() + 60, 0
    for line in proc.stdout:
        if line.startswith("@@PROGRESS"):
            crawled = json.loads(line.split(" ", 1)[1])["pages_crawled"]
            if crawled >= 3:
                proc.stdin.write("stop\n"); proc.stdin.flush()
                break
        assert time.time() < deadline
    proc.stdout.read()
    assert proc.wait(timeout=60) == 3                                  # 3 = cancelled, partial export
    db = CrawlDatabase(tmp_path / "db.sqlite3")
    assert db.get_session(1)["status"] == "cancelled"
    done_before = db.summary(1)["pages_crawled"]
    assert 3 <= done_before < 12 and (tmp_path / "csv" / "website_data.csv").exists()
    db.close()

    resumed = cli(tmp_path, "--resume", "1", "--allow-private-network")
    assert resumed.returncode == 0, resumed.stdout[-1500:]
    db = CrawlDatabase(tmp_path / "db.sqlite3")
    urls = [p["url"] for p in db.iter_pages(1)]
    assert len(urls) == len(set(urls)) == 12, urls                     # nothing re-crawled, nothing lost
    assert db.get_session(1)["status"] == "completed"
    db.close()


@pytest.mark.browser
@pytest.mark.skipif(not browser_available(), reason="Playwright browser not installed")
def test_javascript_rendering(site, tmp_path):
    proc = cli(tmp_path, "--url", site.base_url + "/js-app", "--allow-private-network", "--delay", "0",
               "--javascript", "auto", "--depth", "1", "--no-sitemaps", "--format", "csv")
    assert proc.returncode == 0, proc.stdout[-1500:]
    db = CrawlDatabase(tmp_path / "db.sqlite3")
    pages = {p["url"].replace(site.base_url, ""): p for p in db.iter_pages(1)}
    app = pages["/js-app"]
    assert app["render_mode"] == "rendered"
    assert any(h["text"] == "Rendered Heading" for h in app["headings"])
    assert "/js-page" in pages                                          # link only exists after rendering
    assert pages["/js-page"]["render_mode"] == "static"                 # normal pages are NOT rendered
    db.close()


def test_javascript_failure_falls_back_gracefully(site, tmp_path):
    from config import CrawlConfig
    from crawler.javascript_renderer import JavaScriptRenderer, RenderError
    import asyncio
    r = JavaScriptRenderer(CrawlConfig(browser="webkit"))
    try:
        asyncio.run(r.render(site.base_url))
    except RenderError:
        assert not r.available
    finally:
        r.close()
