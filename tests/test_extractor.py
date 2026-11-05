"""Unit tests for extraction, URL handling and discovery helpers (no network)."""
from __future__ import annotations

import gzip

from config import CrawlConfig
from crawler.duplicate_filter import DuplicateFilter
from crawler.javascript_renderer import needs_rendering
from crawler.page_extractor import extract_page
from crawler.robots_handler import RobotsHandler
from crawler.sitemap_parser import parse_sitemap
from crawler.url_discovery import DomainPolicy, LinkClassifier
from crawler.url_normalizer import UrlNormalizer

CFG = CrawlConfig(url="https://example.com")
NORM = UrlNormalizer(CFG.strip_params)
POLICY = DomainPolicy("https://example.com")
CLS = LinkClassifier(CFG, POLICY, NORM)

HTML = """<html lang="en"><head><title> My  Title </title><meta name="description" content="Desc">
<meta name="keywords" content="a,b"><meta name="author" content="Zed"><meta name="robots" content="noindex, nofollow">
<meta property="og:title" content="OG"><meta name="twitter:card" content="summary"><link rel="canonical" href="/canon">
<script type="application/ld+json">{"@type":"Product","name":"P"}</script></head><body>
<nav><a href="/nav">Nav</a></nav><main><h1>One</h1><h2>Two</h2><h6>Six</h6><p>Para <strong>bold</strong></p>
<ol><li>first</li></ol><blockquote>Quote</blockquote><figure><img src="/a.png" alt="A" title="T" width="5" height="6">
<figcaption>Cap</figcaption></figure><a href="/about#x" rel="nofollow" target="_blank">About</a>
<a href="https://other.org/">Other</a><a href="mailto:a@b.c">Mail</a><a href="#top">Top</a>
<table><tr><th>H1</th><th>H2</th></tr><tr><td rowspan="2">a</td><td>b</td></tr><tr><td>c</td></tr></table>
<form action="/go" method="post"><input name="user"><input type="password" name="pw" value="hunter2">
<input type="hidden" name="t" value="TOKEN"><input type="submit" value="Go"></form></main></body></html>"""


def page(html=HTML, **kw):
    return extract_page(html, "https://example.com/start", config=kw.pop("config", CFG), classifier=CLS,
                        normalizer=NORM, headers={"Content-Type": "text/html", "Set-Cookie": "s=1"}, **kw)


def test_metadata_and_basic_info():
    p = page()
    assert p["title"] == "My Title" and p["meta_description"] == "Desc" and p["language"] == "en"
    assert p["canonical_url"] == "https://example.com/canon" and p["author"] == "Zed"
    assert p["metadata"]["open_graph"] == {"og:title": "OG"} and p["metadata"]["twitter_card"] == {"twitter:card": "summary"}
    assert p["metadata"]["schema_types"] == ["Product"]
    assert p["follow_links"] is False                                   # robots meta nofollow
    assert p["technical"]["response_headers"]["set-cookie"] == "[redacted]"


def test_headings_preserve_levels():
    assert [(h["level"], h["text"]) for h in page()["headings"]] == [(1, "One"), (2, "Two"), (6, "Six")]


def test_text_lists_quotes_captions():
    p = page()
    assert "Para bold" in p["main_text"] and "Nav" not in p["main_text"]
    assert p["lists"][0] == {"type": "ordered", "items": ["first"], "nested": False}
    assert p["blockquotes"] == ["Quote"] and "Cap" in p["captions"] and p["highlighted"] == ["bold"]


def test_links_classified():
    links = {l["text"]: l for l in page()["links"]}
    assert links["About"]["url"] == "https://example.com/about" and links["About"]["internal"]
    assert links["About"]["rel"] == "nofollow" and links["About"]["target"] == "_blank"
    assert not links["Other"]["internal"] and links["Mail"]["type"] == "mailto" and links["Top"]["type"] == "anchor"


def test_images():
    img = page()["images"][0]
    assert img == {"url": "https://example.com/a.png", "alt": "A", "title": "T", "width": "5", "height": "6",
                   "dimensions": "5x6", "extension": "png"}


def test_tables_rowspan():
    t = page()["tables"][0]
    assert t["headers"] == ["H1", "H2"] and t["rows"] == [["a", "b"], ["a", "c"]]


def test_forms_never_store_values():
    f = page()["forms"][0]
    assert f["method"] == "POST" and f["action"] == "https://example.com/go" and f["has_password_field"]
    assert "hunter2" not in str(f) and "TOKEN" not in str(f) and f["buttons"] == ["Go"]


def test_optional_extraction_switches():
    cfg = CrawlConfig(url="https://example.com", extract_tables=False, extract_images=False,
                      extract_metadata=False, extract_external_links=False)
    p = page(config=cfg)
    assert p["tables"] == [] and p["images"] == [] and p["metadata"] == {}
    assert all(l["internal"] or l["type"] in ("mailto", "anchor") for l in p["links"])


def test_missing_metadata_is_safe():
    p = page("<html><body>plain</body></html>")
    assert p["title"] is None and p["canonical_url"] is None and p["main_text"] == "plain"


def test_js_signals_and_rendering_decision():
    shell = '<html><body><div id="root"></div><script src="a.js"></script></body></html>'
    assert needs_rendering(page(shell)["js_signals"], CFG)
    assert not needs_rendering(page()["js_signals"], CFG)
    assert not needs_rendering(page(shell)["js_signals"], CrawlConfig(javascript="never"))
    assert needs_rendering(page()["js_signals"], CrawlConfig(javascript="always"))


def test_url_normalisation():
    n = NORM.normalize
    assert n("HTTP://Example.COM:80/a/./b/../c/?b=2&a=1&a=1&utm_source=x#f") == "http://example.com/a/c?a=1&b=2"
    assert n("https://example.com/p?id=1") != n("https://example.com/p?id=2")
    assert n("https://example.com/%7Euser//x/") == "https://example.com/~user/x"
    assert n("https://example.com") == n("https://example.com/")
    assert n("https://example.com/a?sid=1&x=2") == "https://example.com/a?x=2"      # session ids dropped
    assert NORM.join("https://example.com/a/b", "../c") == "https://example.com/c"


def test_domain_policy_and_eligibility():
    assert POLICY.is_internal("https://www.example.com/x") and not POLICY.is_internal("https://sub.example.com/")
    assert DomainPolicy("https://example.com", True).is_internal("https://sub.example.com/")
    assert not DomainPolicy("https://example.com", True).is_internal("https://notexample.com/")
    assert CLS.check("https://example.com/a.pdf").reason.startswith("binary")
    assert CLS.check("https://example.com/search?q=x").reason.startswith("excluded")
    assert "loop" in CLS.check("https://example.com/a/b/a/b/a/b").reason
    assert not CLS.check("https://evil.com/").crawlable and CLS.check("https://example.com/ok").crawlable
    inc = LinkClassifier(CrawlConfig(include_patterns=["/blog/"]), POLICY, NORM)
    assert inc.check("https://example.com/blog/x").crawlable and not inc.check("https://example.com/other").crawlable


def test_duplicate_filter():
    d = DuplicateFilter()
    assert d.add("a") and not d.add("a") and d.add("b")
    assert d.duplicate_hits == 1 and d.duplicated_urls == 1 and len(d) == 2


def test_sitemaps():
    urlset = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/a</loc></url></urlset>'
    index = b'<sitemapindex xmlns="x"><sitemap><loc>https://example.com/s1.xml</loc></sitemap></sitemapindex>'
    assert parse_sitemap(urlset).urls == ["https://example.com/a"]
    assert parse_sitemap(index).is_index and parse_sitemap(index).sitemaps == ["https://example.com/s1.xml"]
    assert parse_sitemap(gzip.compress(urlset)).urls == ["https://example.com/a"]
    assert parse_sitemap(b'<!DOCTYPE x [<!ENTITY a "b">]><urlset/>').urls == []   # entity bombs rejected
    assert parse_sitemap(b"not xml").urls == []


def test_robots_handler():
    r = RobotsHandler("https://example.com/robots.txt",
                      "User-agent: *\nDisallow: /private/\nCrawl-delay: 2\nSitemap: https://example.com/s.xml\n",
                      "WebCrawlerPro/1.0")
    assert not r.can_fetch("https://example.com/private/x") and r.can_fetch("https://example.com/ok")
    assert r.sitemaps() == ["https://example.com/s.xml"] and r.crawl_delay() == 2.0
