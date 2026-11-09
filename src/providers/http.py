import base64
import re
from email.utils import parseaddr
from typing import Any

import httpx

from src.config import Settings, validate_recipient
from src.providers.base import (
    EmailAttachment,
    EmailPayload,
    EmailSendResult,
    ProviderPermanentError,
    ProviderTransientError,
)

_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
_RESERVED_HEADERS = {
    "from",
    "to",
    "subject",
    "cc",
    "bcc",
    "reply-to",
    "authorization",
    "api-key",
    "content-type",
}
_ATTACHMENT_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.pdf$")
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


class HttpEmailProvider:
    name: str
    endpoint: str

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._settings = settings
        self._api_key = api_key.strip()
        self._transport = transport
        self._timeout = timeout
        if not self._api_key:
            raise ValueError(f"{self.name.upper()} API key is required.")

    def _headers(self, payload: EmailPayload) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        headers.update(payload.headers)
        return headers

    @staticmethod
    def _validate_payload(settings: Settings, payload: EmailPayload) -> str:
        recipient = validate_recipient(settings, payload.recipient)
        if any("\r" in value or "\n" in value for value in (payload.sender, payload.subject)):
            raise ValueError("Email headers must not contain line breaks.")
        if not payload.sender.strip() or not payload.subject.strip():
            raise ValueError("Sender and subject are required.")
        for name, value in payload.headers.items():
            if (
                not _HEADER_NAME.fullmatch(name)
                or name.casefold() in _RESERVED_HEADERS
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("Additional email headers are invalid.")
        total_attachment_bytes = sum(len(item.content) for item in payload.attachments)
        if total_attachment_bytes > _MAX_ATTACHMENT_BYTES:
            raise ValueError("Email attachments exceed the 5 MB total limit.")
        for attachment in payload.attachments:
            if (
                attachment.content_type != "application/pdf"
                or not _ATTACHMENT_FILENAME.fullmatch(attachment.filename)
                or not attachment.content.startswith(b"%PDF-")
                or not attachment.content.rstrip().endswith(b"%%EOF")
            ):
                raise ValueError("Only validated PDF email attachments are allowed.")
        return recipient

    @staticmethod
    def _attachments(attachments: tuple[EmailAttachment, ...]) -> list[dict[str, str]]:
        return [
            {
                "filename": item.filename,
                "content": base64.b64encode(item.content).decode("ascii"),
            }
            for item in attachments
        ]

    def _message_body(self, payload: EmailPayload, recipient: str) -> dict[str, Any]:
        raise NotImplementedError

    def _message_id(self, response_body: object) -> str:
        raise NotImplementedError

    def send(self, payload: EmailPayload) -> EmailSendResult:
        recipient = self._validate_payload(self._settings, payload)
        request_body = self._message_body(payload, recipient)
        try:
            with httpx.Client(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    self.endpoint,
                    json=request_body,
                    headers=self._headers(payload),
                )
        except httpx.RequestError as error:
            raise ProviderTransientError("Email provider request failed.") from error

        if response.status_code == 429 or response.status_code == 408 or response.status_code >= 500:
            raise ProviderTransientError(
                f"Email provider returned transient HTTP {response.status_code}."
            )
        if not 200 <= response.status_code < 300:
            raise ProviderPermanentError(
                f"Email provider rejected the request with HTTP {response.status_code}."
            )
        try:
            response_body = response.json()
        except ValueError as error:
            raise ProviderPermanentError("Email provider returned invalid JSON.") from error
        provider_message_id = self._message_id(response_body)
        return EmailSendResult(
            provider=self.name,
            provider_message_id=provider_message_id,
        )


def sender_parts(sender: str) -> tuple[str, str | None]:
    name, address = parseaddr(sender)
    if not address or "@" not in address:
        raise ValueError("Sender must contain a valid email address.")
    return address, name or None
