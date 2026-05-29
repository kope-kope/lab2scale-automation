"""Claude API wrapper for relevance scoring, structured extraction, and the
weekly summary.

Cost split (per the spec): Claude Haiku for high-volume scoring and extraction,
Claude Sonnet for the weekly executive summary. Model IDs are read from the
environment so they can be changed without code edits:

    LLM_SCORING_MODEL   (default: claude-haiku-4-5)
    LLM_SUMMARY_MODEL   (default: claude-sonnet-4-6)
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("lib.llm")

DEFAULT_SCORING_MODEL = "claude-haiku-4-5"
DEFAULT_SUMMARY_MODEL = "claude-sonnet-4-6"

# Items scoring at or above this are kept (per spec).
RELEVANCE_THRESHOLD = 8.0

# Cap content sent to the model to keep token cost bounded.
_MAX_CONTENT_CHARS = 6000

# --- Prompt templates (verbatim from IMPLEMENTATION_SPEC Section 4.2) --------

SCORING_PROMPT = """You are a research analyst for Lab2Scale, a deep tech commercialization firm.
Score the following content for relevance to {focus_area} on a scale of 0-10.

Scoring criteria:
- 9-10: Breakthrough discovery, new prototype, major funding for commercialization-ready tech
- 7-8: Significant research advance, new startup, notable partnership
- 5-6: Incremental progress, interesting but not actionable
- 3-4: Tangentially related, low novelty
- 0-2: Not relevant to {focus_area}

Content: {content}

Return ONLY a JSON object: {{"score": <float>, "reason": "<one sentence>"}}"""

EXTRACTION_PROMPT = """Extract structured data from this research finding. Return a JSON object with these fields:
- title: concise title (max 100 chars)
- summary: 2-3 sentence summary of the finding and why it matters
- researchers: array of researcher/founder names mentioned
- affiliation: university, lab, or company name
- contact_info: any email addresses or contact links found
- trl_estimate: estimated Technology Readiness Level (e.g. "TRL 2-3")
- source_type: one of [preprint, journal, news, patent, lab_page, startup]

If a field is not found in the content, use null.

Content: {content}"""

# tool_use schema for structured extraction (forces well-formed output).
EXTRACTION_TOOL = {
    "name": "record_finding",
    "description": "Record the structured fields extracted from a research finding.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Concise title (max 100 chars)"},
            "summary": {"type": "string", "description": "2-3 sentence summary"},
            "researchers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Researcher/founder names mentioned",
            },
            "affiliation": {"type": "string", "description": "University, lab, or company"},
            "contact_info": {"type": "string", "description": "Emails or contact links"},
            "trl_estimate": {"type": "string", "description": 'e.g. "TRL 2-3"'},
            "source_type": {
                "type": "string",
                "enum": ["preprint", "journal", "news", "patent", "lab_page", "startup"],
            },
        },
        "required": ["title", "summary"],
    },
}

_EXTRACTION_FIELDS = (
    "title", "summary", "researchers", "affiliation",
    "contact_info", "trl_estimate", "source_type",
)

SUMMARY_SYSTEM_PROMPT = (
    "You are the lead intelligence analyst for Lab2Scale, a deep tech "
    "commercialization firm. You write concise, high-signal weekly briefs for "
    "the team."
)


def _extract_json(text: str) -> dict:
    """Parse a JSON object from model text, tolerating surrounding prose."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _first_text(response) -> str:
    """Return the text of the first text block in a Messages API response."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


class LLMFilter:
    """Uses the Claude API for relevance scoring, extraction, and summaries."""

    def __init__(
        self,
        client=None,
        *,
        scoring_model: str | None = None,
        summary_model: str | None = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._max_retries = max_retries
        self.scoring_model = scoring_model or os.getenv(
            "LLM_SCORING_MODEL", DEFAULT_SCORING_MODEL
        )
        self.summary_model = summary_model or os.getenv(
            "LLM_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL
        )
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    @property
    def client(self):
        """Lazily construct the Anthropic client so importing/constructing this
        class never requires an API key (tests inject a fake client)."""
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(max_retries=self._max_retries)
        return self._client

    def _track_usage(self, response, model: str) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        self.total_input_tokens += in_tok
        self.total_output_tokens += out_tok
        log.info(
            "LLM %s usage: input=%d output=%d (cumulative in=%d out=%d)",
            model, in_tok, out_tok, self.total_input_tokens, self.total_output_tokens,
        )

    async def score_relevance(self, content: str, focus_area: str) -> float:
        """Score content 0-10 for relevance to a focus area (Claude Haiku).

        On API failure, logs and returns 0.0 (the item is then filtered out).
        """
        prompt = SCORING_PROMPT.format(
            focus_area=focus_area, content=(content or "")[:_MAX_CONTENT_CHARS]
        )
        try:
            response = await self.client.messages.create(
                model=self.scoring_model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("score_relevance failed (%s): %s — defaulting to 0.0", focus_area, exc)
            return 0.0
        self._track_usage(response, self.scoring_model)
        data = _extract_json(_first_text(response))
        try:
            return float(data.get("score", 0.0))
        except (TypeError, ValueError):
            return 0.0

    async def extract_structured_data(self, content: str, focus_area: str) -> dict:
        """Extract structured finding data from raw content (Claude Haiku, tool_use).

        Returns a dict with all extraction fields present (missing → None / []).
        On API failure, returns an empty-but-shaped dict.
        """
        prompt = EXTRACTION_PROMPT.format(content=(content or "")[:_MAX_CONTENT_CHARS])
        try:
            response = await self.client.messages.create(
                model=self.scoring_model,
                max_tokens=1024,
                tools=[EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": EXTRACTION_TOOL["name"]},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("extract_structured_data failed: %s — returning empty", exc)
            return self._normalize_extraction({})
        self._track_usage(response, self.scoring_model)

        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                return self._normalize_extraction(dict(getattr(block, "input", {}) or {}))
        # Fallback: model returned text JSON instead of a tool call.
        return self._normalize_extraction(_extract_json(_first_text(response)))

    @staticmethod
    def _normalize_extraction(data: dict) -> dict:
        result = {field: data.get(field) for field in _EXTRACTION_FIELDS}
        if result["researchers"] is None:
            result["researchers"] = []
        elif isinstance(result["researchers"], str):
            result["researchers"] = [result["researchers"]]
        return result

    async def generate_weekly_summary(self, findings: list[dict], events: list[dict]) -> str:
        """Generate an executive summary for the weekly report (Claude Sonnet).

        Returns markdown/prose. On API failure, returns an empty string.
        """
        prompt = self._build_summary_prompt(findings, events)
        try:
            response = await self.client.messages.create(
                model=self.summary_model,
                max_tokens=4096,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.error("generate_weekly_summary failed: %s", exc)
            return ""
        self._track_usage(response, self.summary_model)
        return _first_text(response)

    @staticmethod
    def _build_summary_prompt(findings: list[dict], events: list[dict]) -> str:
        lines = [
            "Here is this week's accumulated intelligence for Lab2Scale.",
            "",
            f"RESEARCH FINDINGS ({len(findings)}):",
        ]
        for f in findings:
            score = f.get("relevance_score")
            score_str = f"{score:.1f}" if isinstance(score, (int, float)) else "n/a"
            affil = f.get("affiliation") or "unknown affiliation"
            summary = (f.get("summary") or "").strip()
            lines.append(
                f"- [{f.get('focus_area', '?')} | score {score_str}] "
                f"{f.get('title', 'Untitled')} — {summary} ({affil})"
            )
        lines += ["", f"UPCOMING EVENTS ({len(events)}):"]
        for e in events:
            lines.append(
                f"- [{e.get('city', '?')} | {e.get('event_date', 'TBD')}] "
                f"{e.get('event_name', 'Untitled')} — {e.get('venue') or 'venue TBD'}"
            )
        lines += [
            "",
            "Write an executive summary of 3-5 sentences covering the week's most "
            "significant developments. Highlight the top findings, rank by novelty "
            "and technology readiness, and flag any actionable commercialization "
            "opportunities or notable contacts worth reaching out to. Return prose "
            "only — no preamble, no headers.",
        ]
        return "\n".join(lines)
