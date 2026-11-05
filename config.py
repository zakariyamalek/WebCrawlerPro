"""Central configuration for WebCrawler Pro.

All behaviour is controlled by :class:`CrawlConfig`.  Values are resolved in
this order (later wins):

1. Built-in defaults (this file)
2. A configuration file (``--config crawler_config.toml`` or ``.json``)
3. Command-line options

No source code needs to be edited to change any setting.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - project requires 3.11+, kept for clearer error
    raise RuntimeError("WebCrawler Pro requires Python 3.11 or newer.")

APP_NAME = "WebCrawler Pro"
APP_FULL_NAME = "WebCrawler Pro – Automated Website Intelligence & Data Extraction System"
APP_VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "crawl_history.sqlite3"

# Query parameters that only track users/sessions.  They are removed during URL
# normalisation so that tracking variants of one page are not crawled twice and
# so that session identifiers/tokens are never stored or exported.
DEFAULT_STRIP_PARAMS: list[str] = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid",
    "_ga", "_gl", "ref_src", "spm",
    # Session identifiers / credentials - never kept (privacy requirement)
    "phpsessid", "jsessionid", "sessionid", "session_id", "sid", "aspsessionid",
    "token", "access_token", "id_token", "auth", "authkey", "apikey", "api_key",
    "password", "passwd", "pwd", "secret",
]

# URL patterns (regular expressions, searched against the full URL) that are
# excluded by default because they usually produce unbounded or harmful crawls.
DEFAULT_EXCLUDE_PATTERNS: list[str] = [
    r"(?i)/(log|sign)[-_]?out\b",                  # state-changing links
    r"(?i)/wp-admin\b|/wp-login\.php",             # admin / login screens
    r"(?i)[?&](replytocom|add-to-cart|action=logout)\b",
    r"(?i)[?&](month|year|date|day|cal|calendar|start_date|end_date)=",  # calendars
    r"(?i)/(calendar|events/(list|month|day))/\d{4}[-/]\d{1,2}",          # calendar archives
    r"(?i)/\d{4}/\d{1,2}/\d{1,2}/\d{4}/\d{1,2}/\d{1,2}",                  # chained date paths
    r"(?i)[?&](s|q|query|search|keyword|keywords)=",                        # search results
    r"(?i)/search/?($|\?)",                                                  # search pages
    r"(?i)[?&](sort|order|orderby|dir|view|display|limit|per_?page)=",      # sort/filter permutations
]

BINARY_EXTENSIONS: frozenset[str] = frozenset({
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "tif", "tiff", "avif",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf",
    "zip", "rar", "7z", "gz", "tar", "bz2", "xz", "iso", "dmg", "exe", "msi", "apk", "bin",
    "mp3", "wav", "ogg", "flac", "aac", "m4a", "mp4", "avi", "mov", "wmv", "mkv", "webm", "flv",
    "woff", "woff2", "ttf", "eot", "otf", "css", "js", "map", "json", "xml", "csv", "txt",
})


@dataclass
class CrawlConfig:
    """All user-tunable crawler settings with sensible defaults."""

    # --- target -----------------------------------------------------------
    url: str = ""

    # --- crawl limits ------------------------------------------------------
    max_depth: int = 5                 # homepage = depth 0
    max_pages: int = 1000              # hard cap on pages requested
    timeout: float = 30.0              # request timeout (seconds)
    concurrency: int = 4               # concurrent requests
    delay: float = 1.0                 # delay between requests to the same site (s)
    max_retries: int = 2               # retries for transient failures
    backoff_base: float = 2.0          # exponential backoff base delay (s)
    backoff_max: float = 60.0          # cap for backoff / Retry-After waits (s)
    max_response_mb: float = 10.0      # responses larger than this are skipped
    user_agent: str = (
        f"WebCrawlerPro/{APP_VERSION} (+authorized-site-crawler; respects robots.txt)"
    )

    # --- scope -------------------------------------------------------------
    include_subdomains: bool = False
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    strip_params: list[str] = field(default_factory=lambda: list(DEFAULT_STRIP_PARAMS))
    include_binary_files: bool = False  # request (but never parse) binary-looking URLs
    max_path_depth: int = 12            # URL-trap guard: max path segments
    max_query_params: int = 6           # URL-trap guard: max query parameters

    # --- robots / sitemaps ---------------------------------------------------
    respect_robots: bool = True
    use_sitemaps: bool = True
    max_sitemaps: int = 50

    # --- JavaScript rendering -----------------------------------------------
    javascript: str = "auto"           # auto | always | never
    render_timeout: float = 30.0
    browser: str = "chromium"          # chromium | firefox | webkit
    headless: bool = True
    js_min_text_chars: int = 200       # static pages with less text may need rendering

    # --- extraction ----------------------------------------------------------
    extract_tables: bool = True
    extract_metadata: bool = True
    extract_external_links: bool = True
    extract_images: bool = True        # extract image *information*
    extract_forms: bool = True
    download_images: bool = False      # actually download image files
    max_image_mb: float = 5.0

    # --- output ---------------------------------------------------------------
    output_format: str = "both"        # csv | xlsx | both
    output_dir: str = str(DEFAULT_OUTPUT_DIR)
    db_path: str = str(DEFAULT_DB_PATH)

    # --- safety -----------------------------------------------------------------
    allow_private_network: bool = False  # ONLY for crawling your own local test sites
    use_system_proxy: bool = False       # proxies would bypass SSRF DNS checks

    # --- runtime / logging ---------------------------------------------------------
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Raise ``ValueError`` when a setting is out of range."""
        problems: list[str] = []
        if self.max_depth < 0:
            problems.append("max_depth must be >= 0")
        if self.max_pages < 1:
            problems.append("max_pages must be >= 1")
        if self.timeout <= 0:
            problems.append("timeout must be > 0")
        if self.concurrency < 1:
            problems.append("concurrency must be >= 1")
        if self.delay < 0:
            problems.append("delay must be >= 0")
        if self.max_retries < 0:
            problems.append("max_retries must be >= 0")
        if self.javascript not in {"auto", "always", "never"}:
            problems.append("javascript must be auto, always or never")
        if self.browser not in {"chromium", "firefox", "webkit"}:
            problems.append("browser must be chromium, firefox or webkit")
        if self.output_format not in {"csv", "xlsx", "both"}:
            problems.append("output_format must be csv, xlsx or both")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            problems.append("log_level must be DEBUG, INFO, WARNING or ERROR")
        import re
        for label, patterns in (("include", self.include_patterns), ("exclude", self.exclude_patterns)):
            for pat in patterns:
                try:
                    re.compile(pat)
                except re.error as exc:
                    problems.append(f"invalid {label} pattern {pat!r}: {exc}")
        if problems:
            raise ValueError("; ".join(problems))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CrawlConfig":
        """Build a config, ignoring unknown keys (forward compatible)."""
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    @classmethod
    def from_file(cls, path: str | Path) -> "CrawlConfig":
        return cls.from_dict(load_config_file(path))


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load a ``.toml`` or ``.json`` configuration file into a flat dict.

    TOML files may use a ``[crawler]`` table or be flat.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Configuration file not found: {p}")
    raw = p.read_bytes()
    if p.suffix.lower() == ".json":
        data = json.loads(raw.decode("utf-8"))
    elif p.suffix.lower() == ".toml":
        data = tomllib.loads(raw.decode("utf-8"))
    else:
        raise ValueError("Configuration file must end in .toml or .json")
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat
