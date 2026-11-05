"""A small, fully controlled multi-page website used by the test-suite and the demo.

Run it by hand with:  python tests/fixtures/site_server.py --port 8765
No third-party site is ever contacted by the tests.
"""
from __future__ import annotations

import argparse
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="description" content="{title} description"><meta name="keywords" content="demo,{slug}">
<meta name="author" content="Test Author"><link rel="canonical" href="{canonical}">
<meta property="og:title" content="OG {title}"><meta name="twitter:card" content="summary">
{extra_head}</head><body><nav><a href="/">Home</a> <a href="/about">About</a></nav>
<main><h1>{title}</h1>{body}</main><footer>(c) test</footer></body></html>"""

LOREM = ("This is a sufficiently long paragraph of visible text so that the JavaScript heuristics never "
         "mistake a normal static page for an empty application shell. " * 2)


def page(title: str, body: str, slug: str = "p", canonical: str = "", extra_head: str = "") -> str:
    return PAGE.format(title=title, body=body, slug=slug, canonical=canonical or "", extra_head=extra_head)


class _Handler(BaseHTTPRequestHandler):
    server_version = "TestSite/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    # --------------------------------------------------------------- helpers
    def _send(self, status=200, body="", ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "session=SECRET123; Path=/")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        site: "TestSite" = self.server.site  # type: ignore[attr-defined]
        parts = urlsplit(self.path)
        path = parts.path
        site.hits[path] += 1
        site.log.append((time.monotonic(), self.path, self.headers.get("User-Agent", "")))
        base = site.base_url
        routes = site.routes(base)
        if path in routes:
            result = routes[path]
            result = result(self) if callable(result) else result
            if isinstance(result, tuple):
                self._send(*result)
            else:
                self._send(200, result)
        elif path.startswith("/slow/"):
            n = int(path.rsplit("/", 1)[1])
            time.sleep(site.slow_delay)
            nxt = f'<a href="/slow/{n + 1}">next</a>' if n < site.slow_pages else ""
            self._send(200, page(f"Slow {n}", f"<p>{LOREM}</p>{nxt}", f"slow{n}"))
        else:
            self._send(404, page("Not Found", f"<p>{LOREM}</p>"))


class TestSite:
    __test__ = False  # not a pytest class

    def __init__(self, port: int = 0, slow_delay: float = 0.4, slow_pages: int = 60) -> None:
        self.hits: Counter[str] = Counter()
        self.log: list[tuple[float, str, str]] = []
        self.slow_delay = slow_delay
        self.slow_pages = slow_pages
        self._rate_limit_state = Counter()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.site = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def start(self) -> "TestSite":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # --------------------------------------------------------------- content
    def routes(self, base: str) -> dict:
        def rate_limited(handler):
            self._rate_limit_state["n"] += 1
            if self._rate_limit_state["n"] == 1:
                return 429, "slow down", "text/plain", {"Retry-After": "1"}
            return 200, page("Rate Limited OK", f"<p>{LOREM}</p>", "rl")

        def redirect(to, code=301):
            return lambda h: (code, "", "text/plain", {"Location": to})

        return {
            "/": page("Home", f"""<p>{LOREM}</p>
<ul><li><a href="/about">About Us</a></li><li><a href="/about/">About (trailing slash)</a></li>
<li><a href="/about#team">About team anchor</a></li><li><a href="/about?utm_source=news">About tracked</a></li>
<li><a href="/services">Services</a></li><li><a href="/contact">Contact</a></li><li><a href="/blog">Blog</a></li>
<li><a href="products/">Products (relative)</a></li><li><a href="https://external.example.org/x" rel="nofollow">External</a></li>
<li><a href="mailto:hello@example.org">Mail</a></li><li><a href="/files/report.pdf">Report PDF</a></li>
<li><a href="/old-page">Old page</a></li><li><a href="/missing">Missing</a></li>
<li><a href="/search?q=test">Search results</a></li><li><a href="/calendar?month=5">Calendar</a></li>
<li><a href="/private/secret">Private area</a></li><li><a href="/js-app">JS app</a></li>
<li><a href="/caf%C3%A9">Café</a></li><li><a href="/plain">Plain text</a></li>
<li><a href="/server-error">Broken</a></li><li><a href="/empty">Empty</a></li>
<li><a href="/rate-limited">Rate limited</a></li><li><a href="/loop-a">Redirect loop</a></li>
<li><a href="/data">Data page</a></li></ul>
<img src="/img/logo.png" alt="Company logo" title="Logo" width="120" height="40">""", "home"),
            "/about": page("About Us", f"""<h2>Our team</h2><p>{LOREM}</p><h3>History</h3>
<blockquote>Quality first.</blockquote><ul><li>Founded in 2001</li><li>Based in <strong>Ahmedabad</strong></li></ul>
<table><caption>Team</caption><thead><tr><th>Name</th><th>Role</th></tr></thead>
<tbody><tr><td>Asha</td><td>CEO</td></tr><tr><td>Ravi</td><td>CTO, "Security"</td></tr></tbody></table>
<a href="/services">Services</a>""", "about", canonical=f"{base}/about"),
            "/services": page("Services", f"""<p>{LOREM}</p><a href="/services/service-1">Service 1</a>
<a href="/services/service-2">Service 2</a><a href="/">Home</a>""", "services"),
            "/services/service-1": page("Service 1", f"<p>{LOREM}</p><a href='/services'>Back</a>", "s1"),
            "/services/service-2": page("Service 2", f"<p>{LOREM}</p><img src='/img/s2.jpg' alt='S2'>", "s2"),
            "/contact": page("Contact", f"""<p>{LOREM}</p><form action="/contact/send" method="post" id="contactForm">
<input type="text" name="name" required><input type="email" name="email"><input type="password" name="pw">
<input type="hidden" name="csrf" value="TOPSECRETTOKEN"><textarea name="msg"></textarea>
<button type="submit">Send message</button></form>""", "contact"),
            "/blog": page("Blog", f"""<p>{LOREM}</p><a href="/blog/article-1">A1</a> <a href="/blog/article-2">A2</a>""", "blog"),
            "/blog/article-1": page("Article 1", f"<p>{LOREM}</p><a href='/blog/article-3'>A3</a>", "a1",
                                    extra_head='<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","headline":"Article 1"}</script>'),
            "/blog/article-2": page("Article 2", f"<p>{LOREM}</p><a href='/blog'>Blog</a>", "a2"),
            "/blog/article-3": page("Article 3 (depth 3)", f"<p>{LOREM}</p>", "a3"),
            "/products/": page("Products", f"<p>{LOREM}</p><a href='/products/widget?id=1'>W1</a><a href='/products/widget?id=2'>W2</a>", "prod"),
            "/products/widget": lambda h: page(f"Widget {urlsplit(h.path).query}", f"<p>{LOREM}</p>", "widget"),
            "/old-page": redirect("/about", 301),
            "/private/secret": page("Secret", f"<p>{LOREM}</p>", "secret"),
            "/sitemap-only": page("Sitemap Only Page", f"<p>{LOREM}</p>", "sitemaponly"),
            "/caf%C3%A9": page("Café ☕ – ünïcode", f"<p>{LOREM} Crème brûlée, 日本語, العربية</p>", "cafe"),
            "/plain": (200, "just text", "text/plain"),
            "/server-error": (500, "boom", "text/plain"),
            "/empty": (200, "", "text/html"),
            "/rate-limited": rate_limited,
            "/loop-a": redirect("/loop-b", 302), "/loop-b": redirect("/loop-a", 302),
            "/data": page("Data", f"""<p>{LOREM}</p><table><tr><td>=HYPERLINK("http://evil.example","click")</td><td>+cmd</td></tr></table>
<p>=2+2 should never become a formula</p>""", "data"),
            "/js-app": """<!DOCTYPE html><html><head><title>JS App</title></head><body><div id="root"></div>
<noscript>You need to enable JavaScript to run this app.</noscript>
<script>document.getElementById('root').innerHTML = '<h1>Rendered Heading</h1><p>' + 'Rendered by JavaScript. '.repeat(20) + '</p><a href="/js-page">JS discovered page</a>';</script></body></html>""",
            "/js-page": page("JS Page", f"<p>{LOREM}</p>", "jspage"),
            "/robots.txt": (200, f"User-agent: *\nDisallow: /private/\nCrawl-delay: 0\nSitemap: {base}/sitemap.xml\n", "text/plain"),
            "/sitemap.xml": (200, f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{base}/</loc></url>
<url><loc>{base}/sitemap-only</loc></url><url><loc>{base}/private/secret</loc></url>
<url><loc>https://other-site.example.net/page</loc></url></urlset>""", "application/xml"),
            "/img/logo.png": (200, b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png"),
            "/img/s2.jpg": (200, b"\xff\xd8\xff" + b"0" * 64, "image/jpeg"),
            "/files/report.pdf": (200, b"%PDF-1.4 fake", "application/pdf"),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    site = TestSite(args.port).start()
    print(f"Test site running at {site.base_url} - Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        site.stop()
