"""GUI back-end tests (the Tk window itself is smoke-tested manually / under Xvfb)."""
from __future__ import annotations

import threading

from gui.controller import CrawlController, build_command, parse_line

SETTINGS = {"url": "https://example.com", "max_depth": 2, "max_pages": 10, "concurrency": 2, "delay": 0.5,
            "timeout": 20, "javascript": "never", "output_format": "csv", "include_subdomains": False,
            "extract_tables": True, "extract_metadata": False, "extract_external_links": True,
            "extract_images": True, "download_images": False, "respect_robots": True, "output_dir": "/tmp/x"}


def test_build_command_and_parse_line():
    cmd = build_command(SETTINGS)
    assert "--no-subdomains" in cmd and "--tables" in cmd and "--no-metadata" in cmd and "--json-progress" in cmd
    assert cmd[cmd.index("--depth") + 1] == "2" and "--allow-private-network" not in cmd
    assert parse_line('@@PROGRESS {"a": 1}\n') == ("progress", {"a": 1})
    assert parse_line("[INFO] hello\n") == ("log", "[INFO] hello")
    assert parse_line("@@PROGRESS not-json")[0] == "log"


def test_controller_runs_and_stops_real_crawl(site, tmp_path):
    site.slow_pages = 30
    events, done = [], threading.Event()
    c = CrawlController(lambda p: events.append(p), lambda t: None, lambda rc: (events.append(("exit", rc)), done.set()))
    s = {**SETTINGS, "url": site.base_url + "/slow/1", "javascript": "never", "delay": 0, "concurrency": 1,
         "max_depth": 20, "output_dir": str(tmp_path), "allow_private_network": True}
    # the GUI never passes --db, so use the default DB inside the output dir via environment-free CLI: run
    c.start(s)
    import time
    t0 = time.time()
    while time.time() - t0 < 60 and not any(isinstance(e, dict) and e["pages_crawled"] >= 2 for e in events):
        time.sleep(0.2)
    c.pause(); time.sleep(0.5); c.resume(); c.stop()
    assert done.wait(60)
    assert ("exit", 3) in events
