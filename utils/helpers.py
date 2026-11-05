"""Small shared helpers (text cleaning, spreadsheet-safe values, timestamps)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

# Control characters that are illegal in XLSX (XML 1.0) cells.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_WS = re.compile(r"[ \t\r\f\v\u00a0]+")
EXCEL_CELL_LIMIT = 32767
EXCEL_SAFE_LIMIT = 32000
_FORMULA_PREFIXES = ("=", "+", "@", "\t", "\r")
SENSITIVE_HEADERS = {"set-cookie", "cookie", "authorization", "proxy-authorization", "x-api-key", "x-auth-token"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def collapse_ws(text: str) -> str:
    """Collapse runs of whitespace (but not newlines) and strip."""
    return _WS.sub(" ", text).strip()


def strip_illegal_chars(text: str) -> str:
    return _ILLEGAL_XML.sub("", text)


def to_json(value: Any) -> str:
    """Compact, readable, UTF-8 friendly JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def sanitize_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection for text from untrusted pages.

    A cell whose text begins with ``= + @`` (or tab/CR) is executed as a formula by
    Excel/LibreOffice.  A leading ``-`` followed by something other than a digit or
    space is also treated as dangerous.  Such text is prefixed with an apostrophe.
    Non-string values pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    value = strip_illegal_chars(value)
    if value.startswith(_FORMULA_PREFIXES) or (
        value.startswith("-") and len(value) > 1 and not (value[1].isdigit() or value[1] in " .-")
    ):
        return "'" + value
    return value


def truncate_for_excel(text: str, limit: int = EXCEL_SAFE_LIMIT) -> str:
    """Trim text to Excel's per-cell limit, noting how much was cut."""
    if len(text) <= limit:
        return text
    note = f" …[truncated: {len(text):,} chars total - see CSV for full text]"
    return text[: limit - len(note)] + note


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove secrets (cookies, auth) from response headers before storage."""
    return {k: ("[redacted]" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


def image_filename(url: str) -> str:
    """Deterministic local file name for a downloaded image (hash + original extension)."""
    import hashlib
    import posixpath
    from urllib.parse import urlsplit
    ext = posixpath.splitext(urlsplit(url).path)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{2,5}", ext or ""):
        ext = ".img"
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + ext
