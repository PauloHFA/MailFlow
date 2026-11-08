import smtplib
from email.message import EmailMessage

from src.config import Settings, validate_recipient
from src.providers.base import EmailPayload, EmailSendResult


class MailpitSMTPProvider:
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
        message.set_content(payload.text_body)
        message.add_alternative(payload.html_body, subtype="html")
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
