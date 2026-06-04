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


# ----- extract_articles (general-purpose HTML article extraction) ---------


ARTICLE_TAG_HTML = """
<html><body>
  <header><nav><a href="/">Home</a></nav></header>
  <main>
    <article>
      <h2><a href="/news/solid-state-1000">Solid-state battery hits 1000 cycles</a></h2>
      <p>Researchers demonstrate a durable cell stable across a thousand cycles.</p>
      <p>Funding from DOE supported the work at MIT.</p>
    </article>
    <article>
      <h2><a href="/news/flow-battery">Flow battery cuts grid storage cost 40%</a></h2>
      <p>A vanadium-free flow chemistry lowers levelized cost of storage.</p>
    </article>
  </main>
  <footer>copyright</footer>
</body></html>
"""

CLASSY_NEWS_HTML = """
<html><body>
  <div class="news-list">
    <div class="post-item">
      <h3><a href="https://example.com/posts/1">GaN inverter reaches 99% efficiency</a></h3>
      <p>Wide-bandgap device cuts switching losses to record lows in lab tests.</p>
    </div>
    <div class="post-item">
      <h3><a href="/posts/2">Photonic chip carries 1 Tb/s of data</a></h3>
      <p>Silicon photonics integration enables a single-die terabit interconnect.</p>
    </div>
  </div>
</body></html>
"""

LIST_STYLE_HTML = """
<html><body>
  <main>
    <ul>
      <li><a href="/p1">Advanced packaging breakthrough at TSMC</a> — 3D stacking yield improves dramatically over prior nodes.</li>
      <li><a href="/p2">New chiplet interconnect standard published by UCIe</a> — open spec covers PCIe6 lanes.</li>
      <li><a href="/p3">Compound semi yield record reported</a> — gallium-nitride growth on silicon at scale.</li>
    </ul>
  </main>
</body></html>
"""

JS_ONLY_HTML = """
<html><body>
  <div id="root"></div>
  <script>renderApp()</script>
</body></html>
"""


def test_extract_articles_from_article_tags():
    items = Scraper().extract_articles(ARTICLE_TAG_HTML, base_url="https://example.com")
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "Solid-state battery hits 1000 cycles"
    assert first["link"] == "https://example.com/news/solid-state-1000"
    assert "durable cell" in first["summary"]
    assert first["published"] is None


def test_extract_articles_finds_class_named_containers():
    items = Scraper().extract_articles(CLASSY_NEWS_HTML, base_url="https://example.com")
    titles = [i["title"] for i in items]
    assert "GaN inverter reaches 99% efficiency" in titles
    assert "Photonic chip carries 1 Tb/s of data" in titles
    # Relative URL was resolved.
    photonic = next(i for i in items if "Photonic" in i["title"])
    assert photonic["link"] == "https://example.com/posts/2"


def test_extract_articles_falls_back_to_list_items():
    items = Scraper().extract_articles(LIST_STYLE_HTML, base_url="https://example.com")
    titles = [i["title"] for i in items]
    assert "Advanced packaging breakthrough at TSMC" in titles
    assert len(items) == 3
    assert all(i["link"].startswith("https://example.com/") for i in items)


def test_extract_articles_dedupes_by_link():
    html = """
    <html><body>
      <article>
        <h2><a href="/a">First mention of breakthrough device</a></h2>
        <p>Lead paragraph describes the device.</p>
      </article>
      <div class="post">
        <h3><a href="/a">First mention of breakthrough device</a></h3>
        <p>Second appearance of the same link.</p>
      </div>
    </body></html>
    """
    items = Scraper().extract_articles(html, base_url="https://example.com")
    assert len(items) == 1


def test_extract_articles_returns_empty_on_unextractable_page():
    assert Scraper().extract_articles(JS_ONLY_HTML, base_url="https://example.com") == []
    assert Scraper().extract_articles("", base_url="https://example.com") == []


def test_extract_articles_skips_junk_anchors():
    """Anchors like ``#section`` or ``javascript:void(0)`` are not real links."""
    html = """
    <html><body><article>
      <h2><a href="#top">Solid-state battery research roundup</a></h2>
      <p>Some text.</p>
      <a href="javascript:void(0)">share</a>
    </article></body></html>
    """
    assert Scraper().extract_articles(html, base_url="https://example.com") == []


# ----- richer date detection in extract_articles --------------------------


def test_extract_articles_picks_up_class_named_date():
    html = """
    <html><body><article>
      <h2><a href="/a">Solid-state battery hits 1000 cycles</a></h2>
      <span class="post-date">May 25, 2026</span>
      <p>Lead paragraph describes the result.</p>
    </article></body></html>
    """
    items = Scraper().extract_articles(html, base_url="https://example.com")
    assert len(items) == 1
    assert items[0]["published"] == "May 25, 2026"


def test_extract_articles_picks_up_url_date():
    html = """
    <html><body><article>
      <h2><a href="/news/2026/05/25/flow-battery">Flow battery cuts grid cost</a></h2>
      <p>A new chemistry lowers levelized storage cost.</p>
    </article></body></html>
    """
    items = Scraper().extract_articles(html, base_url="https://example.com")
    assert len(items) == 1
    assert items[0]["published"] == "2026-05-25"


def test_extract_articles_uses_og_meta_for_single_article_page():
    html = """
    <html><head>
      <meta property="article:published_time" content="2026-05-28T09:00:00Z">
    </head><body><article>
      <h2><a href="/post/breakthrough">Cathode breakthrough at Stanford</a></h2>
      <p>Researchers demonstrate a new layered cathode.</p>
    </article></body></html>
    """
    items = Scraper().extract_articles(html, base_url="https://example.com")
    assert len(items) == 1
    assert items[0]["published"] == "2026-05-28T09:00:00Z"


def test_extract_articles_uses_jsonld_date():
    html = """
    <html><body><article>
      <h2><a href="/post/x">Photonic chip carries 1 Tb/s of data</a></h2>
      <p>Silicon photonics integration enables terabit interconnect.</p>
      <script type="application/ld+json">
      {"@type": "NewsArticle", "datePublished": "2026-06-01"}
      </script>
    </article></body></html>
    """
    items = Scraper().extract_articles(html, base_url="https://example.com")
    assert len(items) == 1
    assert items[0]["published"] == "2026-06-01"


def test_page_date_not_used_when_multiple_candidates():
    """OG meta on a listing page would smear the same date across all items —
    we deliberately ignore it when there's more than one container."""
    html = """
    <html><head>
      <meta property="article:published_time" content="2026-05-28">
    </head><body><main>
      <article><h2><a href="/a">Article one with substantial title</a></h2>
        <p>Body of article one.</p></article>
      <article><h2><a href="/b">Article two with substantial title</a></h2>
        <p>Body of article two.</p></article>
    </main></body></html>
    """
    items = Scraper().extract_articles(html, base_url="https://example.com")
    assert len(items) == 2
    assert all(i["published"] is None for i in items)


# ----- _fetch_source(scrape) integration via MockTransport ----------------


def test_scrape_method_via_mock_transport():
    """End-to-end through the agent's fetch path: scrape source -> items."""
    from systems.base_agent import BaseAgent

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=ARTICLE_TAG_HTML)

    # A throwaway BaseAgent subclass we can construct (run + _build_record + _save unused).
    class _Probe(BaseAgent):
        async def run(self): ...
        def _build_record(self, item, data): ...
        async def _save(self, record): ...

    async def body():
        scraper = _mock_scraper(handler)
        probe = _Probe.__new__(_Probe)
        probe.scraper = scraper
        probe.log = __import__("logging").getLogger("test")
        items = await probe._fetch_source(
            {"name": "MIT news", "url": "https://example.com/news", "method": "web_scrape"}
        )
        await scraper.close()
        return items

    items = asyncio.run(body())
    assert len(items) == 2
    assert all(i["source_name"] == "MIT news" for i in items)
    assert items[0]["title"] == "Solid-state battery hits 1000 cycles"


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
