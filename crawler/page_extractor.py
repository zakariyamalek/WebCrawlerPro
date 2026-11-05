"""Page information extraction (pure functions - no Scrapy dependency).

``extract_page`` turns an HTML string into one *page record*: a plain dict that is
stored in SQLite and later turned into CSV/XLSX rows.  Keeping this module free of
Scrapy makes it easy to unit test with literal HTML.

Order matters inside ``extract_page``: structural data (links, images, tables, forms,
metadata) is read from the untouched document first; only afterwards is the tree
cleaned (scripts, styles, navigation removed) to produce readable main text,
paragraphs, lists and so on.
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Any
from urllib.parse import urldefrag, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from config import CrawlConfig
from crawler.url_discovery import LinkClassifier
from crawler.url_normalizer import UrlNormalizer
from utils.helpers import collapse_ws, redact_headers, utc_now_iso

HEADING_RE = re.compile(r"^h[1-6]$")
NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed")
BLOCK_TAGS = frozenset({
    "html", "body", "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "li", "ul", "ol",
    "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "thead", "tbody", "tfoot",
    "tr", "td", "th", "caption", "figure", "figcaption", "form", "fieldset", "address", "details", "summary", "hr",
})
MAX_TABLES = 100
MAX_TABLE_ROWS = 2000
MAX_ITEMS = 5000


def _text(el: Tag) -> str:
    return collapse_ws(el.get_text(" ", strip=True))


def _attr(el: Tag, name: str) -> str | None:
    value = el.get(name)
    if isinstance(value, list):
        value = " ".join(value)
    return value.strip() if isinstance(value, str) and value.strip() else None


def empty_record(url: str, depth: int = 0, **fields: Any) -> dict[str, Any]:
    """A record with every key present (used for failed/skipped pages)."""
    record: dict[str, Any] = {
        "ok": False, "url": url, "requested_url": url, "canonical_url": None, "status_code": None,
        "title": None, "meta_description": None, "meta_keywords": None, "author": None, "language": None,
        "content_type": None, "last_modified": None, "headings": [], "main_text": "", "paragraphs": [],
        "lists": [], "highlighted": [], "blockquotes": [], "captions": [], "links": [], "images": [],
        "tables": [], "metadata": {}, "forms": [], "technical": {}, "depth": depth,
        "crawl_time": utc_now_iso(), "error_message": None, "response_time_ms": None,
        "render_mode": "static", "status": "failed", "follow_links": False, "js_signals": {},
    }
    record.update(fields)
    return record


# --------------------------------------------------------------------------- tables
def _table_grid(table: Tag) -> tuple[list[list[str]], list[bool]]:
    """Expand rowspan/colspan into a rectangular grid; returns (rows, is_header_row)."""
    rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table][:MAX_TABLE_ROWS]
    grid: list[list[str]] = []
    header_flags: list[bool] = []
    carry: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(rows):
        cells = [c for c in tr.find_all(["td", "th"], recursive=False)]
        row: list[str] = []
        col = 0
        for cell in cells:
            while (r, col) in carry:
                row.append(carry.pop((r, col)))
                col += 1
            try:
                colspan = max(1, min(int(cell.get("colspan", 1)), 50))
                rowspan = max(1, min(int(cell.get("rowspan", 1)), 200))
            except (TypeError, ValueError):
                colspan = rowspan = 1
            value = _text(cell)
            for dc in range(colspan):
                row.append(value)
                for dr in range(1, rowspan):
                    carry[(r + dr, col + dc)] = value
            col += colspan
        while (r, col) in carry:
            row.append(carry.pop((r, col)))
            col += 1
        grid.append(row)
        header_flags.append(bool(cells) and all(c.name == "th" for c in cells))
    return grid, header_flags


def extract_tables(soup: BeautifulSoup) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for index, table in enumerate(soup.find_all("table")[:MAX_TABLES], start=1):
        grid, header_flags = _table_grid(table)
        if not grid:
            continue
        headers: list[str] = []
        body = grid
        thead = table.find("thead")
        if header_flags and header_flags[0]:
            headers, body = grid[0], grid[1:]
        elif thead is not None and len(grid) > 1:
            headers, body = grid[0], grid[1:]
        width = max((len(r) for r in grid), default=0)
        caption = table.find("caption")
        tables.append({
            "index": index,
            "caption": _text(caption) if caption else None,
            "headers": headers,
            "rows": body,
            "row_count": len(body),
            "column_count": width,
            "truncated": len(table.find_all("tr")) > MAX_TABLE_ROWS,
        })
    return tables


# ------------------------------------------------------------------------- metadata
def _iter_types(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            found.append(t)
        elif isinstance(t, list):
            found.extend(str(x) for x in t)
        for value in node.values():
            found.extend(_iter_types(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_types(item))
    return found


def extract_json_ld(soup: BeautifulSoup) -> list[Any]:
    blocks: list[Any] = []
    for script in soup.find_all("script", type=lambda t: bool(t) and "ld+json" in t.lower()):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except (ValueError, RecursionError):
            blocks.append({"_parse_error": "invalid JSON-LD", "raw": raw[:2000]})
    return blocks


def extract_microdata(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for el in soup.find_all(attrs={"itemscope": True}):
        if el.find_parent(attrs={"itemscope": True}) is not None:
            continue  # nested items are reported through their parent's properties
        props: dict[str, Any] = {}
        for prop in el.find_all(attrs={"itemprop": True})[:60]:
            name = _attr(prop, "itemprop")
            if not name:
                continue
            value = (_attr(prop, "content") or _attr(prop, "datetime") or _attr(prop, "href")
                     or _attr(prop, "src") or _text(prop))
            props.setdefault(name, value)
        items.append({"type": _attr(el, "itemtype"), "properties": props})
        if len(items) >= 50:
            break
    return items


def extract_metadata(soup: BeautifulSoup) -> dict[str, Any]:
    meta_tags: dict[str, Any] = {}
    open_graph: dict[str, Any] = {}
    twitter: dict[str, Any] = {}
    for tag in soup.find_all("meta")[:400]:
        key = _attr(tag, "property") or _attr(tag, "name") or _attr(tag, "http-equiv") or _attr(tag, "itemprop")
        content = tag.get("content")
        if not key or content is None:
            continue
        key_l = key.lower()
        content = collapse_ws(str(content))
        target = open_graph if key_l.startswith(("og:", "article:", "fb:", "product:", "book:", "profile:")) \
            else twitter if key_l.startswith("twitter:") else meta_tags
        if key_l in target:
            existing = target[key_l]
            target[key_l] = (existing if isinstance(existing, list) else [existing]) + [content]
        else:
            target[key_l] = content
    json_ld = extract_json_ld(soup)
    return {
        "meta_tags": meta_tags,
        "open_graph": open_graph,
        "twitter_card": twitter,
        "json_ld": json_ld,
        "schema_types": sorted(set(_iter_types(json_ld))),
        "microdata": extract_microdata(soup),
    }


# --------------------------------------------------------------------- forms/images
def extract_forms(soup: BeautifulSoup, base_url: str, normalizer: UrlNormalizer) -> list[dict[str, Any]]:
    """Describe forms (never submits them and never records field *values*)."""
    forms: list[dict[str, Any]] = []
    for form in soup.find_all("form")[:50]:
        action_raw = _attr(form, "action")
        action = normalizer.join(base_url, action_raw) if action_raw else base_url
        fields: list[dict[str, Any]] = []
        buttons: list[str] = []
        for el in form.find_all(["input", "select", "textarea", "button"])[:200]:
            if el.name == "button":
                label = _text(el) or _attr(el, "value") or _attr(el, "aria-label")
                if label:
                    buttons.append(label)
                continue
            ftype = (_attr(el, "type") or ("select" if el.name == "select" else "textarea" if el.name == "textarea" else "text")).lower()
            if ftype in ("submit", "button", "reset", "image"):
                label = _attr(el, "value") or _attr(el, "alt") or ftype.title()
                buttons.append(label)
                continue
            fields.append({"name": _attr(el, "name"), "type": ftype, "id": _attr(el, "id"),
                           "required": el.has_attr("required"), "placeholder": _attr(el, "placeholder")})
        forms.append({
            "id": _attr(form, "id"), "name": _attr(form, "name"), "action": action,
            "method": (_attr(form, "method") or "GET").upper(), "fields": fields, "buttons": buttons,
            "has_password_field": any(f["type"] == "password" for f in fields),
        })
    return forms


def _first_srcset_url(srcset: str | None) -> str | None:
    if not srcset:
        return None
    first = srcset.split(",")[0].strip().split(" ")[0]
    return first or None


def extract_images(soup: BeautifulSoup, base_url: str, normalizer: UrlNormalizer) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for img in soup.find_all("img")[:MAX_ITEMS]:
        raw = (_attr(img, "src") or _attr(img, "data-src") or _attr(img, "data-lazy-src")
               or _first_srcset_url(_attr(img, "srcset")) or _first_srcset_url(_attr(img, "data-srcset")))
        if not raw or raw.lower().startswith("data:"):
            continue
        absolute = normalizer.join(base_url, raw)
        if not absolute or not absolute.lower().startswith(("http://", "https://")):
            continue
        absolute = urldefrag(absolute)[0]
        if absolute in seen:
            continue
        seen.add(absolute)
        width, height = _attr(img, "width"), _attr(img, "height")
        ext = posixpath.splitext(urlsplit(absolute).path)[1].lstrip(".").lower() or None
        images.append({
            "url": absolute, "alt": _attr(img, "alt") or "", "title": _attr(img, "title") or "",
            "width": width, "height": height,
            "dimensions": f"{width}x{height}" if width and height else None,
            "extension": ext,
        })
    return images


# ------------------------------------------------------------------------- links
def extract_links(soup: BeautifulSoup, base_url: str, config: CrawlConfig,
                  classifier: LinkClassifier, normalizer: UrlNormalizer) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for a in soup.find_all("a", href=True)[:MAX_ITEMS]:
        href = a["href"].strip()
        if not href:
            continue
        absolute = None
        if not href.lower().startswith(("mailto:", "tel:", "sms:", "javascript:", "data:")):
            joined = normalizer.join(base_url, href)
            absolute = urldefrag(joined)[0] if joined else None
        link_type = classifier.link_type(href, absolute)
        if link_type in ("mailto", "tel"):
            dest = href
            internal = False
        elif link_type in ("javascript", "other") or absolute is None:
            dest, internal = href[:200], False
        elif link_type == "anchor":
            dest, internal = base_url + href, True
        else:
            dest = absolute
            internal = classifier.policy.is_internal(absolute)
        if not internal and not config.extract_external_links and link_type in ("page", "file"):
            continue
        text = _text(a) or _attr(a, "aria-label") or _attr(a, "title") or ""
        if not text:
            img = a.find("img")
            text = (_attr(img, "alt") or "") if img is not None else ""
        links.append({
            "text": text[:500], "url": dest,
            "normalized": normalizer.safe_normalize(dest) if link_type in ("page", "file") else None,
            "type": link_type, "internal": internal,
            "rel": _attr(a, "rel"), "target": _attr(a, "target"),
        })
    return links


# ----------------------------------------------------------------------- text
def _nearest_block(node: NavigableString) -> Tag | None:
    parent = node.parent
    while parent is not None and parent.name not in BLOCK_TAGS:
        parent = parent.parent
    return parent


def blocks_to_text(root: Tag) -> str:
    """Readable text with one line per block element (paragraph boundaries kept)."""
    lines: list[str] = []
    buf: list[str] = []
    current: Tag | None = None

    def flush() -> None:
        if buf:
            line = collapse_ws("".join(buf))
            if line:
                lines.append(line)
            buf.clear()

    for el in root.descendants:
        if isinstance(el, Tag):
            if el.name == "br":
                flush()
            continue
        if type(el) is not NavigableString:  # skip comments, doctype, CDATA...
            continue
        block = _nearest_block(el)
        if block is not current:
            flush()
            current = block
        buf.append(str(el))
    flush()
    return "\n".join(lines)


def _own_li_text(li: Tag) -> str:
    parts: list[str] = []
    for child in li.children:
        if isinstance(child, Tag):
            if child.name in ("ul", "ol"):
                continue
            parts.append(child.get_text(" ", strip=True))
        elif type(child) is NavigableString:
            parts.append(str(child))
    return collapse_ws(" ".join(parts))


def _unique(items: list[str], cap: int = 500) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
            if len(out) >= cap:
                break
    return out


def _clean_for_text(soup: BeautifulSoup) -> Tag:
    """Remove non-content nodes in place and return the best content root."""
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for nav in soup.find_all(["nav"]) + soup.find_all(attrs={"role": "navigation"}):
        nav.decompose()
    body = soup.body or soup
    body_len = len(body.get_text(" ", strip=True))
    candidate = soup.find("main") or soup.find(attrs={"role": "main"})
    if candidate is None:
        articles = soup.find_all("article")
        candidate = articles[0] if len(articles) == 1 else None
    if candidate is not None and len(candidate.get_text(" ", strip=True)) >= 0.5 * max(body_len, 1):
        return candidate
    return body


def js_signals(soup: BeautifulSoup, text_chars: int) -> dict[str, Any]:
    """Cheap indicators that the static HTML is only a JavaScript shell."""
    scripts = len(soup.find_all("script"))
    empty_root = False
    for el_id in ("root", "app", "__next", "__nuxt", "app-root", "main-app", "q-app"):
        el = soup.find(id=el_id)
        if el is not None and len(el.get_text(strip=True)) < 20:
            empty_root = True
    warning = False
    for ns in soup.find_all("noscript"):
        t = ns.get_text(" ", strip=True).lower()
        if "javascript" in t and any(w in t for w in ("enable", "require", "need")):
            warning = True
    return {"text_chars": text_chars, "scripts": scripts, "empty_root": empty_root, "noscript_warning": warning}


# ---------------------------------------------------------------------- main API
def extract_page(html: str, url: str, *, config: CrawlConfig, classifier: LinkClassifier,
                 normalizer: UrlNormalizer, status_code: int | None = 200,
                 headers: dict[str, str] | None = None, response_time_ms: float | None = None,
                 depth: int = 0, requested_url: str | None = None,
                 redirect_chain: list[str] | None = None, render_mode: str = "static") -> dict[str, Any]:
    """Extract every supported piece of information from one HTML document."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    soup = BeautifulSoup(html, "lxml")

    html_tag = soup.find("html")
    title_tag = soup.find("title")
    meta_map = {(_attr(m, "name") or _attr(m, "http-equiv") or "").lower(): m.get("content")
                for m in soup.find_all("meta") if (_attr(m, "name") or _attr(m, "http-equiv"))}
    canonical_tag = soup.find("link", rel="canonical")
    canonical = normalizer.join(url, _attr(canonical_tag, "href") or "") if canonical_tag else None
    robots_meta = ", ".join(str(meta_map[k]).lower() for k in ("robots", "googlebot") if meta_map.get(k))
    x_robots = headers.get("x-robots-tag", "")
    follow_links = not ("nofollow" in robots_meta or "nofollow" in x_robots.lower()) or not config.respect_robots

    record = empty_record(url, depth)
    record.update({
        "ok": True, "status": "crawled", "requested_url": requested_url or url, "status_code": status_code,
        "canonical_url": canonical,
        "title": _text(title_tag) if title_tag else None,
        "meta_description": collapse_ws(str(meta_map["description"])) if meta_map.get("description") else None,
        "meta_keywords": collapse_ws(str(meta_map["keywords"])) if meta_map.get("keywords") else None,
        "author": collapse_ws(str(meta_map["author"])) if meta_map.get("author") else None,
        "language": (_attr(html_tag, "lang") if html_tag else None) or (meta_map.get("content-language") or None),
        "content_type": headers.get("content-type"),
        "last_modified": headers.get("last-modified"),
        "response_time_ms": response_time_ms,
        "render_mode": render_mode,
        "follow_links": follow_links,
        "technical": {
            "response_headers": redact_headers(headers), "server": headers.get("server"),
            "redirect_chain": redirect_chain or [], "robots_meta": robots_meta or None,
            "x_robots_tag": x_robots or None, "canonical_url": canonical,
            "response_time_ms": response_time_ms,
        },
    })

    # -- structural data from the untouched tree ---------------------------------
    record["headings"] = [{"level": int(h.name[1]), "text": _text(h)}
                          for h in soup.find_all(HEADING_RE)[:MAX_ITEMS] if _text(h)]
    record["links"] = extract_links(soup, url, config, classifier, normalizer)
    if config.extract_images:
        record["images"] = extract_images(soup, url, normalizer)
    if config.extract_tables:
        record["tables"] = extract_tables(soup)
    if config.extract_metadata:
        record["metadata"] = extract_metadata(soup)
    if config.extract_forms:
        record["forms"] = extract_forms(soup, url, normalizer)
    record["captions"] = _unique([_text(c) for c in soup.find_all(["figcaption", "caption"])])

    # -- text (mutates the tree, so it comes last) --------------------------------
    root = _clean_for_text(soup)
    main_text = blocks_to_text(root)
    record["main_text"] = main_text
    record["paragraphs"] = [t for t in (_text(p) for p in root.find_all("p")[:MAX_ITEMS]) if t]
    lists: list[dict[str, Any]] = []
    for lst in root.find_all(["ul", "ol"])[:MAX_ITEMS]:
        items = [t for t in (_own_li_text(li) for li in lst.find_all("li", recursive=False)) if t]
        if items:
            lists.append({"type": "ordered" if lst.name == "ol" else "unordered", "items": items,
                          "nested": lst.find_parent(["ul", "ol"]) is not None})
    record["lists"] = lists
    record["highlighted"] = _unique([t for t in (_text(e) for e in root.find_all(["strong", "b", "em", "mark"])) if len(t) >= 3])
    record["blockquotes"] = _unique([_text(b) for b in root.find_all("blockquote")])
    record["js_signals"] = js_signals(soup, len(main_text))
    return record
