"""System 1 orchestrator — runs all domain agents in parallel.

Creates one DomainAgent per focus area and runs them concurrently with
asyncio.gather. A single agent failing (bad config, crash) is captured and
recorded; it never aborts the other agents or the sweep.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from lib.data_store import DataStore, db_path_from_url
from lib.dedup import Deduplicator
from lib.llm import LLMFilter
from lib.scraper import Scraper
from lib.tavily_searcher import TavilySearcher
from systems.system1_research.domain_agent import DomainAgent
from systems.system1_research.search_domain_agent import SearchDomainAgent

log = logging.getLogger("system1.orchestrator")

# Automation/config/domains  (this file is systems/system1_research/orchestrator.py)
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "domains"

_AGGREGATE_KEYS = (
    "sources", "fetched", "filtered", "new_items",
    "skipped", "errors", "dropped_old",
)


class ResearchOrchestrator:
    """Spins up all domain agents in parallel."""

    DOMAINS = [
        "power_generation",
        "energy_storage",
        "power_electronics",
        "semiconductors",
        "deep_tech_infra",
    ]

    def __init__(
        self,
        scraper: Scraper | None = None,
        llm: LLMFilter | None = None,
        dedup: Deduplicator | None = None,
        store: DataStore | None = None,
        *,
        domains: list[str] | None = None,
        config_dir=None,
        methods: set[str] | None = None,
        threshold: float | None = None,
        max_items: int | None = None,
        week_window_days: int | None = 7,
        search_threshold: float = 6.0,
        search_time_range: str | None = "week",
        max_results_per_query: int = 10,
        # Injected for tests; otherwise built from TAVILY_API_KEY.
        tavily_searcher: TavilySearcher | None = None,
    ):
        self.domains = domains if domains is not None else list(self.DOMAINS)
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.methods = methods if methods is not None else {"rss"}
        self.threshold = threshold
        self.max_items = max_items
        self.week_window_days = week_window_days
        # The web-search path gets its own (lower) bar: curated RSS feeds stay
        # strict at `threshold`, while broad web search uses `search_threshold`
        # so genuinely relevant deal flow isn't lost to the high RSS bar.
        self.search_threshold = search_threshold
        self.search_time_range = search_time_range
        self.max_results_per_query = max_results_per_query

        # Track which resources we created so we only close those.
        self._owns_store = store is None
        self._owns_scraper = scraper is None
        if store is None:
            url = os.getenv("DATABASE_URL", "sqlite:///data/lab2scale.db")
            store = DataStore(db_path_from_url(url))
        self.store = store
        self.scraper = scraper or Scraper()
        self.llm = llm or LLMFilter()
        self.dedup = dedup or Deduplicator(self.store)

        # Web-search discovery (System 1's richness boost). Built from env
        # unless injected; None disables it (research still runs via RSS).
        self._owns_tavily = tavily_searcher is None
        if tavily_searcher is not None:
            self.tavily = tavily_searcher
        else:
            key = os.getenv("TAVILY_API_KEY", "")
            self.tavily = TavilySearcher(key) if key else None

    async def _run_domain(self, domain: str) -> dict:
        """Run a domain's RSS agent and (if Tavily is available) its web-search
        agent, and merge their stats into one per-domain result. Per-agent
        errors are isolated so one failing path never drops the other."""
        rss_agent = DomainAgent(
            str(self.config_dir / f"{domain}.yaml"),
            self.scraper, self.llm, self.dedup, self.store,
            methods=self.methods, threshold=self.threshold,
            max_items=self.max_items, week_window_days=self.week_window_days,
        )
        runs = [rss_agent.run()]
        if self.tavily is not None:
            search_agent = SearchDomainAgent(
                domain, self.tavily, self.llm, self.dedup, self.store,
                threshold=self.search_threshold,
                max_results_per_query=self.max_results_per_query,
                time_range=self.search_time_range,
            )
            runs.append(search_agent.run())

        results = await asyncio.gather(*runs, return_exceptions=True)
        merged = {key: 0 for key in _AGGREGATE_KEYS}
        for result in results:
            if isinstance(result, Exception):
                log.error("Domain '%s' sub-agent failed: %r", domain, result)
                merged["errors"] += 1
                continue
            for key in _AGGREGATE_KEYS:
                merged[key] += result.get(key, 0)
        return merged

    async def run(self) -> dict:
        await self.store.connect()
        await self.store.init_db()  # idempotent — guarantees the tables exist

        log.info("System 1: running %d domain agents in parallel", len(self.domains))
        results = await asyncio.gather(
            *(self._run_domain(d) for d in self.domains), return_exceptions=True
        )

        per_domain: dict[str, dict] = {}
        totals = {key: 0 for key in _AGGREGATE_KEYS}
        for domain, result in zip(self.domains, results):
            if isinstance(result, Exception):
                log.error("Domain agent '%s' failed: %r", domain, result)
                per_domain[domain] = {"error": repr(result)}
                totals["errors"] += 1
                continue
            per_domain[domain] = result
            for key in _AGGREGATE_KEYS:
                totals[key] += result.get(key, 0)

        if self._owns_scraper:
            await self.scraper.close()
        if self._owns_tavily and self.tavily is not None:
            await self.tavily.close()
        if self._owns_store:
            await self.store.close()

        log.info(
            "System 1 done: %d new findings across %d domains (%d errors)",
            totals["new_items"], len(self.domains), totals["errors"],
        )
        return {"system": "research", "totals": totals, "domains": per_domain}
