#!/usr/bin/env python3
"""WebCrawler Pro - command-line entry point.

Examples:
    python main.py --url https://example.com
    python main.py --url https://example.com --depth 5 --max-pages 1000 --delay 1 --format both --javascript auto
    python main.py --url https://example.com --depth 3 --max-pages 500 --format xlsx
    python main.py --list-sessions
    python main.py --resume 3
    python main.py --gui
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import APP_FULL_NAME, APP_VERSION, CrawlConfig, DEFAULT_DB_PATH, DEFAULT_OUTPUT_DIR, load_config_file

AUTHORIZATION_NOTICE = """
================================ AUTHORIZATION NOTICE ================================
WebCrawler Pro sends automated requests to the target website.
Only crawl websites that you own, or for which you have explicit permission.

  * Respect the site's terms of service, robots.txt and applicable laws.
  * Public content only: the crawler never logs in, submits forms, solves CAPTCHAs
    or bypasses paywalls / anti-bot measures.
  * Keep request rates reasonable; you are responsible for the load you cause.
  * Collected data may contain personal data - store and use it lawfully.
======================================================================================
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python main.py", description=f"{APP_FULL_NAME} v{APP_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python main.py --url https://example.com\n"
               "  python main.py --url https://example.com --depth 5 --max-pages 1000 --delay 1 "
               "--format both --javascript auto\n"
               "  python main.py --url https://example.com --depth 3 --max-pages 500 --format xlsx\n"
               "  python main.py --list-sessions        # show previous crawls\n"
               "  python main.py --resume 3             # continue an unfinished crawl\n"
               "  python main.py --export 3 --format xlsx   # re-export an old session\n")
    B = argparse.BooleanOptionalAction

    g = p.add_argument_group("target")
    g.add_argument("--url", help="website to crawl (http/https). Required unless --resume/--gui/--list-sessions/--export")
    g.add_argument("--config", metavar="FILE", help="TOML/JSON settings file (command-line options override it)")

    g = p.add_argument_group("crawl limits and politeness")
    g.add_argument("--depth", type=int, dest="max_depth", help="maximum link depth, homepage = 0 (default 5)")
    g.add_argument("--max-pages", type=int, dest="max_pages", help="maximum number of pages to request (default 1000)")
    g.add_argument("--timeout", type=float, help="request timeout in seconds (default 30)")
    g.add_argument("--concurrency", type=int, help="concurrent requests (default 4)")
    g.add_argument("--delay", type=float, help="seconds between requests to the site (default 1.0)")
    g.add_argument("--retries", type=int, dest="max_retries", help="retries for transient errors (default 2)")
    g.add_argument("--user-agent", dest="user_agent", help="User-Agent header")

    g = p.add_argument_group("scope")
    g.add_argument("--subdomains", action=B, dest="include_subdomains", help="also crawl subdomains (default: off)")
    g.add_argument("--include", action="append", dest="include_patterns", metavar="REGEX",
                   help="only crawl URLs matching this regex (repeatable)")
    g.add_argument("--exclude", action="append", dest="exclude_patterns", metavar="REGEX",
                   help="skip URLs matching this regex (repeatable; replaces the built-in exclusions)")
    g.add_argument("--include-binary", action=B, dest="include_binary_files",
                   help="request binary-looking URLs (pdf, zip...); they are recorded but never parsed")
    g.add_argument("--robots", action=B, dest="respect_robots", help="respect robots.txt (default: on)")
    g.add_argument("--sitemaps", action=B, dest="use_sitemaps", help="use sitemaps for URL discovery (default: on)")

    g = p.add_argument_group("JavaScript rendering (Playwright)")
    g.add_argument("--javascript", choices=["auto", "always", "never"], help="render JS pages (default auto)")
    g.add_argument("--render-timeout", type=float, dest="render_timeout", help="browser page timeout in seconds")
    g.add_argument("--browser", choices=["chromium", "firefox", "webkit"], help="browser engine (default chromium)")
    g.add_argument("--headless", action=B, help="run the browser headless (default: on)")

    g = p.add_argument_group("extraction")
    g.add_argument("--tables", action=B, dest="extract_tables", help="extract tables (default: on)")
    g.add_argument("--metadata", action=B, dest="extract_metadata", help="extract meta/OpenGraph/JSON-LD (default: on)")
    g.add_argument("--external-links", action=B, dest="extract_external_links", help="record external links (default: on)")
    g.add_argument("--images", action=B, dest="extract_images", help="extract image information (default: on)")
    g.add_argument("--download-images", action=B, dest="download_images", help="download image files (default: off)")

    g = p.add_argument_group("output")
    g.add_argument("--format", choices=["csv", "xlsx", "both"], dest="output_format", help="output format (default both)")
    g.add_argument("--output-dir", dest="output_dir", help=f"output directory (default {DEFAULT_OUTPUT_DIR})")
    g.add_argument("--db", dest="db_path", help=f"SQLite crawl history file (default {DEFAULT_DB_PATH})")
    g.add_argument("--log-level", dest="log_level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    g = p.add_argument_group("sessions")
    g.add_argument("--resume", nargs="?", const="latest", metavar="SESSION_ID",
                   help="resume an unfinished crawl (latest one for --url, or the given id)")
    g.add_argument("--list-sessions", action="store_true", help="list previous crawl sessions and exit")
    g.add_argument("--export", type=int, metavar="SESSION_ID", help="re-export a stored session and exit")

    g = p.add_argument_group("safety / automation")
    g.add_argument("--accept-authorization", "--yes", action="store_true", dest="accept",
                   help="confirm you are authorised to crawl the target (skips the prompt)")
    g.add_argument("--allow-private-network", action="store_true", dest="allow_private_network",
                   help="DISABLE SSRF protection - only for crawling your own local test sites")
    g.add_argument("--gui", action="store_true", help="open the graphical interface")
    g.add_argument("--json-progress", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--control-stdin", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"WebCrawler Pro {APP_VERSION}")
    return p


def config_from_args(args: argparse.Namespace) -> CrawlConfig:
    """defaults < config file < command line."""
    data: dict = {}
    if args.config:
        data.update(load_config_file(args.config))
    for key, value in vars(args).items():
        if key in {"config", "accept", "gui", "resume", "list_sessions", "export", "json_progress",
                   "control_stdin", "url"} or value is None:
            continue
        data[key] = value
    if args.url:
        data["url"] = args.url
    # Keep the crawl history next to the results unless --db / config says otherwise.
    if "db_path" not in data and "output_dir" in data:
        data["db_path"] = str(Path(data["output_dir"]) / "crawl_history.sqlite3")
    cfg = CrawlConfig.from_dict(data)
    return cfg


def confirm_authorization(args: argparse.Namespace, cfg: CrawlConfig) -> bool:
    """Show the notice; require explicit confirmation (flag or typed 'yes')."""
    print(AUTHORIZATION_NOTICE)
    if args.accept:
        print("Authorization confirmed via --accept-authorization.\n")
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print("ERROR: non-interactive session. Re-run with --accept-authorization to confirm that you are "
              "authorised to crawl this website.", file=sys.stderr)
        return False
    answer = input(f"Are you authorised to crawl {cfg.url}? Type 'yes' to continue: ").strip().lower()
    return answer in ("yes", "y")


def list_sessions(db_path: str) -> int:
    from database.crawl_database import CrawlDatabase
    db = CrawlDatabase(db_path)
    sessions = db.list_sessions()
    db.close()
    if not sessions:
        print("No crawl sessions found.")
        return 0
    print(f"{'ID':>4}  {'Status':<10} {'Crawled':>8} {'Pending':>8} {'Started (UTC)':<20}  Start URL")
    for s in sessions:
        print(f"{s['id']:>4}  {s['status']:<10} {s['urls_crawled']:>8} {s['urls_pending']:>8} "
              f"{s['started_at'][:19]:<20}  {s['start_url']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        from gui.app import launch_gui
        return launch_gui()

    try:
        cfg = config_from_args(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"invalid configuration: {exc}")

    if args.list_sessions:
        return list_sessions(cfg.db_path)

    if args.export is not None:
        from database.crawl_database import CrawlDatabase
        from crawler.runner import export_outputs
        from utils.logger import setup_logging
        setup_logging(cfg.log_level)
        db = CrawlDatabase(cfg.db_path)
        session = db.get_session(args.export)
        if session is None:
            print(f"ERROR: no session with id {args.export}", file=sys.stderr)
            return 2
        stored = CrawlConfig.from_dict(json.loads(session["config_json"]))
        stored.output_dir, stored.output_format = cfg.output_dir, cfg.output_format
        files = export_outputs(stored, db, args.export)
        db.close()
        for kind, path in files.items():
            print(f"{kind.upper()}: {path}")
        return 0

    resume_id: int | None = None
    if args.resume is not None:
        from database.crawl_database import CrawlDatabase
        db = CrawlDatabase(cfg.db_path)
        if args.resume == "latest":
            session = db.latest_resumable_session(cfg.url or None)
            if session is None:
                db.close()
                print("ERROR: no unfinished crawl session found to resume (see --list-sessions).", file=sys.stderr)
                return 2
            resume_id = int(session["id"])
        else:
            try:
                resume_id = int(args.resume)
            except ValueError:
                parser.error("--resume expects a numeric session id")
            session = db.get_session(resume_id)
            if session is None:
                db.close()
                print(f"ERROR: no session with id {resume_id}", file=sys.stderr)
                return 2
        cfg.url = session["start_url"]
        cfg.allow_private_network = bool(json.loads(session["config_json"]).get("allow_private_network")) \
            or cfg.allow_private_network
        db.close()
    elif not cfg.url:
        parser.error("--url is required (or use --resume, --list-sessions, --export, --gui)")

    from utils.validators import validate_start_url
    if resume_id is None:
        url, check = validate_start_url(cfg.url, allow_private=cfg.allow_private_network)
        if not check.ok:
            print(f"ERROR: start URL rejected - {check.reason}", file=sys.stderr)
            return 2
        cfg.url = url
        try:
            cfg.validate()
        except ValueError as exc:
            parser.error(str(exc))
    if cfg.allow_private_network:
        print("WARNING: SSRF protection is DISABLED (--allow-private-network). "
              "Use this only for your own local test sites.", file=sys.stderr)
    if not cfg.respect_robots:
        print("WARNING: robots.txt will not be respected. Make sure you have permission.", file=sys.stderr)

    if not confirm_authorization(args, cfg):
        print("Crawl cancelled: authorization not confirmed.", file=sys.stderr)
        return 2

    from crawler.runner import run_crawl
    outcome = run_crawl(cfg, resume_session=resume_id, json_progress=args.json_progress,
                        control_stdin=args.control_stdin)
    s = outcome.summary
    print(f"\nSession {outcome.session_id} {outcome.status}: {s['pages_crawled']} pages "
          f"({s['successful']} ok, {s['failed']} failed, {s['skipped']} skipped), "
          f"{s['total_discovered']} URLs discovered.")
    for kind, path in outcome.files.items():
        print(f"  {kind.upper()}: {path}")
    return {"completed": 0, "cancelled": 3}.get(outcome.status, 1)


if __name__ == "__main__":
    sys.exit(main())
