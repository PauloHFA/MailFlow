import re
import smtplib
from email.message import EmailMessage

from src.config import Settings, validate_recipient
from src.providers.base import EmailPayload, EmailSendResult


class MailpitSMTPProvider:
    _HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
    _RESERVED_HEADERS = {"from", "to", "subject", "cc", "bcc", "reply-to"}
    _ATTACHMENT_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.pdf$")
    _MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

    def __init__(self, settings: Settings, *, timeout: float = 10.0) -> None:
        self._settings = settings
        self._timeout = timeout

    def build_message(self, payload: EmailPayload) -> EmailMessage:
        recipient = validate_recipient(self._settings, payload.recipient)
        if any("\r" in value or "\n" in value for value in (payload.sender, payload.subject)):
            raise ValueError("Email headers must not contain line breaks.")
        if not payload.sender.strip() or not payload.subject.strip():
            raise ValueError("Sender and subject are required.")

        message = EmailMessage()
        message["From"] = payload.sender
        message["To"] = recipient
        message["Subject"] = payload.subject
        for name, value in payload.headers.items():
            if (
                not self._HEADER_NAME.fullmatch(name)
                or name.casefold() in self._RESERVED_HEADERS
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("Additional email headers are invalid.")
            message[name] = value
        message.set_content(payload.text_body)
        message.add_alternative(payload.html_body, subtype="html")
        total_attachment_bytes = sum(len(item.content) for item in payload.attachments)
        if total_attachment_bytes > self._MAX_ATTACHMENT_BYTES:
            raise ValueError("Email attachments exceed the 5 MB total limit.")
        for attachment in payload.attachments:
            if (
                attachment.content_type != "application/pdf"
                or not self._ATTACHMENT_FILENAME.fullmatch(attachment.filename)
                or not attachment.content.startswith(b"%PDF-")
                or not attachment.content.rstrip().endswith(b"%%EOF")
            ):
                raise ValueError("Only validated PDF email attachments are allowed.")
            message.add_attachment(
                attachment.content,
                maintype="application",
                subtype="pdf",
                filename=attachment.filename,
            )
        return message

    def send(self, payload: EmailPayload) -> EmailSendResult:
        message = self.build_message(payload)
        with smtplib.SMTP(
            self._settings.smtp_host,
            self._settings.smtp_port,
            timeout=self._timeout,
        ) as client:
            client.send_message(message)
        return EmailSendResult(provider="mailpit-smtp", provider_message_id=message.get("Message-ID"))
