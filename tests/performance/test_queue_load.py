from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.db import models  # noqa: F401
from src.db.base import Base
from src.db.models import EmailTemplate, Message, Recipient
from src.queue.retries import dispatch_pending_messages

_LOAD_SIZE = 5000


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
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


@pytest.mark.performance
def test_dispatches_five_thousand_queued_messages_without_duplicates(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        recipient = Recipient(email="load-test@example.invalid")
        template = EmailTemplate(
            name="queue-load",
            version=1,
            subject="Load test",
            html_body="<p>Load test</p>",
            text_body="Load test",
            required_variables=[],
        )
        session.add_all([recipient, template])
        session.flush()
        session.add_all(
            Message(
                recipient_id=recipient.id,
                template_id=template.id,
                idempotency_key=f"load-{index}",
                request_hash=f"{index:064x}",
                status="queued",
                message_type="transactional",
                variables={},
            )
            for index in range(_LOAD_SIZE)
        )
        session.commit()

    published: list[str] = []
    total_dispatched = 0
    while total_dispatched < _LOAD_SIZE:
        count = dispatch_pending_messages(
            factory,
            now=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc),
            enqueue=published.append,
            batch_size=500,
        )
        assert count > 0
        total_dispatched += count

    assert total_dispatched == _LOAD_SIZE
    assert len(published) == _LOAD_SIZE
    assert len(set(published)) == _LOAD_SIZE
    assert (
        dispatch_pending_messages(
            factory,
            now=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc),
            enqueue=published.append,
            batch_size=500,
        )
        == 0
    )
    with factory() as session:
        assert session.scalar(select(Message).where(Message.enqueued_at.is_(None))) is None
