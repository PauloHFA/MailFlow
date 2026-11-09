from collections.abc import Iterator
from datetime import datetime, timezone
import smtplib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from urllib.parse import parse_qs, urlsplit

from src.config import Settings
from src.db import models  # noqa: F401
from src.db.base import Base
from src.db.models import (
    Campaign,
    Consent,
    DeadLetter,
    EmailTemplate,
    Message,
    MessageEvent,
    MessageStatus,
    Recipient,
)
from src.providers.base import EmailPayload, EmailSendResult
from src import worker
from src.suppression.unsubscribe import verify_unsubscribe_token


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, int]] = {}

    def eval(self, _script: str, key_count: int, *values: object) -> list[int]:
        keys = [str(value) for value in values[:key_count]]
        limits = [int(value) for value in values[key_count::2]]
        for key, limit in zip(keys, limits, strict=True):
            if self.hashes.get(key, {}).get("count", 0) >= limit:
                return [0, 1, keys.index(key) + 1]
        for key in keys:
            bucket = self.hashes.setdefault(key, {})
            bucket["count"] = bucket.get("count", 0) + 1
        return [1, 0]

    def hincrby(self, key: str, field: str, amount: int) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    def expire(self, _key: str, _seconds: int) -> bool:
        return True

    def hgetall(self, key: str) -> dict[str, int]:
        return self.hashes.get(key, {})

    def close(self) -> None:
        return None


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
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
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
    redis = FakeRedis()
    monkeypatch.setattr(worker, "redis_client", lambda redis_url: redis)

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


def test_worker_rechecks_promotional_consent_and_adds_one_click_headers(
    worker_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "test-unsubscribe-signing-secret-that-is-long-enough"
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
        unsubscribe_signing_secret=secret,
        unsubscribe_base_url="https://mailflow.example/api/v1/unsubscribe",
    )
    with worker_session_factory() as session:
        recipient = Recipient(email="qa@example.com")
        template = EmailTemplate(
            name="newsletter",
            version=1,
            subject="Monthly news",
            html_body="<p>Monthly news</p>",
            text_body="Monthly news",
            required_variables=[],
            message_type="promotional",
        )
        session.add_all([recipient, template])
        session.flush()
        consent = Consent(
            recipient_id=recipient.id,
            purpose="monthly-updates",
            legal_basis="consent",
            source="signup",
        )
        campaign = Campaign(
            name="newsletter",
            template_id=template.id,
            message_type="promotional",
            purpose="monthly-updates",
            status="active",
            variables={},
        )
        session.add_all([consent, campaign])
        session.flush()
        message = Message(
            recipient_id=recipient.id,
            template_id=template.id,
            campaign_id=campaign.id,
            idempotency_key="worker-promotional",
            request_hash="e" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="promotional",
            variables={},
        )
        session.add(message)
        session.commit()
        message_id = message.id
        consent_id = consent.id

    sent_payloads: list[EmailPayload] = []

    class FakeProvider:
        def send(self, payload: EmailPayload) -> EmailSendResult:
            sent_payloads.append(payload)
            return EmailSendResult(provider="test-provider", provider_message_id="provider-456")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", lambda configured_settings: FakeProvider())
    monkeypatch.setattr(worker, "redis_client", lambda redis_url: FakeRedis())

    worker.process_message.run(message_id)

    assert len(sent_payloads) == 1
    headers = sent_payloads[0].headers
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    url = headers["List-Unsubscribe"].removeprefix("<").removesuffix(">")
    token = parse_qs(urlsplit(url).query)["token"][0]
    assert verify_unsubscribe_token(token, secret) == consent_id


def test_worker_blocks_promotional_message_after_consent_revocation(
    worker_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
        unsubscribe_signing_secret="test-unsubscribe-signing-secret-that-is-long-enough",
    )
    with worker_session_factory() as session:
        recipient = Recipient(email="qa@example.com")
        template = EmailTemplate(
            name="newsletter",
            version=1,
            subject="Monthly news",
            html_body="<p>Monthly news</p>",
            text_body="Monthly news",
            required_variables=[],
            message_type="promotional",
        )
        session.add_all([recipient, template])
        session.flush()
        consent = Consent(
            recipient_id=recipient.id,
            purpose="monthly-updates",
            legal_basis="consent",
            source="signup",
            revoked_at=datetime.now(timezone.utc),
        )
        campaign = Campaign(
            name="newsletter",
            template_id=template.id,
            message_type="promotional",
            purpose="monthly-updates",
            status="active",
            variables={},
        )
        session.add_all([consent, campaign])
        session.flush()
        message = Message(
            recipient_id=recipient.id,
            template_id=template.id,
            campaign_id=campaign.id,
            idempotency_key="worker-promotional-revoked",
            request_hash="f" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="promotional",
            variables={},
        )
        session.add(message)
        session.commit()
        message_id = message.id

    class UnexpectedProvider:
        def __init__(self, _settings: Settings) -> None:
            pass

        def send(self, _payload: EmailPayload) -> EmailSendResult:
            pytest.fail("Promotional send must be blocked after consent revocation.")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", UnexpectedProvider)
    monkeypatch.setattr(worker, "redis_client", lambda redis_url: FakeRedis())

    worker.process_message.run(message_id)

    with worker_session_factory() as session:
        persisted = session.get(Message, message_id)
    assert persisted is not None
    assert persisted.status == MessageStatus.FAILED.value
    assert persisted.error == "Promotional consent is not active."


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

    class UnexpectedProvider:
        def __init__(self, _settings: Settings) -> None:
            pass

        def send(self, _payload: EmailPayload) -> EmailSendResult:
            pytest.fail("Suppressed recipient must not reach the email provider.")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", UnexpectedProvider)
    monkeypatch.setattr(worker, "redis_client", lambda redis_url: FakeRedis())

    worker.process_message.run(message_id)

    with worker_session_factory() as session:
        persisted = session.get(Message, message_id)

    assert persisted is not None
    assert persisted.status == MessageStatus.FAILED.value
    assert persisted.error == "Recipient is suppressed."


def test_worker_persists_retry_after_temporary_provider_failure(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
        retry_backoff_base_seconds=2,
        worker_max_attempts=3,
    )
    with worker_session_factory() as session:
        recipient = Recipient(email="qa@example.com")
        template = EmailTemplate(
            name="retry",
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
            idempotency_key="worker-retry",
            request_hash="d" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="transactional",
            variables={},
            enqueued_at=datetime.now(timezone.utc),
        )
        session.add(message)
        session.commit()
        message_id = message.id

    class TemporaryFailureProvider:
        def __init__(self, _settings: Settings) -> None:
            pass

        def send(self, _payload: EmailPayload) -> EmailSendResult:
            raise smtplib.SMTPServerDisconnected(
                "temporary outage for qa@example.com using test-provider-key"
            )

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", TemporaryFailureProvider)
    monkeypatch.setattr(worker, "redis_client", lambda redis_url: FakeRedis())

    worker.process_message.run(message_id)

    with worker_session_factory() as session:
        persisted = session.get(Message, message_id)
        events = session.query(MessageEvent).filter_by(message_id=message_id).all()

    assert persisted is not None
    assert persisted.status == MessageStatus.RETRY.value
    assert persisted.attempts == 1
    assert persisted.next_attempt_at is not None
    assert persisted.error.startswith("Temporary provider failure")
    assert "qa@example.com" not in caplog.text
    assert "test-provider-key" not in caplog.text
    assert [event.event_type for event in events] == ["retry_scheduled"]


def test_worker_dead_letters_permanent_provider_failure(
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
            name="permanent",
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
            idempotency_key="worker-permanent",
            request_hash="e" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="transactional",
            variables={},
            enqueued_at=datetime.now(timezone.utc),
        )
        session.add(message)
        session.commit()
        message_id = message.id

    class PermanentFailureProvider:
        def __init__(self, _settings: Settings) -> None:
            pass

        def send(self, _payload: EmailPayload) -> EmailSendResult:
            raise smtplib.SMTPDataError(550, b"permanent rejection")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", PermanentFailureProvider)
    monkeypatch.setattr(worker, "redis_client", lambda redis_url: FakeRedis())

    worker.process_message.run(message_id)

    with worker_session_factory() as session:
        persisted = session.get(Message, message_id)
        dead_letter = session.query(DeadLetter).filter_by(message_id=message_id).one()

    assert persisted is not None
    assert persisted.status == MessageStatus.FAILED.value
    assert dead_letter.attempts == 1
    assert "550" in dead_letter.reason


def test_worker_defers_rate_limited_message_without_spending_an_attempt(
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
            name="limited",
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
            idempotency_key="worker-rate-limited",
            request_hash="a" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="transactional",
            variables={},
            enqueued_at=datetime.now(timezone.utc),
        )
        session.add(message)
        session.commit()
        message_id = message.id

    class RateLimitedRedis(FakeRedis):
        def eval(self, _script: str, _key_count: int, *_values: object) -> list[int]:
            return [0, 5, 1]

    class UnexpectedProvider:
        def __init__(self, _settings: Settings) -> None:
            pass

        def send(self, _payload: EmailPayload) -> EmailSendResult:
            pytest.fail("Rate-limited message must not reach the provider.")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_session_factory", lambda database_url: worker_session_factory)
    monkeypatch.setattr(worker, "MailpitSMTPProvider", UnexpectedProvider)
    monkeypatch.setattr(worker, "redis_client", lambda redis_url: RateLimitedRedis())

    worker.process_message.run(message_id)

    with worker_session_factory() as session:
        persisted = session.get(Message, message_id)
    assert persisted is not None
    assert persisted.status == MessageStatus.RETRY.value
    assert persisted.attempts == 0
    assert persisted.next_attempt_at is not None
