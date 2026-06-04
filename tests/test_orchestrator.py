"""Offline test for the System 1 ResearchOrchestrator.

Runs the orchestrator over a couple of temp-config domains with a fake scraper
and fake LLM against a real in-memory DataStore, verifying parallel aggregation
and that one broken domain doesn't abort the sweep.
"""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from systems.system1_research.orchestrator import ResearchOrchestrator


class FakeScraper:
    def __init__(self, feeds):
        self.feeds = feeds

    async def fetch_rss(self, url):
        return [dict(item) for item in self.feeds.get(url, [])]

    async def close(self):
        pass


class FakeLLM:
    def __init__(self, scores):
        self.scores = scores

    async def score_relevance(self, content, focus_area):
        for title, score in self.scores.items():
            if title in content:
                return score
        return 0.0

    async def extract_structured_data(self, content, focus_area):
        return {
            "title": None, "summary": "summary", "researchers": [],
            "affiliation": "Lab", "contact_info": None,
            "trl_estimate": "TRL 3", "source_type": "preprint",
        }


def _write_domain(config_dir, name, feed_url):
    (config_dir / f"{name}.yaml").write_text(
        "arxiv:\n"
        f'  - name: "{name} feed"\n'
        f'    url: "{feed_url}"\n'
        "    method: rss\n"
    )


async def _fresh_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def test_orchestrator_runs_all_domains_and_aggregates(tmp_path):
    async def body():
        _write_domain(tmp_path, "energy_storage", "https://ex.com/es.rss")
        _write_domain(tmp_path, "semiconductors", "https://ex.com/semi.rss")
        feeds = {
            "https://ex.com/es.rss": [
                {"title": "Solid-state battery", "link": "https://ex.com/es1",
                 "summary": "great", "published": "2026-05-25"},
            ],
            "https://ex.com/semi.rss": [
                {"title": "Chiplet packaging advance", "link": "https://ex.com/se1",
                 "summary": "great", "published": "2026-05-25"},
            ],
        }
        scores = {"Solid-state battery": 9.0, "Chiplet packaging advance": 8.0}
        store = await _fresh_store()
        orch = ResearchOrchestrator(
            scraper=FakeScraper(feeds), llm=FakeLLM(scores),
            dedup=Deduplicator(store), store=store,
            domains=["energy_storage", "semiconductors"],
            config_dir=tmp_path, methods={"rss"}, threshold=6.0, week_window_days=None,
        )
        result = await orch.run()
        findings = await store.get_unreported_findings()
        await store.close()
        return result, findings

    result, findings = asyncio.run(body())

    assert result["system"] == "research"
    assert result["totals"]["new_items"] == 2
    assert result["totals"]["sources"] == 2
    assert result["totals"]["errors"] == 0
    assert set(result["domains"]) == {"energy_storage", "semiconductors"}
    assert result["domains"]["energy_storage"]["new_items"] == 1
    assert result["domains"]["semiconductors"]["new_items"] == 1

    assert len(findings) == 2
    assert {f["focus_area"] for f in findings} == {"energy_storage", "semiconductors"}


def test_orchestrator_isolates_a_failing_domain(tmp_path):
    async def body():
        # Only energy_storage has a config file; "missing_domain" does not.
        _write_domain(tmp_path, "energy_storage", "https://ex.com/es.rss")
        feeds = {
            "https://ex.com/es.rss": [
                {"title": "Solid-state battery", "link": "https://ex.com/es1",
                 "summary": "great", "published": "2026-05-25"},
            ],
        }
        store = await _fresh_store()
        orch = ResearchOrchestrator(
            scraper=FakeScraper(feeds), llm=FakeLLM({"Solid-state battery": 9.0}),
            dedup=Deduplicator(store), store=store,
            domains=["energy_storage", "missing_domain"],
            config_dir=tmp_path, methods={"rss"}, threshold=6.0, week_window_days=None,
        )
        result = await orch.run()
        findings = await store.get_unreported_findings()
        await store.close()
        return result, findings

    result, findings = asyncio.run(body())

    # The missing-config domain is recorded as an error, but the sweep completes
    # and the healthy domain still produced its finding.
    assert "error" in result["domains"]["missing_domain"]
    assert result["totals"]["errors"] == 1
    assert result["domains"]["energy_storage"]["new_items"] == 1
    assert len(findings) == 1
    assert findings[0]["focus_area"] == "energy_storage"
