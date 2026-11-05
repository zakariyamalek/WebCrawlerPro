"""CSV / XLSX export tests."""
from __future__ import annotations

import csv
import io

import openpyxl

from config import CrawlConfig
from exporters.csv_exporter import CSV_COLUMNS, export_csv
from exporters.excel_exporter import export_excel
from utils.helpers import sanitize_cell

SUMMARY = {"start_url": "https://e.com", "domain": "e.com", "status": "completed", "total_discovered": 2,
           "pages_crawled": 2, "successful": 1, "failed": 1, "skipped": 0, "duplicate_urls": 0,
           "duplicate_hits": 0, "avg_response_ms": 5.0, "started_at": "2026-01-01T00:00:00+00:00",
           "finished_at": "2026-01-01T00:01:00+00:00", "not_queued_limits": 0, "queued_unfinished": 0}


def record(**kw):
    base = {"page_id": 1, "ok": True, "status": "crawled", "url": "https://e.com/", "canonical_url": None,
            "status_code": 200, "title": 'Title, with "quotes"', "meta_description": "line1\nline2",
            "headings": [{"level": 1, "text": "Head"}, {"level": 2, "text": "Sub"}], "main_text": "Café 日本語 ☕\nsecond",
            "paragraphs": ["p1"], "links": [
                {"text": "a", "url": "https://e.com/a", "normalized": "https://e.com/a", "type": "page", "internal": True, "rel": None, "target": None},
                {"text": "x", "url": "https://x.org/", "normalized": "https://x.org/", "type": "page", "internal": False, "rel": "nofollow", "target": None}],
            "images": [{"url": "https://e.com/i.png", "alt": "alt", "title": "", "dimensions": "1x1", "extension": "png"}],
            "tables": [{"index": 1, "caption": "Cap", "headers": ["A", "B"], "rows": [["1", "2"]], "row_count": 1, "column_count": 2}],
            "metadata": {"json_ld": [{"@type": "Thing"}]}, "forms": [], "technical": {}, "depth": 0,
            "crawl_time": "2026-01-01T00:00:00+00:00", "error_message": None, "response_time_ms": 3.0, "render_mode": "static"}
    base.update(kw)
    return base


def test_csv_roundtrip_with_special_characters(tmp_path):
    path = tmp_path / "website_data.csv"
    export_csv([record()], path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                       # UTF-8 (with BOM for Excel)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    assert list(rows[0].keys()) == CSV_COLUMNS
    r = rows[0]
    assert r["Page_Title"] == 'Title, with "quotes"' and r["Meta_Description"] == "line1\nline2"
    assert r["Main_Text"] == "Café 日本語 ☕\nsecond" and r["H1"] == "Head" and r["H2"] == "Sub"
    assert r["Internal_Links"] == "https://e.com/a" and r["External_Links"] == "https://x.org/"
    assert '"Cap"' in r["Table_Data"] and "Thing" in r["JSON_LD"]


def test_formula_injection_is_neutralised(tmp_path):
    assert sanitize_cell("=SUM(A1)") == "'=SUM(A1)" and sanitize_cell("@cmd") == "'@cmd"
    assert sanitize_cell("-5") == "-5" and sanitize_cell("plain") == "plain" and sanitize_cell(7) == 7
    path = tmp_path / "w.xlsx"
    export_excel([record(title="=HYPERLINK(\"http://evil\")")], SUMMARY, {}, path)
    ws = openpyxl.load_workbook(path)["Pages"]
    cell = [c for c in ws[1] if c.value == "Page_Title"][0]
    assert ws.cell(row=2, column=cell.column).data_type == "s"
    assert ws.cell(row=2, column=cell.column).value.startswith("'=")


def test_excel_structure_and_formatting(tmp_path):
    path = tmp_path / "website_data.xlsx"
    export_excel([record(), record(page_id=2, status="failed", ok=False, url="https://e.com/bad", status_code=404,
                                   error_message="HTTP 404", headings=[], links=[], images=[], tables=[])],
                 SUMMARY, {"https://e.com/a": "crawled"}, path, config_snapshot=CrawlConfig().to_dict())
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames[:7] == ["Pages", "Headings", "Internal Links", "External Links", "Images", "Tables", "Crawl Summary"]
    pages = wb["Pages"]
    assert pages.max_row == 3 and pages.freeze_panes == "A2" and pages.auto_filter.ref
    assert pages["A1"].font.bold and pages["B2"].hyperlink is not None
    assert wb["Headings"]["B2"].value == "H1" and wb["Headings"]["C3"].value == "Sub"
    assert wb["Internal Links"]["D2"].value == "Crawled"
    assert wb["External Links"]["C2"].value == "https://x.org/"
    assert wb["Images"]["C2"].value == "alt"
    assert wb["Tables"]["F2"].value == "A | B"
    grid = [[c.value for c in r] for r in wb["Table Grids"].iter_rows()]
    assert ["https://e.com/", 1, "Cap", "Header", "A", "B"] in grid and [None, None, None, 1, "1", "2"] in grid
    summary = dict(wb["Crawl Summary"].iter_rows(values_only=True, max_col=2))
    assert summary["Successful pages"] == 1 and summary["Failed pages"] == 1 and summary["Duration"] == "1m 0s"


def test_excel_limits_and_illegal_characters(tmp_path):
    path = tmp_path / "big.xlsx"
    huge = "x" * 40000
    export_excel([record(main_text=huge + "\x00\x07bad")], SUMMARY, {}, path)
    wb = openpyxl.load_workbook(path)
    col = [c for c in wb["Pages"][1] if c.value == "Main_Text"][0].column
    text = wb["Pages"].cell(row=2, column=col).value
    assert len(text) <= 32767 and "truncated" in text
    notes = [r[0] for r in wb["Crawl Summary"].iter_rows(values_only=True) if r[0] and "32,767" in str(r[0])]
    assert notes
