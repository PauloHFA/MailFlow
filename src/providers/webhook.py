import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class WebhookSignatureError(ValueError):
    pass


class WebhookPayloadError(ValueError):
    pass


_SIGNATURE_PATTERN = re.compile(r"^sha256=([0-9a-f]{64})$")


def verify_webhook_signature(
    raw_body: bytes,
    *,
    timestamp: str,
    signature: str,
    secret: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> None:
    if len(secret.encode("utf-8")) < 32:
        raise WebhookSignatureError("Webhook signing secret is not configured securely.")
    if len(timestamp) > 12 or not timestamp.isascii() or not timestamp.isdecimal():
        raise WebhookSignatureError("Webhook timestamp is invalid.")
    match = _SIGNATURE_PATTERN.fullmatch(signature)
    if match is None:
        raise WebhookSignatureError("Webhook signature is invalid.")

    issued_at = int(timestamp)
    current_time = int(time.time()) if now is None else now
    if issued_at > current_time + 30 or current_time - issued_at > tolerance_seconds:
        raise WebhookSignatureError("Webhook signature timestamp is outside the accepted window.")

    signed_content = timestamp.encode("ascii") + b"." + raw_body
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_content,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(match.group(1), expected):
        raise WebhookSignatureError("Webhook signature is invalid.")


class _GenericWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=40)
    message_id: str = Field(min_length=36, max_length=36)
    occurred_at: datetime
    bounce_type: str | None = Field(default=None, max_length=16)

    @field_validator("event_id", "event_type", "message_id", "bounce_type")
    @classmethod
    def reject_line_breaks(cls, value: str | None) -> str | None:
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("Webhook fields must not contain line breaks.")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone.")
        return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    event_type: str
    message_id: str
    occurred_at: datetime


_SUPPORTED_EVENTS = {
    "delivered",
    "hard_bounce",
    "soft_bounce",
    "complaint",
    "opened",
    "clicked",
}


def parse_generic_webhook(raw_body: bytes) -> ProviderEvent:
    try:
        payload = _GenericWebhookPayload.model_validate_json(raw_body)
    except (ValidationError, ValueError) as error:
        raise WebhookPayloadError("Webhook payload is invalid.") from error

    event_type = payload.event_type.casefold()
    if event_type == "bounce":
        if payload.bounce_type is None:
            raise WebhookPayloadError("Bounce events require bounce_type.")
        bounce_type = payload.bounce_type.casefold()
        if bounce_type in {"hard", "hard_bounce"}:
            event_type = "hard_bounce"
        elif bounce_type in {"soft", "soft_bounce"}:
            event_type = "soft_bounce"
        else:
            raise WebhookPayloadError("bounce_type must be hard or soft.")
    if event_type not in _SUPPORTED_EVENTS:
        raise WebhookPayloadError("Webhook event_type is not supported.")

    return ProviderEvent(
        event_id=payload.event_id,
        event_type=event_type,
        message_id=payload.message_id,
        occurred_at=payload.occurred_at,
    )
