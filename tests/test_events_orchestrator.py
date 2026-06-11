"""Offline test for the System 2 EventsOrchestrator (Tavily search mode).

Runs the orchestrator over a couple of cities with a fake Tavily searcher and
fake event-aware LLM against a real in-memory DataStore, verifying parallel
aggregation, graceful degradation when a city's search fails, and the no-key
short-circuit.
"""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from systems.system2_events.orchestrator import EventsOrchestrator
from systems.system2_events.search_city_agent import DOMAIN_QUERIES

# content includes a locality token (MIT / Columbia) so the agent's locality
# filter keeps them.
BOSTON_RESULT = {
    "url": "https://ex.com/bos1",
    "title": "MIT Energy Night",
    "content": "MIT Energy Night — power electronics panel at MIT, Cambridge",
    "score": 0.9,
}
NYC_RESULT = {
    "url": "https://ex.com/nyc1",
    "title": "Columbia Energy Conference",
    "content": "Columbia Energy Conference on grid storage, New York",
    "score": 0.8,
}
SCORES = {"MIT Energy Night": 9.0, "Columbia Energy Conference": 8.0}


class FakeTavily:
    """Returns results keyed by a location marker present in the query.

    The agent builds queries using CITY_CONTEXT phrasing (e.g. "Boston OR
    Cambridge Massachusetts", "New York City"), so we match on those markers
    case-insensitively rather than the bare city slug.
    """

    def __init__(self, results_by_marker, explode_for=()):
        self.results_by_marker = results_by_marker
        self.explode_for = tuple(explode_for)
        self.queries = []

    async def search(self, query, max_results=None):
        self.queries.append(query)
        ql = query.lower()
        for marker in self.explode_for:
            if marker.lower() in ql:
                raise RuntimeError(f"boom: {marker}")
        for marker, results in self.results_by_marker.items():
            if marker.lower() in ql:
                return [dict(r) for r in results]
        return []

    async def close(self):
        pass


class FakeEventLLM:
    def __init__(self, scores):
        self.scores = scores

    async def score_event_relevance(self, content):
        for title, score in self.scores.items():
            if title in content:
                return score
        return 0.0

    async def extract_event_data(self, content):
        return {
            "event_name": None, "event_date": "2026-06-15",
            "event_time": None, "venue": "Hall A",
            "description": "summary", "cost": "Free",
            "event_type": "seminar", "relevance_tags": ["energy_storage"],
        }


async def _fresh_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def test_orchestrator_runs_all_cities_and_aggregates():
    async def body():
        store = await _fresh_store()
        tavily = FakeTavily({"Boston": [BOSTON_RESULT], "New York": [NYC_RESULT]})
        orch = EventsOrchestrator(
            llm=FakeEventLLM(SCORES), dedup=Deduplicator(store), store=store,
            cities=["boston", "nyc"], threshold=6.0,
            future_horizon_days=None,   # disable date filter for determinism
            tavily_searcher=tavily,
        )
        result = await orch.run()
        events = await store.get_unreported_events()
        await store.close()
        return result, events, tavily

    result, events, tavily = asyncio.run(body())

    assert result["system"] == "events"
    assert result["totals"]["new_items"] == 2
    # Each city fires one query per focus area.
    assert result["totals"]["sources"] == 2 * len(DOMAIN_QUERIES)
    assert result["totals"]["errors"] == 0
    assert set(result["cities"]) == {"boston", "nyc"}
    assert result["cities"]["boston"]["new_items"] == 1
    assert result["cities"]["nyc"]["new_items"] == 1

    # Every city ran all 5 domain queries.
    assert len(tavily.queries) == 2 * len(DOMAIN_QUERIES)

    assert len(events) == 2
    assert {e["city"] for e in events} == {"boston", "nyc"}
    assert all(e["relevance_tags"] == ["energy_storage"] for e in events)


def test_orchestrator_isolates_a_failing_city_search():
    """If one city's searches all raise, that city degrades to zero events with
    an error count — the other city is unaffected and the sweep completes."""
    async def body():
        store = await _fresh_store()
        tavily = FakeTavily(
            {"Boston": [BOSTON_RESULT], "New York": [NYC_RESULT]},
            explode_for=["New York"],
        )
        orch = EventsOrchestrator(
            llm=FakeEventLLM(SCORES), dedup=Deduplicator(store), store=store,
            cities=["boston", "nyc"], threshold=6.0,
            future_horizon_days=None,
            tavily_searcher=tavily,
        )
        result = await orch.run()
        events = await store.get_unreported_events()
        await store.close()
        return result, events

    result, events = asyncio.run(body())

    assert result["cities"]["boston"]["new_items"] == 1
    assert result["cities"]["nyc"]["new_items"] == 0
    assert result["cities"]["nyc"]["errors"] >= 1   # every nyc query raised
    assert result["totals"]["new_items"] == 1
    assert len(events) == 1
    assert events[0]["city"] == "boston"


def test_orchestrator_without_tavily_returns_empty(monkeypatch):
    """No TAVILY_API_KEY and no injected searcher → empty result, no crash."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    async def body():
        store = await _fresh_store()
        orch = EventsOrchestrator(
            llm=FakeEventLLM(SCORES), dedup=Deduplicator(store), store=store,
            cities=["boston", "nyc"],
            # tavily_searcher omitted → built from env → None (key removed)
        )
        result = await orch.run()
        await store.close()
        return result

    result = asyncio.run(body())
    assert result["totals"]["new_items"] == 0
    assert set(result["cities"]) == {"boston", "nyc"}
