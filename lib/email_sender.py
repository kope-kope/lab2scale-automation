"""Send HTML emails via the Resend API.

The ``resend`` SDK is synchronous, so the actual ``send()`` is dispatched to an
executor to avoid blocking the event loop. The Anthropic-style ``client`` seam
(passing in a fake) makes tests easy.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

log = logging.getLogger("lib.email_sender")


class EmailSender:
    """Resend wrapper. ``client`` accepts any object with ``Emails.send(params)``."""

    def __init__(
        self,
        api_key: str | None = None,
        sender: str | None = None,
        reply_to: str | None = None,
        client: Any | None = None,
    ):
        self._api_key = api_key if api_key is not None else os.getenv("RESEND_API_KEY")
        self.sender = sender or os.getenv("REPORT_FROM", "reports@lab-2-scale.com")
        # By default replies bounce to the team mailbox so the brief feels
        # like a conversation, not a one-way blast.
        self.reply_to = reply_to or os.getenv("REPORT_RECIPIENT", "team@lab-2-scale.com")
        self._client = client
        if self._client is None and self._api_key:
            import resend  # noqa: WPS433 — lazy so import-time has no side effect
            resend.api_key = self._api_key
            self._client = resend

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def send_report(self, html: str, subject: str, to: str | list[str]) -> dict:
        """Send the report email. Raises if Resend isn't configured."""
        if not self.configured:
            raise RuntimeError(
                "RESEND_API_KEY is not set — set it in .env or inject a client."
            )
        params = {
            "from": self.sender,
            "to": [to] if isinstance(to, str) else list(to),
            "subject": subject,
            "html": html,
            "reply_to": [self.reply_to] if isinstance(self.reply_to, str) else list(self.reply_to),
        }
        log.info("Sending report to %s (subject: %s)", params["to"], subject)
        send_fn: Callable[..., Any] = self._client.Emails.send
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: send_fn(params)
        )
