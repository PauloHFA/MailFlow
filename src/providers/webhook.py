import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

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
    "unsubscribed",
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


_RESEND_EVENT_TYPES = {
    "email.delivered": "delivered",
    "email.bounced": "hard_bounce",
    "email.delivery_delayed": "soft_bounce",
    "email.complained": "complaint",
    "email.suppressed": "unsubscribed",
    "email.opened": "opened",
    "email.clicked": "clicked",
}
_BREVO_EVENT_TYPES = {
    "delivered": "delivered",
    "hard_bounce": "hard_bounce",
    "soft_bounce": "soft_bounce",
    "spam": "complaint",
    "unsubscribed": "unsubscribed",
    "opened": "opened",
    "unique_opened": "opened",
    "click": "clicked",
}


def verify_resend_webhook_signature(
    raw_body: bytes,
    *,
    message_id: str,
    timestamp: str,
    signature: str,
    secret: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> None:
    if not message_id or not message_id.isascii() or len(message_id) > 255:
        raise WebhookSignatureError("Resend webhook message ID is invalid.")
    if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12:
        raise WebhookSignatureError("Resend webhook timestamp is invalid.")
    if not secret.startswith("whsec_"):
        raise WebhookSignatureError("Resend webhook signing secret is not configured.")
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError) as error:
        raise WebhookSignatureError("Resend webhook signing secret is invalid.") from error
    if len(key) < 32:
        raise WebhookSignatureError("Resend webhook signing secret is invalid.")

    issued_at = int(timestamp)
    current_time = int(time.time()) if now is None else now
    if issued_at > current_time + 30 or current_time - issued_at > tolerance_seconds:
        raise WebhookSignatureError("Resend webhook timestamp is outside the accepted window.")

    signed_content = b".".join(
        (message_id.encode("ascii"), timestamp.encode("ascii"), raw_body)
    )
    expected = base64.b64encode(
        hmac.new(key, signed_content, hashlib.sha256).digest()
    ).decode("ascii")
    if len(signature) > 4096:
        raise WebhookSignatureError("Resend webhook signature is invalid.")
    candidates: list[str] = []
    for versioned_signature in signature.split()[:8]:
        version, separator, value = versioned_signature.partition(",")
        if separator and version == "v1" and value.isascii() and len(value) <= 128:
            candidates.append(value)
    if not candidates or not any(
        hmac.compare_digest(expected, candidate) for candidate in candidates
    ):
        raise WebhookSignatureError("Resend webhook signature is invalid.")


def parse_resend_webhook(
    raw_body: bytes,
    *,
    event_id: str,
) -> ProviderEvent:
    try:
        if not event_id or len(event_id) > 255:
            raise ValueError
        payload = json.loads(raw_body)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ValueError
        provider_event_type = payload.get("type")
        provider_message_id = payload["data"].get("email_id")
        occurred_at = datetime.fromisoformat(
            str(payload.get("created_at", "")).replace("Z", "+00:00")
        )
        if (
            not isinstance(provider_event_type, str)
            or not isinstance(provider_message_id, str)
            or not provider_message_id
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() is None
        ):
            raise ValueError
        event_type = _RESEND_EVENT_TYPES[provider_event_type]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise WebhookPayloadError("Resend webhook payload is invalid or unsupported.") from error

    return ProviderEvent(
        event_id=event_id,
        event_type=event_type,
        message_id=provider_message_id,
        occurred_at=occurred_at.astimezone(timezone.utc),
    )


def parse_brevo_webhook(raw_body: bytes) -> ProviderEvent:
    try:
        payload: Any = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError
        provider_event_type = payload.get("event")
        provider_message_id = payload.get("message-id")
        timestamp = payload.get("ts_event")
        custom_message_id = payload.get("X-Mailin-custom")
        if (
            not isinstance(provider_event_type, str)
            or not isinstance(provider_message_id, str)
            or not provider_message_id
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (str, int))
            or not str(timestamp).isdecimal()
            or len(str(timestamp)) > 12
        ):
            raise ValueError
        event_type = _BREVO_EVENT_TYPES[provider_event_type.casefold()]
        occurred_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        message_id = (
            custom_message_id
            if isinstance(custom_message_id, str) and custom_message_id
            else provider_message_id
        )
        event_id = hashlib.sha256(raw_body).hexdigest()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OverflowError) as error:
        raise WebhookPayloadError("Brevo webhook payload is invalid or unsupported.") from error

    return ProviderEvent(
        event_id=event_id,
        event_type=event_type,
        message_id=message_id,
        occurred_at=occurred_at,
    )
