"""Abstract base class for all sub-agents.

A sub-agent runs the same four-stage pipeline regardless of domain or city:

    fetch_all_sources  →  filter_and_score  →  extract_and_store

The base class owns that pipeline plus the generic, config-driven source
discovery (it walks whatever category keys a YAML config happens to use).
Subclasses supply the focus, the per-item scoring focus area, and the mapping
from an extracted item to a database record.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

from lib.llm import RELEVANCE_THRESHOLD

# YAML configs use either "scrape" or "web_scrape" for the same thing.
_SCRAPE_ALIASES = {"scrape", "web_scrape"}


def load_yaml(path: str) -> dict:
    """Load a YAML config file into a dict (empty dict if the file is blank)."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _parse_date_string(raw: str) -> datetime | None:
    """Parse a date string from a feed or web page into a tz-aware UTC datetime.

    Tries RFC 822 (the common RSS form, e.g. ``Mon, 25 May 2026 09:00:00 GMT``)
    and then ISO 8601 (``2026-05-25``, ``2026-05-25T09:00:00Z``). Returns None
    if neither parses — the agent will then drop the item.
    """
    if not raw:
        return None
    s = raw.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    candidate = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Try a handful of common textual formats from scraped HTML.
    for fmt in (
        "%B %d, %Y",   # May 25, 2026
        "%b %d, %Y",   # May 25, 2026 (or "Jan 5, 2026")
        "%d %B %Y",    # 25 May 2026
        "%d %b %Y",    # 25 May 2026 / 25 Jan 2026
        "%B %d %Y",    # May 25 2026 (no comma)
        "%Y/%m/%d",    # 2026/05/25
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Last attempt — date-only, e.g. "2026-05-25".
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_method(method: str | None) -> str:
    m = (method or "").strip().lower()
    return "scrape" if m in _SCRAPE_ALIASES else m


class BaseAgent(ABC):
    """Abstract base class for all sub-agents."""

    def __init__(
        self,
        config_path: str,
        scraper,
        llm,
        dedup,
        store,
        *,
        methods: set[str] | None = None,
        threshold: float | None = None,
        max_items: int | None = None,
        week_window_days: int | None = 7,
        _now: datetime | None = None,
    ):
        self.config_path = config_path
        self.config = load_yaml(config_path)
        self.scraper = scraper
        self.llm = llm
        self.dedup = dedup
        self.store = store
        # Optional filter: only fetch sources whose normalized method is in this
        # set (e.g. {"rss"}). None means all methods.
        self.methods = methods
        self.threshold = RELEVANCE_THRESHOLD if threshold is None else threshold
        # Optional cap on how many fetched items proceed to scoring (cost/time
        # guard for demos and dry runs). None means no cap.
        self.max_items = max_items
        # Drop items outside this rolling window (days back from now). Items
        # whose date can't be parsed are also dropped. None disables the filter.
        self.week_window_days = week_window_days
        # Injectable "now" for tests; production uses datetime.now(utc).
        self._now = _now
        # Subclasses set these.
        self.name = "base_agent"
        self.focus_area = ""
        self.log = logging.getLogger("agent.base")
        self._reset_counters()

    def _reset_counters(self) -> None:
        self._skipped = 0   # duplicates (already seen in a prior sweep) + table dups
        self._filtered = 0  # scored below threshold
        self._errors = 0
        self._dropped_old = 0  # outside the week window or undated
        # Hashes scored in THIS run. Lets us skip items that appear in
        # overlapping feeds without persisting "seen" before the finding is
        # saved — so an interrupted run never silently drops kept items.
        self._seen_this_run: set[str] = set()

    # ----- source discovery ------------------------------------------------

    def iter_sources(self) -> list[dict]:
        """Walk the config and return every source dict, regardless of which
        category key it lives under. A source is any dict in a top-level list
        that has both ``url`` and ``method``. Filtered by ``self.methods``."""
        sources: list[dict] = []
        for value in self.config.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or not item.get("method"):
                    continue
                # A source needs a fetchable URL: some configs use `url`, others
                # put the feed in `rss_url` (e.g. arXiv categories).
                if not (item.get("url") or item.get("rss_url")):
                    continue
                if self.methods is not None and normalize_method(item["method"]) not in self.methods:
                    continue
                sources.append(item)
        return sources

    # ----- pipeline --------------------------------------------------------

    async def fetch_all_sources(self) -> list[dict]:
        """Fetch from all (filtered) sources concurrently, then drop items
        outside the current-week window. Items without a parseable date are
        dropped too — the system runs weekly, so undated content can't be
        bucketed correctly. Returns survivors annotated with ``source_name``."""
        sources = self.iter_sources()
        results = await asyncio.gather(
            *(self._fetch_source(s) for s in sources), return_exceptions=True
        )
        raw: list[dict] = []
        for source, result in zip(sources, results):
            if isinstance(result, Exception):
                self._errors += 1
                self.log.error("Failed to fetch %s: %s", source.get("name"), result)
                continue
            raw.extend(result)

        if self.week_window_days is not None:
            cutoff = self._window_cutoff()
            fresh: list[dict] = []
            for item in raw:
                dt = self._item_datetime(item)
                if dt is None or dt < cutoff:
                    self._dropped_old += 1
                    continue
                fresh.append(item)
            self.log.info(
                "Fetched %d raw items from %d sources; %d kept after the "
                "%dd window, %d dropped (undated or stale)",
                len(raw), len(sources), len(fresh),
                self.week_window_days, self._dropped_old,
            )
            return fresh

        self.log.info("Fetched %d raw items from %d sources", len(raw), len(sources))
        return raw

    def _window_cutoff(self) -> datetime:
        now = self._now or datetime.now(timezone.utc)
        return now - timedelta(days=self.week_window_days)

    @staticmethod
    def _item_datetime(item: dict) -> datetime | None:
        """Return a tz-aware UTC datetime for an item, or None if undateable."""
        parsed = item.get("published_parsed")
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        raw = (
            item.get("published")
            or item.get("updated")
            or item.get("event_date")
        )
        if not raw:
            return None
        return _parse_date_string(raw)

    async def _fetch_source(self, source: dict) -> list[dict]:
        method = normalize_method(source.get("method"))
        name = source.get("name") or source.get("url") or source.get("rss_url") or "?"
        if method == "rss":
            # Prefer an explicit feed URL (rss_url) over a page URL (url).
            feed_url = source.get("rss_url") or source.get("url")
            items = await self.scraper.fetch_rss(feed_url)
        elif method == "scrape":
            page_url = source.get("url") or source.get("rss_url")
            html = await self.scraper.fetch_page(page_url)
            if not html:
                return []
            items = self.scraper.extract_articles(html, base_url=page_url)
        else:
            # api method — not built yet.
            self.log.debug("Skipping %s source: %s", method, name)
            return []
        for item in items:
            item["source_name"] = name
        return items

    async def filter_and_score(self, items: list[dict]) -> list[dict]:
        """Deduplicate, then LLM-score each new item. Returns the items scoring
        at or above the threshold (with ``_hash``, ``_score``, ``_content`` set)."""
        kept: list[dict] = []
        for item in items:
            title = (item.get("title") or "").strip()
            url = item.get("link") or item.get("url") or ""
            if not title or not url:
                continue

            content_hash = self.dedup.compute_hash(url, title)
            if content_hash in self._seen_this_run:  # overlapping feeds, same run
                self._skipped += 1
                continue
            try:
                if await self.dedup.is_seen(content_hash):  # seen in a prior sweep
                    self._skipped += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                self.log.error("dedup check failed for %s: %s", url, exc)
                continue
            self._seen_this_run.add(content_hash)

            content = self._item_content(item)
            score = await self._score_item(content)

            if score >= self.threshold:
                item["_hash"] = content_hash
                item["_score"] = score
                item["_content"] = content
                kept.append(item)
            else:
                self._filtered += 1
                # Below threshold and nothing to persist — safe to mark seen now.
                await self._mark_seen(content_hash, item.get("source_name", self.name))

        self.log.info(
            "Scored %d items: %d kept (>= %.1f), %d below threshold, %d duplicates",
            len(items), len(kept), self.threshold, self._filtered, self._skipped,
        )
        return kept

    async def extract_and_store(self, items: list[dict]) -> int:
        """Extract structured data for kept items and save them. Returns the
        number of new rows written. Database errors propagate (fatal per spec)."""
        saved = 0
        for item in items:
            try:
                data = await self._extract_item(item["_content"])
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                self.log.error("extraction failed for %s: %s", item.get("link"), exc)
                continue
            if not self._should_save(item, data):
                # Post-extraction filter (e.g. event_date outside the upcoming
                # window). Mark seen so we don't re-score next sweep.
                self._dropped_old += 1
                await self._mark_seen(item["_hash"], item.get("source_name", self.name))
                continue
            record = self._build_record(item, data)
            if await self._save(record):
                saved += 1
            else:
                self._skipped += 1  # already present in the table
            # Mark seen only AFTER persistence, so an interrupted run leaves
            # un-saved kept items un-seen and they get retried next sweep.
            await self._mark_seen(item["_hash"], item.get("source_name", self.name))
        self.log.info("Saved %d new items (%d duplicates skipped)", saved, self._skipped)
        return saved

    async def _mark_seen(self, content_hash: str, source: str) -> None:
        try:
            await self.dedup.mark_seen(content_hash, source)
        except Exception as exc:  # noqa: BLE001
            self.log.error("mark_seen failed for %s: %s", content_hash, exc)

    @staticmethod
    def _item_content(item: dict) -> str:
        title = (item.get("title") or "").strip()
        summary = (item.get("summary") or "").strip()
        return f"{title}\n\n{summary}".strip()

    async def _pipeline(self) -> dict:
        """The standard fetch → score → store run, with aggregated stats."""
        self._reset_counters()
        raw = await self.fetch_all_sources()
        if self.max_items is not None and len(raw) > self.max_items:
            self.log.info("Capping %d fetched items to max_items=%d", len(raw), self.max_items)
            raw = raw[: self.max_items]
        kept = await self.filter_and_score(raw)
        new_items = await self.extract_and_store(kept)
        return {
            "agent": self.name,
            "sources": len(self.iter_sources()),
            "new_items": new_items,
            "skipped": self._skipped,
            "errors": self._errors,
            "fetched": len(raw),
            "filtered": self._filtered,
            "dropped_old": self._dropped_old,
        }

    # ----- subclass hooks --------------------------------------------------

    async def _score_item(self, content: str) -> float:
        """Score a single item. Defaults to research-style scoring against
        ``self.focus_area``; CityAgent overrides for event scoring."""
        return await self.llm.score_relevance(content, self.focus_area)

    async def _extract_item(self, content: str) -> dict:
        """Extract structured fields from an item. Defaults to research-style
        extraction; CityAgent overrides for event extraction."""
        return await self.llm.extract_structured_data(content, self.focus_area)

    def _should_save(self, item: dict, data: dict) -> bool:
        """Post-extraction veto. Default: always save. CityAgent overrides
        to enforce ``event_date`` being in the upcoming window."""
        return True

    @abstractmethod
    async def run(self) -> dict:
        """Execute the agent's pipeline. Returns {new_items, skipped, errors}."""

    @abstractmethod
    def _build_record(self, item: dict, data: dict) -> dict:
        """Map a scored item + extracted data to a database record dict."""

    @abstractmethod
    async def _save(self, record: dict) -> bool:
        """Persist a record. Returns False if it was a duplicate."""
