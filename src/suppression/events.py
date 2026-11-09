from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import Message, MessageEvent, Recipient
from src.providers.webhook import ProviderEvent
from src.queue.warmup import pause_promotional_campaigns_if_needed
from src.suppression.service import apply_delivery_event, reputation_snapshot_for_utc_day


class UnknownWebhookMessageError(LookupError):
    pass


def process_provider_event(
    session: Session,
    event: ProviderEvent,
    *,
    provider: str,
    settings: Settings,
) -> bool:
    if not provider or len(provider) > 40:
        raise ValueError("provider must contain between 1 and 40 characters.")
    message = session.get(Message, event.message_id)
    if message is None:
        raise UnknownWebhookMessageError("Webhook message was not found.")
    recipient = session.get(Recipient, message.recipient_id)
    if recipient is None:
        raise UnknownWebhookMessageError("Webhook message recipient was not found.")

    existing = session.scalar(
        select(MessageEvent.id).where(
            MessageEvent.provider == provider,
            MessageEvent.provider_event_id == event.event_id,
        )
    )
    if existing is not None:
        return True

    session.add(
        MessageEvent(
            message_id=message.id,
            event_type=event.event_type,
            provider=provider,
            provider_event_id=event.event_id,
            payload={"provider": provider},
            received_at=datetime.now(timezone.utc),
            occurred_at=event.occurred_at,
        )
    )
    apply_delivery_event(
        session,
        recipient,
        event_type=event.event_type,
        provider=provider,
        occurred_at=event.occurred_at,
    )
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        duplicate = session.scalar(
            select(MessageEvent.id).where(
                MessageEvent.provider == provider,
                MessageEvent.provider_event_id == event.event_id,
            )
        )
        if duplicate is not None:
            return True
        raise

    snapshot = reputation_snapshot_for_utc_day(session, now=event.occurred_at)
    pause_promotional_campaigns_if_needed(session, snapshot, settings)
    session.commit()
    return False
