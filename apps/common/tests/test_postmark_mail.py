"""System mail through Postmark's HTTPS API (``EMAIL_BACKEND_TYPE=postmark``)."""

import json
import smtplib
from typing import Any

import httpx
import pytest
from django.core.mail import EmailMessage, EmailMultiAlternatives

from apps.common import postmark_mail

TOKEN = "00000000-1111-2222-3333-444444444444"  # noqa: S105 - a fake credential for tests


class Recorder:
    """Every request the fake Postmark saw, and what it answers next."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: dict[str, Any] = {"MessageID": "pm-1", "ErrorCode": 0}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def postmark(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Route every Postmark call through a recording mock transport, keeping the real error mapping."""
    settings.POSTMARK_SERVER_TOKEN = TOKEN
    settings.POSTMARK_MESSAGE_STREAM = "outbound"
    settings.DEFAULT_FROM_EMAIL = "All Things Rugby <info@sender.test>"
    recorder = Recorder()
    client = httpx.Client(transport=httpx.MockTransport(recorder.handle))
    from apps.channels.providers import base

    original = base.request_json

    def through(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.pop("client", None)
        return original(method, url, client=client, **kwargs)

    monkeypatch.setattr(base, "request_json", through)
    return recorder


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def test_a_text_and_html_message_is_sent(postmark: Recorder) -> None:
    message = EmailMultiAlternatives("Reset your password", "Text body", to=["reader@example.test"])
    message.attach_alternative("<p>HTML body</p>", "text/html")

    assert postmark_mail.PostmarkEmailBackend().send_messages([message]) == 1

    (request,) = postmark.requests
    assert (request.url.host, request.url.path) == ("api.postmarkapp.com", "/email")
    assert request.headers["X-Postmark-Server-Token"] == TOKEN
    body = _body(request)
    assert body["From"] == "All Things Rugby <info@sender.test>"
    assert body["To"] == "reader@example.test"
    assert body["Subject"] == "Reset your password"
    assert body["TextBody"] == "Text body"
    assert body["HtmlBody"] == "<p>HTML body</p>"
    assert body["MessageStream"] == "outbound"


def test_cc_bcc_reply_to_headers_and_attachments_are_carried(postmark: Recorder) -> None:
    message = EmailMessage(
        "Hello",
        "Body",
        from_email="ops@sender.test",
        to=["a@example.test", "b@example.test"],
        cc=["c@example.test"],
        bcc=["d@example.test"],
        reply_to=["help@sender.test"],
        headers={"X-Tag": "invite"},
    )
    message.attach("note.txt", "hi", "text/plain")

    postmark_mail.PostmarkEmailBackend().send_messages([message])

    body = _body(postmark.requests[0])
    assert body["To"] == "a@example.test, b@example.test"
    assert body["Cc"] == "c@example.test"
    assert body["Bcc"] == "d@example.test"
    assert body["ReplyTo"] == "help@sender.test"
    assert body["Headers"] == [{"Name": "X-Tag", "Value": "invite"}]
    assert body["Attachments"] == [{"Name": "note.txt", "Content": "aGk=", "ContentType": "text/plain"}]


def test_a_refusal_raises_an_smtp_exception_the_send_sites_already_catch(postmark: Recorder) -> None:
    postmark.status, postmark.body = 422, {"ErrorCode": 400, "Message": "Sender signature not found"}
    message = EmailMessage("Hi", "Body", to=["reader@example.test"])

    with pytest.raises(smtplib.SMTPException):
        postmark_mail.PostmarkEmailBackend().send_messages([message])


def test_fail_silently_counts_instead_of_raising(postmark: Recorder) -> None:
    postmark.status, postmark.body = 500, {"ErrorCode": 500, "Message": "down"}
    message = EmailMessage("Hi", "Body", to=["reader@example.test"])

    assert postmark_mail.PostmarkEmailBackend(fail_silently=True).send_messages([message]) == 0


def test_no_token_is_refused_without_a_call(settings: Any, postmark: Recorder) -> None:
    settings.POSTMARK_SERVER_TOKEN = ""
    with pytest.raises(smtplib.SMTPException, match="POSTMARK_SERVER_TOKEN"):
        postmark_mail.PostmarkEmailBackend().send_messages([EmailMessage("Hi", "Body", to=["x@example.test"])])
    assert postmark.requests == []


def test_the_token_never_reaches_the_error_text(postmark: Recorder) -> None:
    postmark.status, postmark.body = 401, {"ErrorCode": 10, "Message": "Bad token"}
    with pytest.raises(smtplib.SMTPException) as caught:
        postmark_mail.PostmarkEmailBackend().send_messages([EmailMessage("Hi", "Body", to=["x@example.test"])])
    assert TOKEN not in str(caught.value)
