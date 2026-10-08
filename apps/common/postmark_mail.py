"""Django's system mail — invites, password resets, notifications — through Postmark's API.

``EMAIL_BACKEND_TYPE=postmark`` selects it. The reason to want it over SMTP is
the host: Railway below its Pro plan blocks every outbound SMTP port, so on
such a deployment an HTTPS API is the only way the application can send mail
at all. The email *channel* has its own Postmark provider
(``apps.channels.providers.email_backends``); this is the deployment's own mail.

Calls go through ``apps.channels.providers.base.request_json``, one of the two
modules allowed to issue HTTP (``tests/test_ssrf_call_sites.py``), to a host
built from a constant.

**Failures surface as** ``smtplib.SMTPException``. Every send site in the
project — ``notifications.mail.send_delivery``, the invite in
``members.services``, allauth's adapter — catches ``(OSError,
smtplib.SMTPException)``, because until this existed SMTP was the only real
backend. Raising the same family keeps all of them correct without touching
any of them.
"""

import base64
import smtplib
from collections.abc import Sequence
from email.utils import formataddr, parseaddr
from typing import Any

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage, EmailMultiAlternatives

API_ROOT = "https://api.postmarkapp.com"
DEFAULT_STREAM = "outbound"
TIMEOUT = 30.0


class PostmarkSendError(smtplib.SMTPException):
    """Postmark refused a message, or could not be reached."""


class PostmarkEmailBackend(BaseEmailBackend):
    """``POST /email`` for each message. No connection to open or close."""

    def __init__(self, fail_silently: bool = False, **kwargs: Any) -> None:
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.token: str = getattr(settings, "POSTMARK_SERVER_TOKEN", "") or ""
        self.stream: str = getattr(settings, "POSTMARK_MESSAGE_STREAM", "") or DEFAULT_STREAM

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        sent = 0
        for message in email_messages:
            try:
                self._send(message)
            except PostmarkSendError:
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent

    def _send(self, message: EmailMessage) -> None:
        from apps.channels.providers.base import request_json
        from apps.channels.providers.exceptions import APIError

        if not self.token:
            raise PostmarkSendError("POSTMARK_SERVER_TOKEN is not set.")
        if not message.recipients():
            return
        try:
            request_json(
                "POST",
                f"{API_ROOT}/email",
                json=self.payload(message),
                headers={
                    "X-Postmark-Server-Token": self.token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT,
            )
        except APIError as exc:
            # The exception text carries status and code only — never the token,
            # which rode in a header — so it is safe to chain.
            raise PostmarkSendError(str(exc)) from exc

    def payload(self, message: EmailMessage) -> dict[str, Any]:
        """The ``POST /email`` body for one Django message."""
        body: dict[str, Any] = {
            "From": _address(message.from_email or settings.DEFAULT_FROM_EMAIL),
            "To": ", ".join(message.to),
            "Subject": message.subject,
            "MessageStream": self.stream,
        }
        if message.cc:
            body["Cc"] = ", ".join(message.cc)
        if message.bcc:
            body["Bcc"] = ", ".join(message.bcc)
        if message.reply_to:
            body["ReplyTo"] = ", ".join(message.reply_to)

        html = ""
        if isinstance(message, EmailMultiAlternatives):
            html = next((str(content) for content, mimetype in message.alternatives if mimetype == "text/html"), "")
        if message.content_subtype == "html":
            html = html or str(message.body)
        else:
            body["TextBody"] = str(message.body)
        if html:
            body["HtmlBody"] = html

        if message.extra_headers:
            body["Headers"] = [
                {"Name": str(name), "Value": str(value)} for name, value in message.extra_headers.items()
            ]

        attachments = [_attachment(item) for item in message.attachments]
        attachments = [item for item in attachments if item]
        if attachments:
            body["Attachments"] = attachments
        return body


def _address(value: str) -> str:
    """``Name <addr>`` normalised, so a display name with a comma survives."""
    name, addr = parseaddr(value)
    return formataddr((name, addr)) if addr else value


def _attachment(item: Any) -> dict[str, str] | None:
    """A ``(filename, content, mimetype)`` attachment as Postmark wants it, base64."""
    if not isinstance(item, tuple) or len(item) != 3:
        return None
    filename, content, mimetype = item
    raw = content.encode() if isinstance(content, str) else bytes(content)
    return {
        "Name": str(filename or "attachment"),
        "Content": base64.b64encode(raw).decode("ascii"),
        "ContentType": str(mimetype or "application/octet-stream"),
    }
