"""Tests for the markdown prompt loader (lib/prompts.py) and the shipped
prompt files in prompts/."""

import pytest

from lib.prompts import PROMPTS_DIR, load_prompt, render_prompt

# Every prompt the code references must exist on disk.
EXPECTED_PROMPTS = [
    "research_scoring",
    "research_extraction",
    "event_scoring",
    "event_extraction",
    "summary_system",
    "summary_user",
]


@pytest.mark.parametrize("name", EXPECTED_PROMPTS)
def test_every_expected_prompt_file_exists_and_is_nonempty(name):
    assert (PROMPTS_DIR / f"{name}.md").exists(), f"missing prompts/{name}.md"
    assert load_prompt(name).strip(), f"prompts/{name}.md is empty"


def test_load_prompt_raises_for_unknown():
    with pytest.raises(FileNotFoundError, match="Prompt file not found"):
        load_prompt("does_not_exist_xyz")


def test_render_substitutes_placeholders():
    out = render_prompt("research_scoring", focus_area="energy_storage",
                        content="solid-state battery breakthrough")
    assert "energy_storage" in out
    assert "solid-state battery breakthrough" in out
    # The marker tokens are gone once filled.
    assert "{{FOCUS_AREA}}" not in out
    assert "{{CONTENT}}" not in out


def test_render_keeps_json_and_dollar_signs_literal():
    """JSON examples and $ amounts must survive untouched (no escaping needed)."""
    scoring = render_prompt("research_scoring", focus_area="x", content="y")
    assert '"score":' in scoring            # JSON score field intact
    assert '"reason":' in scoring           # reason-first format intact
    event_extract = render_prompt("event_extraction", content="z")
    assert "$50" in event_extract          # dollar amount intact


def test_render_is_single_pass():
    """A value containing a {{...}} marker must not get re-substituted."""
    out = render_prompt(
        "research_scoring",
        focus_area="energy",
        content="user text with {{FOCUS_AREA}} inside it",
    )
    # The literal marker from the content survives verbatim — it was not
    # replaced with 'energy' on a second pass.
    assert "{{FOCUS_AREA}} inside it" in out


def test_unknown_placeholders_are_left_intact():
    # research_extraction has no {{FOCUS_AREA}}; passing it is a harmless no-op,
    # and a marker with no provided value stays literal.
    out = render_prompt("research_extraction", content="c", focus_area="ignored")
    assert "c" in out


def test_scoring_prompts_still_request_a_json_score():
    """Guardrail: the code parses a numeric `score` back out, so both scoring
    prompts must keep asking for it."""
    for name in ("research_scoring", "event_scoring"):
        text = load_prompt(name).lower()
        assert "score" in text
        assert "json" in text


def test_summary_user_has_all_data_markers():
    text = load_prompt("summary_user")
    for marker in ("{{FINDINGS_COUNT}}", "{{FINDINGS_LIST}}",
                  "{{EVENTS_COUNT}}", "{{EVENTS_LIST}}"):
        assert marker in text
