"""EmailSender unit tests with a fake Resend client."""

import asyncio
import pytest

from lib.email_sender import EmailSender


class _FakeEmails:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, params):
        self.sent.append(params)
        return {"id": "fake-email-id"}


class FakeResendClient:
    def __init__(self):
        self.Emails = _FakeEmails()


def test_send_report_passes_correct_params():
    client = FakeResendClient()
    sender = EmailSender(
        api_key="re_test", sender="reports@example.com",
        reply_to="team@example.com", client=client,
    )
    result = asyncio.run(sender.send_report(
        html="<h1>Brief</h1>",
        subject="Weekly Brief — Week of May 25",
        to="team@example.com",
    ))
    assert result == {"id": "fake-email-id"}
    assert len(client.Emails.sent) == 1
    params = client.Emails.sent[0]
    assert params["from"] == "reports@example.com"
    assert params["to"] == ["team@example.com"]
    assert params["subject"].startswith("Weekly Brief")
    assert params["reply_to"] == ["team@example.com"]
    assert "<h1>Brief</h1>" in params["html"]


def test_send_raises_when_unconfigured():
    sender = EmailSender(api_key=None, client=None)
    assert sender.configured is False
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        asyncio.run(sender.send_report("html", "subj", "to@example.com"))


def test_send_accepts_a_list_of_recipients():
    client = FakeResendClient()
    sender = EmailSender(api_key="re_test", client=client)
    asyncio.run(sender.send_report(
        "html", "subj", to=["a@example.com", "b@example.com"]
    ))
    assert client.Emails.sent[0]["to"] == ["a@example.com", "b@example.com"]
