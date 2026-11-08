from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import Settings
from src.db import models  # noqa: F401
from src.db.base import Base
from src.db.models import EmailTemplate, Message, MessageEvent, MessageStatus, Recipient
from src.providers.base import EmailPayload, EmailSendResult
from src import worker


@pytest.fixture
def worker_session_factory() -> Iterator[sessionmaker[Session]]:
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


def test_worker_sends_a_queued_message_once(
    worker_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    with worker_session_factory() as session:
        recipient = Recipient(email="qa@example.com")
        template = EmailTemplate(
            name="receipt",
            version=1,
            subject="Receipt for {{ name }}",
            html_body="<p>Receipt for {{ name }}</p>",
            text_body="Receipt for {{ name }}",
            required_variables=["name"],
            message_type="transactional",
        )
        session.add_all([recipient, template])
        session.flush()
        message = Message(
            recipient_id=recipient.id,
            template_id=template.id,
            idempotency_key="worker-001",
            request_hash="a" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="transactional",
            variables={"name": "Ada"},
        )
        session.add(message)
        session.commit()
        message_id = message.id

    sent_payloads: list[EmailPayload] = []

    class FakeProvider:
        def send(self, payload: EmailPayload) -> EmailSendResult:
            sent_payloads.append(payload)
            return EmailSendResult(provider="test-provider", provider_message_id="provider-123")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", lambda configured_settings: FakeProvider())

    worker.process_message.run(message_id)
    worker.process_message.run(message_id)

    with worker_session_factory() as session:
        persisted = session.get(Message, message_id)
        events = session.query(MessageEvent).filter_by(message_id=message_id).all()

    assert len(sent_payloads) == 1
    assert sent_payloads[0].recipient == "qa@example.com"
    assert sent_payloads[0].subject == "Receipt for Ada"
    assert persisted is not None
    assert persisted.status == MessageStatus.SENT.value
    assert persisted.provider_message_id == "provider-123"
    assert len(events) == 1


def test_worker_marks_suppressed_recipient_without_sending(
    worker_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    with worker_session_factory() as session:
        recipient = Recipient(email="qa@example.com", status="suppressed")
        template = EmailTemplate(
            name="receipt",
            version=1,
            subject="Receipt",
            html_body="<p>Receipt</p>",
            text_body="Receipt",
            required_variables=[],
            message_type="transactional",
        )
        session.add_all([recipient, template])
        session.flush()
        message = Message(
            recipient_id=recipient.id,
            template_id=template.id,
            idempotency_key="worker-002",
            request_hash="b" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="transactional",
            variables={},
        )
        session.add(message)
        session.commit()
        message_id = message.id

    def unexpected_provider(_settings: Settings) -> None:
        pytest.fail("Suppressed recipient must not reach the email provider.")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", unexpected_provider)

    worker.process_message.run(message_id)

    with worker_session_factory() as session:
        persisted = session.get(Message, message_id)

    assert persisted is not None
    assert persisted.status == MessageStatus.FAILED.value
    assert persisted.error == "Recipient is suppressed."
