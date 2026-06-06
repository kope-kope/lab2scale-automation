"""Tests for the report summarizer's data shaping."""

import asyncio

from systems.system3_delivery.summarizer import ReportSummarizer, parse_bullets


class FakeLLM:
    def __init__(self, text: str = "This week, MIT's solid-state cell led the field."):
        self.text = text
        self.calls = 0

    async def generate_weekly_summary(self, findings, events):
        self.calls += 1
        return self.text


def test_groups_findings_by_focus_and_sorts_by_score():
    findings = [
        {"focus_area": "energy_storage", "title": "Mid", "relevance_score": 7.0,
         "researchers": []},
        {"focus_area": "semiconductors", "title": "Sem", "relevance_score": 8.0,
         "researchers": []},
        {"focus_area": "energy_storage", "title": "Top", "relevance_score": 9.5,
         "researchers": []},
        {"focus_area": "energy_storage", "title": "Low", "relevance_score": 6.5,
         "researchers": []},
    ]
    s = ReportSummarizer(FakeLLM())
    data = asyncio.run(s.build_report_data(findings, []))
    assert list(data["findings_by_focus"]["energy_storage"][0:3]) == [
        findings[2], findings[0], findings[3]   # 9.5, 7.0, 6.5
    ]
    # power_generation comes before energy_storage in the canonical order,
    # so iterating finds energy_storage second when only those two exist.
    keys = list(data["findings_by_focus"].keys())
    assert keys.index("energy_storage") < keys.index("semiconductors")


def test_caps_findings_at_five_per_focus_area():
    """A big group should be trimmed to the top 5 — the brief stays short
    even after a productive sweep."""
    # 8 findings in energy_storage; only the top 5 by score should show.
    findings = [
        {"focus_area": "energy_storage", "title": f"Paper {i}",
         "relevance_score": float(i), "researchers": []}
        for i in range(1, 9)   # scores 1..8
    ]
    s = ReportSummarizer(FakeLLM())
    data = asyncio.run(s.build_report_data(findings, []))
    es = data["findings_by_focus"]["energy_storage"]
    assert len(es) == 5
    # Highest 5 scores kept, in descending order
    assert [int(f["relevance_score"]) for f in es] == [8, 7, 6, 5, 4]


def test_caps_events_at_five_per_city():
    events = [
        {"city": "boston", "event_name": f"E{i}", "event_date": f"2026-06-{i:02d}"}
        for i in range(1, 9)
    ]
    s = ReportSummarizer(FakeLLM())
    data = asyncio.run(s.build_report_data([], events))
    boston = data["events_by_city"]["boston"]
    assert len(boston) == 5
    # Earliest 5 dates (ASC) kept.
    assert [e["event_name"] for e in boston] == ["E1", "E2", "E3", "E4", "E5"]


def test_summary_only_sees_visible_top_5():
    """The Sonnet summary call should receive only the items that actually
    make it into the email — not every fetched finding."""
    findings = [
        {"focus_area": "energy_storage", "title": f"P{i}",
         "relevance_score": float(i), "researchers": []}
        for i in range(1, 9)
    ]

    received: list[list[dict]] = []

    class CapturingLLM:
        calls = 0
        async def generate_weekly_summary(self, fs, es):
            CapturingLLM.calls += 1
            received.append(fs)
            return "ok"

    s = ReportSummarizer(CapturingLLM())
    asyncio.run(s.build_report_data(findings, []))
    assert CapturingLLM.calls == 1
    # Sonnet got the top 5 only.
    assert len(received[0]) == 5
    assert [int(f["relevance_score"]) for f in received[0]] == [8, 7, 6, 5, 4]


def test_groups_events_by_city_and_sorts_by_date_asc():
    events = [
        {"city": "boston", "event_name": "B-Aug", "event_date": "2026-08-01"},
        {"city": "nyc",    "event_name": "N-Jun", "event_date": "2026-06-01"},
        {"city": "boston", "event_name": "B-Jun", "event_date": "2026-06-15"},
    ]
    s = ReportSummarizer(FakeLLM())
    data = asyncio.run(s.build_report_data([], events))
    boston = data["events_by_city"]["boston"]
    assert [e["event_name"] for e in boston] == ["B-Jun", "B-Aug"]
    assert data["events_by_city"]["nyc"][0]["event_name"] == "N-Jun"


def test_notable_contacts_dedup_and_cap():
    findings = [
        {"focus_area": "energy_storage", "title": "A", "relevance_score": 9.0,
         "researchers": ["Dr. Jane Smith", "Prof. John Doe"],
         "affiliation": "MIT", "contact_info": "jsmith@mit.edu"},
        {"focus_area": "energy_storage", "title": "B", "relevance_score": 8.0,
         "researchers": ["Dr. Jane Smith"],            # duplicate
         "affiliation": "MIT", "contact_info": None},
        {"focus_area": "semiconductors", "title": "C", "relevance_score": 7.0,
         "researchers": ["Dr. Alice Lee"],
         "affiliation": "Stanford", "contact_info": None},
    ]
    s = ReportSummarizer(FakeLLM())
    data = asyncio.run(s.build_report_data(findings, []))
    names = [c["name"] for c in data["contacts"]]
    assert names == ["Dr. Jane Smith", "Prof. John Doe", "Dr. Alice Lee"]
    # First mention wins, so Jane's "context" points at finding A.
    assert data["contacts"][0]["context"] == "A"
    assert data["contacts"][0]["contact"] == "jsmith@mit.edu"


def test_parse_bullets_handles_numbered_dotted():
    assert parse_bullets("1. Foo did A.\n2. Bar did B.") == ["Foo did A.", "Bar did B."]


def test_parse_bullets_handles_numbered_paren_and_dashes():
    assert parse_bullets("1) Foo\n2) Bar") == ["Foo", "Bar"]
    assert parse_bullets("- Foo\n- Bar\n* Baz") == ["Foo", "Bar", "Baz"]


def test_parse_bullets_returns_empty_for_plain_prose():
    """When Sonnet returns a paragraph instead of bullets, the parser yields
    nothing so the template falls back to plain prose."""
    assert parse_bullets("This is just a paragraph with no markers.") == []


def test_parse_bullets_skips_blank_and_garbage_lines():
    text = "\n1. First.\n\nLeftover header noise\n2. Second.\n"
    assert parse_bullets(text) == ["First.", "Second."]


def test_build_report_data_extracts_bullets():
    class BulletLLM:
        async def generate_weekly_summary(self, fs, es):
            return "1. MIT shipped a 1000-cycle cell.\n2. Greentown announced a summit."

    s = ReportSummarizer(BulletLLM())
    findings = [{"focus_area": "energy_storage", "title": "x", "relevance_score": 9.0,
                 "researchers": []}]
    data = asyncio.run(s.build_report_data(findings, []))
    assert data["executive_bullets"] == [
        "MIT shipped a 1000-cycle cell.",
        "Greentown announced a summit.",
    ]


def test_empty_inputs_yield_heartbeat_summary_without_llm_call():
    s = ReportSummarizer(FakeLLM("should not be called"))
    data = asyncio.run(s.build_report_data([], []))
    # Heartbeat fallback (no Sonnet call) so the cron stays visible even on
    # quiet weeks.
    assert "No new findings or events" in data["executive_summary"]
    assert s.llm.calls == 0
    assert data["findings_by_focus"] == {}
    assert data["events_by_city"] == {}
    assert data["contacts"] == []
