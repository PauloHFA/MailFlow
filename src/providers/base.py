from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class EmailPayload:
    recipient: str
    sender: str
    subject: str
    html_body: str
    text_body: str
    headers: dict[str, str] = field(default_factory=dict)
    attachments: tuple[EmailAttachment, ...] = ()


@dataclass(frozen=True)
class EmailSendResult:
    provider: str
    provider_message_id: str | None = None


class EmailProvider(Protocol):
    def send(self, payload: EmailPayload) -> EmailSendResult:
        """Submit one fully rendered message to the configured email provider."""


class ProviderTransientError(ConnectionError):
    """A provider failure that is safe to retry."""


class ProviderPermanentError(ValueError):
    """A provider rejected a message for a non-transient reason."""
