"""Tests for GoogleSheetsWriter with a fake gspread-like client."""

from lib.sheets_writer import HEADER, GoogleSheetsWriter

FINDINGS = [
    {"title": "Ferveret — datacenter cooling", "focus_area": "water_cooling",
     "trl_estimate": "TRL 4", "summary": "cooling startup", "researchers": ["Dr. Jane"],
     "contact_info": "jane@ferveret.com", "source_url": "https://ex.com/ferveret",
     "relevance_score": 8.2},
    {"title": "Apollo Atomics — SMR", "focus_area": "nuclear_advanced_energy",
     "trl_estimate": "TRL 3", "summary": "SMR startup", "researchers": [],
     "contact_info": None, "source_url": "https://ex.com/apollo", "relevance_score": 9.0},
]


class FakeWorksheet:
    def __init__(self, values=None):
        self.values = [list(r) for r in (values or [])]

    def get_all_values(self):
        return [list(r) for r in self.values]

    def append_row(self, row, value_input_option=None):
        self.values.append(list(row))

    def append_rows(self, rows, value_input_option=None):
        self.values.extend([list(r) for r in rows])


class FakeSpreadsheet:
    def __init__(self, ws, has_tab=True):
        self._ws = ws
        self.has_tab = has_tab

    def worksheet(self, tab):
        if not self.has_tab:
            raise KeyError(tab)
        return self._ws

    def add_worksheet(self, title, rows, cols):
        self.has_tab = True
        return self._ws


class FakeClient:
    def __init__(self, spreadsheet, raise_on_open=False):
        self.spreadsheet = spreadsheet
        self.raise_on_open = raise_on_open

    def open_by_key(self, sheet_id):
        if self.raise_on_open:
            raise RuntimeError("boom")
        return self.spreadsheet


def _writer(ws, **kw):
    client = FakeClient(FakeSpreadsheet(ws, has_tab=kw.pop("has_tab", True)),
                        raise_on_open=kw.pop("raise_on_open", False))
    return GoogleSheetsWriter(sheet_id="sheet123", client=client, **kw)


def test_configured_requires_sheet_id_and_auth():
    assert _writer(FakeWorksheet()).configured is True
    assert GoogleSheetsWriter(sheet_id=None, client=object()).configured is False
    assert GoogleSheetsWriter(sheet_id="x", credentials_json=None, client=None).configured is False


def test_appends_header_and_rows_when_empty():
    ws = FakeWorksheet()
    added = _writer(ws).append_leads(FINDINGS)
    assert added == 2
    assert ws.values[0] == HEADER
    row = ws.values[1]
    assert row[1] == "Ferveret"                 # Company (name split from title)
    assert row[2] == "Water & Cooling"          # Sector (plain label)
    assert "Dr. Jane" in row[5] and "jane@ferveret.com" in row[5]  # Contacts
    assert row[6] == "https://ex.com/ferveret"  # Source URL
    assert row[8] == "New"                       # Status


def test_dedups_companies_already_in_sheet():
    ws = FakeWorksheet([
        HEADER,
        ["2026-06-01", "Ferveret", "Water & Cooling", "TRL 4", "x", "y",
         "https://ex.com/ferveret", "8.2", "Contacted"],
    ])
    added = _writer(ws).append_leads(FINDINGS)
    assert added == 1                            # Ferveret skipped, Apollo added
    assert ws.values[-1][1] == "Apollo Atomics"


def test_creates_tab_when_missing():
    ws = FakeWorksheet()
    added = _writer(ws, has_tab=False).append_leads(FINDINGS)
    assert added == 2                            # add_worksheet path worked


def test_fails_soft_on_client_error():
    assert _writer(FakeWorksheet(), raise_on_open=True).append_leads(FINDINGS) == 0


def test_not_configured_or_empty_returns_zero():
    assert GoogleSheetsWriter(sheet_id=None).append_leads(FINDINGS) == 0
    assert _writer(FakeWorksheet()).append_leads([]) == 0
