"""Build the data shape that the weekly_report.html template renders.

Reads from already-saved findings/events (System 1 + System 2 output), calls
Claude Sonnet for the executive summary, groups + sorts items for display, and
surfaces a Notable Contacts list from the top findings.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from lib.llm import LLMFilter

# Recognize "1. ", "1) ", "- ", "• ", "* " bullet markers at the start of a line.
_BULLET_RE = re.compile(r"^\s*(?:\d+[.)]\s+|[-•*]\s+)(.*?)\s*$")


def parse_bullets(text: str) -> list[str]:
    """Pull numbered or hyphenated bullet items out of LLM text.

    Returns an empty list if the text isn't bullet-formatted, so the template
    can fall back to plain prose for the heartbeat brief.
    """
    bullets: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _BULLET_RE.match(line)
        if match:
            bullets.append(match.group(1).strip())
    return bullets

# Human labels and subtle color accents used by the template.
FOCUS_LABELS = {
    "power_generation": "⚡ Power Generation",
    "energy_storage": "🔋 Energy Storage",
    "power_electronics": "🔌 Power Electronics",
    "semiconductors": "🧬 Semiconductors",
    "deep_tech_infra": "🏗️ Deep Tech Infrastructure",
}

FOCUS_PALETTE = {
    "power_generation": "#0ea5e9",   # sky
    "energy_storage": "#10b981",      # emerald
    "power_electronics": "#a855f7",   # purple
    "semiconductors": "#f59e0b",      # amber
    "deep_tech_infra": "#ef4444",     # red
}

CITY_LABELS = {
    "boston": "📍 Boston / Cambridge",
    "nyc": "📍 New York City",
    "sf": "📍 San Francisco / Bay Area",
}

CITY_PALETTE = {
    "boston": "#dc2626",   # red
    "nyc": "#2563eb",      # blue
    "sf": "#059669",       # green
}

_MAX_CONTACTS = 5
# Cap displayed items per group so a long sweep produces a SHORT brief.
_MAX_PER_GROUP = 5


class ReportSummarizer:
    """Shapes findings + events into a template-ready dict."""

    def __init__(self, llm: LLMFilter):
        self.llm = llm

    async def build_report_data(
        self, findings: list[dict], events: list[dict]
    ) -> dict[str, Any]:
        # Group + cap first so everything downstream — the Sonnet summary,
        # the notable contacts, the template — works with the SAME tight set
        # the reader actually sees.
        findings_by_focus = self._group_findings(findings)
        events_by_city = self._group_events(events)
        visible_findings = [
            f for items in findings_by_focus.values() for f in items
        ]
        visible_events = [e for items in events_by_city.values() for e in items]

        if visible_findings or visible_events:
            executive_summary = await self.llm.generate_weekly_summary(
                visible_findings, visible_events
            )
        else:
            executive_summary = (
                "No new findings or events landed this period. The monitoring "
                "pipeline ran as scheduled — the next brief will return to its "
                "regular cadence as soon as new items are picked up."
            )

        contacts = self._notable_contacts(visible_findings)

        return {
            "week_label": self._week_label(),
            "executive_summary": executive_summary,
            "executive_bullets": parse_bullets(executive_summary),
            "findings_by_focus": findings_by_focus,
            "events_by_city": events_by_city,
            "contacts": contacts,
            "findings_count": len(findings),
            "events_count": len(events),
            "focus_labels": FOCUS_LABELS,
            "focus_palette": FOCUS_PALETTE,
            "city_labels": CITY_LABELS,
            "city_palette": CITY_PALETTE,
        }

    # ----- grouping helpers ------------------------------------------------

    @staticmethod
    def _group_findings(findings: list[dict]) -> dict[str, list[dict]]:
        """Group by focus area, each group sorted by relevance_score DESC,
        capped at _MAX_PER_GROUP so the brief stays short."""
        grouped: dict[str, list[dict]] = {}
        for f in findings:
            grouped.setdefault(f.get("focus_area") or "other", []).append(f)
        for focus, items in grouped.items():
            items.sort(key=lambda x: x.get("relevance_score") or 0, reverse=True)
            del items[_MAX_PER_GROUP:]
        # Order focus areas by the canonical sequence (research priorities).
        ordered_keys = list(FOCUS_LABELS.keys()) + [
            k for k in grouped if k not in FOCUS_LABELS
        ]
        return {k: grouped[k] for k in ordered_keys if k in grouped}

    @staticmethod
    def _group_events(events: list[dict]) -> dict[str, list[dict]]:
        """Group by city, each group sorted by event_date ASC, capped at
        _MAX_PER_GROUP."""
        grouped: dict[str, list[dict]] = {}
        for e in events:
            grouped.setdefault(e.get("city") or "other", []).append(e)
        for city, items in grouped.items():
            items.sort(key=lambda x: x.get("event_date") or "")
            del items[_MAX_PER_GROUP:]
        ordered_keys = list(CITY_LABELS.keys()) + [
            k for k in grouped if k not in CITY_LABELS
        ]
        return {k: grouped[k] for k in ordered_keys if k in grouped}

    @staticmethod
    def _notable_contacts(findings: list[dict]) -> list[dict]:
        """First-time mention of each researcher across the top findings."""
        seen: set[str] = set()
        contacts: list[dict] = []
        ordered = sorted(
            findings, key=lambda x: x.get("relevance_score") or 0, reverse=True
        )
        for f in ordered:
            for researcher in (f.get("researchers") or []):
                name = (researcher or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                contacts.append({
                    "name": name,
                    "affiliation": f.get("affiliation"),
                    "context": f.get("title"),
                    "contact": f.get("contact_info"),
                })
                if len(contacts) >= _MAX_CONTACTS:
                    return contacts
        return contacts

    @staticmethod
    def _week_label() -> str:
        # ARCHITECTURE expects "Week of {date}" — Monday is the publication day.
        now = datetime.now(timezone.utc)
        return now.strftime("Week of %B %d, %Y")
