"""Offline integration test for the CityAgent pipeline.

Same shape as test_domain_agent.py but for the events side: fake scraper +
fake event-aware LLM + in-memory DataStore, exercising the full
fetch → score → dedup → extract → save flow into the events table.
"""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from systems.system2_events.city_agent import CityAgent

CONFIG_YAML = """
university_events:
  - name: "MIT Energy Initiative Events"
    url: "https://example.com/mit-energy.rss"
    method: rss
incubators:
  - name: "Greentown Labs"
    url: "https://example.com/greentown.rss"
    method: rss
industry_orgs:
  - name: "Random Web Page"
    url: "https://example.com/page"
    method: web_scrape
"""

FEEDS = {
    "https://example.com/mit-energy.rss": [
        {"title": "MIT Energy Night: Next-Gen Power Electronics", "link": "https://ex.com/e1",
         "summary": "Panel on GaN and SiC.", "published": "2026-06-15"},
        {"title": "Local Cooking Class — Sourdough Edition", "link": "https://ex.com/e2",
         "summary": "Bread workshop.", "published": "2026-06-16"},
    ],
    "https://example.com/greentown.rss": [
        {"title": "Greentown Climatetech Summit 2026", "link": "https://ex.com/g1",
         "summary": "Climate tech leaders convene.", "published": "2026-07-10"},
    ],
}

SCORES = {
    "MIT Energy Night: Next-Gen Power Electronics": 9.0,
    "Greentown Climatetech Summit 2026": 7.5,
    "Local Cooking Class — Sourdough Edition": 2.0,
}


class FakeScraper:
    def __init__(self, feeds, raise_on=()):
        self.feeds = feeds
        self.raise_on = set(raise_on)
        self.fetched = []

    async def fetch_rss(self, url):
        self.fetched.append(url)
        if url in self.raise_on:
            raise RuntimeError(f"boom: {url}")
        return [dict(item) for item in self.feeds.get(url, [])]


class FakeEventLLM:
    def __init__(self, scores):
        self.scores = scores
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
        # The model would normalize event_name; for testing, key off the title
        # in the content so different items get distinct extractions.
        name = None
        for title in self.scores:
            if title in content:
                name = title
                break
        return {
            "event_name": name,
            "event_date": "2026-06-15",
            "event_time": "18:00-20:00",
            "venue": "MIT Media Lab",
            "description": "Extracted description.",
            "cost": "Free",
            "event_type": "panel",
            "relevance_tags": ["power_electronics", "semiconductors"],
        }


def _write_config(tmp_path) -> str:
    cfg = tmp_path / "boston.yaml"
    cfg.write_text(CONFIG_YAML)
    return str(cfg)


async def _fresh_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def test_city_agent_end_to_end(tmp_path):
    async def body():
        store = await _fresh_store()
        scraper = FakeScraper(FEEDS)
        agent = CityAgent(
            _write_config(tmp_path), scraper, FakeEventLLM(SCORES),
            Deduplicator(store), store, methods={"rss"}, threshold=6.0, week_window_days=None, future_horizon_days=None,
        )
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events, scraper

    stats, events, scraper = asyncio.run(body())

    # Two events cleared the 6.0 threshold; the cooking class was filtered.
    assert stats["new_items"] == 2
    assert stats["fetched"] == 3
    assert stats["filtered"] == 1
    assert stats["errors"] == 0

    # Only RSS sources were fetched — the web_scrape source was skipped.
    assert "https://example.com/page" not in scraper.fetched
    assert len(scraper.fetched) == 2

    # Events landed in date order (ASC).
    assert [e["event_name"] for e in events] == [
        "MIT Energy Night: Next-Gen Power Electronics",
        "Greentown Climatetech Summit 2026",
    ]
    top = events[0]
    assert top["city"] == "boston"
    assert top["agent"] == "boston_events_agent"
    assert top["relevance_score"] == 9.0
    assert top["venue"] == "MIT Media Lab"
    assert top["event_type"] == "panel"
    assert top["cost"] == "Free"
    assert top["relevance_tags"] == ["power_electronics", "semiconductors"]  # JSON list
    assert top["url"] == "https://ex.com/e1"
    assert top["reported"] is False


def test_city_agent_drops_events_outside_future_horizon(tmp_path):
    """Events with an event_date in the past, too far out, or missing entirely
    are dropped — only events happening in the next ``future_horizon_days``
    land in the table."""
    from datetime import datetime, timezone

    NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    feeds = {
        "https://example.com/mit-energy.rss": [
            {"title": "MIT Energy Night soon", "link": "https://ex.com/soon",
             "summary": "x", "published": "2026-05-30"},
            {"title": "Past Conference", "link": "https://ex.com/past",
             "summary": "x", "published": "2026-05-30"},
            {"title": "Far-future Summit", "link": "https://ex.com/far",
             "summary": "x", "published": "2026-05-30"},
            {"title": "Undated Workshop", "link": "https://ex.com/undated",
             "summary": "x", "published": "2026-05-30"},
        ],
    }

    cfg = tmp_path / "boston.yaml"
    cfg.write_text(
        "university_events:\n"
        '  - name: "MIT Energy"\n'
        '    url: "https://example.com/mit-energy.rss"\n'
        "    method: rss\n"
    )

    class FixedScoreLLM:
        score_calls = 0
        extract_calls = 0
        async def score_event_relevance(self, content):
            FixedScoreLLM.score_calls += 1
            return 9.0  # everything passes scoring
        async def extract_event_data(self, content):
            FixedScoreLLM.extract_calls += 1
            # Pull the original title back out of the prompt content.
            for title, event_date in [
                ("MIT Energy Night soon",  "2026-06-05"),  # 4d ahead → KEEP
                ("Past Conference",        "2026-05-20"),  # past → drop
                ("Far-future Summit",      "2026-09-01"),  # 90d ahead → drop
                ("Undated Workshop",       None),          # no date → drop
            ]:
                if title in content:
                    return {
                        "event_name": title, "event_date": event_date,
                        "event_time": None, "venue": "Lab",
                        "description": "x", "cost": "Free",
                        "event_type": "seminar", "relevance_tags": ["energy_storage"],
                    }
            return {"event_name": None, "event_date": None,
                    "event_time": None, "venue": None,
                    "description": None, "cost": None,
                    "event_type": None, "relevance_tags": []}

    async def body():
        store = await _fresh_store()
        agent = CityAgent(
            str(cfg), FakeScraper(feeds), FixedScoreLLM(),
            Deduplicator(store), store,
            methods={"rss"}, threshold=6.0,
            week_window_days=None,        # don't filter at fetch
            future_horizon_days=7,        # next 7 days only
            _now=NOW,
        )
        stats = await agent.run()
        events = await store.get_unreported_events()
        await store.close()
        return stats, events

    stats, events = asyncio.run(body())
    # All 4 fetched and scored; 1 saved, 3 dropped by the future-horizon filter.
    assert stats["fetched"] == 4
    assert stats["new_items"] == 1
    assert stats["dropped_old"] == 3
    assert len(events) == 1
    assert events[0]["event_name"] == "MIT Energy Night soon"


def test_city_agent_dedups_on_rerun(tmp_path):
    async def body():
        store = await _fresh_store()
        dedup = Deduplicator(store)
        config = _write_config(tmp_path)

        agent1 = CityAgent(config, FakeScraper(FEEDS), FakeEventLLM(SCORES),
                           dedup, store, methods={"rss"}, threshold=6.0, week_window_days=None, future_horizon_days=None)
        first = await agent1.run()

        llm2 = FakeEventLLM(SCORES)
        agent2 = CityAgent(config, FakeScraper(FEEDS), llm2,
                           dedup, store, methods={"rss"}, threshold=6.0, week_window_days=None, future_horizon_days=None)
        second = await agent2.run()
        await store.close()
        return first, second, llm2

    first, second, llm2 = asyncio.run(body())
    assert first["new_items"] == 2
    assert second["new_items"] == 0
    assert second["skipped"] == 3  # all three items recognized as seen
    assert llm2.score_calls == 0   # dedup short-circuits before scoring
