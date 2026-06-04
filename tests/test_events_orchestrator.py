"""Offline test for the System 2 EventsOrchestrator.

Runs the orchestrator over a couple of temp-config cities with a fake scraper
and fake event-aware LLM against a real in-memory DataStore, verifying parallel
aggregation and per-city failure isolation.
"""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from systems.system2_events.orchestrator import EventsOrchestrator


class FakeScraper:
    def __init__(self, feeds):
        self.feeds = feeds

    async def fetch_rss(self, url):
        return [dict(item) for item in self.feeds.get(url, [])]

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


def _write_city(config_dir, name, feed_url):
    (config_dir / f"{name}.yaml").write_text(
        "university_events:\n"
        f'  - name: "{name} events"\n'
        f'    url: "{feed_url}"\n'
        "    method: rss\n"
    )


async def _fresh_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def test_orchestrator_runs_all_cities_and_aggregates(tmp_path):
    async def body():
        _write_city(tmp_path, "boston", "https://ex.com/bos.rss")
        _write_city(tmp_path, "nyc", "https://ex.com/nyc.rss")
        feeds = {
            "https://ex.com/bos.rss": [
                {"title": "MIT Energy Night", "link": "https://ex.com/bos1",
                 "summary": "great", "published": "2026-06-15"},
            ],
            "https://ex.com/nyc.rss": [
                {"title": "Columbia Energy Conference", "link": "https://ex.com/nyc1",
                 "summary": "great", "published": "2026-06-20"},
            ],
        }
        scores = {"MIT Energy Night": 9.0, "Columbia Energy Conference": 8.0}
        store = await _fresh_store()
        orch = EventsOrchestrator(
            scraper=FakeScraper(feeds), llm=FakeEventLLM(scores),
            dedup=Deduplicator(store), store=store,
            cities=["boston", "nyc"], config_dir=tmp_path,
            methods={"rss"}, threshold=6.0, week_window_days=None, future_horizon_days=None,
        )
        result = await orch.run()
        events = await store.get_unreported_events()
        await store.close()
        return result, events

    result, events = asyncio.run(body())

    assert result["system"] == "events"
    assert result["totals"]["new_items"] == 2
    assert result["totals"]["sources"] == 2
    assert result["totals"]["errors"] == 0
    assert set(result["cities"]) == {"boston", "nyc"}
    assert result["cities"]["boston"]["new_items"] == 1
    assert result["cities"]["nyc"]["new_items"] == 1

    assert len(events) == 2
    assert {e["city"] for e in events} == {"boston", "nyc"}
    assert all(e["relevance_tags"] == ["energy_storage"] for e in events)


def test_orchestrator_isolates_a_failing_city(tmp_path):
    async def body():
        _write_city(tmp_path, "boston", "https://ex.com/bos.rss")
        # "missing_city" intentionally has no config file → CityAgent ctor raises.
        feeds = {
            "https://ex.com/bos.rss": [
                {"title": "MIT Energy Night", "link": "https://ex.com/bos1",
                 "summary": "great", "published": "2026-06-15"},
            ],
        }
        store = await _fresh_store()
        orch = EventsOrchestrator(
            scraper=FakeScraper(feeds),
            llm=FakeEventLLM({"MIT Energy Night": 9.0}),
            dedup=Deduplicator(store), store=store,
            cities=["boston", "missing_city"], config_dir=tmp_path,
            methods={"rss"}, threshold=6.0, week_window_days=None, future_horizon_days=None,
        )
        result = await orch.run()
        events = await store.get_unreported_events()
        await store.close()
        return result, events

    result, events = asyncio.run(body())
    assert "error" in result["cities"]["missing_city"]
    assert result["totals"]["errors"] == 1
    assert result["cities"]["boston"]["new_items"] == 1
    assert len(events) == 1
    assert events[0]["city"] == "boston"
