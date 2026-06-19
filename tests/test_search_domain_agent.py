"""Unit tests for the Tavily-backed SearchDomainAgent (System 1 research)."""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from systems.system1_research.search_domain_agent import (
    DOMAIN_SEARCH_QUERIES,
    SearchDomainAgent,
)

N_QUERIES = len(DOMAIN_SEARCH_QUERIES["energy_storage"])


class FakeTavily:
    """Returns the same results for every query (simulates query overlap)."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    async def search(self, query, max_results=None, *, time_range=None, topic=None):
        self.calls.append((query, time_range))
        return [dict(r) for r in self.results]

    async def close(self):
        pass


class FakeResearchLLM:
    def __init__(self, scores, fixed_title=None):
        self.scores = scores
        self.fixed_title = fixed_title
        self.score_calls = 0
        self.extract_calls = 0

    async def score_relevance(self, content, focus_area):
        self.score_calls += 1
        for title, score in self.scores.items():
            if title in content:
                return score
        return 0.0

    async def extract_structured_data(self, content, focus_area):
        self.extract_calls += 1
        name = next((t for t in self.scores if t in content), None)
        return {
            "title": self.fixed_title or name,
            "summary": "why an investor would care",
            "researchers": ["Dr. A. Founder"],
            "affiliation": "National Lab",
            "contact_info": None,
            "trl_estimate": "TRL 4",
            "source_type": "news",
        }


async def _fresh_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def _agent(store, searcher, llm, **kw):
    return SearchDomainAgent(
        "energy_storage", searcher, llm, Deduplicator(store), store,
        threshold=kw.pop("threshold", 8.0), **kw,
    )


def test_dedups_overlapping_urls_across_queries():
    results = [{"url": "https://ex.com/1", "title": "Form Energy raises Series F",
                "content": "Form Energy raises Series F to scale iron-air batteries",
                "score": 0.9}]
    scores = {"Form Energy raises Series F": 9.0}

    async def body():
        store = await _fresh_store()
        llm = FakeResearchLLM(scores)
        stats = await _agent(store, FakeTavily(results), llm).run()
        findings = await store.get_unreported_findings()
        await store.close()
        return stats, findings, llm

    stats, findings, llm = asyncio.run(body())
    assert stats["fetched"] == N_QUERIES          # one result per query
    assert stats["new_items"] == 1                # collapsed to one unique URL
    assert llm.score_calls == 1                   # dedup happened before scoring
    assert llm.extract_calls == 1
    assert len(findings) == 1
    assert findings[0]["focus_area"] == "energy_storage"
    assert findings[0]["agent"] == "energy_storage_search_agent"


def test_below_threshold_is_filtered():
    results = [{"url": "https://ex.com/x", "title": "Minor incremental tweak",
                "content": "Minor incremental tweak to an existing product", "score": 0.4}]

    async def body():
        store = await _fresh_store()
        stats = await _agent(store, FakeTavily(results),
                             FakeResearchLLM({"Minor incremental tweak": 5.0})).run()
        findings = await store.get_unreported_findings()
        await store.close()
        return stats, findings

    stats, findings = asyncio.run(body())
    assert stats["new_items"] == 0
    assert stats["filtered"] == 1
    assert findings == []


def test_same_story_from_different_sources_dedups():
    """Same story under two URLs → one saved row via normalized-title dedup."""
    results = [
        {"url": "https://siteA.com/a", "title": "Sila lands battery deal",
         "content": "Sila lands battery deal A", "score": 0.9},
        {"url": "https://siteB.com/b", "title": "Sila Battery Deal!",
         "content": "Sila lands battery deal B", "score": 0.9},
    ]

    async def body():
        store = await _fresh_store()
        # Both results score high; extraction returns the SAME title for both.
        llm = FakeResearchLLM(
            {"Sila lands battery deal A": 9.0, "Sila lands battery deal B": 9.0},
            fixed_title="Sila Lands Battery Deal",
        )
        stats = await _agent(store, FakeTavily(results), llm).run()
        findings = await store.get_unreported_findings()
        await store.close()
        return stats, findings

    stats, findings = asyncio.run(body())
    assert stats["new_items"] == 1, "duplicate story collapses to one finding"
    assert len(findings) == 1


def test_dedups_across_reruns():
    results = [{"url": "https://ex.com/1", "title": "Form Energy raises Series F",
                "content": "Form Energy raises Series F", "score": 0.9}]
    scores = {"Form Energy raises Series F": 9.0}

    async def body():
        store = await _fresh_store()
        dedup = Deduplicator(store)
        a1 = SearchDomainAgent("energy_storage", FakeTavily(results),
                               FakeResearchLLM(scores), dedup, store, threshold=8.0)
        first = await a1.run()
        llm2 = FakeResearchLLM(scores)
        a2 = SearchDomainAgent("energy_storage", FakeTavily(results),
                               llm2, dedup, store, threshold=8.0)
        second = await a2.run()
        await store.close()
        return first, second, llm2

    first, second, llm2 = asyncio.run(body())
    assert first["new_items"] == 1
    assert second["new_items"] == 0
    assert second["skipped"] >= 1
    assert llm2.score_calls == 0       # dedup short-circuits before scoring


def test_passes_time_range_to_searcher():
    results = [{"url": "https://ex.com/1", "title": "X", "content": "X", "score": 0.1}]

    async def body():
        store = await _fresh_store()
        searcher = FakeTavily(results)
        await _agent(store, searcher, FakeResearchLLM({}), time_range="day").run()
        await store.close()
        return searcher.calls

    calls = asyncio.run(body())
    assert calls, "searcher should have been called"
    assert all(time_range == "day" for _, time_range in calls)
