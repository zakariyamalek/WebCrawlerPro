# WebCrawler Pro – Automated Website Intelligence & Data Extraction System

A modular, Scrapy-based recursive website crawler. Give it a URL and it discovers internal links (HTML, sitemaps,
canonical tags), visits every eligible page, extracts structured information, and exports **CSV** and **Excel**
reports. Optional Playwright rendering handles JavaScript sites; SQLite provides crawl history and resume.

> **Use only on websites you own or have permission to crawl.** See [Ethical and legal considerations](#ethical-and-legal-considerations).

---

## 1. Requirements

* **Python 3.11 or newer** (check with `python --version`). Older versions fail because the project uses `tomllib`.
* Internet access for installing packages.
* **Linux GUI only:** the system package `python3-tk` (it cannot be installed with pip).

### Python packages used

| Package | Needed for | Required? |
|---|---|---|
| `scrapy` (installs `twisted` automatically) | Crawling engine | **Yes** |
| `beautifulsoup4` | HTML parsing | **Yes** |
| `lxml` | Fast HTML parser used by BeautifulSoup | **Yes** |
| `openpyxl` | Excel (.xlsx) export | **Yes** |
| `playwright` | JavaScript-rendered pages | Only for `--javascript auto/always` (default is `auto`; install it, or use `--javascript never`) |
| `customtkinter` | Graphical interface (`--gui`) | Only for the GUI |
| `pytest` | Running the tests | Only for development |

> `requirements.txt` also lists `pandas` and `httpx`. The current code does **not** import them, so if they
> fail to install you can safely delete those two lines.

---

## 2. Installation (step by step)

### Windows (PowerShell)
```powershell
cd WebCrawlerPro
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

### Linux / macOS
```bash
cd WebCrawlerPro
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
sudo apt install python3-tk        # Linux, only if you want the GUI
```

### Install only the essentials (manual)
```bash
pip install scrapy beautifulsoup4 lxml openpyxl          # core
pip install playwright && playwright install chromium    # JavaScript sites
pip install customtkinter                                # GUI
pip install pytest                                       # tests
```

### Check the installation
```bash
python main.py --help
python -c "import scrapy, bs4, lxml, openpyxl; print('core packages OK')"
```

---

## 3. Running

```bash
python main.py --url https://example.com
python main.py --url https://example.com --depth 5 --max-pages 1000 --delay 1 --format both --javascript auto
python main.py --url https://example.com --depth 3 --max-pages 500 --format xlsx
python main.py --gui
python main.py --help
```

Before crawling you must confirm authorization: type `yes` when asked, or pass `--accept-authorization` in scripts.

| Option | Default | Meaning |
|---|---|---|
| `--url` | – | Target site (http/https; a bare `example.com` becomes https) |
| `--depth N` | 5 | Maximum link depth (homepage = 0) |
| `--max-pages N` | 1000 | Maximum pages requested |
| `--delay S` / `--concurrency N` / `--timeout S` / `--retries N` | 1.0 / 4 / 30 / 2 | Politeness and reliability |
| `--subdomains` | off | Also crawl subdomains (`www.` is always treated as the same site) |
| `--include REGEX` / `--exclude REGEX` | – | Repeatable URL filters (`--exclude` replaces the built-in exclusions) |
| `--robots/--no-robots`, `--sitemaps/--no-sitemaps` | on | robots.txt and sitemap discovery |
| `--javascript auto\|always\|never` | auto | Also `--browser`, `--render-timeout`, `--headless/--no-headless` |
| `--tables`, `--metadata`, `--external-links`, `--images` (each has `--no-…`) | on | Extraction switches |
| `--download-images` | off | Saves image files to `<output>/images/` |
| `--include-binary` | off | Request pdf/zip URLs (recorded, never parsed) |
| `--format csv\|xlsx\|both` | both | Output format |
| `--output-dir`, `--db` | `output/` | Output folder; history DB defaults to `<output-dir>/crawl_history.sqlite3` |
| `--config FILE` | – | TOML/JSON settings file (see `crawler_config.example.toml`); command line overrides it |
| `--list-sessions`, `--resume [ID]`, `--export ID` | – | Session management |
| `--allow-private-network` | off | **Turns off SSRF protection** – only for your own local test sites |

### Stop, resume, history
* **Ctrl-C once** = graceful stop; in-flight requests finish and partial results are exported.
* `python main.py --list-sessions`, then `python main.py --resume 3` continues without re-crawling finished pages.
* `python main.py --export 3 --format xlsx` re-exports a stored session.
* Exit codes: `0` completed, `3` cancelled, `2` invalid input / authorization declined, `1` failed.

### GUI
`python main.py --gui` – enter the URL, tick the authorization box and press **Start Crawling**. The dashboard shows
discovered / queued / crawled / remaining / successful / failed counts, current depth and URL, an **estimated**
progress bar (it grows as new links are found, so it is not a completion guarantee) and a live activity log.
Pause/Resume and Stop are available. The crawl runs in a separate process, so the window never freezes.

---

## 4. Output files

| File | Content |
|---|---|
| `output/csv/website_data.csv` | One row per page, UTF-8 (with BOM so Excel shows Unicode correctly), proper quoting |
| `output/excel/website_data.xlsx` | Sheets: Pages, Headings, Internal Links, External Links, Images, Tables, Crawl Summary, Table Grids, Forms |
| `output/logs/crawl_session_N.log` | Activity log |
| `output/crawl_history.sqlite3` | All sessions, URLs and page records |
| `output/images/` | Only when `--download-images` is used |

Notes: each run overwrites the two `website_data.*` files (use `--output-dir` per crawl to keep several).
Excel cells over 32,767 characters are truncated (the CSV keeps the full text). Scraped text starting with `=`, `+`
or `@` is prefixed with `'` so spreadsheets never run it as a formula.

---

## 5. Features

* **Crawling:** Scrapy engine, breadth-first, depth/page limits, concurrency, delays, retries with exponential back-off, honours `Retry-After`, automatic slow-down on 429/503, crash-safe resume.
* **Discovery:** anchor links, relative→absolute URLs, `sitemap.xml`, `Sitemap:` lines in robots.txt, sitemap indexes, nested and `.gz` sitemaps, canonical links.
* **Duplicate prevention:** normalises trailing slashes, fragments, URL encoding, dot-segments, duplicate and tracking parameters. Different query values (`?id=1`, `?id=2`) are never merged.
* **Extraction:** title, meta tags, canonical URL, language, headings H1–H6, text, paragraphs, lists, quotes, captions, links, images, tables (rowspan/colspan), Open Graph, Twitter Cards, JSON-LD, microdata, forms (structure only), response headers, redirects, timing.
* **JavaScript:** Playwright is used only when a page looks like an empty JavaScript shell (or always / never, by option).
* **Safety:** SSRF protection (host names, IPs, redirects, DNS rebinding, browser sub-requests); no logins, form submission or CAPTCHA bypass; cookies disabled; secrets redacted.

## 6. Project structure

```
WebCrawlerPro/
              ├── main.py                      CLI entry point (+ --gui)
              ├── config.py                    settings and defaults
              ├── requirements.txt
              ├── crawler_config.example.toml
              ├── crawler/                     spider, extractor, URL normaliser/discovery, robots, sitemaps, JS renderer, runner
              ├── exporters/                   csv_exporter.py, excel_exporter.py
              ├── database/crawl_database.py   SQLite sessions / URLs / pages
              ├── gui/                         CustomTkinter app + controller
              ├── utils/                       logger, validators (SSRF), helpers
              ├── output/                      csv/, excel/, logs/
              ├── tests/                       unit + end-to-end tests with a local test website
              └── examples/                    run_demo.py and sample_output/
```

## 7. Testing

```bash
pytest                       # 64 tests (~45 s); uses only a local test website
python examples/run_demo.py  # regenerates examples/sample_output/
```
Browser tests are skipped automatically if Chromium is not installed.

---

## 8. Troubleshooting

| Problem | Solution |
|---|---|
| `ModuleNotFoundError: No module named 'scrapy'` (or `bs4`, `lxml`, `openpyxl`) | The virtual environment is not active, or packages were installed for another Python. Run `.venv\Scripts\activate` (Windows) / `source .venv/bin/activate`, then `pip install -r requirements.txt` |
| `No module named 'playwright'` | `pip install playwright` then `playwright install chromium` |
| `Executable doesn't exist ... playwright install` / "JavaScript rendering unavailable" | Run `playwright install chromium`, or crawl with `--javascript never` |
| `No module named 'customtkinter'` | `pip install customtkinter` (only needed for `--gui`) |
| `No module named 'tkinter'` | Windows/macOS: reinstall Python from python.org with Tcl/Tk. Linux: `sudo apt install python3-tk` |
| `ImportError: tomllib` or syntax errors | Python is older than 3.11; install 3.11+ |
| `pip install` fails on `lxml` / `Twisted` | Upgrade pip (`python -m pip install --upgrade pip`); on Windows use 64-bit Python 3.11–3.12 so wheels are available. If `pandas`/`httpx` fail, delete them from `requirements.txt` (unused) |
| `start URL rejected - blocked non-public IP` | SSRF protection. For your own local site add `--allow-private-network` |
| Many pages "Blocked by robots.txt" | The site disallows them. Use `--no-robots` only with explicit permission |
| 429 / 503 messages | The crawler slows down automatically. Raise `--delay` or lower `--concurrency` |
| Page text is empty on a modern site | Use `--javascript auto` or `always` |
| Crawl stops early | `--max-pages` / `--depth` limits; skipped URLs are listed as "Not crawled (depth/page limit)" |

---

## Ethical and legal considerations

* Crawl only websites you own or have explicit permission to crawl. Follow their terms of service, robots.txt, copyright rules and data-protection laws.
* The tool does not log in, submit forms, solve CAPTCHAs, bypass paywalls or anti-bot systems, and refuses private/internal network targets by default.
* Keep request rates reasonable (defaults: 4 concurrent requests, 1 s delay). You are responsible for the load you create. A sitemap entry is not permission to ignore access restrictions.
* Collected pages may contain personal data; minimise, secure and delete it appropriately.
* `--no-robots` and `--allow-private-network` exist for authorized owners and testing only.

## Screen Shots:
Sample url: https://www.geeksforgeeks.org/python/email-id-extractor-project-from-sites-in-scrapy-python/  

Plugins and Options:
<img width="962" height="263" alt="image" src="https://github.com/user-attachments/assets/8f661cd4-6fc1-41c9-b494-4131c8d9d724" />

GUI Based: 
<img width="1366" height="694" alt="image" src="https://github.com/user-attachments/assets/c9d11374-867c-4102-9bc7-d457bf013ab9" />

Output:
<img width="1366" height="719" alt="image" src="https://github.com/user-attachments/assets/3ea6babd-cc36-4aa3-8d79-9eedff5e7503" />

Website Crawls Summary: many more...
<img width="817" height="447" alt="image" src="https://github.com/user-attachments/assets/d73ac014-5e0c-4098-a731-4c7e36890e55" />

Extracted Headings:
<img width="961" height="467" alt="image" src="https://github.com/user-attachments/assets/d933ec8d-8458-42bb-be28-19f61bb6c640" />

Internal Links: another page, file, folder and section many more...
<img width="1196" height="494" alt="image" src="https://github.com/user-attachments/assets/0ac8c015-33b6-45d4-b5e9-c65a62195048" />

External Links: like Instagram, twitter and LinkedIn and many more...
<img width="1285" height="475" alt="image" src="https://github.com/user-attachments/assets/2db593ce-11f2-4dbb-a681-9b81ddf57f65" />

Images from websites:
<img width="1333" height="486" alt="image" src="https://github.com/user-attachments/assets/62421e53-8eb9-48ac-916d-79eb6417ddd0" />

## Important Extract

Websites backend Tables:
<img width="1307" height="489" alt="image" src="https://github.com/user-attachments/assets/c64a8e9c-c8cc-4d71-8043-ca10a102fdf6" />

Table Grids: structured layouts of Websites
<img width="1313" height="478" alt="image" src="https://github.com/user-attachments/assets/f317fc28-90e2-4094-a731-aff6f5c34873" />
Forms that need an credentials:
<img width="1325" height="480" alt="image" src="https://github.com/user-attachments/assets/8f27d528-8917-4894-82cf-7319b4774425" />

Author: https://github.com/zakariyamalek
