"""Tests for GoogleSheetsWriter (Apps Script web-app POST) via httpx MockTransport."""

import json

import httpx

from lib.sheets_writer import GoogleSheetsWriter

FINDINGS = [
    {"title": "Ferveret — datacenter cooling", "focus_area": "water_cooling",
     "trl_estimate": "TRL 4", "summary": "cooling startup", "researchers": ["Dr. Jane"],
     "contact_info": "jane@ferveret.com", "source_url": "https://ex.com/ferveret",
     "relevance_score": 8.2},
    {"title": "Apollo Atomics — SMR", "focus_area": "nuclear_advanced_energy",
     "trl_estimate": "TRL 3", "summary": "SMR startup", "researchers": [],
     "contact_info": None, "source_url": "https://ex.com/apollo", "relevance_score": 9.0},
]

URL = "https://script.google.com/macros/s/abc/exec"


def _writer(handler, **kw):
    return GoogleSheetsWriter(
        webapp_url=kw.pop("url", URL), secret=kw.pop("secret", "s3cret"),
        transport=httpx.MockTransport(handler), **kw,
    )


def test_configured_requires_webapp_url():
    assert GoogleSheetsWriter(webapp_url=URL).configured is True
    assert GoogleSheetsWriter(webapp_url=None).configured is False


def test_posts_leads_and_returns_added_count():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"added": 2})

    added = _writer(handler).append_leads(FINDINGS)
    assert added == 2

    body = captured["body"]
    assert body["secret"] == "s3cret"
    assert len(body["leads"]) == 2
    lead = body["leads"][0]
    assert lead["company"] == "Ferveret"                 # name split from title
    assert lead["sector"] == "Water & Cooling"           # plain sector label
    assert "Dr. Jane" in lead["contacts"] and "jane@ferveret.com" in lead["contacts"]
    assert lead["url"] == "https://ex.com/ferveret"


def test_fails_soft_on_http_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    assert _writer(handler).append_leads(FINDINGS) == 0


def test_unauthorized_response_yields_zero():
    def handler(request):
        return httpx.Response(200, json={"error": "unauthorized"})

    assert _writer(handler).append_leads(FINDINGS) == 0


def test_not_configured_or_empty_returns_zero():
    def handler(request):
        return httpx.Response(200, json={"added": 5})

    assert GoogleSheetsWriter(webapp_url=None).append_leads(FINDINGS) == 0
    assert _writer(handler).append_leads([]) == 0
