"""Thread-safe duplicate URL tracker with per-URL hit counts."""
from __future__ import annotations

import threading
from collections import Counter


class DuplicateFilter:
    """Remembers normalised URLs that were already discovered.

    ``add`` returns True the first time a URL is seen and False afterwards; every
    repeat is counted so the crawl summary can report how many duplicates were
    suppressed.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._hits: Counter[str] = Counter()
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                self._hits[key] += 1
                return False
            self._seen.add(key)
            return True

    def mark_seen(self, key: str) -> None:
        """Remember *key* without counting it as a duplicate hit."""
        with self._lock:
            self._seen.add(key)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._seen

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    @property
    def duplicate_hits(self) -> int:
        """Total number of suppressed duplicate encounters."""
        with self._lock:
            return sum(self._hits.values())

    @property
    def duplicated_urls(self) -> int:
        """Number of distinct URLs that were encountered more than once."""
        with self._lock:
            return len(self._hits)

    def hit_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._hits)
