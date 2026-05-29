"""Offline integration test for the DomainAgent pipeline.

Uses a fake scraper (canned RSS items) and a fake LLM (canned scores +
extraction) against a real in-memory DataStore and a real Deduplicator, so the
full fetch → score → dedup → extract → store path is exercised with no network
and no API key.
"""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from systems.system1_research.domain_agent import DomainAgent

# A config that uses several category names (to exercise the generic walker)
# and a web_scrape source (which Day 3 must skip).
CONFIG_YAML = """
arxiv:
  - name: "arXiv feed"
    url: "https://example.com/arxiv.rss"
    method: rss
journals:
  - name: "Journal feed"
    url: "https://example.com/journal.rss"
    method: rss
academic_labs:
  - name: "Some lab page"
    url: "https://example.com/lab"
    method: web_scrape
"""

FEEDS = {
    "https://example.com/arxiv.rss": [
        {"title": "Solid-state battery breakthrough", "link": "https://ex.com/a1",
         "summary": "A durable cell", "published": "2026-05-25"},
        {"title": "Mildly interesting capacitor", "link": "https://ex.com/a2",
         "summary": "incremental", "published": "2026-05-25"},
    ],
    "https://example.com/journal.rss": [
        {"title": "Flow battery cost cut 40%", "link": "https://ex.com/j1",
         "summary": "cheaper grid storage", "published": "2026-05-26"},
    ],
}

SCORES = {
    "Solid-state battery breakthrough": 9.0,
    "Flow battery cost cut 40%": 7.0,
    "Mildly interesting capacitor": 3.0,
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


class FakeLLM:
    def __init__(self, scores):
        self.scores = scores
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
        return {
            "title": None,  # forces fall-back to the item's RSS title
            "summary": "extracted summary",
            "researchers": ["Dr. Test"],
            "affiliation": "Test University",
            "contact_info": None,
            "trl_estimate": "TRL 3-4",
            "source_type": "preprint",
        }


def _write_config(tmp_path) -> str:
    cfg = tmp_path / "energy_storage.yaml"
    cfg.write_text(CONFIG_YAML)
    return str(cfg)


async def _fresh_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def test_domain_agent_end_to_end(tmp_path):
    async def body():
        store = await _fresh_store()
        scraper = FakeScraper(FEEDS)
        agent = DomainAgent(
            _write_config(tmp_path), scraper, FakeLLM(SCORES),
            Deduplicator(store), store, methods={"rss"}, threshold=6.0,
        )
        stats = await agent.run()
        findings = await store.get_unreported_findings()
        await store.close()
        return stats, findings, scraper

    stats, findings, scraper = asyncio.run(body())

    # Two items cleared the 6.0 threshold; the capacitor (3.0) was filtered.
    assert stats["new_items"] == 2
    assert stats["fetched"] == 3
    assert stats["filtered"] == 1
    assert stats["errors"] == 0

    # Only RSS sources were fetched — the web_scrape lab page was skipped.
    assert "https://example.com/lab" not in scraper.fetched
    assert len(scraper.fetched) == 2

    # Findings landed with scores, summaries, and structured data, ordered by score.
    assert [f["title"] for f in findings] == [
        "Solid-state battery breakthrough",
        "Flow battery cost cut 40%",
    ]
    top = findings[0]
    assert top["focus_area"] == "energy_storage"
    assert top["agent"] == "energy_storage_agent"
    assert top["relevance_score"] == 9.0
    assert top["summary"] == "extracted summary"
    assert top["researchers"] == ["Dr. Test"]          # JSON round-trips to a list
    assert top["affiliation"] == "Test University"
    assert top["source_type"] == "preprint"
    assert top["trl_estimate"] == "TRL 3-4"
    assert top["source_url"] == "https://ex.com/a1"
    assert top["reported"] is False


def test_domain_agent_dedups_on_rerun(tmp_path):
    async def body():
        store = await _fresh_store()
        dedup = Deduplicator(store)
        config = _write_config(tmp_path)

        agent1 = DomainAgent(config, FakeScraper(FEEDS), FakeLLM(SCORES),
                             dedup, store, methods={"rss"}, threshold=6.0)
        first = await agent1.run()

        # Second sweep: same items are all already seen.
        scraper2 = FakeScraper(FEEDS)
        llm2 = FakeLLM(SCORES)
        agent2 = DomainAgent(config, scraper2, llm2, dedup, store,
                             methods={"rss"}, threshold=6.0)
        second = await agent2.run()

        findings = await store.get_unreported_findings()
        await store.close()
        return first, second, llm2, findings

    first, second, llm2, findings = asyncio.run(body())

    assert first["new_items"] == 2
    assert second["new_items"] == 0
    assert second["skipped"] == 3          # all three items recognized as seen
    assert llm2.score_calls == 0           # dedup short-circuits before scoring
    assert len(findings) == 2              # no duplicate rows


def test_failing_source_does_not_crash_sweep(tmp_path):
    async def body():
        cfg = tmp_path / "energy_storage.yaml"
        cfg.write_text(
            "arxiv:\n"
            '  - name: "good"\n'
            '    url: "https://example.com/good.rss"\n'
            "    method: rss\n"
            '  - name: "boom"\n'
            '    url: "https://example.com/boom.rss"\n'
            "    method: rss\n"
        )
        feeds = {
            "https://example.com/good.rss": [
                {"title": "Solid-state battery breakthrough", "link": "https://ex.com/g1",
                 "summary": "ok", "published": "2026-05-25"},
            ],
        }
        store = await _fresh_store()
        scraper = FakeScraper(feeds, raise_on={"https://example.com/boom.rss"})
        agent = DomainAgent(str(cfg), scraper, FakeLLM(SCORES),
                            Deduplicator(store), store, methods={"rss"}, threshold=6.0)
        stats = await agent.run()
        findings = await store.get_unreported_findings()
        await store.close()
        return stats, findings

    stats, findings = asyncio.run(body())
    assert stats["errors"] == 1            # the boom source was counted, not fatal
    assert stats["new_items"] == 1         # the good source still produced a finding
    assert len(findings) == 1
