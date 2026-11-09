from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    Message,
    MessageEvent,
    Recipient,
    RecipientStatus,
    Suppression,
)
from src.queue.warmup import ReputationSnapshot

_BOUNCE_STREAK_EVENTS = (
    "delivered",
    "soft_bounce",
    "hard_bounce",
    "complaint",
    "unsubscribed",
)


def _suppress_recipient(
    session: Session,
    recipient: Recipient,
    *,
    reason: str,
    source: str,
    now: datetime,
) -> None:
    suppression = session.scalar(
        select(Suppression).where(Suppression.email == recipient.email)
    )
    if suppression is None:
        session.add(
            Suppression(
                email=recipient.email,
                reason=reason,
                source=source,
                created_at=now,
            )
        )
    recipient.status = RecipientStatus.SUPPRESSED.value


def apply_delivery_event(
    session: Session,
    recipient: Recipient,
    *,
    event_type: str,
    provider: str,
    occurred_at: datetime,
) -> bool:
    if event_type in {"hard_bounce", "complaint", "unsubscribed"}:
        _suppress_recipient(
            session,
            recipient,
            reason=event_type,
            source=f"webhook:{provider}",
            now=occurred_at,
        )
        return True

    if event_type != "soft_bounce":
        return False

    recent_events = session.scalars(
        select(MessageEvent.event_type)
        .join(Message, Message.id == MessageEvent.message_id)
        .where(
            Message.recipient_id == recipient.id,
            MessageEvent.event_type.in_(_BOUNCE_STREAK_EVENTS),
        )
        .order_by(
            MessageEvent.occurred_at.desc(),
            MessageEvent.received_at.desc(),
            MessageEvent.id.desc(),
        )
        .limit(3)
    ).all()
    if len(recent_events) == 3 and all(
        recent_event == "soft_bounce" for recent_event in recent_events
    ):
        _suppress_recipient(
            session,
            recipient,
            reason="soft_bounce",
            source=f"webhook:{provider}",
            now=occurred_at,
        )
        return True
    return False


def reputation_snapshot_for_utc_day(
    session: Session,
    *,
    now: datetime,
) -> ReputationSnapshot:
    utc_now = now.astimezone(timezone.utc)
    since = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    event_types = session.scalars(
        select(MessageEvent.event_type).where(
            MessageEvent.occurred_at >= since,
            MessageEvent.occurred_at <= utc_now,
        )
    ).all()
    sent = event_types.count("sent")
    bounces = event_types.count("soft_bounce") + event_types.count("hard_bounce")
    complaints = event_types.count("complaint")
    return ReputationSnapshot(sent=sent, bounces=bounces, complaints=complaints)
