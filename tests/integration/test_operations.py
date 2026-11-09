from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.dependencies import get_db, get_settings
from src.api.main import app
from src.config import Settings
from src.db import models  # noqa: F401
from src.db.base import Base
from src.db.models import (
    Campaign,
    CampaignSchedule,
    Consent,
    DeadLetter,
    EmailTemplate,
    Message,
    MessageEvent,
    Recipient,
    Suppression,
)
from src.privacy.retention import purge_expired_messages
from src.providers.base import EmailPayload, EmailSendResult
from src.scheduler.reports import send_daily_delivery_report

OPERATIONS_TOKEN = "test-operations-token-with-at-least-32-bytes"


@pytest.fixture
def operations_client() -> Iterator[tuple[TestClient, sessionmaker[Session], str, str]]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
        operations_api_token=OPERATIONS_TOKEN,
    )
    with factory() as session:
        recipient = Recipient(email="qa@example.com")
        template = EmailTemplate(
            name="ops",
            version=1,
            subject="Report",
            html_body="<p>Report</p>",
            text_body="Report",
            required_variables=[],
        )
        session.add_all([recipient, template])
        session.flush()
        message = Message(
            recipient_id=recipient.id,
            template_id=template.id,
            idempotency_key="ops-message",
            request_hash="a" * 64,
            status="sent",
            message_type="transactional",
            variables={},
            sent_at=datetime.now(timezone.utc),
        )
        session.add(message)
        session.flush()
        session.add(
            MessageEvent(
                message_id=message.id,
                event_type="delivered",
                occurred_at=datetime.now(timezone.utc),
            )
        )
        consent = Consent(
            recipient_id=recipient.id,
            purpose="updates",
            legal_basis="consent",
            source="signup",
        )
        session.add(consent)
        session.commit()
        message_id = message.id
        consent_id = consent.id
    try:
        yield TestClient(app), factory, message_id, consent_id
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATIONS_TOKEN}"}


def test_operations_endpoints_require_bearer_token(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, _, _, _ = operations_client

    for path in (
        "/api/v1/domain/check",
        "/api/v1/metrics/delivery",
        "/api/v1/exports/messages.csv",
        "/api/v1/exports/consents.csv",
    ):
        response = client.get(path)
        assert response.status_code == 401
    response = client.delete("/api/v1/recipients/recipient-id")
    assert response.status_code == 401


def test_domain_check_api_returns_authentication_findings(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.api.domain as domain_api

    monkeypatch.setattr(
        domain_api,
        "check_domain_records",
        lambda domain, selectors: {
            "domain": domain,
            "spf": {"status": "pass"},
            "dkim": {"status": "missing", "selectors": {}},
            "dmarc": {"status": "pass", "policy": "reject"},
            "pending": ["Publish DKIM for the configured selectors."],
        },
    )
    client, _, _, _ = operations_client

    response = client.get(
        "/api/v1/domain/check",
        params={"domain": "example.com", "dkim_selectors": ["mail", "s1"]},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["spf"]["status"] == "pass"
    assert response.json()["dmarc"]["policy"] == "reject"
    assert response.json()["pending"] == ["Publish DKIM for the configured selectors."]


def test_delivery_metrics_return_aggregates_without_recipient_data(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, _, _, _ = operations_client
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    start = end - timedelta(days=1)

    response = client.get(
        "/api/v1/metrics/delivery",
        params={"start_at": start.isoformat(), "end_at": end.isoformat()},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["messages"]["sent"] == 1
    assert body["events"]["delivered"] == 1
    assert "qa@example.com" not in response.text


def test_csv_exports_include_sends_and_consent_only_for_authorized_operator(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, _, _, _ = operations_client

    messages = client.get("/api/v1/exports/messages.csv", headers=_auth_headers())
    consents = client.get("/api/v1/exports/consents.csv", headers=_auth_headers())

    assert messages.status_code == 200
    assert messages.headers["content-type"].startswith("text/csv")
    assert "qa@example.com" in messages.text
    assert "sent" in messages.text
    assert consents.status_code == 200
    assert "qa@example.com" in consents.text
    assert "updates" in consents.text
    assert "consent" in consents.text


def test_csv_exports_escape_spreadsheet_formulas(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, _, consent_id = operations_client
    with factory() as session:
        consent = session.get(Consent, consent_id)
        assert consent is not None
        consent.source = "=HYPERLINK(\"https://attacker.example\")"
        session.commit()

    response = client.get("/api/v1/exports/consents.csv", headers=_auth_headers())

    assert response.status_code == 200
    assert "'=HYPERLINK" in response.text


def test_export_rejects_an_oversized_date_range(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, _, _, _ = operations_client
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)

    response = client.get(
        "/api/v1/exports/messages.csv",
        params={"start_at": start.isoformat(), "end_at": end.isoformat()},
        headers=_auth_headers(),
    )

    assert response.status_code == 422


def test_reports_are_scheduled_as_daily_beat_task() -> None:
    from src.worker import celery_app

    schedule = celery_app.conf.beat_schedule["send-daily-delivery-report"]
    retention_schedule = celery_app.conf.beat_schedule["purge-expired-message-data"]

    assert schedule["task"] == "src.worker.send_daily_delivery_report"
    assert retention_schedule["task"] == "src.worker.purge_expired_message_data"


def test_recipient_erasure_requires_auth_and_removes_linked_personal_data(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, message_id, _ = operations_client
    with factory() as session:
        message = session.get(Message, message_id)
        assert message is not None
        recipient = session.get(Recipient, message.recipient_id)
        assert recipient is not None
        recipient_id = recipient.id
        template = session.get(EmailTemplate, message.template_id)
        assert template is not None
        campaign = Campaign(
            name="erase-test",
            template_id=template.id,
            message_type="transactional",
            variables={},
        )
        session.add(campaign)
        session.flush()
        session.add(
            CampaignSchedule(
                campaign_id=campaign.id,
                recipient_id=recipient.id,
                rule={"kind": "once"},
                next_execution=datetime.now(timezone.utc),
                timezone="UTC",
            )
        )
        session.add(
            DeadLetter(message_id=message.id, attempts=1, reason="test failure")
        )
        session.add(Suppression(email=recipient.email, reason="test", source="test"))
        session.commit()

    unauthorized = client.delete(f"/api/v1/recipients/{recipient_id}")
    response = client.delete(
        f"/api/v1/recipients/{recipient_id}",
        headers=_auth_headers(),
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 204
    assert response.content == b""
    with factory() as session:
        assert session.get(Recipient, recipient_id) is None
        assert session.get(Message, message_id) is None
        assert session.scalar(select(Consent.id)) is None
        assert session.scalar(select(MessageEvent.id)) is None
        assert session.scalar(select(CampaignSchedule.id)) is None
        assert session.scalar(select(DeadLetter.id)) is None
        assert session.scalar(select(Suppression.id)) is None

    missing = client.delete(
        f"/api/v1/recipients/{recipient_id}",
        headers=_auth_headers(),
    )
    assert missing.status_code == 404


def test_openapi_documents_protected_recipient_erasure(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, _, _, _ = operations_client

    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/api/v1/recipients/{recipient_id}"]["delete"]["responses"][
        "204"
    ]


def test_retention_purges_only_expired_message_data(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    _, factory, message_id, _ = operations_client
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    with factory() as session:
        recent = session.get(Message, message_id)
        assert recent is not None
        recent.created_at = datetime(2026, 11, 1, tzinfo=timezone.utc)
        old = Message(
            recipient_id=recent.recipient_id,
            template_id=recent.template_id,
            idempotency_key="expired-message",
            request_hash="b" * 64,
            status="failed",
            message_type="transactional",
            variables={},
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        session.add(old)
        session.flush()
        old_id = old.id
        old_event = MessageEvent(message_id=old.id, event_type="failed")
        session.add_all(
            [old_event, DeadLetter(message_id=old.id, attempts=1, reason="expired")]
        )
        session.flush()
        old_event_id = old_event.id
        session.commit()

    deleted = purge_expired_messages(
        factory,
        Settings(_env_file=None, message_retention_days=30),
        now=now,
    )

    assert deleted == 1
    with factory() as session:
        assert session.get(Message, old_id) is None
        assert session.get(MessageEvent, old_event_id) is None
        assert session.scalar(select(DeadLetter.id)) is None
        assert session.get(Message, message_id) is not None


def test_disabled_retention_does_not_purge_messages(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    _, factory, message_id, _ = operations_client

    deleted = purge_expired_messages(
        factory,
        Settings(_env_file=None, message_retention_days=0),
        now=datetime.now(timezone.utc),
    )

    assert deleted == 0
    with factory() as session:
        assert session.get(Message, message_id) is not None


def test_daily_report_is_sent_as_a_pdf_attachment(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    _, factory, _, _ = operations_client
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
        daily_report_enabled=True,
        report_recipient_email="qa@example.com",
    )
    sent: list[EmailPayload] = []

    class FakeProvider:
        def send(self, payload: EmailPayload) -> EmailSendResult:
            sent.append(payload)
            return EmailSendResult(
                provider="test",
                provider_message_id="report-message-1",
            )

    created = send_daily_delivery_report(
        factory,
        settings,
        now=datetime.now(timezone.utc),
        provider=FakeProvider(),
    )

    assert created is True
    assert len(sent) == 1
    assert sent[0].recipient == "qa@example.com"
    assert len(sent[0].attachments) == 1
    assert sent[0].attachments[0].content_type == "application/pdf"
    assert sent[0].attachments[0].content.startswith(b"%PDF-")


def test_daily_report_fails_when_enabled_without_recipient(
    operations_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    _, factory, _, _ = operations_client
    settings = Settings(_env_file=None, daily_report_enabled=True)

    with pytest.raises(ValueError, match="REPORT_RECIPIENT_EMAIL"):
        send_daily_delivery_report(
            factory,
            settings,
            now=datetime.now(timezone.utc),
        )
