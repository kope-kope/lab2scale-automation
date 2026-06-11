"""System 2 orchestrator — runs all city event agents in parallel.

Each city is handled by a SearchCityAgent, which discovers events via Tavily
web search (one query per focus area), scores them with Claude Haiku, and
stores the upcoming, on-topic ones. The orchestrator runs the cities
concurrently, aggregates per-city + total stats, and isolates failures so one
bad city never aborts the sweep.

Requires ``TAVILY_API_KEY``. Without it, each city returns zero events (with a
warning) rather than crashing the sweep.
"""

from __future__ import annotations

import asyncio
import logging
import os

from lib.data_store import DataStore, db_path_from_url
from lib.dedup import Deduplicator
from lib.llm import LLMFilter
from lib.tavily_searcher import TavilySearcher
from systems.system2_events.search_city_agent import SearchCityAgent

log = logging.getLogger("system2.orchestrator")

_AGGREGATE_KEYS = (
    "sources", "fetched", "filtered", "new_items",
    "skipped", "errors", "dropped_old",
)


class EventsOrchestrator:
    """Spins up one SearchCityAgent per city and runs them in parallel."""

    CITIES = ["boston", "nyc", "sf"]

    def __init__(
        self,
        llm: LLMFilter | None = None,
        dedup: Deduplicator | None = None,
        store: DataStore | None = None,
        *,
        cities: list[str] | None = None,
        threshold: float | None = None,
        future_horizon_days: int | None = 30,
        max_results_per_query: int = 10,
        # Injected for tests; otherwise built from TAVILY_API_KEY.
        tavily_searcher: TavilySearcher | None = None,
    ):
        self.cities = cities if cities is not None else list(self.CITIES)
        self.threshold = threshold if threshold is not None else 6.0
        self.future_horizon_days = future_horizon_days
        self.max_results_per_query = max_results_per_query

        self._owns_store = store is None
        if store is None:
            url = os.getenv("DATABASE_URL", "sqlite:///data/lab2scale.db")
            store = DataStore(db_path_from_url(url))
        self.store = store
        self.llm = llm or LLMFilter()
        self.dedup = dedup or Deduplicator(self.store)

        # Build a Tavily searcher from env unless one was injected. We own
        # (and must close) only a searcher we created ourselves.
        self._owns_tavily = tavily_searcher is None
        if tavily_searcher is not None:
            self.tavily = tavily_searcher
        else:
            key = os.getenv("TAVILY_API_KEY", "")
            self.tavily = TavilySearcher(key) if key else None

    async def _run_city(self, city: str) -> dict:
        agent = SearchCityAgent(
            city, self.tavily, self.llm, self.dedup, self.store,
            threshold=self.threshold,
            max_results_per_query=self.max_results_per_query,
            future_horizon_days=self.future_horizon_days,  # None → date filter disabled
        )
        return await agent.run()

    async def run(self) -> dict:
        await self.store.connect()
        await self.store.init_db()  # idempotent — guarantees tables exist

        if self.tavily is None:
            log.warning(
                "System 2: no TAVILY_API_KEY set — event discovery is disabled. "
                "Set TAVILY_API_KEY to enable web-search-based events."
            )
            empty = {key: 0 for key in _AGGREGATE_KEYS}
            if self._owns_store:
                await self.store.close()
            return {
                "system": "events",
                "totals": empty,
                "cities": {c: dict(empty) for c in self.cities},
            }

        log.info("System 2: running %d city agents in parallel [Tavily search]", len(self.cities))
        results = await asyncio.gather(
            *(self._run_city(c) for c in self.cities), return_exceptions=True
        )

        per_city: dict[str, dict] = {}
        totals = {key: 0 for key in _AGGREGATE_KEYS}
        for city, result in zip(self.cities, results):
            if isinstance(result, Exception):
                log.error("City agent '%s' failed: %r", city, result)
                per_city[city] = {"error": repr(result)}
                totals["errors"] += 1
                continue
            per_city[city] = result
            for key in _AGGREGATE_KEYS:
                totals[key] += result.get(key, 0)

        if self._owns_tavily and self.tavily is not None:
            await self.tavily.close()
        if self._owns_store:
            await self.store.close()

        log.info(
            "System 2 done: %d new events across %d cities (%d errors)",
            totals["new_items"], len(self.cities), totals["errors"],
        )
        return {"system": "events", "totals": totals, "cities": per_city}
