"""Tests for the report summarizer's data shaping."""

import asyncio

from systems.system3_delivery.summarizer import ReportSummarizer


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


def test_empty_inputs_yield_no_summary():
    s = ReportSummarizer(FakeLLM("should not be called"))
    data = asyncio.run(s.build_report_data([], []))
    assert data["executive_summary"] == ""
    assert s.llm.calls == 0
    assert data["findings_by_focus"] == {}
    assert data["events_by_city"] == {}
    assert data["contacts"] == []
