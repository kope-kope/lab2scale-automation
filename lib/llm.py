"""Claude API wrapper for relevance scoring, structured extraction, and the
weekly summary.

Cost split (per the spec): Claude Haiku for high-volume scoring and extraction,
Claude Sonnet for the weekly executive summary. Model IDs are read from the
environment so they can be changed without code edits:

    LLM_SCORING_MODEL   (default: claude-haiku-4-5)
    LLM_SUMMARY_MODEL   (default: claude-sonnet-4-6)

The prompts themselves live in ``prompts/*.md`` and are loaded via
``lib.prompts`` — edit those files to change scoring/extraction/summary
behavior without touching this code. The tool-use JSON schemas below stay in
code because they're structural API contracts (field types, enums), not prose.
"""

from __future__ import annotations

import json
import logging
import os
import re

from lib.prompts import load_prompt, render_prompt

log = logging.getLogger("lib.llm")

DEFAULT_SCORING_MODEL = "claude-haiku-4-5"
DEFAULT_SUMMARY_MODEL = "claude-sonnet-4-6"

# Items scoring at or above this are kept. Calibrated against a real sweep
# (8.0 keeps the top ~18% — sharp signal without the long tail).
RELEVANCE_THRESHOLD = 8.0

# Cap content sent to the model to keep token cost bounded.
_MAX_CONTENT_CHARS = 6000

# Approximate prices per 1M tokens (USD). Used for cost summaries — not a
# billing source of truth. Update when Anthropic's published pricing changes.
_MODEL_PRICING = {
    "claude-haiku-4-5":        {"input": 1.0,  "output": 5.0},
    "claude-sonnet-4-6":       {"input": 3.0,  "output": 15.0},
    "claude-opus-4-7":         {"input": 15.0, "output": 75.0},
    "claude-3-5-haiku-latest": {"input": 0.8,  "output": 4.0},
    "claude-3-5-sonnet-latest":{"input": 3.0,  "output": 15.0},
}
_UNKNOWN_MODEL_PRICING = {"input": 0.0, "output": 0.0}

# Prompts live in prompts/*.md (loaded via lib.prompts):
#   research_scoring.md, research_extraction.md, event_scoring.md,
#   event_extraction.md, summary_system.md, summary_user.md
# The tool_use JSON schemas stay here — they're structural, not prose.

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

# Models often emit these as a literal field value when a field is absent
# (the extraction prompt says "use null"). Treat them as missing.
_NULLISH = {"", "null", "none", "n/a", "na", "unknown", "not found", "not specified"}


def _is_nullish(value) -> bool:
    return isinstance(value, str) and value.strip().lower() in _NULLISH


# --- Event-specific structured-extraction schema (System 2) -----------------

EVENT_EXTRACTION_TOOL = {
    "name": "record_event",
    "description": "Record the structured fields extracted from an event listing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "event_name": {"type": "string", "description": "Concise event name"},
            "event_date": {"type": "string", "description": "ISO 8601 date (YYYY-MM-DD) if known"},
            "event_time": {"type": "string", "description": "e.g. 18:00-20:00"},
            "venue": {"type": "string", "description": "Location or platform"},
            "description": {"type": "string", "description": "2-3 sentence summary"},
            "cost": {"type": "string", "description": "Free / $50 / TBD"},
            "event_type": {
                "type": "string",
                "enum": ["conference", "seminar", "meetup", "workshop", "demo_day", "panel", "summit"],
            },
            "relevance_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lab2Scale focus areas this event touches",
            },
        },
        "required": ["event_name"],
    },
}

_EVENT_EXTRACTION_FIELDS = (
    "event_name", "event_date", "event_time", "venue",
    "description", "cost", "event_type", "relevance_tags",
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
        # Per-model token tracking so a mixed Haiku + Sonnet run reports
        # spend by model. Empty until at least one call is made.
        self.tokens_by_model: dict[str, dict[str, int]] = {}

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
        bucket = self.tokens_by_model.setdefault(model, {"input": 0, "output": 0})
        bucket["input"] += in_tok
        bucket["output"] += out_tok
        log.info(
            "LLM %s usage: input=%d output=%d (cumulative in=%d out=%d)",
            model, in_tok, out_tok, self.total_input_tokens, self.total_output_tokens,
        )

    def cost_estimate(self) -> dict:
        """Per-model token + USD estimate. Cost numbers are approximate."""
        by_model: dict[str, dict] = {}
        total_cost = 0.0
        for model, toks in self.tokens_by_model.items():
            price = _MODEL_PRICING.get(model, _UNKNOWN_MODEL_PRICING)
            in_cost = toks["input"] * price["input"] / 1_000_000
            out_cost = toks["output"] * price["output"] / 1_000_000
            cost = in_cost + out_cost
            by_model[model] = {
                "input": toks["input"],
                "output": toks["output"],
                "cost_usd": round(cost, 4),
                "known_pricing": model in _MODEL_PRICING,
            }
            total_cost += cost
        return {
            "by_model": by_model,
            "total_input": self.total_input_tokens,
            "total_output": self.total_output_tokens,
            "total_cost_usd": round(total_cost, 4),
        }

    def log_usage_summary(self) -> None:
        """Emit a human-readable LLM spend summary. No-op when nothing ran."""
        est = self.cost_estimate()
        if not est["by_model"]:
            return
        log.info("---- LLM usage summary ----")
        for model, data in est["by_model"].items():
            tag = "" if data["known_pricing"] else "  (pricing unknown)"
            log.info(
                "  %s: %d in / %d out  ~ $%.4f%s",
                model, data["input"], data["output"], data["cost_usd"], tag,
            )
        log.info(
            "  TOTAL: %d in / %d out  ~ $%.4f",
            est["total_input"], est["total_output"], est["total_cost_usd"],
        )

    async def score_relevance(self, content: str, focus_area: str) -> float:
        """Score content 0-10 for relevance to a focus area (Claude Haiku).

        On API failure, logs and returns 0.0 (the item is then filtered out).
        """
        prompt = render_prompt(
            "research_scoring",
            focus_area=focus_area,
            content=(content or "")[:_MAX_CONTENT_CHARS],
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
        prompt = render_prompt(
            "research_extraction", content=(content or "")[:_MAX_CONTENT_CHARS]
        )
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
        # Coerce literal "null"/"none"/"" strings to real None.
        for field in _EXTRACTION_FIELDS:
            if _is_nullish(result[field]):
                result[field] = None
        # researchers → a clean list of real names.
        researchers = result["researchers"]
        if researchers is None:
            researchers = []
        elif isinstance(researchers, str):
            researchers = [researchers]
        result["researchers"] = [
            r for r in researchers if isinstance(r, str) and not _is_nullish(r)
        ]
        return result

    # ----- event-specific scoring + extraction (System 2) -----------------

    async def score_event_relevance(self, content: str) -> float:
        """Score an event 0-10 for relevance to Lab2Scale focus areas (Haiku).

        On API failure, logs and returns 0.0 (the item is then filtered out).
        """
        prompt = render_prompt(
            "event_scoring", content=(content or "")[:_MAX_CONTENT_CHARS]
        )
        try:
            response = await self.client.messages.create(
                model=self.scoring_model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("score_event_relevance failed: %s — defaulting to 0.0", exc)
            return 0.0
        self._track_usage(response, self.scoring_model)
        data = _extract_json(_first_text(response))
        try:
            return float(data.get("score", 0.0))
        except (TypeError, ValueError):
            return 0.0

    async def extract_event_data(self, content: str) -> dict:
        """Extract structured event data (Haiku, tool_use).

        Returns a dict with all event fields present (missing → None / []).
        On API failure, returns an empty-but-shaped dict.
        """
        prompt = render_prompt(
            "event_extraction", content=(content or "")[:_MAX_CONTENT_CHARS]
        )
        try:
            response = await self.client.messages.create(
                model=self.scoring_model,
                max_tokens=1024,
                tools=[EVENT_EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": EVENT_EXTRACTION_TOOL["name"]},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("extract_event_data failed: %s — returning empty", exc)
            return self._normalize_event_extraction({})
        self._track_usage(response, self.scoring_model)

        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                return self._normalize_event_extraction(dict(getattr(block, "input", {}) or {}))
        return self._normalize_event_extraction(_extract_json(_first_text(response)))

    @staticmethod
    def _normalize_event_extraction(data: dict) -> dict:
        result = {field: data.get(field) for field in _EVENT_EXTRACTION_FIELDS}
        for field in _EVENT_EXTRACTION_FIELDS:
            if _is_nullish(result[field]):
                result[field] = None
        tags = result["relevance_tags"]
        if tags is None:
            tags = []
        elif isinstance(tags, str):
            tags = [tags]
        result["relevance_tags"] = [
            t for t in tags if isinstance(t, str) and not _is_nullish(t)
        ]
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
                system=load_prompt("summary_system"),
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.error("generate_weekly_summary failed: %s", exc)
            return ""
        self._track_usage(response, self.summary_model)
        return _first_text(response)

    @staticmethod
    def _build_summary_prompt(findings: list[dict], events: list[dict]) -> str:
        # The prose/framing lives in prompts/summary_user.md; here we only
        # serialize the data rows it interpolates. (Row formatting stays in
        # code because it's tied to the finding/event dict shape.)
        findings_lines = []
        for f in findings:
            score = f.get("relevance_score")
            score_str = f"{score:.1f}" if isinstance(score, (int, float)) else "n/a"
            affil = f.get("affiliation") or "unknown affiliation"
            summary = (f.get("summary") or "").strip()
            findings_lines.append(
                f"- [{f.get('focus_area', '?')} | score {score_str}] "
                f"{f.get('title', 'Untitled')} — {summary} ({affil})"
            )
        events_lines = []
        for e in events:
            events_lines.append(
                f"- [{e.get('city', '?')} | {e.get('event_date', 'TBD')}] "
                f"{e.get('event_name', 'Untitled')} — {e.get('venue') or 'venue TBD'}"
            )
        return render_prompt(
            "summary_user",
            findings_count=len(findings),
            events_count=len(events),
            findings_list="\n".join(findings_lines),
            events_list="\n".join(events_lines),
        )
