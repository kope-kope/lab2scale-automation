"""Unit tests for lib/scraper.py.

RSS/HTML parsing is tested directly; network methods are tested offline via
an httpx MockTransport so no live calls are made.
"""

import asyncio
from pathlib import Path

import httpx

from lib.scraper import Scraper

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_FEED = (FIXTURES / "sample_feed.xml").read_bytes()

SAMPLE_HTML = """
<html><body>
  <div class="article">
    <h2><a href="/news/fusion-milestone">Fusion reactor hits net energy gain</a></h2>
    <p class="excerpt">A tokamak sustains a burning plasma for record duration.</p>
  </div>
  <div class="article">
    <h2><a href="/news/gan-inverter">GaN inverter reaches 99% efficiency</a></h2>
    <p class="excerpt">Wide-bandgap device cuts switching losses.</p>
  </div>
</body></html>
"""


def _mock_scraper(handler, **kwargs) -> Scraper:
    """Scraper wired to a MockTransport; rate limiting effectively disabled."""
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("rate_limit", 1000)
    return Scraper(transport=transport, **kwargs)


# ----- pure parsing (no network) ------------------------------------------


def test_parse_feed_from_fixture():
    items = Scraper._parse_feed(SAMPLE_FEED)
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "Solid-state battery achieves 1000-cycle stability"
    assert first["link"] == "https://example.com/papers/solid-state-1000"
    assert "solid-state lithium cell" in first["summary"]
    assert first["published"]  # pubDate mapped through


def test_parse_html_with_item_containers():
    scraper = Scraper()
    rows = scraper.parse_html(
        SAMPLE_HTML,
        {"item": "div.article", "title": "h2 a", "link": "h2 a@href", "blurb": "p.excerpt"},
    )
    assert len(rows) == 2
    assert rows[0]["title"] == "Fusion reactor hits net energy gain"
    assert rows[0]["link"] == "/news/fusion-milestone"
    assert rows[0]["blurb"].startswith("A tokamak")
    assert rows[1]["title"] == "GaN inverter reaches 99% efficiency"


def test_parse_html_without_item_key_returns_single_row():
    scraper = Scraper()
    rows = scraper.parse_html(SAMPLE_HTML, {"first_title": "h2 a", "first_link": "h2 a@href"})
    assert len(rows) == 1
    assert rows[0]["first_title"] == "Fusion reactor hits net energy gain"


def test_parse_html_empty_string_returns_empty_list():
    assert Scraper().parse_html("", {"item": "div", "t": "h2"}) == []


# ----- network methods via MockTransport ----------------------------------


def test_fetch_rss_via_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=SAMPLE_FEED, headers={"content-type": "application/rss+xml"}
        )

    async def body():
        scraper = _mock_scraper(handler)
        items = await scraper.fetch_rss("https://example.com/feed.xml")
        await scraper.close()
        return items

    items = asyncio.run(body())
    assert len(items) == 2
    assert items[1]["title"].startswith("Flow battery")


def test_fetch_page_allowed_when_robots_404():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/news":
            return httpx.Response(200, text=SAMPLE_HTML)
        return httpx.Response(404)

    async def body():
        scraper = _mock_scraper(handler, respect_robots=True)
        html = await scraper.fetch_page("https://example.com/news")
        await scraper.close()
        return html

    html = asyncio.run(body())
    assert "Fusion reactor" in html


def test_fetch_page_blocked_by_robots():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\nDisallow: /private\n"
            )
        return httpx.Response(200, text="<html>secret</html>")

    async def body():
        scraper = _mock_scraper(handler, respect_robots=True)
        html = await scraper.fetch_page("https://example.com/private/page")
        await scraper.close()
        return html

    assert asyncio.run(body()) == ""  # disallowed → empty, no fetch


def test_fetch_api_returns_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"events": [{"name": "MIT Energy Night"}]})

    async def body():
        scraper = _mock_scraper(handler)
        data = await scraper.fetch_api("https://example.com/api/events")
        await scraper.close()
        return data

    data = asyncio.run(body())
    assert data["events"][0]["name"] == "MIT Energy Night"


def test_fetch_rss_failure_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def body():
        # max_attempts=1 → no retry/backoff, so this stays fast.
        scraper = _mock_scraper(handler, max_attempts=1)
        items = await scraper.fetch_rss("https://example.com/down.xml")
        await scraper.close()
        return items

    assert asyncio.run(body()) == []
