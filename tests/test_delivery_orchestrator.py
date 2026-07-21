"""End-to-end test for the System 3 DeliveryOrchestrator.

Uses an in-memory DataStore, a fake LLM (returns a canned summary string), and
a fake EmailSender (records what it would have sent). Verifies that
unreported findings/events flow into the rendered HTML, get marked reported
on send, and produce a reports-table row.
"""

import asyncio

from lib.data_store import DataStore
from lib.email_sender import EmailSender
from systems.system3_delivery.orchestrator import DeliveryOrchestrator
from systems.system3_delivery.summarizer import ReportSummarizer


class FakeLLM:
    def __init__(self, text: str = "Solid-state cells led the field."):
        self.text = text
        self.calls = 0

    async def generate_weekly_summary(self, findings, events):
        self.calls += 1
        return self.text


class _FakeEmails:
    def __init__(self):
        self.sent = []

    def send(self, params):
        self.sent.append(params)
        return {"id": "fake"}


class FakeResendClient:
    def __init__(self):
        self.Emails = _FakeEmails()


async def _seeded_store() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    await store.save_finding({
        "id": "f1", "focus_area": "energy_storage",
        "agent": "energy_storage_agent", "title": "Solid-state battery hits 1000 cycles",
        "summary": "Durable cell at MIT.", "relevance_score": 9.0,
        "researchers": ["Dr. Jane Smith"], "affiliation": "MIT",
        "source_url": "https://ex.com/f1", "source_type": "preprint",
    })
    await store.save_event({
        "id": "e1", "city": "boston", "agent": "boston_events_agent",
        "event_name": "MIT Energy Night", "event_date": "2026-06-15",
        "venue": "MIT Media Lab", "url": "https://ex.com/e1",
        "description": "Panel.", "cost": "Free", "event_type": "panel",
        "relevance_tags": ["power_electronics"], "relevance_score": 8.0,
    })
    return store


def test_orchestrator_end_to_end_sends_and_marks_reported():
    async def body():
        store = await _seeded_store()
        client = FakeResendClient()
        llm = FakeLLM()
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=client),
            recipient="team@example.com",
        )
        result = await orch.run()

        sent_params = client.Emails.sent[0]
        # Items should now be marked reported.
        remaining_findings = await store.get_unreported_findings()
        remaining_events = await store.get_unreported_events()
        # And the reports table should hold a row.
        async with store.connection.execute(
            "SELECT findings_count, events_count, recipient, status FROM reports"
        ) as cur:
            log_row = await cur.fetchone()
        return result, sent_params, remaining_findings, remaining_events, dict(log_row)

    result, sent, remaining_f, remaining_e, log_row = asyncio.run(body())

    assert result["sent"] is True
    assert result["findings"] == 1 and result["events"] == 1
    # The rendered HTML carries the brief's substance.
    assert "Solid-state battery hits 1000 cycles" in sent["html"]
    assert "MIT Energy Night" in sent["html"]
    assert "Dr. Jane Smith" in sent["html"]   # surfaced as a notable contact
    # Reported.
    assert remaining_f == []
    assert remaining_e == []
    # Logged.
    assert log_row["findings_count"] == 1
    assert log_row["events_count"] == 1
    assert log_row["recipient"] == "team@example.com"
    assert log_row["status"] == "sent"


def test_report_ccs_explicit_addresses():
    """An explicit cc list reaches the Resend payload."""
    async def body():
        store = await _seeded_store()
        client = FakeResendClient()
        llm = FakeLLM()
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=client),
            recipient="team@example.com",
            cc=["lead@example.com", "partner@example.com"],
        )
        result = await orch.run()
        return result, client.Emails.sent[0]

    result, sent = asyncio.run(body())
    assert result["cc"] == ["lead@example.com", "partner@example.com"]
    assert sent["to"] == ["team@example.com"]
    assert sent["cc"] == ["lead@example.com", "partner@example.com"]


def test_report_cc_from_env_var(monkeypatch):
    """REPORT_CC (comma/semicolon list) configures cc without code changes."""
    monkeypatch.setenv("REPORT_CC", "a@example.com, b@example.com")

    async def body():
        store = await _seeded_store()
        client = FakeResendClient()
        llm = FakeLLM()
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=client),
            recipient="team@example.com",
            # cc omitted → picked up from REPORT_CC
        )
        await orch.run()
        return client.Emails.sent[0]

    sent = asyncio.run(body())
    assert sent["cc"] == ["a@example.com", "b@example.com"]


def test_no_cc_when_unset(monkeypatch):
    """No cc arg and no REPORT_CC → payload carries no cc."""
    monkeypatch.delenv("REPORT_CC", raising=False)

    async def body():
        store = await _seeded_store()
        client = FakeResendClient()
        llm = FakeLLM()
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=client),
            recipient="team@example.com",
        )
        result = await orch.run()
        return result, client.Emails.sent[0]

    result, sent = asyncio.run(body())
    assert result["cc"] == []
    assert "cc" not in sent


def test_writes_leads_to_google_sheet_on_real_run():
    """On a real (non-dry-run) send, findings are appended to the leads sheet."""
    class FakeSheets:
        def __init__(self):
            self.written = None

        @property
        def configured(self):
            return True

        def append_leads(self, findings):
            self.written = findings
            return len(findings)

    async def body():
        store = await _seeded_store()
        llm = FakeLLM()
        sheets = FakeSheets()
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=FakeResendClient()),
            sheets_writer=sheets,
            recipient="team@example.com",
        )
        result = await orch.run()
        return result, sheets

    result, sheets = asyncio.run(body())
    assert result["leads_added"] == 1          # the one seeded finding
    assert sheets.written is not None and len(sheets.written) == 1


def test_dry_run_does_not_write_to_sheet():
    """Dry-run previews must not touch the real leads sheet."""
    class FakeSheets:
        configured = True
        def __init__(self):
            self.calls = 0
        def append_leads(self, findings):
            self.calls += 1
            return len(findings)

    async def body():
        store = await _seeded_store()
        llm = FakeLLM()
        sheets = FakeSheets()
        orch = DeliveryOrchestrator(
            store=store, llm=llm, summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key=None, client=None),
            sheets_writer=sheets, recipient="team@example.com",
        )
        result = await orch.run(dry_run=True)
        return result, sheets

    result, sheets = asyncio.run(body())
    assert result["leads_added"] == 0
    assert sheets.calls == 0


def test_dry_run_writes_html_and_marks_reported():
    """Dry-run still marks items reported (preview = published from our POV)."""
    async def body():
        store = await _seeded_store()
        llm = FakeLLM()
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key=None, client=None),  # unconfigured
            recipient="team@example.com",
        )
        result = await orch.run(dry_run=True)
        remaining_findings = await store.get_unreported_findings()
        return result, remaining_findings

    result, remaining = asyncio.run(body())
    assert result["dry_run"] is True
    assert result["status"] == "dry_run"
    assert result["html_path"] is not None
    assert remaining == []   # marked reported on dry-run too


def test_empty_queue_still_sends_a_heartbeat_brief():
    """When there's nothing unreported, send a "no new items" email anyway
    so the weekly cron remains a visible heartbeat."""

    async def body():
        store = DataStore(":memory:")
        await store.init_db()
        client = FakeResendClient()
        llm = FakeLLM("should not be called for empty queue")
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=client),
            recipient="team@example.com",
        )
        result = await orch.run()
        async with store.connection.execute(
            "SELECT findings_count, events_count, status FROM reports"
        ) as cur:
            row = await cur.fetchone()
        return result, client.Emails.sent, dict(row)

    result, sent, log_row = asyncio.run(body())

    # We DID send — and the subject signals the empty state.
    assert result["sent"] is True
    assert result["is_empty"] is True
    assert result["findings"] == 0 and result["events"] == 0
    assert len(sent) == 1
    assert "(no new items)" in sent[0]["subject"]
    # The reports table records the heartbeat.
    assert log_row["findings_count"] == 0
    assert log_row["events_count"] == 0
    assert log_row["status"] == "sent"


def test_empty_brief_does_not_call_llm():
    """No findings or events → no Sonnet call (cost guard)."""
    async def body():
        store = DataStore(":memory:")
        await store.init_db()
        llm = FakeLLM("should never appear")
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=FakeResendClient()),
        )
        await orch.run()
        return llm.calls

    assert asyncio.run(body()) == 0


def test_send_failure_keeps_items_unreported_and_saves_html(tmp_path, monkeypatch):
    """If Resend errors, items stay unreported so the next run can retry."""
    monkeypatch.setattr(
        "systems.system3_delivery.orchestrator.FALLBACK_REPORT_PATH",
        tmp_path / "latest_report.html",
    )

    class _Boom:
        def send(self, params):
            raise RuntimeError("resend down")

    class _BoomClient:
        Emails = _Boom()

    async def body():
        store = await _seeded_store()
        llm = FakeLLM()
        orch = DeliveryOrchestrator(
            store=store, llm=llm,
            summarizer=ReportSummarizer(llm),
            email_sender=EmailSender(api_key="re_test", client=_BoomClient()),
        )
        result = await orch.run()
        remaining = await store.get_unreported_findings()
        async with store.connection.execute(
            "SELECT status, error_message FROM reports"
        ) as cur:
            row = await cur.fetchone()
        return result, remaining, dict(row)

    result, remaining, row = asyncio.run(body())
    assert result["sent"] is False
    assert result["status"] == "failed"
    assert len(remaining) == 1                       # not marked reported
    assert row["status"] == "failed"
    assert "resend down" in row["error_message"]
    assert (tmp_path / "latest_report.html").exists()  # fallback saved
