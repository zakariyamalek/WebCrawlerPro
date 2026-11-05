"""SQLite persistence: crawl sessions, URL tracking, page records, resume support.

Tables
------
``sessions``  one row per crawl (start URL, domain, full config, status, timestamps)
``urls``      every discovered URL with status/depth/error (status values below)
``pages``     one row per crawled page: the complete extraction record as JSON

URL status values: ``discovered`` (found but not queued: depth/page limit),
``queued``, ``crawled``, ``failed``, ``skipped`` (excluded, robots, non-HTML...).

The database is opened in WAL mode and committed after every page so that a
crash or ``kill`` loses at most the page that was being processed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from utils.helpers import utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_url TEXT NOT NULL,
    domain TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running | completed | cancelled | failed
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duplicate_hits INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS urls (
    session_id INTEGER NOT NULL,
    url TEXT NOT NULL,                 -- normalised URL
    status TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    discovered_from TEXT,
    discovered_via TEXT,               -- link | sitemap | seed | canonical
    http_status INTEGER,
    error TEXT,
    duplicate_hits INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    crawled_at TEXT,
    PRIMARY KEY (session_id, url)
);
CREATE INDEX IF NOT EXISTS idx_urls_status ON urls (session_id, status);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    requested_url TEXT,
    status_code INTEGER,
    depth INTEGER,
    ok INTEGER NOT NULL,
    error TEXT,
    response_time_ms REAL,
    crawled_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_session ON pages (session_id, id);
"""


class CrawlDatabase:
    """Small thread-safe wrapper around a SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ sessions
    def create_session(self, start_url: str, domain: str, config_json: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (start_url, domain, config_json, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (start_url, domain, config_json, utc_now_iso()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM urls u WHERE u.session_id=s.id) AS urls_total, "
                "(SELECT COUNT(*) FROM urls u WHERE u.session_id=s.id AND u.status='crawled') AS urls_crawled, "
                "(SELECT COUNT(*) FROM urls u WHERE u.session_id=s.id AND u.status='queued') AS urls_pending "
                "FROM sessions s ORDER BY s.id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_resumable_session(self, start_url: str | None = None) -> dict[str, Any] | None:
        """Most recent session that has unfinished (queued) URLs."""
        query = ("SELECT s.* FROM sessions s WHERE EXISTS "
                 "(SELECT 1 FROM urls u WHERE u.session_id=s.id AND u.status IN ('queued','discovered'))")
        params: tuple = ()
        if start_url:
            query += " AND s.start_url=?"
            params = (start_url,)
        query += " ORDER BY s.id DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def finish_session(self, session_id: int, status: str, duplicate_hits: int = 0, notes: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status=?, finished_at=?, duplicate_hits=?, notes=? WHERE id=?",
                (status, utc_now_iso(), duplicate_hits, notes, session_id),
            )
            self._conn.commit()

    def reopen_session(self, session_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET status='running', finished_at=NULL WHERE id=?", (session_id,))
            self._conn.commit()

    # ---------------------------------------------------------------------- urls
    def add_url(self, session_id: int, url: str, status: str, depth: int,
                discovered_from: str | None = None, via: str = "link", error: str | None = None,
                commit: bool = False) -> bool:
        """Insert a URL; returns False if it already existed."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO urls (session_id, url, status, depth, discovered_from, discovered_via, "
                "error, discovered_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, url, status, depth, discovered_from, via, error, utc_now_iso()),
            )
            if commit:
                self._conn.commit()
            return cur.rowcount > 0

    def set_url_status(self, session_id: int, url: str, status: str, http_status: int | None = None,
                       error: str | None = None, commit: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE urls SET status=?, http_status=COALESCE(?, http_status), error=?, crawled_at=? "
                "WHERE session_id=? AND url=?",
                (status, http_status, error, utc_now_iso(), session_id, url),
            )
            if commit:
                self._conn.commit()

    def pending_urls(self, session_id: int, statuses: tuple[str, ...] = ("queued",)) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT url, depth, discovered_from, discovered_via, status FROM urls "
                f"WHERE session_id=? AND status IN ({marks}) ORDER BY depth, rowid",
                (session_id, *statuses),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_url_keys(self, session_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT url FROM urls WHERE session_id=?", (session_id,)).fetchall()
        return [r["url"] for r in rows]

    def url_status_map(self, session_id: int) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT url, status FROM urls WHERE session_id=?", (session_id,)).fetchall()
        return {r["url"]: r["status"] for r in rows}

    def save_duplicate_hits(self, session_id: int, hits: dict[str, int]) -> None:
        if not hits:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE urls SET duplicate_hits=? WHERE session_id=? AND url=?",
                [(n, session_id, u) for u, n in hits.items()],
            )
            self._conn.commit()

    def count_status(self, session_id: int, status: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM urls WHERE session_id=? AND status=?", (session_id, status)).fetchone()[0])

    # --------------------------------------------------------------------- pages
    def add_page(self, session_id: int, record: dict[str, Any], commit: bool = True) -> int:
        """Store one page record (success or failure); commits unless ``commit=False``."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO pages (session_id, url, requested_url, status_code, depth, ok, error, "
                "response_time_ms, crawled_at, data_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, record.get("url"), record.get("requested_url"), record.get("status_code"),
                 record.get("depth"), 1 if record.get("ok") else 0, record.get("error_message"),
                 record.get("response_time_ms"), record.get("crawl_time") or utc_now_iso(),
                 json.dumps(record, ensure_ascii=False, default=str)),
            )
            if commit:
                self._conn.commit()
            return int(cur.lastrowid)

    def iter_pages(self, session_id: int) -> Iterator[dict[str, Any]]:
        """Yield stored page records in crawl order, adding ``page_id``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, data_json FROM pages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        for row in rows:
            record = json.loads(row["data_json"])
            record["page_id"] = row["id"]
            yield record

    def page_count(self, session_id: int) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM pages WHERE session_id=?", (session_id,)).fetchone()[0])

    def summary(self, session_id: int) -> dict[str, Any]:
        """Aggregate numbers for the Crawl Summary sheet (works after a resume too)."""
        with self._lock:
            c = self._conn
            session = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            counts = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM urls WHERE session_id=? GROUP BY status", (session_id,))}
            total_urls = sum(counts.values())
            pages_ok = counts.get("crawled", 0)
            pages_bad = counts.get("failed", 0)
            avg_ms = c.execute(
                "SELECT AVG(response_time_ms) FROM pages WHERE session_id=? AND response_time_ms IS NOT NULL",
                (session_id,)).fetchone()[0]
            dup_urls = c.execute(
                "SELECT COUNT(*) FROM urls WHERE session_id=? AND duplicate_hits>0", (session_id,)).fetchone()[0]
        return {
            "session_id": session_id,
            "start_url": session["start_url"],
            "domain": session["domain"],
            "status": session["status"],
            "started_at": session["started_at"],
            "finished_at": session["finished_at"],
            "total_discovered": total_urls,
            "pages_crawled": pages_ok + pages_bad,
            "successful": pages_ok,
            "failed": pages_bad,
            "skipped": counts.get("skipped", 0),
            "queued_unfinished": counts.get("queued", 0),
            "not_queued_limits": counts.get("discovered", 0),
            "duplicate_hits": session["duplicate_hits"],
            "duplicate_urls": dup_urls,
            "avg_response_ms": round(avg_ms, 1) if avg_ms is not None else None,
            "notes": session["notes"] or "",
        }

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
