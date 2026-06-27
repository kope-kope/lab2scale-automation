"""City event agent backed by Tavily web search.

Instead of polling iCal/RSS feeds, this agent runs one Tavily search per
focus area for a single city, scores each result with Claude Haiku, then
extracts structured event data for anything above the relevance threshold.

Cost per city per run:
  5 domains × 10 results × ~2 Haiku calls = ~100 Haiku calls + 5 Tavily credits
  ≈ $0.005 Tavily + ~$0.002 Haiku ≈ $0.007/city/run → $0.021/sweep (3 cities)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from lib.tavily_searcher import TavilySearcher
from systems.base_agent import _parse_date_string

# ── Domain query fragments ────────────────────────────────────────────────────
# Each fragment is appended with "{city} {month} {next_month}" to form the
# full Tavily query. Keep these tight — Tavily's snippet quality degrades with
# very long queries.

# Lab2Scale's five active sectors (Operating Memo §5). Each query hunts events
# where early-stage founders and companies in the sector show up — conferences,
# summits, demo days, pitch nights — so the team can meet deal flow (and find
# Insights speaking slots).
DOMAIN_QUERIES: dict[str, str] = {
    "nuclear_advanced_energy": (
        "nuclear SMR advanced energy fusion startup founder "
        "conference OR summit OR demo day"
    ),
    "water_cooling": (
        "datacenter cooling atmospheric water waste-heat startup founder "
        "conference OR summit OR meetup"
    ),
    "power_electronics": (
        "power electronics GaN SiC wide-bandgap startup founder "
        "conference OR summit OR workshop"
    ),
    "autonomous_systems": (
        "autonomous systems robotics AV defense autonomy startup founder "
        "conference OR summit OR demo day"
    ),
    "advanced_manufacturing": (
        "advanced manufacturing hard tech materials startup founder "
        "demo day OR pitch OR conference"
    ),
}

# Location phrasing injected into each search query, per city.
CITY_CONTEXT: dict[str, str] = {
    "boston": "Boston OR Cambridge Massachusetts",
    "nyc": "New York City",
    "sf": "San Francisco Bay Area",
}

# Locality evidence: an event is treated as belonging to a city only if its
# text (title + snippet + extracted venue/description) mentions one of these
# terms. This keeps globally-relevant but non-local events (and generic online
# webinars) out of a city's brief.
#
# Single-word entries match whole word-tokens (so "mit" matches "MIT" but NOT
# "summit"); multi-word entries match as a substring of the text.
CITY_LOCALITY: dict[str, tuple[str, ...]] = {
    "boston": (
        "boston", "cambridge", "somerville", "massachusetts", "ma",
        "mit", "harvard", "northeastern", "tufts", "greentown", "kendall",
        "new england",
    ),
    "nyc": (
        "new york", "nyc", "manhattan", "brooklyn", "queens", "columbia",
        "nyu", "cornell tech", "newlab", "ny",
    ),
    "sf": (
        "san francisco", "bay area", "silicon valley", "berkeley", "stanford",
        "palo alto", "oakland", "san jose", "menlo park", "mountain view",
        "sunnyvale", "santa clara", "sf", "california",
    ),
}

# Cap on page text sent to the extractor (the LLM layer truncates further).
_EXTRACT_CHARS = 4000

log = logging.getLogger("system2.search")


def _normalize_name(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for matching the same
    event title across sources (e.g. '2026 NYC Solar + Storage Summit')."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class SearchCityAgent:
    """Event-tracking agent that uses Tavily search instead of feed scraping.

    The pipeline is:
        search (5 queries) → score (Haiku) → dedup → extract (Haiku) → store

    The ``run()`` return dict matches the shape produced by ``CityAgent.run()``
    so the existing ``EventsOrchestrator`` aggregation logic works unchanged.
    """

    def __init__(
        self,
        city: str,
        searcher: TavilySearcher,
        llm,
        dedup,
        store,
        *,
        threshold: float = 6.0,
        max_results_per_query: int = 10,
        future_horizon_days: int | None = 30,
        _now: datetime | None = None,
    ):
        self.city = city
        self.searcher = searcher
        self.llm = llm
        self.dedup = dedup
        self.store = store
        self.threshold = threshold
        self.max_results_per_query = max_results_per_query
        self.future_horizon_days = future_horizon_days
        self._now = _now
        self.name = f"{city}_events_agent"
        self.log = logging.getLogger(f"system2.{city}")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _now_utc(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def _build_query(self, domain_fragment: str) -> str:
        """Compose a Tavily query: domain keywords + location + current/next month."""
        now = self._now_utc()
        month = now.strftime("%B %Y")
        next_month = (now.replace(day=1) + timedelta(days=32)).strftime("%B %Y")
        location = CITY_CONTEXT.get(self.city, self.city)
        return f"{domain_fragment} in {location} {month} OR {next_month}"

    def _in_city(self, *texts: str) -> bool:
        """True if any text shows evidence the event is in this city's metro.

        Single-word keywords match whole word-tokens (so "mit" matches "MIT"
        but not "summit"); multi-word keywords match as a substring. Cities
        without a locality list (custom/test cities) pass through.
        """
        keywords = CITY_LOCALITY.get(self.city)
        if not keywords:
            return True
        blob = " ".join(t for t in texts if t).lower()
        tokens = set(re.findall(r"[a-z0-9]+", blob))
        for kw in keywords:
            if " " in kw:
                if kw in blob:
                    return True
            elif kw in tokens:
                return True
        return False

    def _is_upcoming(self, event_date_str: str | None) -> bool:
        """True iff the extracted event_date falls in the upcoming window."""
        if self.future_horizon_days is None:
            return True
        if not event_date_str:
            return False
        event_date = _parse_date_string(event_date_str)
        if event_date is None:
            return False
        now = self._now_utc()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        horizon = today + timedelta(days=self.future_horizon_days)
        return today <= event_date <= horizon

    # ── pipeline steps ───────────────────────────────────────────────────────

    async def _search_all_domains(self) -> tuple[list[dict], int]:
        """Run all domain queries for this city concurrently.

        Each result dict is annotated with ``_domain`` and ``_query`` so we
        can trace which search produced it. Returns ``(all_items, errors)``.
        """
        tasks = {
            domain: self._build_query(fragment)
            for domain, fragment in DOMAIN_QUERIES.items()
        }
        search_results = await asyncio.gather(
            *(
                self.searcher.search(query, max_results=self.max_results_per_query)
                for query in tasks.values()
            ),
            return_exceptions=True,
        )
        all_items: list[dict] = []
        errors = 0
        for (domain, query), result in zip(tasks.items(), search_results):
            if isinstance(result, Exception):
                self.log.error("Search failed [%s]: %s", domain, result)
                errors += 1
                continue
            for r in result:
                r["_domain"] = domain
                r["_query"] = query
            all_items.extend(result)
        return all_items, errors

    async def _collect_unique(self, results: list[dict]) -> tuple[list[dict], int, int]:
        """Deduplicate results before any LLM work.

        Drops URL duplicates across the (overlapping) domain queries, then
        drops anything already seen in a prior sweep — so we never spend Haiku
        tokens scoring an event we've already processed. Returns
        ``(new_items, skipped, errors)``; each new item is annotated with
        ``_hash``, ``_score_text`` (title + snippet) and ``_extract_text``
        (title + full page text) for downstream scoring and extraction.
        """
        seen_urls: set[str] = set()
        new_items: list[dict] = []
        skipped = errors = 0
        for r in results:
            url = r.get("url", "")
            title = (r.get("title") or "").strip()
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            content_hash = self.dedup.compute_hash(url, title)
            try:
                if await self.dedup.is_seen(content_hash):  # seen in a prior sweep
                    skipped += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                self.log.error("dedup check failed for %s: %s", url, exc)
                errors += 1
                continue
            snippet = r.get("content") or ""
            raw = r.get("raw_content") or ""
            r["_hash"] = content_hash
            # Scoring uses the short snippet (cheap, enough to judge relevance).
            r["_score_text"] = f"{title}\n\n{snippet}".strip()
            # Extraction uses the full page text when available — event dates,
            # venues and times live in the page body, not the snippet. Bounded
            # to keep Haiku token cost in check.
            r["_extract_text"] = f"{title}\n\n{raw or snippet}".strip()[:_EXTRACT_CHARS]
            new_items.append(r)
        return new_items, skipped, errors

    async def _score(self, items: list[dict]) -> list[dict]:
        """Score new items concurrently with Haiku; keep those at or above the
        threshold. Below-threshold items are marked seen so the next sweep
        doesn't re-score them."""
        if not items:
            return []
        scores = await asyncio.gather(
            *(self.llm.score_event_relevance(it["_score_text"]) for it in items),
            return_exceptions=True,
        )
        kept: list[dict] = []
        for it, score in zip(items, scores):
            if isinstance(score, Exception):
                self.log.warning("score failed for %s: %s", it.get("url"), score)
                continue
            if score >= self.threshold:
                it["_score"] = score
                kept.append(it)
            else:
                await self._mark_seen(it["_hash"])
        self.log.info(
            "Scored %d new items: %d kept (>= %.1f), %d below threshold",
            len(items), len(kept), self.threshold, len(items) - len(kept),
        )
        return kept

    async def _extract_and_store(self, kept: list[dict]) -> tuple[int, int, int, int, int]:
        """Extract event data, apply the future-horizon + locality filters, save.

        Returns ``(saved, dropped_old, off_location, skipped, errors)``.
        """
        saved = dropped_old = off_location = skipped = errors = 0
        # Collapse the same event arriving from different sources/URLs within
        # this run (e.g. a summit listed on its own site, Facebook, and an
        # aggregator). Keyed by (normalized name, date) — known only post-extract.
        seen_events: set[tuple[str, str]] = set()
        for item in kept:
            try:
                data = await self.llm.extract_event_data(item["_extract_text"])
            except Exception as exc:  # noqa: BLE001
                self.log.error("extraction failed for %s: %s", item.get("url"), exc)
                errors += 1
                continue

            event_date = data.get("event_date")
            if not self._is_upcoming(event_date):
                dropped_old += 1
                await self._mark_seen(item["_hash"])
                continue

            event_key = (
                _normalize_name(data.get("event_name") or item.get("title") or ""),
                (event_date or "")[:10],
            )
            if event_key[0] and event_key in seen_events:
                skipped += 1                      # same event, different source
                await self._mark_seen(item["_hash"])
                continue

            # Locality: drop events with no evidence of being in this city's
            # metro (a conference elsewhere, or a generic online webinar).
            # Judge on the per-event extracted fields + search snippet — NOT
            # the raw page body, which on aggregator/listing pages mentions
            # other cities' events and would cause false matches.
            if not self._in_city(
                data.get("venue") or "",
                data.get("event_name") or "",
                data.get("description") or "",
                item.get("content") or "",
                item.get("title") or "",
            ):
                off_location += 1
                await self._mark_seen(item["_hash"])
                continue

            record = {
                "id": item["_hash"],
                "system": "events",
                "city": self.city,
                "agent": self.name,
                "event_name": (data.get("event_name") or item.get("title") or "").strip(),
                "event_date": event_date,
                "event_time": data.get("event_time"),
                "venue": data.get("venue"),
                "url": item.get("url"),
                "description": (
                    data.get("description") or (item.get("content") or "")[:500]
                ),
                "cost": data.get("cost"),
                "event_type": data.get("event_type"),
                "relevance_tags": data.get("relevance_tags") or [],
                "relevance_score": item["_score"],
            }
            try:
                if await self.store.save_event(record):
                    saved += 1
                    if event_key[0]:
                        seen_events.add(event_key)
                    self.log.debug(
                        "Saved [%.1f] %s — %s",
                        item["_score"], event_date, record["event_name"][:60],
                    )
                else:
                    skipped += 1  # already in events table
                await self._mark_seen(item["_hash"])
            except Exception as exc:  # noqa: BLE001
                self.log.error("save failed for %s: %s", item.get("url"), exc)
                errors += 1

        self.log.info(
            "Saved %d new events (%d outside window, %d off-location, "
            "%d duplicates, %d errors)",
            saved, dropped_old, off_location, skipped, errors,
        )
        return saved, dropped_old, off_location, skipped, errors

    async def _mark_seen(self, content_hash: str) -> None:
        try:
            await self.dedup.mark_seen(content_hash, self.name)
        except Exception as exc:  # noqa: BLE001
            self.log.error("mark_seen failed: %s", exc)

    # ── public entry point ───────────────────────────────────────────────────

    async def run(self) -> dict:
        self.log.info(
            "Running %s (Tavily search, threshold=%.1f, horizon=%sd)",
            self.name, self.threshold, self.future_horizon_days or "off",
        )

        raw, search_errors = await self._search_all_domains()
        fetched = len(raw)

        new_items, dedup_skipped, dedup_errors = await self._collect_unique(raw)
        kept = await self._score(new_items)
        below_threshold = len(new_items) - len(kept)

        (saved, dropped_old, off_location,
         save_skipped, save_errors) = await self._extract_and_store(kept)

        return {
            "agent": self.name,
            "sources": len(DOMAIN_QUERIES),   # "sources" = number of search queries
            "fetched": fetched,
            # "filtered" = relevance drops: below threshold + off-location.
            "filtered": below_threshold + off_location,
            "new_items": saved,
            "skipped": dedup_skipped + save_skipped,
            "errors": search_errors + dedup_errors + save_errors,
            "dropped_old": dropped_old,
        }
