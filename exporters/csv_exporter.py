"""CSV export: ``website_data.csv`` (UTF-8, one row per page)."""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Iterable

from utils.helpers import sanitize_cell, to_json

log = logging.getLogger("webcrawler.export")

CSV_COLUMNS = [
    "Page_ID", "URL", "Canonical_URL", "Status_Code", "Page_Title", "Meta_Description", "H1", "H2", "H3",
    "All_Headings", "Main_Text", "Paragraphs", "Internal_Links", "External_Links", "Image_URLs",
    "Image_Alt_Text", "Table_Data", "JSON_LD", "Content_Type", "Crawl_Depth", "Crawl_Time", "Error_Message",
    # additional columns
    "Crawl_Status", "Requested_URL", "Meta_Keywords", "Author", "Language", "Last_Modified", "Robots_Meta",
    "Server", "Response_Time_ms", "Redirect_Chain", "Render_Mode", "Open_Graph", "Twitter_Card", "Forms",
]


def _join(values: Iterable[Any], sep: str = " | ") -> str:
    return sep.join(str(v) for v in values if v not in (None, ""))


def page_to_row(page: dict[str, Any]) -> dict[str, Any]:
    """Flatten one page record into the CSV column layout."""
    headings = page.get("headings", [])
    by_level = lambda n: _join(h["text"] for h in headings if h["level"] == n)  # noqa: E731
    links = page.get("links", [])
    internal = [l["url"] for l in links if l.get("internal") and l.get("type") in ("page", "file")]
    external = [l["url"] for l in links if not l.get("internal") and l.get("type") in ("page", "file", "mailto", "tel")]
    images = page.get("images", [])
    meta = page.get("metadata") or {}
    tech = page.get("technical") or {}
    tables = [{"index": t["index"], "caption": t.get("caption"), "headers": t["headers"], "rows": t["rows"]}
              for t in page.get("tables", [])]
    return {
        "Page_ID": page.get("page_id"), "URL": page.get("url"), "Canonical_URL": page.get("canonical_url"),
        "Status_Code": page.get("status_code"), "Page_Title": page.get("title"),
        "Meta_Description": page.get("meta_description"),
        "H1": by_level(1), "H2": by_level(2), "H3": by_level(3),
        "All_Headings": _join(f"H{h['level']}: {h['text']}" for h in headings),
        "Main_Text": page.get("main_text", ""), "Paragraphs": "\n".join(page.get("paragraphs", [])),
        "Internal_Links": _join(internal), "External_Links": _join(external),
        "Image_URLs": _join(i["url"] for i in images), "Image_Alt_Text": _join(i.get("alt") for i in images),
        "Table_Data": to_json(tables) if tables else "",
        "JSON_LD": to_json(meta["json_ld"]) if meta.get("json_ld") else "",
        "Content_Type": page.get("content_type"), "Crawl_Depth": page.get("depth"),
        "Crawl_Time": page.get("crawl_time"), "Error_Message": page.get("error_message"),
        "Crawl_Status": page.get("status"), "Requested_URL": page.get("requested_url"),
        "Meta_Keywords": page.get("meta_keywords"), "Author": page.get("author"), "Language": page.get("language"),
        "Last_Modified": page.get("last_modified"), "Robots_Meta": tech.get("robots_meta"),
        "Server": tech.get("server"), "Response_Time_ms": page.get("response_time_ms"),
        "Redirect_Chain": _join(tech.get("redirect_chain", [])), "Render_Mode": page.get("render_mode"),
        "Open_Graph": to_json(meta["open_graph"]) if meta.get("open_graph") else "",
        "Twitter_Card": to_json(meta["twitter_card"]) if meta.get("twitter_card") else "",
        "Forms": to_json(page["forms"]) if page.get("forms") else "",
    }


def export_csv(pages: Iterable[dict[str, Any]], path: str | Path) -> int:
    """Write *pages* to *path*.  Returns the number of rows written.

    The file is UTF-8 with a byte-order mark (``utf-8-sig``) so Excel detects the
    encoding; the BOM is ignored by every other CSV reader.  ``csv.writer`` quotes
    commas, quotes and newlines.  Text that could run as a spreadsheet formula is
    neutralised (see ``sanitize_cell``).
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writeheader()
        for page in pages:
            row = page_to_row(page)
            writer.writerow({k: sanitize_cell(v) if v is not None else "" for k, v in row.items()})
            count += 1
    log.info("CSV written: %s (%d rows)", out, count)
    return count
