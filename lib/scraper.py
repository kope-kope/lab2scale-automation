"""Shared scraping utilities — async HTTP client with retry, rate limiting,
and robots.txt awareness.

A single Scraper instance is shared across all sub-agents. It pools
connections, throttles to a polite per-domain rate, retries transient
failures with exponential backoff (honoring Retry-After), and respects
robots.txt for HTML page fetches.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.robotparser
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger("lib.scraper")

USER_AGENT = "Lab2Scale-Monitor/1.0 (+https://lab-2-scale.com)"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RATE_LIMIT = 2  # max requests per second, per domain
MAX_ATTEMPTS = 3


def _should_retry(exc: BaseException) -> bool:
    """Retry transient network errors and 5xx/429 responses, but not 4xx."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    return False


def _wait(retry_state) -> float:
    """Honor a Retry-After header if present, else exponential backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass  # HTTP-date form — fall through to backoff
    return wait_exponential(multiplier=1, min=1, max=10)(retry_state)


class Scraper:
    """Async HTTP client with retry logic, rate limiting, and robots.txt respect."""

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        rate_limit: int = DEFAULT_RATE_LIMIT,
        respect_robots: bool = True,
        max_attempts: int = MAX_ATTEMPTS,
        transport: httpx.BaseTransport | None = None,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.max_attempts = max_attempts
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._domain_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = defaultdict(float)
        self._min_interval = 1.0 / rate_limit if rate_limit > 0 else 0.0

    # ----- lifecycle -------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
                transport=self._transport,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "Scraper":
        await self._get_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ----- rate limiting + low-level request -------------------------------

    async def _throttle(self, domain: str) -> None:
        """Ensure at least ``_min_interval`` seconds between requests to a domain."""
        if self._min_interval <= 0:
            return
        async with self._domain_locks[domain]:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = self._last_request[domain] + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._last_request[domain] = now

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with throttling and retry. Raises on final failure."""
        domain = urlparse(url).netloc
        client = await self._get_client()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=_wait,
            retry=retry_if_exception(_should_retry),
            reraise=True,
        ):
            with attempt:
                await self._throttle(domain)
                resp = await client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
        raise RuntimeError("unreachable")  # AsyncRetrying always returns or raises

    # ----- robots.txt ------------------------------------------------------

    async def _load_robots(self, base: str):
        try:
            client = await self._get_client()
            resp = await client.get(base + "/robots.txt")
        except Exception as exc:  # noqa: BLE001 — robots failure should not block
            log.debug("robots.txt fetch failed for %s: %s (allowing)", base, exc)
            return None
        if resp.status_code >= 400:
            return None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp

    async def _is_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            self._robots[base] = await self._load_robots(base)
        rp = self._robots[base]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001 — malformed robots → allow
            return True

    # ----- public API ------------------------------------------------------

    async def fetch_rss(self, url: str) -> list[dict]:
        """Fetch and parse an RSS/Atom feed.

        Returns a list of ``{title, link, summary, published}`` dicts.
        On fetch failure, logs and returns an empty list (caller skips the source).
        """
        try:
            resp = await self._request("GET", url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to fetch RSS %s: %s", url, exc)
            return []
        return self._parse_feed(resp.content)

    @staticmethod
    def _parse_feed(content) -> list[dict]:
        feed = feedparser.parse(content)
        items = []
        for entry in feed.entries:
            items.append(
                {
                    "title": entry.get("title"),
                    "link": entry.get("link"),
                    "summary": entry.get("summary") or entry.get("description"),
                    "published": entry.get("published") or entry.get("updated"),
                }
            )
        return items

    async def fetch_page(self, url: str) -> str:
        """Fetch an HTML page. Respects robots.txt. Returns '' on disallow/failure."""
        if not await self._is_allowed(url):
            log.info("robots.txt disallows %s — skipping", url)
            return ""
        try:
            resp = await self._request("GET", url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to fetch page %s: %s", url, exc)
            return ""
        return resp.text

    async def fetch_api(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> dict:
        """Fetch JSON from a REST API. Returns {} on failure or non-JSON response."""
        try:
            resp = await self._request("GET", url, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to fetch API %s: %s", url, exc)
            return {}
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("API response from %s is not JSON: %s", url, exc)
            return {}

    def parse_html(self, html: str, selectors: dict) -> list[dict]:
        """Extract structured data from HTML using CSS selectors.

        ``selectors`` maps field names to CSS selectors. An optional ``item``
        (or ``_item``) key selects repeating container elements; the remaining
        keys are extracted relative to each container. Without an ``item`` key,
        the whole document is treated as a single container.

        A selector may end in ``@attr`` to capture an attribute instead of text,
        e.g. ``"a@href"``.
        """
        soup = BeautifulSoup(html or "", "lxml")
        item_selector = selectors.get("item") or selectors.get("_item")
        field_selectors = {
            k: v for k, v in selectors.items() if k not in ("item", "_item")
        }

        containers = soup.select(item_selector) if item_selector else [soup]
        results = []
        for container in containers:
            row = {
                field: self._extract_one(container, sel)
                for field, sel in field_selectors.items()
            }
            if any(v for v in row.values()):
                results.append(row)
        return results

    @staticmethod
    def _extract_one(element, selector: str):
        attr = None
        if "@" in selector:
            selector, attr = selector.rsplit("@", 1)
            selector = selector.strip()
        target = element.select_one(selector) if selector else element
        if target is None:
            return None
        if attr:
            return target.get(attr)
        return target.get_text(strip=True)

    # --- general-purpose article extraction (web_scrape sources) -----------

    # Tags whose contents are never article items.
    _NOISE_TAGS = (
        "script", "style", "nav", "header", "footer", "aside",
        "form", "noscript", "iframe", "svg",
    )
    # Container selectors, tried in priority order. Anything matching `*=foo i`
    # is a case-insensitive substring match on the class/id.
    _ARTICLE_SELECTORS = (
        "article",
        "[class*='article' i]",
        "[class*='post' i]",
        "[class*='news' i]",
        "[class*='entry' i]",
        "[class*='story' i]",
        "[class*='card' i]",
        "[class*='listing' i]",
    )
    _MIN_CANDIDATES = 3
    _MIN_TITLE_CHARS = 12
    _SNIPPET_MAX_CHARS = 500

    def extract_articles(self, html: str, base_url: str = "") -> list[dict]:
        """Extract article-like items from arbitrary HTML.

        Returns a list of ``{title, link, summary, published}`` dicts — the same
        shape as ``fetch_rss`` output, so the downstream agent pipeline doesn't
        care whether an item came from RSS or a scraped page.

        Heuristics: strip nav/script/style noise, look for article-shaped
        containers (``<article>``, classes containing "post"/"news"/"entry"/…),
        and fall back to ``<li>`` items in the main content area when too few
        containers are found. Returns ``[]`` if nothing extractable.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(self._NOISE_TAGS):
            tag.decompose()

        candidates: list = []
        seen_ids: set[int] = set()
        for selector in self._ARTICLE_SELECTORS:
            try:
                matches = soup.select(selector)
            except Exception:  # noqa: BLE001 — bad selector should never abort
                continue
            for el in matches:
                if id(el) not in seen_ids:
                    seen_ids.add(id(el))
                    candidates.append(el)

        # Fallback: list items in main content (lab "publications" / news lists).
        if len(candidates) < self._MIN_CANDIDATES:
            for el in soup.select("main li, article li, [role='main'] li"):
                if id(el) not in seen_ids:
                    seen_ids.add(id(el))
                    candidates.append(el)

        items: list[dict] = []
        seen_links: set[str] = set()
        for container in candidates:
            row = self._build_article_item(container, base_url)
            if row is None:
                continue
            if row["link"] in seen_links:
                continue
            seen_links.add(row["link"])
            items.append(row)
        return items

    def _build_article_item(self, container, base_url: str) -> dict | None:
        # Title — prefer a heading, fall back to a substantial anchor.
        title = None
        heading = container.find(["h1", "h2", "h3", "h4"])
        if heading is not None:
            title = heading.get_text(strip=True)
        if not title:
            for a in container.find_all("a", limit=5):
                text = a.get_text(strip=True)
                if len(text) >= self._MIN_TITLE_CHARS:
                    title = text
                    break
        if not title or len(title) < self._MIN_TITLE_CHARS:
            return None

        # Link — first usable anchor, resolved to absolute.
        link = None
        for a in container.find_all("a"):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            link = urljoin(base_url, href)
            break
        if not link:
            return None

        # Snippet — concatenate paragraph text, excluding the title.
        text_parts: list[str] = []
        running = 0
        for p in container.find_all("p", limit=6):
            text = p.get_text(strip=True)
            if not text or text == title or len(text) < 20:
                continue
            text_parts.append(text)
            running += len(text)
            if running >= self._SNIPPET_MAX_CHARS:
                break
        snippet = " ".join(text_parts) if text_parts else container.get_text(
            separator=" ", strip=True
        ).replace(title, "", 1)
        snippet = " ".join(snippet.split())[: self._SNIPPET_MAX_CHARS] or None

        return {"title": title, "link": link, "summary": snippet, "published": None}
