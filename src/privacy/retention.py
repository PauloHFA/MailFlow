import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from src.config import Settings
from src.db.models import (
    CampaignSchedule,
    Consent,
    DeadLetter,
    Message,
    MessageEvent,
    Recipient,
    Suppression,
)
from src.privacy.suppression import suppression_digest

logger = logging.getLogger("mailflow.privacy")
_PURGE_BATCH_SIZE = 1000


def purge_expired_messages(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    now: datetime,
) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Retention time must include a timezone.")
    if settings.message_retention_days == 0:
        logger.info("message_retention_disabled")
        return 0

    cutoff = now.astimezone(timezone.utc) - timedelta(
        days=settings.message_retention_days
    )
    with session_factory() as session:
        expired_ids = session.scalars(
            select(Message.id)
            .where(Message.created_at < cutoff)
            .order_by(Message.created_at, Message.id)
            .limit(_PURGE_BATCH_SIZE)
        ).all()
        if not expired_ids:
            return 0
        session.execute(
            delete(MessageEvent).where(MessageEvent.message_id.in_(expired_ids))
        )
        session.execute(
            delete(DeadLetter).where(DeadLetter.message_id.in_(expired_ids))
        )
        deleted_count = session.execute(
            delete(Message).where(Message.id.in_(expired_ids))
        ).rowcount
        session.commit()

    logger.info("expired_message_data_purged", extra={"deleted_count": deleted_count})
    return int(deleted_count or 0)


def erase_recipient_data(
    session: Session,
    recipient_id: str,
    *,
    suppression_hash_secret: str,
) -> bool:
    recipient = session.get(Recipient, recipient_id)
    if recipient is None:
        return False

    suppression_rows = session.scalars(
        select(Suppression).where(Suppression.email == recipient.email.casefold())
    ).all()
    if suppression_rows and len(suppression_hash_secret.encode("utf-8")) < 32:
        raise ValueError(
            "SUPPRESSION_HASH_SECRET must contain at least 32 bytes to erase suppressed recipients."
        )

    message_ids = session.scalars(
        select(Message.id).where(Message.recipient_id == recipient_id)
    ).all()
    if message_ids:
        session.execute(
            delete(MessageEvent).where(MessageEvent.message_id.in_(message_ids))
        )
        session.execute(
            delete(DeadLetter).where(DeadLetter.message_id.in_(message_ids))
        )
        session.execute(delete(Message).where(Message.id.in_(message_ids)))

    session.execute(
        delete(CampaignSchedule).where(
            CampaignSchedule.recipient_id == recipient_id
        )
    )
    session.execute(delete(Consent).where(Consent.recipient_id == recipient_id))
    for suppression in suppression_rows:
        suppression.email = suppression_digest(
            recipient.email,
            suppression_hash_secret,
        )
    session.delete(recipient)
    return True
