"""Append deal-flow leads to a Google Sheet via a service account.

Optional + config-gated: does nothing unless BOTH env vars are set —
``GOOGLE_SERVICE_ACCOUNT_JSON`` (the service-account key JSON) and
``LEADS_SHEET_ID``. Fails soft: a sheet error is logged and never breaks the
weekly run.

Setup (one time):
  1. Google Cloud Console → APIs & Services → enable the *Google Sheets API*.
  2. Create a *service account*; create a key → download the JSON.
  3. Open your Google Sheet → Share → paste the service account's email
     (…@…​.iam.gserviceaccount.com) → give it *Editor*.
  4. Set env:
       GOOGLE_SERVICE_ACCOUNT_JSON = <the entire JSON file contents>
       LEADS_SHEET_ID             = <the id from the sheet URL>
       LEADS_SHEET_TAB            = Leads   (optional; default "Leads")
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("lib.sheets")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column order written to the sheet. "Status" is yours to edit by hand.
HEADER = [
    "Date", "Company", "Sector", "Stage", "Why it fits",
    "Contacts", "Source URL", "Relevance", "Status",
]

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
    """Appends findings (candidate companies) to a Google Sheet as leads.

    ``client`` is injectable for tests (any object exposing
    ``open_by_key(id).worksheet(tab)`` with a gspread-like worksheet).
    """

    def __init__(
        self,
        credentials_json: str | None = None,
        sheet_id: str | None = None,
        tab: str | None = None,
        client=None,
    ):
        self._creds_json = (
            credentials_json if credentials_json is not None
            else os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        )
        self.sheet_id = sheet_id or os.getenv("LEADS_SHEET_ID")
        self.tab = tab or os.getenv("LEADS_SHEET_TAB", "Leads")
        self._client = client

    @property
    def configured(self) -> bool:
        """True when we have a sheet id and a way to authenticate."""
        return bool(self.sheet_id and (self._creds_json or self._client is not None))

    def _get_client(self):
        if self._client is not None:
            return self._client
        import gspread  # noqa: WPS433 — lazy so import-time needs no creds
        from google.oauth2.service_account import Credentials

        info = json.loads(self._creds_json)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        self._client = gspread.authorize(creds)
        return self._client

    def _worksheet(self, client):
        sheet = client.open_by_key(self.sheet_id)
        try:
            return sheet.worksheet(self.tab)
        except Exception:  # noqa: BLE001 — tab doesn't exist yet
            return sheet.add_worksheet(title=self.tab, rows=1000, cols=len(HEADER))

    def append_leads(self, findings: list[dict]) -> int:
        """Append findings as lead rows, skipping companies already in the sheet
        (dedup by company + source URL). Returns rows added. Synchronous
        (gspread is sync); the caller runs it in an executor. Never raises."""
        if not self.configured or not findings:
            return 0
        try:
            ws = self._worksheet(self._get_client())
            existing = ws.get_all_values()
            if not existing:
                ws.append_row(HEADER, value_input_option="USER_ENTERED")
                existing = [HEADER]

            seen: set[tuple[str, str]] = set()
            for row in existing[1:]:
                company = (row[1] if len(row) > 1 else "").strip().lower()
                url = (row[6] if len(row) > 6 else "").strip().lower()
                seen.add((company, url))

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows: list[list] = []
            for f in findings:
                company = _company_name(f)
                url = (f.get("source_url") or "").strip()
                key = (company.lower(), url.lower())
                if key in seen:
                    continue
                seen.add(key)
                contacts = ", ".join(f.get("researchers") or [])
                if f.get("contact_info"):
                    contacts = f"{contacts} · {f['contact_info']}" if contacts else f["contact_info"]
                rows.append([
                    today,
                    company,
                    SECTOR_LABELS.get(f.get("focus_area"), f.get("focus_area") or ""),
                    f.get("trl_estimate") or "",
                    (f.get("summary") or "")[:600],
                    contacts,
                    url,
                    f.get("relevance_score") or "",
                    "New",
                ])

            if rows:
                ws.append_rows(rows, value_input_option="USER_ENTERED")
            log.info("Google Sheet: appended %d new lead(s) to '%s'", len(rows), self.tab)
            return len(rows)
        except Exception as exc:  # noqa: BLE001 — never break the weekly run
            log.error("Google Sheets write failed: %s", exc)
            return 0
