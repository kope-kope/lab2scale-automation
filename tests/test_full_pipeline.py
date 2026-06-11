"""End-to-end integration test for the full pipeline.

Wires Systems 1, 2, and 3 together against an in-memory DataStore with
mocked scraper, mocked LLM, and a mocked Resend client. Proves the four
systems compose correctly:

  sweep → System 1 (research) writes findings
  sweep → System 2 (events)  writes events
  report → System 3 builds the brief, sends, marks reported
  sweep again → dedup blocks re-scoring
"""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator
from lib.email_sender import EmailSender
from systems.system1_research.orchestrator import ResearchOrchestrator
from systems.system2_events.orchestrator import EventsOrchestrator
from systems.system3_delivery.orchestrator import DeliveryOrchestrator
from systems.system3_delivery.summarizer import ReportSummarizer

# --- temp configs ---------------------------------------------------------

DOMAIN_YAML = """
arxiv:
  - name: "arXiv stub"
    url: "https://example.com/arxiv.rss"
    method: rss
"""

# --- canned feed content --------------------------------------------------

# Dates close to "now" so the week-window + horizon filters pass.
FEEDS = {
    "https://example.com/arxiv.rss": [
        {"title": "Solid-state battery hits 1000 cycles", "link": "https://ex.com/f1",
         "summary": "Researchers demonstrate a durable cell.",
         "published": "2026-06-03"},
    ],
}

# Events now come from Tavily web search, not feeds. Keyed by a location marker
# that appears in the agent's query (which uses CITY_CONTEXT phrasing).
EVENT_RESULTS = {
    "Boston": [
        {"url": "https://ex.com/e1", "title": "MIT Energy Night",
         "content": "MIT Energy Night — next-gen power electronics panel at MIT", "score": 0.9},
    ],
}

# --- fakes ----------------------------------------------------------------


class FakeScraper:
    def __init__(self, feeds):
        self.feeds = feeds
        self.fetches: list[str] = []

    async def fetch_rss(self, url):
        self.fetches.append(url)
        return [dict(it) for it in self.feeds.get(url, [])]

    async def close(self):
        pass


class FakeTavily:
    """Returns results keyed by a location marker present in the query."""

    def __init__(self, results_by_marker):
        self.results_by_marker = results_by_marker

    async def search(self, query, max_results=None):
        ql = query.lower()
        for marker, results in self.results_by_marker.items():
            if marker.lower() in ql:
                return [dict(r) for r in results]
        return []

    async def close(self):
        pass


class FakeLLM:
    def __init__(self):
        self.score_calls = 0
        self.extract_calls = 0
        self.event_score_calls = 0
        self.event_extract_calls = 0
        self.summary_calls = 0

    # Research path
    async def score_relevance(self, content, focus_area):
        self.score_calls += 1
        return 9.0

    async def extract_structured_data(self, content, focus_area):
        self.extract_calls += 1
        return {
            "title": None,                 # fall back to feed title
            "summary": "MIT solid-state work",
            "researchers": ["Dr. Jane Smith"],
            "affiliation": "MIT",
            "contact_info": "jsmith@mit.edu",
            "trl_estimate": "TRL 3-4",
            "source_type": "preprint",
        }

    # Events path
    async def score_event_relevance(self, content):
        self.event_score_calls += 1
        return 8.5

    async def extract_event_data(self, content):
        self.event_extract_calls += 1
        return {
            "event_name": "MIT Energy Night",
            "event_date": "2026-06-05",    # within the 30d horizon
            "event_time": "18:00-20:00",
            "venue": "MIT Media Lab",
            "description": "Power electronics panel.",
            "cost": "Free",
            "event_type": "panel",
            "relevance_tags": ["power_electronics"],
        }

    # Delivery path
    async def generate_weekly_summary(self, findings, events):
        self.summary_calls += 1
        return "MIT's solid-state cell and the Energy Night panel led the week."


class _FakeEmails:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, params):
        self.sent.append(params)
        return {"id": "test"}


class FakeResendClient:
    def __init__(self):
        self.Emails = _FakeEmails()


# --- the test ------------------------------------------------------------


def test_full_pipeline_sweep_report_then_dedup(tmp_path):
    """sweep → both systems persist → report sends + marks reported → sweep
    again is a no-op via dedup."""

    # Temp config tree mirroring config/domains/*.yaml (events use Tavily, no config).
    domains_dir = tmp_path / "domains"
    domains_dir.mkdir()
    (domains_dir / "energy_storage.yaml").write_text(DOMAIN_YAML)

    async def body():
        store = DataStore(":memory:")
        await store.init_db()
        scraper = FakeScraper(FEEDS)
        llm = FakeLLM()
        dedup = Deduplicator(store)
        email_client = FakeResendClient()

        # ----- SWEEP -----
        research = ResearchOrchestrator(
            scraper=scraper, llm=llm, dedup=dedup, store=store,
            domains=["energy_storage"], config_dir=domains_dir,
            methods={"rss"}, threshold=6.0, week_window_days=None,
        )
        events = EventsOrchestrator(
            llm=llm, dedup=dedup, store=store,
            cities=["boston"], threshold=6.0, future_horizon_days=None,
            tavily_searcher=FakeTavily(EVENT_RESULTS),
        )
        await asyncio.gather(research.run(), events.run())

        findings_after_sweep = await store.get_unreported_findings()
        events_after_sweep = await store.get_unreported_events()

        # ----- REPORT (dry-run path proves end-to-end without Resend dep) -----
        delivery = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=email_client),
            recipient="team@example.com",
        )
        delivery_result = await delivery.run()

        findings_after_report = await store.get_unreported_findings()
        events_after_report = await store.get_unreported_events()
        sent_html = email_client.Emails.sent[0]["html"] if email_client.Emails.sent else ""

        # ----- SWEEP again — dedup should short-circuit -----
        scraper2 = FakeScraper(FEEDS)
        llm2 = FakeLLM()
        research2 = ResearchOrchestrator(
            scraper=scraper2, llm=llm2, dedup=dedup, store=store,
            domains=["energy_storage"], config_dir=domains_dir,
            methods={"rss"}, threshold=6.0, week_window_days=None,
        )
        events2 = EventsOrchestrator(
            llm=llm2, dedup=dedup, store=store,
            cities=["boston"], threshold=6.0, future_horizon_days=None,
            tavily_searcher=FakeTavily(EVENT_RESULTS),
        )
        await asyncio.gather(research2.run(), events2.run())

        await store.close()
        return {
            "findings_after_sweep": findings_after_sweep,
            "events_after_sweep": events_after_sweep,
            "delivery_result": delivery_result,
            "findings_after_report": findings_after_report,
            "events_after_report": events_after_report,
            "sent_html": sent_html,
            "second_llm": llm2,
        }

    r = asyncio.run(body())

    # Sweep landed both kinds.
    assert len(r["findings_after_sweep"]) == 1
    assert len(r["events_after_sweep"]) == 1
    assert r["findings_after_sweep"][0]["focus_area"] == "energy_storage"
    assert r["events_after_sweep"][0]["city"] == "boston"

    # Report sent and the rendered email actually mentions our items.
    assert r["delivery_result"]["sent"] is True
    assert "Solid-state battery hits 1000 cycles" in r["sent_html"]
    assert "MIT Energy Night" in r["sent_html"]
    assert "Dr. Jane Smith" in r["sent_html"]   # surfaced as notable contact

    # Marked reported.
    assert r["findings_after_report"] == []
    assert r["events_after_report"] == []

    # Second sweep is a true no-op: zero LLM calls because dedup
    # short-circuits before scoring.
    assert r["second_llm"].score_calls == 0
    assert r["second_llm"].event_score_calls == 0
