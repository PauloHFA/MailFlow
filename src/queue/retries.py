import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from kombu.exceptions import OperationalError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import (
    Campaign,
    CampaignStatus,
    DeadLetter,
    Message,
    MessageEvent,
    MessageStatus,
)

logger = logging.getLogger("mailflow.queue")


def compute_retry_delay(attempt: int, *, base_seconds: int) -> int:
    if attempt < 1:
        raise ValueError("attempt must be positive.")
    if base_seconds < 1:
        raise ValueError("base_seconds must be positive.")
    return min(base_seconds * (2 ** (attempt - 1)), 3600)


def _dead_letter(session: Session, message: Message, reason: str) -> None:
    message.status = MessageStatus.FAILED.value
    message.error = reason
    message.next_attempt_at = None
    message.processing_started_at = None
    session.add(
        MessageEvent(
            message_id=message.id,
            event_type="dead_lettered",
            payload={"attempts": message.attempts},
        )
    )
    if session.scalar(select(DeadLetter.id).where(DeadLetter.message_id == message.id)) is None:
        session.add(
            DeadLetter(message_id=message.id, attempts=message.attempts, reason=reason)
        )


def dead_letter_message(session: Session, message: Message, reason: str) -> None:
    _dead_letter(session, message, reason)


def dispatch_pending_messages(
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    enqueue: Callable[[str], None],
    batch_size: int = 500,
) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    if batch_size < 1:
        raise ValueError("batch_size must be positive.")
    current_time = now.astimezone(timezone.utc)
    dispatched = 0

    with session_factory() as session:
        candidates = session.scalars(
            select(Message)
            .where(
                or_(
                    (
                        (Message.status == MessageStatus.QUEUED.value)
                        & Message.enqueued_at.is_(None)
                    ),
                    (
                        (Message.status == MessageStatus.RETRY.value)
                        & (Message.next_attempt_at <= current_time)
                    ),
                )
            )
            .order_by(Message.created_at, Message.id)
            .limit(batch_size)
        ).all()

        for message in candidates:
            campaign = (
                session.get(Campaign, message.campaign_id)
                if message.campaign_id is not None
                else None
            )
            if campaign is not None and campaign.status in (
                CampaignStatus.PAUSED.value,
                CampaignStatus.CANCELLED.value,
            ):
                if campaign.status == CampaignStatus.CANCELLED.value:
                    message.status = MessageStatus.CANCELLED.value
                    message.next_attempt_at = None
                    message.enqueued_at = None
                    session.commit()
                continue

            if message.status == MessageStatus.RETRY.value:
                message.status = MessageStatus.QUEUED.value
                message.next_attempt_at = None
                message.enqueued_at = None

            try:
                enqueue(message.id)
            except OperationalError as error:
                session.rollback()
                logger.error(
                    "message_enqueue_deferred",
                    extra={"message_id": message.id, "error_type": type(error).__name__},
                )
                continue

            message.enqueued_at = current_time
            session.commit()
            dispatched += 1

    return dispatched


def recover_stale_messages(
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    visibility_timeout_seconds: int,
    max_attempts: int,
    batch_size: int = 500,
) -> tuple[int, int]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    if visibility_timeout_seconds < 1 or max_attempts < 1 or batch_size < 1:
        raise ValueError("timeouts, attempts, and batch_size must be positive.")
    current_time = now.astimezone(timezone.utc)
    stale_before = current_time - timedelta(seconds=visibility_timeout_seconds)
    recovered = 0
    dead_lettered = 0

    with session_factory() as session:
        messages = session.scalars(
            select(Message)
            .where(
                Message.status == MessageStatus.PROCESSING.value,
                Message.processing_started_at <= stale_before,
            )
            .order_by(Message.processing_started_at, Message.id)
            .limit(batch_size)
        ).all()
        for message in messages:
            if message.attempts >= max_attempts:
                _dead_letter(session, message, "Worker visibility timeout exhausted attempts.")
                dead_lettered += 1
            else:
                message.status = MessageStatus.RETRY.value
                message.processing_started_at = None
                message.next_attempt_at = current_time
                message.enqueued_at = None
                session.add(
                    MessageEvent(
                        message_id=message.id,
                        event_type="retry_scheduled",
                        payload={"reason": "worker_visibility_timeout"},
                    )
                )
                recovered += 1
        session.commit()

    return recovered, dead_lettered


def schedule_retry(
    message: Message,
    *,
    now: datetime,
    base_seconds: int,
    max_attempts: int,
    reason: str,
    session: Session,
) -> bool:
    if message.attempts >= max_attempts:
        _dead_letter(session, message, reason)
        return False
    message.status = MessageStatus.RETRY.value
    message.error = reason
    message.next_attempt_at = now + timedelta(
        seconds=compute_retry_delay(message.attempts, base_seconds=base_seconds)
    )
    message.processing_started_at = None
    message.enqueued_at = None
    session.add(
        MessageEvent(
            message_id=message.id,
            event_type="retry_scheduled",
            payload={"attempt": message.attempts},
        )
    )
    return True
