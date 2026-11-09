from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.db import models  # noqa: F401
from src.db.base import Base
from src.db.models import (
    DeadLetter,
    EmailTemplate,
    Message,
    MessageEvent,
    MessageStatus,
    Recipient,
    Campaign,
    CampaignStatus,
)
from src.queue.retries import dispatch_pending_messages, recover_stale_messages
from src.queue.warmup import (
    ReputationSnapshot,
    pause_promotional_campaigns_if_needed,
)
from src.config import Settings


@pytest.fixture
def queue_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def make_queued_message(factory: sessionmaker[Session], *, status: str = "queued") -> str:
    with factory() as session:
        recipient = Recipient(email="qa@example.com")
        template = EmailTemplate(
            name="queue",
            version=1,
            subject="Hello",
            html_body="<p>Hello</p>",
            text_body="Hello",
            required_variables=[],
        )
        session.add_all([recipient, template])
        session.flush()
        message = Message(
            recipient_id=recipient.id,
            template_id=template.id,
            idempotency_key=f"queue-{status}",
            request_hash="c" * 64,
            status=status,
            message_type="transactional",
            variables={},
            attempts=1 if status == MessageStatus.RETRY.value else 0,
            next_attempt_at=(
                datetime(2026, 11, 8, 11, 59, tzinfo=timezone.utc)
                if status == MessageStatus.RETRY.value
                else None
            ),
        )
        session.add(message)
        session.commit()
        return message.id


def test_due_retry_is_published_once_and_reset_to_queued(
    queue_factory: sessionmaker[Session],
) -> None:
    message_id = make_queued_message(queue_factory, status=MessageStatus.RETRY.value)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    published: list[str] = []

    assert dispatch_pending_messages(queue_factory, now=now, enqueue=published.append) == 1
    assert dispatch_pending_messages(queue_factory, now=now, enqueue=published.append) == 0
    assert published == [message_id]
    with queue_factory() as session:
        message = session.get(Message, message_id)
    assert message is not None
    assert message.status == MessageStatus.QUEUED.value
    assert message.next_attempt_at is None
    assert message.enqueued_at is not None


def test_stale_worker_claim_without_idempotency_is_marked_unknown(
    queue_factory: sessionmaker[Session],
) -> None:
    message_id = make_queued_message(queue_factory, status=MessageStatus.QUEUED.value)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    with queue_factory() as session:
        message = session.get(Message, message_id)
        assert message is not None
        message.status = MessageStatus.PROCESSING.value
        message.attempts = 5
        message.processing_started_at = now - timedelta(minutes=10)
        session.commit()

    recovered, dead_lettered = recover_stale_messages(
        queue_factory,
        now=now,
        visibility_timeout_seconds=300,
        max_attempts=5,
    )

    assert (recovered, dead_lettered) == (0, 0)
    with queue_factory() as session:
        message = session.get(Message, message_id)
        letter = session.scalar(select(DeadLetter).where(DeadLetter.message_id == message_id))
        events = session.scalars(
            select(MessageEvent).where(MessageEvent.message_id == message_id)
        ).all()
    assert message is not None
    assert message.status == MessageStatus.UNKNOWN.value
    assert letter is None
    assert any(event.event_type == "delivery_unknown" for event in events)


def test_stale_worker_claim_with_idempotency_is_safe_to_recover(
    queue_factory: sessionmaker[Session],
) -> None:
    message_id = make_queued_message(queue_factory, status=MessageStatus.QUEUED.value)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    with queue_factory() as session:
        message = session.get(Message, message_id)
        assert message is not None
        message.status = MessageStatus.PROCESSING.value
        message.attempts = 1
        message.processing_started_at = now - timedelta(minutes=10)
        session.commit()

    recovered, dead_lettered = recover_stale_messages(
        queue_factory,
        now=now,
        visibility_timeout_seconds=300,
        max_attempts=5,
        provider_supports_idempotency=True,
    )

    assert (recovered, dead_lettered) == (1, 0)
    with queue_factory() as session:
        message = session.get(Message, message_id)
    assert message is not None
    assert message.status == MessageStatus.RETRY.value


def test_stale_worker_claim_past_provider_idempotency_window_is_unknown(
    queue_factory: sessionmaker[Session],
) -> None:
    message_id = make_queued_message(queue_factory, status=MessageStatus.QUEUED.value)
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    with queue_factory() as session:
        message = session.get(Message, message_id)
        assert message is not None
        message.status = MessageStatus.PROCESSING.value
        message.attempts = 1
        message.processing_started_at = now - timedelta(hours=25)
        session.commit()

    recovered, dead_lettered = recover_stale_messages(
        queue_factory,
        now=now,
        visibility_timeout_seconds=300,
        max_attempts=5,
        provider_supports_idempotency=True,
    )

    assert (recovered, dead_lettered) == (0, 0)
    with queue_factory() as session:
        message = session.get(Message, message_id)
    assert message is not None
    assert message.status == MessageStatus.UNKNOWN.value


def test_reputation_threshold_pauses_only_promotional_campaigns(
    queue_factory: sessionmaker[Session],
) -> None:
    with queue_factory() as session:
        transactional = Campaign(
            name="transactional",
            template_id="template-placeholder",
            message_type="transactional",
            status=CampaignStatus.ACTIVE.value,
            variables={},
        )
        promotional = Campaign(
            name="promotional",
            template_id="template-placeholder",
            message_type="promotional",
            status=CampaignStatus.ACTIVE.value,
            variables={},
        )
        template = EmailTemplate(
            id="template-placeholder",
            name="reputation",
            version=1,
            subject="Hello",
            html_body="<p>Hello</p>",
            text_body="Hello",
            required_variables=[],
        )
        session.add_all([template, transactional, promotional])
        session.commit()

        paused_count = pause_promotional_campaigns_if_needed(
            session,
            ReputationSnapshot(sent=100, bounces=6, complaints=0),
            Settings(_env_file=None, reputation_bounce_threshold=0.05),
        )

        session.refresh(transactional)
        session.refresh(promotional)
        assert paused_count == 1
        assert transactional.status == CampaignStatus.ACTIVE.value
        assert promotional.status == CampaignStatus.PAUSED.value
