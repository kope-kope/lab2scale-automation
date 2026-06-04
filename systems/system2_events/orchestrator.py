"""System 2 orchestrator — runs all city agents in parallel.

Same shape as ResearchOrchestrator: creates one CityAgent per city, runs them
concurrently via asyncio.gather, aggregates per-city + total stats, and
isolates failures so one bad city never aborts the sweep.
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
from systems.system2_events.city_agent import CityAgent

log = logging.getLogger("system2.orchestrator")

# Automation/config/cities  (this file is systems/system2_events/orchestrator.py)
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "cities"

_AGGREGATE_KEYS = (
    "sources", "fetched", "filtered", "new_items",
    "skipped", "errors", "dropped_old",
)


class EventsOrchestrator:
    """Spins up all city agents in parallel."""

    CITIES = ["boston", "nyc", "sf"]

    def __init__(
        self,
        scraper: Scraper | None = None,
        llm: LLMFilter | None = None,
        dedup: Deduplicator | None = None,
        store: DataStore | None = None,
        *,
        cities: list[str] | None = None,
        config_dir=None,
        methods: set[str] | None = None,
        threshold: float | None = None,
        max_items: int | None = None,
        week_window_days: int | None = None,
        future_horizon_days: int | None = 30,
    ):
        self.cities = cities if cities is not None else list(self.CITIES)
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.methods = methods if methods is not None else {"rss"}
        self.threshold = threshold
        self.max_items = max_items
        self.week_window_days = week_window_days
        self.future_horizon_days = future_horizon_days

        self._owns_store = store is None
        self._owns_scraper = scraper is None
        if store is None:
            url = os.getenv("DATABASE_URL", "sqlite:///data/lab2scale.db")
            store = DataStore(db_path_from_url(url))
        self.store = store
        self.scraper = scraper or Scraper()
        self.llm = llm or LLMFilter()
        self.dedup = dedup or Deduplicator(self.store)

    async def _run_city(self, city: str) -> dict:
        agent = CityAgent(
            str(self.config_dir / f"{city}.yaml"),
            self.scraper, self.llm, self.dedup, self.store,
            methods=self.methods, threshold=self.threshold,
            max_items=self.max_items, week_window_days=self.week_window_days,
            future_horizon_days=self.future_horizon_days,
        )
        return await agent.run()

    async def run(self) -> dict:
        await self.store.connect()
        await self.store.init_db()  # idempotent — guarantees tables exist

        log.info("System 2: running %d city agents in parallel", len(self.cities))
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

        if self._owns_scraper:
            await self.scraper.close()
        if self._owns_store:
            await self.store.close()

        log.info(
            "System 2 done: %d new events across %d cities (%d errors)",
            totals["new_items"], len(self.cities), totals["errors"],
        )
        return {"system": "events", "totals": totals, "cities": per_city}
