"""Research agent backed by Tavily web search (System 1).

The RSS ``DomainAgent`` polls a fixed list of feeds. This agent complements it
by *searching the live web* for recent deal-flow signal in a focus area —
new startups, funding rounds, breakthroughs, spin-outs — so the daily brief
isn't limited to whatever a static feed happened to publish.

Pipeline (mirrors the events SearchCityAgent):
    search (a few deal-flow queries) → dedup by URL → score (Haiku, VC lens)
    → extract (Haiku) → cross-source dedup by title → store as findings

Recency is bounded by ``time_range`` (default "week") so each daily run pulls
a fresh slice rather than re-reporting the same items. Without persistent
dedup across runs, a wider range can repeat across consecutive days — tighten
``time_range`` to "day" (or add a persistent store) if that becomes an issue.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from lib.tavily_searcher import TavilySearcher

# A few deal-flow query angles per focus area. Each is sent to Tavily as its
# own search, so 2 fragments × 5 focus areas = 10 research searches per sweep.
# Keys MUST match the System 1 focus-area names (config/domains/*.yaml stems).
# Lab2Scale is a deep tech commercialization platform; these queries hunt for
# EARLY-STAGE COMPANIES, FOUNDERS, and SPIN-OUTS we could take into the Incubator
# (not research papers or investments). Each focus area gets two angles: one
# weighted to our ecosystem (MIT/Boston > Stanford/Berkeley/national labs), one
# broad-US. Keys MUST match the System 1 sector names (config/domains/*.yaml stems).
# Three angles per sector: (1) ecosystem-weighted spin-outs/founders, (2) broad-US
# early companies, (3) freshest deal-flow signal — recently funded / accelerator
# cohort / grant-awarded early teams. Keys MUST match the System 1 sector names.
DOMAIN_SEARCH_QUERIES: dict[str, list[str]] = {
    "nuclear_advanced_energy": [
        "early-stage nuclear SMR OR advanced fission OR fusion startup OR spin-out founder pre-seed OR seed",
        "MIT OR national lab clean firm power OR advanced nuclear spin-out new company prototype",
        "nuclear OR SMR OR advanced energy startup raised pre-seed OR seed OR DOE OR ARPA-E grant OR accelerator cohort",
    ],
    "water_cooling": [
        "atmospheric water generation OR datacenter cooling OR waste-heat startup OR spin-out founder pre-seed OR seed",
        "early-stage water-energy nexus OR liquid cooling technology company new prototype OR pilot",
        "datacenter cooling OR water technology startup raised pre-seed OR seed OR grant OR accelerator cohort",
    ],
    "power_electronics": [
        "GaN OR SiC OR wide-bandgap power electronics startup OR spin-out founder pre-seed OR seed",
        "MIT OR Stanford power electronics OR power conversion spin-out new company prototype",
        "GaN OR SiC OR power electronics startup raised pre-seed OR seed OR grant OR YC OR accelerator cohort",
    ],
    "autonomous_systems": [
        "autonomous vehicle safety OR defense ground robotics OR industrial autonomy startup OR spin-out founder",
        "early-stage deterministic control OR autonomy software company pre-seed OR seed new prototype",
        "autonomy OR robotics OR autonomous vehicle startup raised pre-seed OR seed OR DARPA OR accelerator cohort",
    ],
    "advanced_manufacturing": [
        "advanced manufacturing AI process OR roll-to-roll OR hardware scale-up startup OR spin-out founder",
        "MIT OR national lab advanced manufacturing OR materials spin-out new company pre-seed OR seed",
        "advanced manufacturing OR hardware OR materials startup raised pre-seed OR seed OR grant OR accelerator cohort",
    ],
}

# Cap on page text sent to the extractor (the LLM layer truncates further).
_EXTRACT_CHARS = 4000

log = logging.getLogger("system1.search")


def _normalize_title(title: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for matching the same
    story reported across multiple sites."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


class SearchDomainAgent:
    """Web-search research agent for one focus area; writes to ``findings``.

    The ``run()`` return dict matches the shape produced by ``DomainAgent.run()``
    so the existing ``ResearchOrchestrator`` aggregation works unchanged.
    """

    def __init__(
        self,
        focus_area: str,
        searcher: TavilySearcher,
        llm,
        dedup,
        store,
        *,
        threshold: float = 6.0,
        max_results_per_query: int = 10,
        time_range: str | None = "week",
        _now: datetime | None = None,
    ):
        self.focus_area = focus_area
        self.searcher = searcher
        self.llm = llm
        self.dedup = dedup
        self.store = store
        self.threshold = threshold
        self.max_results_per_query = max_results_per_query
        self.time_range = time_range
        self._now = _now
        self.name = f"{focus_area}_search_agent"
        self.queries = DOMAIN_SEARCH_QUERIES.get(focus_area, [])
        self.log = logging.getLogger(f"system1.search.{focus_area}")

    # ── pipeline steps ───────────────────────────────────────────────────────

    async def _search_all(self) -> tuple[list[dict], int]:
        """Run this focus area's deal-flow queries concurrently."""
        if not self.queries:
            return [], 0
        results = await asyncio.gather(
            *(
                self.searcher.search(
                    q, max_results=self.max_results_per_query, time_range=self.time_range
                )
                for q in self.queries
            ),
            return_exceptions=True,
        )
        all_items: list[dict] = []
        errors = 0
        for query, result in zip(self.queries, results):
            if isinstance(result, Exception):
                self.log.error("search failed [%s]: %s", query[:50], result)
                errors += 1
                continue
            all_items.extend(result)
        return all_items, errors

    async def _collect_unique(self, results: list[dict]) -> tuple[list[dict], int, int]:
        """Dedup by URL (within run) and against the DB (cross-sweep) before any
        LLM work. Annotates each survivor with ``_hash``, ``_score_text`` and
        ``_extract_text``. Returns ``(new_items, skipped, errors)``."""
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
                if await self.dedup.is_seen(content_hash):
                    skipped += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                self.log.error("dedup check failed for %s: %s", url, exc)
                errors += 1
                continue
            snippet = r.get("content") or ""
            raw = r.get("raw_content") or ""
            r["_hash"] = content_hash
            # Scoring uses the cheap snippet; extraction uses the fuller page
            # text (where the people/affiliation/TRL detail lives).
            r["_score_text"] = f"{title}\n\n{snippet}".strip()
            r["_extract_text"] = f"{title}\n\n{raw or snippet}".strip()[:_EXTRACT_CHARS]
            new_items.append(r)
        return new_items, skipped, errors

    async def _score(self, items: list[dict]) -> list[dict]:
        """Score with Haiku against the focus area; keep >= threshold. Below
        threshold is marked seen so the next sweep doesn't re-score it."""
        if not items:
            return []
        scores = await asyncio.gather(
            *(self.llm.score_relevance(it["_score_text"], self.focus_area) for it in items),
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

    async def _extract_and_store(self, kept: list[dict]) -> tuple[int, int, int]:
        """Extract structured finding data and save. Collapses the same story
        from different sources within the run by normalized title.
        Returns ``(saved, skipped, errors)``."""
        saved = skipped = errors = 0
        seen_titles: set[str] = set()
        for item in kept:
            try:
                data = await self.llm.extract_structured_data(
                    item["_extract_text"], self.focus_area
                )
            except Exception as exc:  # noqa: BLE001
                self.log.error("extraction failed for %s: %s", item.get("url"), exc)
                errors += 1
                continue

            title = (data.get("title") or item.get("title") or "").strip()
            key = _normalize_title(title)
            if key and key in seen_titles:
                skipped += 1  # same story, different source
                await self._mark_seen(item["_hash"])
                continue

            # Notable contacts: if extraction named no founders, run one
            # follow-up search to find who to reach out to.
            researchers = data.get("researchers") or []
            if not researchers:
                researchers = await self._enrich_contacts(data.get("affiliation") or title)

            record = {
                "id": item["_hash"],
                "system": "research",
                "focus_area": self.focus_area,
                "agent": self.name,
                "title": title,
                "summary": data.get("summary") or (item.get("content") or "")[:500],
                "relevance_score": item["_score"],
                "researchers": researchers,
                "affiliation": data.get("affiliation"),
                "contact_info": data.get("contact_info"),
                "source_url": item.get("url"),
                "source_type": data.get("source_type"),
                "trl_estimate": data.get("trl_estimate"),
                "raw_content": item.get("_extract_text"),
            }
            try:
                if await self.store.save_finding(record):
                    saved += 1
                    if key:
                        seen_titles.add(key)
                    self.log.debug("Saved [%.1f] %s", item["_score"], title[:70])
                else:
                    skipped += 1  # already in findings table
                await self._mark_seen(item["_hash"])
            except Exception as exc:  # noqa: BLE001
                self.log.error("save failed for %s: %s", item.get("url"), exc)
                errors += 1
        self.log.info("Saved %d new findings (%d duplicates, %d errors)", saved, skipped, errors)
        return saved, skipped, errors

    async def _enrich_contacts(self, company: str) -> list[str]:
        """When extraction named no founders, run ONE follow-up search to find
        who to reach out to. Returns a list of names (possibly empty). Best
        effort — any failure just yields no contacts, never breaks the run."""
        company = (company or "").strip()
        if not company:
            return []
        try:
            results = await self.searcher.search(
                f"{company} founders OR co-founder OR CEO", max_results=3
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("contact enrichment search failed for %s: %s", company, exc)
            return []
        text = "\n\n".join(
            f"{r.get('title', '')} {r.get('content', '')}" for r in (results or [])
        ).strip()[:_EXTRACT_CHARS]
        if not text:
            return []
        try:
            data = await self.llm.extract_structured_data(text, self.focus_area)
            names = data.get("researchers") or []
            if names:
                self.log.debug("Enriched %d contact(s) for %s", len(names), company)
            return names
        except Exception as exc:  # noqa: BLE001
            self.log.warning("contact enrichment extract failed for %s: %s", company, exc)
            return []

    async def _mark_seen(self, content_hash: str) -> None:
        try:
            await self.dedup.mark_seen(content_hash, self.name)
        except Exception as exc:  # noqa: BLE001
            self.log.error("mark_seen failed: %s", exc)

    # ── public entry point ───────────────────────────────────────────────────

    async def run(self) -> dict:
        self.log.info(
            "Running %s (Tavily search, threshold=%.1f, time_range=%s)",
            self.name, self.threshold, self.time_range or "any",
        )
        raw, search_errors = await self._search_all()
        fetched = len(raw)

        new_items, dedup_skipped, dedup_errors = await self._collect_unique(raw)
        kept = await self._score(new_items)
        below_threshold = len(new_items) - len(kept)

        saved, save_skipped, save_errors = await self._extract_and_store(kept)

        return {
            "agent": self.name,
            "sources": len(self.queries),     # "sources" = number of search queries
            "fetched": fetched,
            "filtered": below_threshold,
            "new_items": saved,
            "skipped": dedup_skipped + save_skipped,
            "errors": search_errors + dedup_errors + save_errors,
            "dropped_old": 0,                 # recency handled by Tavily time_range
        }
