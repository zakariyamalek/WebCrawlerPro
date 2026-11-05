"""Excel export: ``website_data.xlsx`` with one formatted worksheet per data type."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell  # noqa: F401  (kept for API parity)
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from exporters.csv_exporter import page_to_row
from utils.helpers import EXCEL_SAFE_LIMIT, human_duration, sanitize_cell, to_json, truncate_for_excel

log = logging.getLogger("webcrawler.export")

FONT = "Arial"
MAX_ROWS = 1_048_575          # Excel worksheet limit (minus header)
MAX_HYPERLINKS = 60_000       # Excel repairs files with > 65,530 hyperlinks per sheet
MAX_URL_LEN = 2000            # Excel hyperlink limit is 2,083 characters
HEADER_FILL = PatternFill("solid", start_color="1F3864")
FAIL_FILL = PatternFill("solid", start_color="FDE9E7")
SKIP_FILL = PatternFill("solid", start_color="FFF6DD")

STATUS_LABELS = {
    "crawled": "Crawled", "failed": "Failed", "skipped": "Skipped", "queued": "Queued (unfinished)",
    "discovered": "Not crawled (depth/page limit)",
}

PAGE_COLUMNS = [
    ("Page_ID", 8), ("URL", 45), ("Canonical_URL", 35), ("Crawl_Status", 12), ("Status_Code", 10),
    ("Page_Title", 35), ("Meta_Description", 45), ("H1", 30), ("H2", 30), ("H3", 30), ("Main_Text", 70),
    ("Language", 9), ("Content_Type", 22), ("Crawl_Depth", 8), ("Response_Time_ms", 10), ("Render_Mode", 11),
    ("Crawl_Time", 22), ("Error_Message", 40), ("Meta_Keywords", 25), ("Author", 18), ("Last_Modified", 24),
    ("Robots_Meta", 16), ("Server", 14), ("Redirect_Chain", 35), ("JSON_LD", 50),
]
URL_HEADERS = {"URL", "Canonical_URL", "Source URL", "Destination URL", "External Destination URL", "Page URL",
               "Image URL", "Local File"}


class SheetWriter:
    """Append rows to a worksheet with sanitising, hyperlinks, width tracking and a row cap."""

    def __init__(self, ws: Worksheet, headers: list[tuple[str, int]], wrap: set[str] | None = None) -> None:
        self.ws = ws
        self.headers = [h for h, _ in headers]
        self.min_widths = [w for _, w in headers]
        self.wrap = wrap or set()
        self.widths = [min(len(h) + 3, 14) for h in self.headers]
        self.rows = 0
        self.hyperlinks = 0
        self.truncated_rows = 0
        self.truncated_cells = 0
        ws.append(self.headers)

    def add(self, values: list[Any], fill: PatternFill | None = None) -> bool:
        if self.rows >= MAX_ROWS:
            self.truncated_rows += 1
            return False
        clean: list[Any] = []
        for i, value in enumerate(values):
            value = sanitize_cell(value)
            if isinstance(value, str):
                if len(value) > EXCEL_SAFE_LIMIT:
                    self.truncated_cells += 1
                value = truncate_for_excel(value)
                self.widths[i] = max(self.widths[i], min(len(value.split("\n", 1)[0]), 80))
            elif value is not None:
                self.widths[i] = max(self.widths[i], min(len(str(value)), 20))
            clean.append(value)
        self.ws.append(clean)
        self.rows += 1
        row_idx = self.ws.max_row
        for i, header in enumerate(self.headers, start=1):
            cell = self.ws.cell(row=row_idx, column=i)
            cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=header in self.wrap)
            value = cell.value
            if (header in URL_HEADERS and isinstance(value, str) and value.lower().startswith(("http://", "https://"))
                    and len(value) <= MAX_URL_LEN and self.hyperlinks < MAX_HYPERLINKS):
                cell.hyperlink = value
                cell.font = Font(name=FONT, size=10, color="0563C1", underline="single")
                self.hyperlinks += 1
            if fill is not None:
                cell.fill = fill
        return True

    def finish(self) -> None:
        ws = self.ws
        for cell in ws[1]:
            cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(vertical="center", horizontal="left", wrap_text=True)
        ws.row_dimensions[1].height = 22
        for i, (min_w, seen) in enumerate(zip(self.min_widths, self.widths), start=1):
            ws.column_dimensions[get_column_letter(i)].width = max(10, min(max(seen + 2, 10), max(min_w, 12)))
        ws.freeze_panes = "A2"
        last_col = get_column_letter(len(self.headers))
        ws.auto_filter.ref = f"A1:{last_col}{max(ws.max_row, 2)}"


def _status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "Unknown")


def export_excel(pages: Iterable[dict[str, Any]], summary: dict[str, Any], url_status: dict[str, str],
                 path: str | Path, output_dir: str | Path | None = None,
                 config_snapshot: dict[str, Any] | None = None) -> dict[str, int]:
    """Write the multi-sheet workbook; returns ``{sheet name: data rows}``."""
    wb = Workbook()
    ws_pages = wb.active
    ws_pages.title = "Pages"
    ws_head = wb.create_sheet("Headings")
    ws_int = wb.create_sheet("Internal Links")
    ws_ext = wb.create_sheet("External Links")
    ws_img = wb.create_sheet("Images")
    ws_tab = wb.create_sheet("Tables")
    ws_sum = wb.create_sheet("Crawl Summary")
    ws_grid = wb.create_sheet("Table Grids")
    ws_form = wb.create_sheet("Forms")

    pages_w = SheetWriter(ws_pages, PAGE_COLUMNS, wrap={"Main_Text", "Meta_Description", "Error_Message", "H1", "H2", "Page_Title"})
    head_w = SheetWriter(ws_head, [("Page URL", 50), ("Heading Level", 10), ("Heading Text", 70)], wrap={"Heading Text"})
    int_w = SheetWriter(ws_int, [("Source URL", 50), ("Link Text", 35), ("Destination URL", 50), ("Crawl Status", 24)])
    ext_w = SheetWriter(ws_ext, [("Source URL", 50), ("Link Text", 35), ("External Destination URL", 55), ("Link Type", 10), ("Rel", 14)])
    img_w = SheetWriter(ws_img, [("Page URL", 45), ("Image URL", 55), ("Alt Text", 35), ("Image Title", 25),
                                 ("Dimensions", 12), ("Extension", 10), ("Downloaded File", 28)])
    tab_w = SheetWriter(ws_tab, [("Source URL", 50), ("Table Number", 8), ("Caption", 25), ("Rows", 8), ("Columns", 8),
                                 ("Column Headings", 40), ("Extracted Table Data (JSON)", 80)], wrap={"Extracted Table Data (JSON)"})
    form_w = SheetWriter(ws_form, [("Page URL", 50), ("Form Action", 50), ("Method", 8), ("Form Id/Name", 18),
                                   ("Input Fields (name:type)", 55), ("Buttons", 25), ("Has Password Field", 10)])

    # Table Grids sheet: each table laid out as a real grid, one block after another.
    ws_grid.append(["Source URL", "Table Number", "Caption", "Row", "Data ->"])
    grid_rows = 1
    grid_truncated = False
    image_dir = None
    if output_dir is not None:
        image_dir = Path(output_dir) / "images"

    for page in pages:
        row = page_to_row(page)
        status = page.get("status")
        fill = FAIL_FILL if status == "failed" else SKIP_FILL if status == "skipped" else None
        pages_w.add([row.get(name) for name, _ in PAGE_COLUMNS], fill)
        url = page.get("url")
        for h in page.get("headings", []):
            head_w.add([url, f"H{h['level']}", h["text"]])
        for link in page.get("links", []):
            ltype = link.get("type")
            if link.get("internal") and ltype in ("page", "file"):
                dest_status = url_status.get(link.get("normalized") or "", None)
                int_w.add([url, link.get("text"), link.get("url"), _status_label(dest_status) if dest_status else "Not tracked"])
            elif not link.get("internal") and ltype in ("page", "file", "mailto", "tel"):
                ext_w.add([url, link.get("text"), link.get("url"), ltype, link.get("rel")])
        for img in page.get("images", []):
            local = ""
            if image_dir is not None:
                from utils.helpers import image_filename
                candidates = list(image_dir.glob(f"session_*/{image_filename(img['url'])}"))
                local = str(candidates[0].relative_to(Path(output_dir))) if candidates else ""
            img_w.add([url, img["url"], img.get("alt"), img.get("title"), img.get("dimensions"), img.get("extension"), local])
        for table in page.get("tables", []):
            tab_w.add([url, table["index"], table.get("caption"), table["row_count"], table["column_count"],
                       " | ".join(table["headers"]), to_json({"headers": table["headers"], "rows": table["rows"]})])
            needed = 3 + len(table["rows"]) + 1
            if grid_rows + needed >= MAX_ROWS:
                grid_truncated = True
                continue
            ws_grid.append([url, table["index"], table.get("caption") or "", "Header" if table["headers"] else "", *[sanitize_cell(truncate_for_excel(str(c))) for c in table["headers"]]])
            grid_rows += 1
            if table["headers"]:
                for cell in ws_grid[ws_grid.max_row]:
                    cell.font = Font(name=FONT, size=10, bold=True)
            for n, r in enumerate(table["rows"], start=1):
                ws_grid.append([None, None, None, n, *[sanitize_cell(truncate_for_excel(str(c))) for c in r]])
                grid_rows += 1
            ws_grid.append([])
            grid_rows += 1
        for form in page.get("forms", []):
            form_w.add([url, form.get("action"), form.get("method"), form.get("id") or form.get("name"),
                        ", ".join(f"{f['name'] or '(unnamed)'}:{f['type']}" for f in form["fields"]),
                        " | ".join(form["buttons"]), "Yes" if form.get("has_password_field") else "No"])

    writers = {"Pages": pages_w, "Headings": head_w, "Internal Links": int_w, "External Links": ext_w,
               "Images": img_w, "Tables": tab_w, "Forms": form_w}
    for w in writers.values():
        w.finish()
    for cell in ws_grid[1]:
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    for col, width in zip("ABCDE", (45, 8, 25, 8, 20)):
        ws_grid.column_dimensions[col].width = width
    ws_grid.freeze_panes = "A2"

    notes: list[str] = []
    for name, w in writers.items():
        if w.truncated_rows:
            notes.append(f"Sheet '{name}' hit Excel's row limit; {w.truncated_rows} rows omitted (see CSV/database).")
        if w.truncated_cells:
            notes.append(f"Sheet '{name}': {w.truncated_cells} cell(s) exceeded Excel's 32,767-character limit and were truncated.")
        if w.hyperlinks >= MAX_HYPERLINKS:
            notes.append(f"Sheet '{name}': hyperlinks limited to {MAX_HYPERLINKS:,} (Excel limit).")
    if grid_truncated:
        notes.append("Sheet 'Table Grids' hit the Excel row limit; some tables omitted.")
    _write_summary(ws_sum, summary, url_status, notes, config_snapshot, pages_w.rows)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    counts = {name: w.rows for name, w in writers.items()}
    log.info("Excel written: %s", out)
    return counts


def _write_summary(ws: Worksheet, s: dict[str, Any], url_status: dict[str, str], notes: list[str],
                   config_snapshot: dict[str, Any] | None, page_rows: int) -> None:
    started, finished = s.get("started_at"), s.get("finished_at")
    duration = ""
    try:
        if started and finished:
            duration = human_duration((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds())
    except ValueError:
        pass
    rows: list[tuple[str, Any]] = [
        ("Start URL", s.get("start_url")), ("Target domain", s.get("domain")), ("Crawl status", s.get("status")),
        ("Total URLs discovered", s.get("total_discovered")), ("Total pages crawled", s.get("pages_crawled")),
        ("Successful pages", s.get("successful")), ("Failed pages", s.get("failed")),
        ("Skipped pages", s.get("skipped")),
        ("Discovered but not crawled (depth/page limit)", s.get("not_queued_limits")),
        ("Queued but unfinished (resumable)", s.get("queued_unfinished")),
        ("Duplicate URLs (distinct URLs seen more than once)", s.get("duplicate_urls")),
        ("Duplicate URL encounters suppressed", s.get("duplicate_hits")),
        ("Average response time (ms)", s.get("avg_response_ms")),
        ("Crawl start (UTC)", started), ("Crawl end (UTC)", finished), ("Duration", duration),
    ]
    ws.append(["Metric", "Value"])
    for label, value in rows:
        ws.append([label, sanitize_cell(value)])
    ws.append([])
    ws.append(["Settings used", ""])
    for key in ("max_depth", "max_pages", "concurrency", "delay", "timeout", "javascript", "include_subdomains",
                "respect_robots", "use_sitemaps", "output_format"):
        if config_snapshot and key in config_snapshot:
            ws.append([key, sanitize_cell(config_snapshot[key])])
    if notes:
        ws.append([])
        ws.append(["Export notes", ""])
        for n in notes:
            ws.append([n, ""])
    for r in ws.iter_rows():
        for cell in r:
            cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for cell in ws[1]:
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    for row in ws.iter_rows(min_col=1, max_col=1):
        text = row[0].value
        if text in ("Settings used", "Export notes"):
            row[0].font = Font(name=FONT, size=10, bold=True)
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 60
    ws.freeze_panes = "A2"
