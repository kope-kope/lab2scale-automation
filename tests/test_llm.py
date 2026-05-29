"""Unit tests for lib/llm.py with a mocked Anthropic client.

No live API calls: a fake client records the kwargs each call received and
returns canned response objects shaped like the Messages API response.
"""

import asyncio
from types import SimpleNamespace

from lib import llm
from lib.llm import LLMFilter


# ----- fake Anthropic client ----------------------------------------------


def _text_response(text: str, in_tok: int = 10, out_tok: int = 5):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def _tool_response(payload: dict, in_tok: int = 12, out_tok: int = 8):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="record_finding", input=payload)],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class _FakeMessages:
    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self._handler(kwargs)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, handler):
        self.messages = _FakeMessages(handler)


# ----- score_relevance ------------------------------------------------------


def test_score_relevance_parses_score_and_uses_scoring_model():
    client = FakeClient(lambda kw: _text_response('{"score": 8.5, "reason": "breakthrough"}'))
    f = LLMFilter(client=client, scoring_model="claude-haiku-4-5")

    score = asyncio.run(f.score_relevance("solid-state battery", "energy_storage"))
    assert score == 8.5

    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    # Prompt is the exact spec template, with both substitutions applied.
    prompt = call["messages"][0]["content"]
    assert "energy_storage" in prompt
    assert "solid-state battery" in prompt
    assert "Return ONLY a JSON object" in prompt


def test_score_relevance_tolerates_surrounding_prose():
    client = FakeClient(
        lambda kw: _text_response('Sure! Here you go:\n{"score": 6.0, "reason": "x"}\nThanks')
    )
    f = LLMFilter(client=client)
    assert asyncio.run(f.score_relevance("c", "semiconductors")) == 6.0


def test_score_relevance_returns_zero_on_unparseable_response():
    client = FakeClient(lambda kw: _text_response("no json here"))
    f = LLMFilter(client=client)
    assert asyncio.run(f.score_relevance("c", "semiconductors")) == 0.0


def test_score_relevance_returns_zero_on_api_error():
    client = FakeClient(lambda kw: RuntimeError("rate limited"))
    f = LLMFilter(client=client)
    assert asyncio.run(f.score_relevance("c", "power_generation")) == 0.0


def test_usage_is_tracked():
    client = FakeClient(lambda kw: _text_response('{"score": 5.0}', in_tok=100, out_tok=20))
    f = LLMFilter(client=client)
    asyncio.run(f.score_relevance("c", "energy_storage"))
    assert f.total_input_tokens == 100
    assert f.total_output_tokens == 20


# ----- extract_structured_data ---------------------------------------------


def test_extract_structured_data_from_tool_use():
    payload = {
        "title": "Solid-state battery hits 1000 cycles",
        "summary": "A durable cell.",
        "researchers": ["Dr. Jane Smith"],
        "affiliation": "MIT",
        "source_type": "preprint",
    }
    client = FakeClient(lambda kw: _tool_response(payload))
    f = LLMFilter(client=client)

    result = asyncio.run(f.extract_structured_data("raw text", "energy_storage"))
    assert result["title"] == "Solid-state battery hits 1000 cycles"
    assert result["researchers"] == ["Dr. Jane Smith"]
    assert result["source_type"] == "preprint"
    # Missing optional fields are present as None.
    assert result["contact_info"] is None
    assert result["trl_estimate"] is None

    # tool_use was forced.
    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_finding"}
    assert call["tools"][0]["name"] == "record_finding"


def test_extract_normalizes_missing_researchers_to_list():
    client = FakeClient(lambda kw: _tool_response({"title": "t", "summary": "s"}))
    f = LLMFilter(client=client)
    result = asyncio.run(f.extract_structured_data("c", "semiconductors"))
    assert result["researchers"] == []


def test_extract_coerces_string_researcher_to_list():
    client = FakeClient(
        lambda kw: _tool_response({"title": "t", "summary": "s", "researchers": "Solo Author"})
    )
    f = LLMFilter(client=client)
    result = asyncio.run(f.extract_structured_data("c", "semiconductors"))
    assert result["researchers"] == ["Solo Author"]


def test_extract_returns_shaped_dict_on_api_error():
    client = FakeClient(lambda kw: RuntimeError("boom"))
    f = LLMFilter(client=client)
    result = asyncio.run(f.extract_structured_data("c", "deep_tech_infra"))
    assert set(result.keys()) == set(llm._EXTRACTION_FIELDS)
    assert result["title"] is None
    assert result["researchers"] == []


# ----- generate_weekly_summary ---------------------------------------------


def test_generate_weekly_summary_uses_summary_model_and_returns_text():
    client = FakeClient(lambda kw: _text_response("This week, MIT's solid-state cell led the field."))
    f = LLMFilter(client=client, summary_model="claude-sonnet-4-6")

    findings = [
        {"focus_area": "energy_storage", "relevance_score": 8.5,
         "title": "Solid-state battery", "summary": "durable cell", "affiliation": "MIT"},
    ]
    events = [
        {"city": "boston", "event_date": "2026-06-15",
         "event_name": "MIT Energy Night", "venue": "Media Lab"},
    ]
    out = asyncio.run(f.generate_weekly_summary(findings, events))
    assert "solid-state cell" in out

    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["system"] == llm.SUMMARY_SYSTEM_PROMPT
    prompt = call["messages"][0]["content"]
    assert "RESEARCH FINDINGS (1)" in prompt
    assert "UPCOMING EVENTS (1)" in prompt
    assert "MIT Energy Night" in prompt


def test_generate_weekly_summary_returns_empty_on_error():
    client = FakeClient(lambda kw: RuntimeError("server error"))
    f = LLMFilter(client=client)
    assert asyncio.run(f.generate_weekly_summary([], [])) == ""


# ----- model configuration --------------------------------------------------


def test_models_default_to_haiku_and_sonnet(monkeypatch):
    monkeypatch.delenv("LLM_SCORING_MODEL", raising=False)
    monkeypatch.delenv("LLM_SUMMARY_MODEL", raising=False)
    f = LLMFilter(client=FakeClient(lambda kw: _text_response("{}")))
    assert f.scoring_model == "claude-haiku-4-5"
    assert f.summary_model == "claude-sonnet-4-6"


def test_models_overridable_via_env(monkeypatch):
    monkeypatch.setenv("LLM_SCORING_MODEL", "claude-custom-haiku")
    monkeypatch.setenv("LLM_SUMMARY_MODEL", "claude-custom-sonnet")
    f = LLMFilter(client=FakeClient(lambda kw: _text_response("{}")))
    assert f.scoring_model == "claude-custom-haiku"
    assert f.summary_model == "claude-custom-sonnet"
