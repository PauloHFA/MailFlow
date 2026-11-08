from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailPayload:
    recipient: str
    sender: str
    subject: str
    html_body: str
    text_body: str


@dataclass(frozen=True)
class EmailSendResult:
    provider: str
    provider_message_id: str | None = None


class EmailProvider(Protocol):
    def send(self, payload: EmailPayload) -> EmailSendResult:
        """Submit one fully rendered message to the configured email provider."""
