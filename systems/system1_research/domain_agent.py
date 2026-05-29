"""Configurable research-monitoring agent for a single focus area.

Reads its sources from ``config/domains/{focus_area}.yaml``. The focus area and
agent name are derived from the config filename, so the same class powers all
five System 1 domains — only the YAML differs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from systems.base_agent import BaseAgent


class DomainAgent(BaseAgent):
    """Research agent that writes findings to the ``findings`` table."""

    def __init__(self, config_path, scraper, llm, dedup, store, **kwargs):
        super().__init__(config_path, scraper, llm, dedup, store, **kwargs)
        self.focus_area = Path(config_path).stem  # e.g. "energy_storage"
        self.name = f"{self.focus_area}_agent"
        self.log = logging.getLogger(f"system1.{self.focus_area}")

    async def run(self) -> dict:
        self.log.info("Running %s (threshold %.1f)", self.name, self.threshold)
        return await self._pipeline()

    def _build_record(self, item: dict, data: dict) -> dict:
        return {
            "id": item["_hash"],
            "system": "research",
            "focus_area": self.focus_area,
            "agent": self.name,
            "title": (data.get("title") or item.get("title") or "").strip(),
            "summary": data.get("summary"),
            "relevance_score": item["_score"],
            "researchers": data.get("researchers") or [],
            "affiliation": data.get("affiliation"),
            "contact_info": data.get("contact_info"),
            "source_url": item.get("link") or item.get("url"),
            "source_type": data.get("source_type"),
            "trl_estimate": data.get("trl_estimate"),
            "raw_content": item.get("_content"),
        }

    async def _save(self, record: dict) -> bool:
        return await self.store.save_finding(record)
