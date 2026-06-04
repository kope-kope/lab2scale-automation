"""Tests for the current-week filter on BaseAgent.

Items outside the rolling N-day window are dropped before any LLM call; items
whose date can't be parsed are dropped too. This protects against re-processing
old archive content and against wasting LLM cost on stale items.
"""

import asyncio
from datetime import datetime, timezone

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from systems.base_agent import _parse_date_string
from systems.system1_research.domain_agent import DomainAgent

CONFIG_YAML = """
arxiv:
  - name: "test feed"
    url: "https://example.com/feed.rss"
    method: rss
"""

# "Now" we'll pin for the agent — items dated before 2026-05-29 fall outside
# the 7-day window; items on/after 2026-05-29 fall inside.
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

SCORES = {"recent paper": 9.0, "old paper": 9.0, "undated paper": 9.0}


class FakeScraper:
    def __init__(self, items):
        self._items = items

    async def fetch_rss(self, url):
        return [dict(it) for it in self._items]


class FakeLLM:
    def __init__(self, scores):
        self.scores = scores
        self.score_calls = 0

    async def score_relevance(self, content, focus_area):
        self.score_calls += 1
        for title, score in self.scores.items():
            if title in content:
                return score
        return 0.0

    async def extract_structured_data(self, content, focus_area):
        return {
            "title": None, "summary": "x", "researchers": [],
            "affiliation": None, "contact_info": None,
            "trl_estimate": None, "source_type": "preprint",
        }


async def _store():
    s = DataStore(":memory:")
    await s.init_db()
    return s


# ----- _parse_date_string standalone tests -------------------------------


def test_parse_rfc822_date():
    dt = _parse_date_string("Mon, 25 May 2026 09:00:00 GMT")
    assert dt is not None and dt.year == 2026 and dt.month == 5 and dt.day == 25
    assert dt.tzinfo is not None


def test_parse_iso8601_with_z():
    dt = _parse_date_string("2026-06-01T09:00:00Z")
    assert dt is not None and dt.year == 2026 and dt.month == 6 and dt.day == 1
    assert dt.tzinfo is not None


def test_parse_date_only():
    dt = _parse_date_string("2026-06-01")
    assert dt is not None and dt.day == 1 and dt.tzinfo is not None


def test_parse_textual_dates():
    """Common textual date forms from scraped HTML must parse."""
    cases = [
        ("May 25, 2026",  (2026, 5, 25)),
        ("Jan 5, 2026",   (2026, 1, 5)),
        ("25 May 2026",   (2026, 5, 25)),
        ("2026/05/25",    (2026, 5, 25)),
        ("May 25 2026",   (2026, 5, 25)),
    ]
    for raw, (y, m, d) in cases:
        dt = _parse_date_string(raw)
        assert dt is not None, f"failed to parse {raw!r}"
        assert (dt.year, dt.month, dt.day) == (y, m, d), f"wrong date for {raw!r}"


def test_parse_unparseable_returns_none():
    assert _parse_date_string("sometime soon") is None
    assert _parse_date_string("") is None
    assert _parse_date_string(None) is None  # type: ignore[arg-type]


# ----- pipeline-level filter behavior -------------------------------------


def _build_agent(tmp_path, items, *, window=7, now=NOW):
    cfg = tmp_path / "energy_storage.yaml"
    cfg.write_text(CONFIG_YAML)

    async def setup():
        store = await _store()
        agent = DomainAgent(
            str(cfg), FakeScraper(items), FakeLLM(SCORES),
            Deduplicator(store), store,
            methods={"rss"}, threshold=6.0,
            week_window_days=window, _now=now,
        )
        return store, agent

    return setup


def test_old_items_are_dropped_before_scoring(tmp_path):
    items = [
        {"title": "recent paper", "link": "https://ex.com/1",
         "summary": "x", "published": "2026-06-01"},  # 4 days before NOW — keep
        {"title": "old paper", "link": "https://ex.com/2",
         "summary": "x", "published": "2026-04-01"},  # months old — drop
    ]
    setup = _build_agent(tmp_path, items)

    async def body():
        store, agent = await setup()
        stats = await agent.run()
        await store.close()
        return stats, agent.llm

    stats, llm = asyncio.run(body())
    assert stats["dropped_old"] == 1
    assert stats["fetched"] == 1                 # only the recent item survived
    assert stats["new_items"] == 1
    assert llm.score_calls == 1                  # the old item never reached the LLM


def test_undated_items_are_dropped(tmp_path):
    items = [
        {"title": "undated paper", "link": "https://ex.com/u",
         "summary": "x", "published": None},
        {"title": "recent paper", "link": "https://ex.com/r",
         "summary": "x", "published": "2026-06-03"},
    ]
    setup = _build_agent(tmp_path, items)

    async def body():
        store, agent = await setup()
        stats = await agent.run()
        await store.close()
        return stats, agent.llm

    stats, llm = asyncio.run(body())
    assert stats["dropped_old"] == 1
    assert stats["new_items"] == 1
    assert llm.score_calls == 1


def test_window_none_disables_filter(tmp_path):
    items = [
        {"title": "old paper", "link": "https://ex.com/o",
         "summary": "x", "published": "2025-01-01"},
        {"title": "undated paper", "link": "https://ex.com/u",
         "summary": "x", "published": None},
    ]
    setup = _build_agent(tmp_path, items, window=None)

    async def body():
        store, agent = await setup()
        stats = await agent.run()
        await store.close()
        return stats

    stats = asyncio.run(body())
    assert stats["dropped_old"] == 0
    assert stats["fetched"] == 2
    assert stats["new_items"] == 2
