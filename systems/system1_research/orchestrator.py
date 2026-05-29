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
from systems.system1_research.domain_agent import DomainAgent

log = logging.getLogger("system1.orchestrator")

# Automation/config/domains  (this file is systems/system1_research/orchestrator.py)
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "domains"

_AGGREGATE_KEYS = ("sources", "fetched", "filtered", "new_items", "skipped", "errors")


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
    ):
        self.domains = domains if domains is not None else list(self.DOMAINS)
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.methods = methods if methods is not None else {"rss"}
        self.threshold = threshold
        self.max_items = max_items

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

    async def _run_domain(self, domain: str) -> dict:
        """Construct and run one domain agent. Construction errors (e.g. a
        missing config) surface here so gather can capture them per-domain."""
        agent = DomainAgent(
            str(self.config_dir / f"{domain}.yaml"),
            self.scraper, self.llm, self.dedup, self.store,
            methods=self.methods, threshold=self.threshold, max_items=self.max_items,
        )
        return await agent.run()

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
        if self._owns_store:
            await self.store.close()

        log.info(
            "System 1 done: %d new findings across %d domains (%d errors)",
            totals["new_items"], len(self.domains), totals["errors"],
        )
        return {"system": "research", "totals": totals, "domains": per_domain}
