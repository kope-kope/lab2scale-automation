"""System 3 — compile the weekly report and deliver it.

Reads unreported findings + events from the data store, asks the summarizer to
shape them into template data, renders the Jinja HTML, sends via Resend (or
saves to disk on ``dry_run``), then marks all included items reported and
logs a ``reports`` row.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from lib.data_store import DataStore, db_path_from_url
from lib.email_sender import EmailSender
from lib.llm import LLMFilter
from systems.system3_delivery.summarizer import ReportSummarizer

log = logging.getLogger("system3.orchestrator")

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
DEFAULT_TEMPLATE_NAME = "weekly_report.html"
FALLBACK_REPORT_PATH = Path(__file__).resolve().parents[2] / "data" / "latest_report.html"


class DeliveryOrchestrator:
    """Compiles the weekly intelligence brief and emails it."""

    def __init__(
        self,
        store: DataStore | None = None,
        llm: LLMFilter | None = None,
        email_sender: EmailSender | None = None,
        summarizer: ReportSummarizer | None = None,
        *,
        recipient: str | None = None,
        template_dir: str | Path | None = None,
        template_name: str = DEFAULT_TEMPLATE_NAME,
    ):
        self._owns_store = store is None
        if store is None:
            url = os.getenv("DATABASE_URL", "sqlite:///data/lab2scale.db")
            store = DataStore(db_path_from_url(url))
        self.store = store
        self.llm = llm or LLMFilter()
        self.summarizer = summarizer or ReportSummarizer(self.llm)
        self.email_sender = email_sender if email_sender is not None else EmailSender()
        self.recipient = recipient or os.getenv(
            "REPORT_RECIPIENT", "team@lab-2-scale.com"
        )
        env = Environment(
            loader=FileSystemLoader(str(template_dir or DEFAULT_TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.template = env.get_template(template_name)

    async def run(self, dry_run: bool = False) -> dict:
        await self.store.connect()
        await self.store.init_db()

        findings = await self.store.get_unreported_findings()
        events = await self.store.get_unreported_events()
        log.info("Compiling report from %d finding(s), %d event(s)",
                 len(findings), len(events))

        is_empty = not findings and not events
        if is_empty:
            log.info("No unreported items — sending heartbeat brief.")

        data = await self.summarizer.build_report_data(findings, events)
        html = self.template.render(**data)
        suffix = " (no new items)" if is_empty else ""
        subject = (
            f"Lab2Scale Weekly Intelligence Brief — {data['week_label']}{suffix}"
        )

        status = "sent"
        error_message: str | None = None
        sent = False

        if dry_run:
            FALLBACK_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            FALLBACK_REPORT_PATH.write_text(html, encoding="utf-8")
            status = "dry_run"
            log.info("Dry run — HTML written to %s", FALLBACK_REPORT_PATH)
        else:
            try:
                await self.email_sender.send_report(html, subject, self.recipient)
                sent = True
            except Exception as exc:  # noqa: BLE001 — never crash the cron
                error_message = repr(exc)
                status = "failed"
                log.error("Email send failed: %s — saving HTML to %s",
                          exc, FALLBACK_REPORT_PATH)
                FALLBACK_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
                FALLBACK_REPORT_PATH.write_text(html, encoding="utf-8")

        # Only mark items reported when we actually delivered (or dry-ran for
        # preview). A real send failure leaves items unreported so the next
        # run can retry them. Nothing to mark on a heartbeat brief.
        if (sent or dry_run) and not is_empty:
            await self.store.mark_reported([f["id"] for f in findings], "findings")
            await self.store.mark_reported([e["id"] for e in events], "events")

        await self.store.log_report(
            findings_count=len(findings),
            events_count=len(events),
            recipient=self.recipient,
            status=status,
            error_message=error_message,
        )

        await self._cleanup_resources()
        return {
            "system": "delivery",
            "findings": len(findings),
            "events": len(events),
            "sent": sent,
            "dry_run": dry_run,
            "status": status,
            "is_empty": is_empty,
            "recipient": self.recipient,
            "subject": subject,
            "html_path": str(FALLBACK_REPORT_PATH) if (dry_run or not sent) else None,
        }

    async def _cleanup_resources(self) -> None:
        if self._owns_store:
            await self.store.close()
