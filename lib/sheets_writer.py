"""Append deal-flow leads to a Google Sheet via a Google Apps Script web app.

Why Apps Script and not a service account? Many Google Workspace orgs (incl.
berkeley.edu) block service-account key creation via the org policy
``iam.disableServiceAccountKeyCreation``. A bound Apps Script web app needs no
Google Cloud project and no downloadable key — it runs as the sheet's owner and
we just POST rows to its URL.

Optional + config-gated on ``LEADS_WEBAPP_URL`` (+ optional
``LEADS_WEBAPP_SECRET``). Fails soft: any error is logged and never breaks the
weekly run. The web app itself dedups (by company + URL) and appends.

Setup: see ``deploy/leads_webapp.gs`` — paste it into the Sheet's Apps Script
(Extensions → Apps Script), set SECRET, Deploy → New deployment → Web app
(execute as you, access "Anyone"), then put the /exec URL in LEADS_WEBAPP_URL.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("lib.sheets")

# Plain sector names (no emoji) for the sheet.
SECTOR_LABELS = {
    "nuclear_advanced_energy": "Nuclear & Advanced Energy",
    "water_cooling": "Water & Cooling",
    "power_electronics": "Power Electronics",
    "autonomous_systems": "Autonomous Systems",
    "advanced_manufacturing": "Advanced Manufacturing",
}


def _company_name(finding: dict) -> str:
    """Best-effort company name: the part of the title before an em/en/hyphen
    dash ("Ferveret — cooling for datacenters" → "Ferveret"), else affiliation."""
    title = (finding.get("title") or "").strip()
    for sep in (" — ", " – ", " - "):
        if sep in title:
            return title.split(sep, 1)[0].strip()
    return title or (finding.get("affiliation") or "").strip()


class GoogleSheetsWriter:
    """POSTs findings (candidate companies) to an Apps Script web app that
    appends them to a Google Sheet as leads. ``transport`` is injectable for
    tests (an ``httpx`` transport)."""

    def __init__(
        self,
        webapp_url: str | None = None,
        secret: str | None = None,
        *,
        timeout: int = 20,
        transport=None,
    ):
        self.webapp_url = (
            webapp_url if webapp_url is not None else os.getenv("LEADS_WEBAPP_URL")
        )
        self.secret = (
            secret if secret is not None else os.getenv("LEADS_WEBAPP_SECRET", "")
        )
        self._timeout = timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.webapp_url)

    def _lead_rows(self, findings: list[dict]) -> list[dict]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows: list[dict] = []
        for f in findings:
            contacts = ", ".join(f.get("researchers") or [])
            if f.get("contact_info"):
                contacts = f"{contacts} · {f['contact_info']}" if contacts else f["contact_info"]
            rows.append({
                "date": today,
                "company": _company_name(f),
                "sector": SECTOR_LABELS.get(f.get("focus_area"), f.get("focus_area") or ""),
                "stage": f.get("trl_estimate") or "",
                "why": (f.get("summary") or "")[:600],
                "contacts": contacts,
                "url": (f.get("source_url") or "").strip(),
                "relevance": f.get("relevance_score") or "",
            })
        return rows

    def append_leads(self, findings: list[dict]) -> int:
        """POST leads to the web app; it dedups + appends and returns how many
        were added. Synchronous (the caller runs it in an executor). Never
        raises — returns 0 on any problem."""
        if not self.configured or not findings:
            return 0
        payload = {"secret": self.secret, "leads": self._lead_rows(findings)}
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport, follow_redirects=True
            ) as client:
                resp = client.post(self.webapp_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            added = int(data.get("added", 0))
            if data.get("error"):
                log.error("Leads web app returned error: %s", data["error"])
            log.info("Google Sheet (Apps Script): appended %d new lead(s)", added)
            return added
        except Exception as exc:  # noqa: BLE001 — never break the weekly run
            log.error("Google Sheets web app write failed: %s", exc)
            return 0
