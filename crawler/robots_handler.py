"""robots.txt parsing (sitemap references and crawl-permission checks).

Scrapy's own ``RobotsTxtMiddleware`` enforces robots.txt for every page request.
This handler additionally lets the spider (a) read ``Sitemap:`` directives and
(b) filter sitemap-discovered URLs against ``Disallow`` rules before queueing.
"""
from __future__ import annotations

from urllib.robotparser import RobotFileParser


class RobotsHandler:
    def __init__(self, robots_url: str, text: str, user_agent: str) -> None:
        self.robots_url = robots_url
        self.user_agent = user_agent
        self._parser = RobotFileParser()
        self._parser.parse(text.splitlines())
        self._text = text

    def can_fetch(self, url: str) -> bool:
        # Match on the product token ("WebCrawlerPro") as robots.txt files do.
        token = self.user_agent.split("/")[0].split(" ")[0] or "*"
        return self._parser.can_fetch(token, url)

    def sitemaps(self) -> list[str]:
        """Absolute URLs from ``Sitemap:`` lines (case-insensitive, order kept)."""
        found: list[str] = []
        for line in self._text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.lower().startswith("sitemap:"):
                value = line.split(":", 1)[1].strip()
                if value and value not in found:
                    found.append(value)
        return found

    def crawl_delay(self) -> float | None:
        token = self.user_agent.split("/")[0].split(" ")[0] or "*"
        delay = self._parser.crawl_delay(token)
        return float(delay) if delay is not None else None
