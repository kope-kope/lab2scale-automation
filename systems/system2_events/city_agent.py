"""Configurable event-tracking agent for a single city.

Reads its sources from ``config/cities/{city}.yaml``. Same generic pipeline as
DomainAgent (fetch → dedup → score → extract → store), but with event-shaped
LLM calls and writes to the ``events`` table instead of ``findings``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from systems.base_agent import BaseAgent, _parse_date_string


class CityAgent(BaseAgent):
    """Event-tracking agent that writes events to the ``events`` table.

    Date semantics for events differ from research: the relevant time is when
    the event *happens*, not when its listing was posted. The pre-fetch
    "current week" filter is disabled by default (long-announced conferences
    have old listing dates but valid future event_dates), and a post-extraction
    filter requires the extracted ``event_date`` to fall in the upcoming
    ``future_horizon_days`` window. Events without a parseable ``event_date``
    are dropped, matching the same rule research uses for missing dates.

    Default horizon is 30 days — most conferences and seminars are announced
    2-8 weeks in advance, so a 7-day cap drops too many legitimate events.
    """

    def __init__(
        self,
        config_path,
        scraper,
        llm,
        dedup,
        store,
        *,
        week_window_days=None,          # events: don't filter by listing date
        future_horizon_days: int = 30,  # event_date must be within next N days
        **kwargs,
    ):
        super().__init__(
            config_path, scraper, llm, dedup, store,
            week_window_days=week_window_days, **kwargs,
        )
        self.future_horizon_days = future_horizon_days
        self.city = Path(config_path).stem  # e.g. "boston"
        self.name = f"{self.city}_events_agent"
        # focus_area is a no-op for events scoring, but keep it set for
        # downstream code that reads it (e.g. logging).
        self.focus_area = f"events:{self.city}"
        self.log = logging.getLogger(f"system2.{self.city}")

    async def run(self) -> dict:
        self.log.info("Running %s (threshold %.1f)", self.name, self.threshold)
        return await self._pipeline()

    # ----- hooks override BaseAgent's research defaults -------------------

    async def _score_item(self, content: str) -> float:
        return await self.llm.score_event_relevance(content)

    async def _extract_item(self, content: str) -> dict:
        return await self.llm.extract_event_data(content)

    def _should_save(self, item: dict, data: dict) -> bool:
        """Keep only events whose extracted ``event_date`` is in the upcoming
        window (today through today + ``future_horizon_days``). Missing or
        unparseable dates are dropped — per the spec, undated content can't be
        bucketed into a weekly brief."""
        if self.future_horizon_days is None:
            return True
        event_date = _parse_date_string(data.get("event_date") or "")
        if event_date is None:
            return False
        now = self._now or datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        horizon = today + timedelta(days=self.future_horizon_days)
        return today <= event_date <= horizon

    def _build_record(self, item: dict, data: dict) -> dict:
        # The model is the source of truth for event fields when present,
        # falling back to whatever the listing item gave us.
        return {
            "id": item["_hash"],
            "system": "events",
            "city": self.city,
            "agent": self.name,
            "event_name": (data.get("event_name") or item.get("title") or "").strip(),
            "event_date": data.get("event_date"),
            "event_time": data.get("event_time"),
            "venue": data.get("venue"),
            "url": item.get("link") or item.get("url"),
            "description": data.get("description") or item.get("summary"),
            "cost": data.get("cost"),
            "event_type": data.get("event_type"),
            "relevance_tags": data.get("relevance_tags") or [],
            "relevance_score": item["_score"],
        }

    async def _save(self, record: dict) -> bool:
        return await self.store.save_event(record)
