"""Unit tests for the Tavily-backed SearchCityAgent and TavilySearcher.

Covers: query construction, URL dedup across overlapping domain queries,
threshold filtering, the future-horizon date filter, cross-sweep dedup, and
the TavilySearcher HTTP wrapper (via httpx MockTransport).
"""

import asyncio
from datetime import datetime, timezone

import httpx

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from lib.tavily_searcher import TavilySearcher
from systems.system2_events.search_city_agent import DOMAIN_QUERIES, SearchCityAgent

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


class FakeTavily:
    """Returns the same result set for every query (simulating overlap)."""

    def __init__(self, results):
        self.results = results
        self.queries = []

    async def search(self, query, max_results=None):
        self.queries.append(query)
        return [dict(r) for r in self.results]

    async def close(self):
        pass


class FakeEventLLM:
    def __init__(self, scores, event_date="2026-06-15"):
        self.scores = scores
        self.event_date = event_date
        self.score_calls = 0
        self.extract_calls = 0

    async def score_event_relevance(self, content):
        self.score_calls += 1
        for title, score in self.scores.items():
            if title in content:
                return score
        return 0.0

    async def extract_event_data(self, content):
        self.extract_calls += 1
        name = next((t for t in self.scores if t in content), None)
        return {
            "event_name": name, "event_date": self.event_date,
            "event_time": "18:00", "venue": "Hall A",
            "description": "desc", "cost": "Free",
            "event_type": "seminar", "relevance_tags": ["energy_storage"],
        }


async def _fresh_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def _agent(store, searcher, llm, **kw):
    return SearchCityAgent(
        "boston", searcher, llm, Deduplicator(store), store,
        threshold=kw.pop("threshold", 6.0),
        future_horizon_days=kw.pop("future_horizon_days", 30),
        _now=kw.pop("_now", NOW),
        **kw,
    )


def test_query_includes_city_and_month():
    agent = SearchCityAgent("boston", None, None, None, None, _now=NOW)
    q = agent._build_query("energy storage event")
    assert "Boston" in q          # from the city's location context
    assert "June 2026" in q       # current month
    assert "July 2026" in q       # next month


def test_dedups_overlapping_urls_across_domain_queries():
    """All 5 domain queries return the same event URL → scored once, saved once."""
    results = [{"url": "https://ex.com/e1", "title": "MIT Energy Night",
                "content": "MIT Energy Night power electronics", "score": 0.9}]
    scores = {"MIT Energy Night": 9.0}

    async def body():
        store = await _fresh_store()
        llm = FakeEventLLM(scores)
        agent = _agent(store, FakeTavily(results), llm)
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events, llm

    stats, events, llm = asyncio.run(body())
    # 5 queries fired, but only 1 unique URL → scored & extracted once.
    assert stats["fetched"] == len(DOMAIN_QUERIES)   # 5 raw results
    assert stats["new_items"] == 1
    assert llm.score_calls == 1          # dedup happened before scoring
    assert llm.extract_calls == 1
    assert len(events) == 1
    assert events[0]["event_name"] == "MIT Energy Night"
    assert events[0]["url"] == "https://ex.com/e1"


def test_below_threshold_is_filtered():
    results = [{"url": "https://ex.com/cook", "title": "Sourdough Class",
                "content": "Sourdough Class bread workshop", "score": 0.5}]

    async def body():
        store = await _fresh_store()
        agent = _agent(store, FakeTavily(results), FakeEventLLM({"Sourdough Class": 2.0}))
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events

    stats, events = asyncio.run(body())
    assert stats["new_items"] == 0
    assert len(events) == 0


def test_event_outside_horizon_is_dropped():
    results = [{"url": "https://ex.com/far", "title": "Far Future Summit",
                "content": "Far Future Summit semiconductors", "score": 0.9}]

    async def body():
        store = await _fresh_store()
        # event_date 90 days out, horizon 30 → dropped
        llm = FakeEventLLM({"Far Future Summit": 9.0}, event_date="2026-09-10")
        agent = _agent(store, FakeTavily(results), llm, future_horizon_days=30)
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events

    stats, events = asyncio.run(body())
    assert stats["new_items"] == 0
    assert stats["dropped_old"] == 1
    assert len(events) == 0


def test_off_location_event_is_dropped():
    """A topically-relevant event with no evidence of being in the city's metro
    (e.g. a conference in Nagpur, India) is dropped by the locality filter."""
    results = [{"url": "https://ex.com/india", "title": "India Power Summit",
                "content": "India Power Summit — grid storage, Nagpur India",
                "score": 0.9}]

    async def body():
        store = await _fresh_store()
        # High relevance, valid near date, but not in Boston.
        llm = FakeEventLLM({"India Power Summit": 9.0}, event_date="2026-06-20")
        agent = _agent(store, FakeTavily(results), llm, future_horizon_days=30)
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events

    stats, events = asyncio.run(body())
    assert stats["new_items"] == 0
    assert stats["filtered"] >= 1     # counted as a relevance/locality drop
    assert len(events) == 0


def test_local_event_passes_locality_filter():
    """An event whose text mentions the metro (MIT / Cambridge) is kept."""
    results = [{"url": "https://ex.com/mit", "title": "Cambridge Energy Forum",
                "content": "Cambridge Energy Forum at MIT — power electronics",
                "score": 0.9}]

    async def body():
        store = await _fresh_store()
        llm = FakeEventLLM({"Cambridge Energy Forum": 9.0}, event_date="2026-06-20")
        agent = _agent(store, FakeTavily(results), llm, future_horizon_days=30)
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events

    stats, events = asyncio.run(body())
    assert stats["new_items"] == 1
    assert len(events) == 1


def test_same_event_from_different_sources_dedups():
    """The same event listed under different URLs (own site, Facebook, an
    aggregator) collapses to one saved row via (name, date) dedup."""
    results = [
        {"url": "https://nyseia.org/summit", "title": "2026 NYC Solar + Storage Summit",
         "content": "2026 NYC Solar + Storage Summit New York", "score": 0.9},
        {"url": "https://facebook.com/summit", "title": "2026 NYC Solar + Storage Summit",
         "content": "NYC Solar Storage Summit in New York", "score": 0.9},
        {"url": "https://nysolarmap.com/summit", "title": "NYC Solar + Storage Summit 2026",
         "content": "Solar plus storage summit New York", "score": 0.9},
    ]

    async def body():
        store = DataStore(":memory:")
        await store.init_db()
        llm = FakeEventLLM({"Solar": 9.0}, event_date="2026-06-24")
        # Force the same extracted name for all three.
        async def fixed_extract(content):
            llm.extract_calls += 1
            return {
                "event_name": "2026 NYC Solar + Storage Summit",
                "event_date": "2026-06-24", "event_time": None,
                "venue": "John Jay College, New York", "description": "d",
                "cost": None, "event_type": "summit", "relevance_tags": [],
            }
        llm.extract_event_data = fixed_extract
        agent = SearchCityAgent("nyc", FakeTavily(results), llm,
                                Deduplicator(store), store,
                                threshold=6.0, future_horizon_days=60, _now=NOW)
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events

    stats, events = asyncio.run(body())
    assert stats["new_items"] == 1, "3 sources of one event → 1 saved row"
    assert len(events) == 1


def test_missing_event_date_is_dropped_when_horizon_set():
    results = [{"url": "https://ex.com/nodate", "title": "Undated Talk",
                "content": "Undated Talk on photonics", "score": 0.9}]

    async def body():
        store = await _fresh_store()
        llm = FakeEventLLM({"Undated Talk": 9.0}, event_date=None)
        agent = _agent(store, FakeTavily(results), llm, future_horizon_days=30)
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events

    stats, events = asyncio.run(body())
    assert stats["new_items"] == 0
    assert stats["dropped_old"] == 1


def test_dedups_across_reruns():
    results = [{"url": "https://ex.com/e1", "title": "MIT Energy Night",
                "content": "MIT Energy Night power electronics", "score": 0.9}]
    scores = {"MIT Energy Night": 9.0}

    async def body():
        store = await _fresh_store()
        dedup = Deduplicator(store)
        agent1 = SearchCityAgent("boston", FakeTavily(results), FakeEventLLM(scores),
                                 dedup, store, threshold=6.0, _now=NOW)
        first = await agent1.run()
        llm2 = FakeEventLLM(scores)
        agent2 = SearchCityAgent("boston", FakeTavily(results), llm2,
                                 dedup, store, threshold=6.0, _now=NOW)
        second = await agent2.run()
        await store.close()
        return first, second, llm2

    first, second, llm2 = asyncio.run(body())
    assert first["new_items"] == 1
    assert second["new_items"] == 0
    assert second["skipped"] == 1   # recognized as already-seen
    assert llm2.extract_calls == 0  # dedup short-circuits before extraction


# ----- TavilySearcher HTTP wrapper ----------------------------------------


def _mock_searcher(handler) -> TavilySearcher:
    s = TavilySearcher("fake-key")
    s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return s


def test_tavily_searcher_parses_results():
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"url": "https://ex.com/1", "title": "Event One",
             "content": "snippet", "score": 0.9},
        ]})

    async def body():
        s = _mock_searcher(handler)
        out = await s.search("energy storage boston", max_results=5)
        await s.close()
        return out

    out = asyncio.run(body())
    assert len(out) == 1
    assert out[0]["url"] == "https://ex.com/1"
    assert captured["body"]["query"] == "energy storage boston"
    assert captured["body"]["max_results"] == 5


def test_tavily_searcher_returns_empty_on_http_error():
    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})

    async def body():
        s = _mock_searcher(handler)
        out = await s.search("anything")
        await s.close()
        return out

    assert asyncio.run(body()) == []
