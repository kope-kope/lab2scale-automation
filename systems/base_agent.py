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
from pathlib import Path

import yaml

from lib.llm import RELEVANCE_THRESHOLD

# YAML configs use either "scrape" or "web_scrape" for the same thing.
_SCRAPE_ALIASES = {"scrape", "web_scrape"}


def load_yaml(path: str) -> dict:
    """Load a YAML config file into a dict (empty dict if the file is blank)."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


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
        # Subclasses set these.
        self.name = "base_agent"
        self.focus_area = ""
        self.log = logging.getLogger("agent.base")
        self._reset_counters()

    def _reset_counters(self) -> None:
        self._skipped = 0   # duplicates (already seen in a prior sweep) + table dups
        self._filtered = 0  # scored below threshold
        self._errors = 0
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
        """Fetch from all (filtered) sources concurrently. Returns raw items,
        each annotated with its ``source_name``. Source failures are logged and
        skipped — they never abort the sweep."""
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
        self.log.info("Fetched %d raw items from %d sources", len(raw), len(sources))
        return raw

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
            score = await self.llm.score_relevance(content, self.focus_area)

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
                data = await self.llm.extract_structured_data(item["_content"], self.focus_area)
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                self.log.error("extraction failed for %s: %s", item.get("link"), exc)
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
        }

    # ----- subclass hooks --------------------------------------------------

    @abstractmethod
    async def run(self) -> dict:
        """Execute the agent's pipeline. Returns {new_items, skipped, errors}."""

    @abstractmethod
    def _build_record(self, item: dict, data: dict) -> dict:
        """Map a scored item + extracted data to a database record dict."""

    @abstractmethod
    async def _save(self, record: dict) -> bool:
        """Persist a record. Returns False if it was a duplicate."""
