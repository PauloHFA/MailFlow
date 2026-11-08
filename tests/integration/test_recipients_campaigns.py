from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.dependencies import get_db, get_enqueue_message, get_settings
from src.api.main import app
from src.config import Settings
from src.db import models  # noqa: F401
from src.db.base import Base
from src.db.models import (
    Campaign,
    CampaignSchedule,
    Consent,
    EmailTemplate,
    Message,
    MessageStatus,
    Recipient,
)
from src.scheduler.dispatch import dispatch_due_schedules


@pytest.fixture
def api_database() -> Iterator[tuple[TestClient, sessionmaker[Session], list[str]]]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    enqueued_ids: list[str] = []

    def override_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    def override_settings() -> Settings:
        return Settings(
            _env_file=None,
            app_env="test",
            test_recipient_allowlist="qa@example.com,grace@example.com",
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.dependency_overrides[get_enqueue_message] = lambda: enqueued_ids.append
    try:
        yield TestClient(app), factory, enqueued_ids
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_template(client: TestClient, *, message_type: str = "transactional") -> str:
    response = client.post(
        "/api/v1/templates",
        json={
            "name": f"template-{message_type}",
            "subject": "Hello {{ name }}",
            "html_body": "<p>Hello {{ name }}</p>",
            "text_body": "Hello {{ name }}",
            "required_variables": ["name"],
            "message_type": message_type,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_csv_import_normalizes_deduplicates_and_reports_invalid_rows(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    csv_content = (
        "email,name,timezone\n"
        "QA@EXAMPLE.COM,Ada,America/Sao_Paulo\n"
        "qa@example.com,Duplicate,UTC\n"
        "not-an-email,Invalid,UTC\n"
        "grace@example.com,Grace,America/New_York\n"
    )

    response = client.post(
        "/api/v1/recipients/import",
        files={"file": ("recipients.csv", csv_content, "text/csv")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["imported_count"] == 2
    assert body["duplicate_count"] == 1
    assert body["rejected_count"] == 1
    assert body["rejected"][0]["row"] == 4
    with factory() as session:
        recipients = session.scalars(select(Recipient).order_by(Recipient.email)).all()
    assert [recipient.email for recipient in recipients] == [
        "grace@example.com",
        "qa@example.com",
    ]


def test_csv_import_rejects_missing_email_header(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, _, _ = api_database
    response = client.post(
        "/api/v1/recipients/import",
        files={"file": ("recipients.csv", "name\nAda\n", "text/csv")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "CSV must include an email column."


def test_csv_import_records_explicit_consent_without_inventing_it(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    csv_content = (
        "email,name,consent_purpose,legal_basis,consent_source,consented_at\n"
        "qa@example.com,Ada,monthly-updates,consent,signup-form,2026-11-01T10:00:00Z\n"
    )

    response = client.post(
        "/api/v1/recipients/import",
        files={"file": ("recipients.csv", csv_content, "text/csv")},
    )

    assert response.status_code == 200
    with factory() as session:
        consent = session.scalar(select(Consent))
    assert consent is not None
    assert consent.purpose == "monthly-updates"
    assert consent.legal_basis == "consent"
    assert consent.source == "signup-form"
    assert consent.registered_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 11, 1, 10, 0, tzinfo=timezone.utc
    )


def test_csv_import_does_not_create_partial_consent_record(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    csv_content = (
        "email,name,consent_purpose,legal_basis,consent_source,consented_at\n"
        "qa@example.com,Ada,monthly-updates,,signup-form,\n"
    )

    response = client.post(
        "/api/v1/recipients/import",
        files={"file": ("recipients.csv", csv_content, "text/csv")},
    )

    assert response.status_code == 200
    assert response.json()["imported_count"] == 0
    assert response.json()["rejected_count"] == 1
    with factory() as session:
        assert session.scalar(select(Consent)) is None


def test_recipient_consent_can_be_recorded_and_revoked(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    imported = client.post(
        "/api/v1/recipients/import",
        files={"file": ("recipients.csv", "email\nqa@example.com\n", "text/csv")},
    )
    recipient_id = imported.json()["imported"][0]["id"]

    created = client.post(
        f"/api/v1/recipients/{recipient_id}/consents",
        json={
            "purpose": "monthly-updates",
            "legal_basis": "consent",
            "source": "preference-center",
        },
    )
    consent_id = created.json()["id"]
    revoked = client.delete(
        f"/api/v1/recipients/{recipient_id}/consents/{consent_id}"
    )

    assert created.status_code == 201
    assert revoked.status_code == 200
    with factory() as session:
        consent = session.get(Consent, consent_id)
    assert consent is not None
    assert consent.revoked_at is not None


def test_campaign_creates_recipient_schedules_in_each_recipient_timezone(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    template_id = create_template(client)
    with factory() as session:
        recipients = [
            Recipient(email="qa@example.com", timezone="America/Sao_Paulo"),
            Recipient(email="grace@example.com", timezone="America/New_York"),
        ]
        session.add_all(recipients)
        session.commit()
        recipient_ids = [recipient.id for recipient in recipients]

    response = client.post(
        "/api/v1/campaigns",
        json={
            "name": "appointment-reminder",
            "template_id": template_id,
            "recipient_ids": recipient_ids,
            "variables": {"name": "Customer"},
            "schedule": {
                "kind": "once",
                "send_at": "2026-11-10T10:00:00Z",
                "timezone": "UTC",
            },
        },
    )

    assert response.status_code == 201
    with factory() as session:
        schedules = session.scalars(
            select(CampaignSchedule).where(
                CampaignSchedule.campaign_id == response.json()["id"]
            )
        ).all()
    assert len(schedules) == 2
    assert {schedule.timezone for schedule in schedules} == {
        "America/Sao_Paulo",
        "America/New_York",
    }
    assert {
        item["next_execution"] for item in response.json()["schedules"]
    } == {
        "2026-11-10T08:00:00-03:00",
        "2026-11-10T08:00:00-05:00",
    }


def test_relative_reminder_occurrence_creates_idempotent_messages(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, enqueued_ids = api_database
    template_id = create_template(client)
    with factory() as session:
        recipient = Recipient(email="qa@example.com", timezone="America/Sao_Paulo")
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    created = client.post(
        "/api/v1/campaigns",
        json={
            "name": "due-date-reminder",
            "template_id": template_id,
            "recipient_ids": [recipient_id],
            "variables": {"name": "Ada"},
            "schedule": {
                "kind": "relative",
                "due_at": "2026-11-13T14:30:00-03:00",
                "days_before": 3,
            },
        },
    )
    assert created.status_code == 201

    count = dispatch_due_schedules(
        factory,
        now=datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc),
        enqueue=enqueued_ids.append,
    )
    repeated_count = dispatch_due_schedules(
        factory,
        now=datetime(2026, 11, 10, 18, 1, tzinfo=timezone.utc),
        enqueue=enqueued_ids.append,
    )

    assert count == 1
    assert repeated_count == 0
    assert len(enqueued_ids) == 1
    with factory() as session:
        messages = session.scalars(select(Message)).all()
        schedule = session.scalar(select(CampaignSchedule))
    assert len(messages) == 1
    assert messages[0].status == MessageStatus.QUEUED.value
    assert messages[0].scheduled_for.replace(tzinfo=timezone.utc) == datetime(
        2026, 11, 10, 17, 30, tzinfo=timezone.utc
    )
    assert schedule is not None
    assert schedule.processed_at is not None


def test_promotional_campaign_is_disabled_until_unsubscribe_support(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    template_id = create_template(client, message_type="promotional")
    with factory() as session:
        recipient = Recipient(email="qa@example.com", timezone="UTC")
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    response = client.post(
        "/api/v1/campaigns",
        json={
            "name": "newsletter",
            "template_id": template_id,
            "message_type": "promotional",
            "purpose": "monthly-updates",
            "recipient_ids": [recipient_id],
            "variables": {"name": "Ada"},
            "schedule": {
                "kind": "once",
                "send_at": "2026-11-10T12:00:00Z",
                "timezone": "UTC",
            },
        },
    )

    assert response.status_code == 422
    assert "one-click unsubscribe" in response.json()["detail"]


def test_revoked_consent_before_scheduled_campaign_prevents_dispatch(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    _, factory, enqueued_ids = api_database
    with factory() as session:
        recipient = Recipient(email="qa@example.com", timezone="UTC")
        session.add(recipient)
        session.flush()
        template = EmailTemplate(
            name="newsletter",
            version=1,
            subject="News",
            html_body="<p>News</p>",
            text_body="News",
            required_variables=[],
            message_type="promotional",
        )
        session.add(template)
        session.flush()
        consent = Consent(
            recipient_id=recipient.id,
            purpose="monthly-updates",
            legal_basis="consent",
            source="signup-form",
        )
        campaign = Campaign(
            name="newsletter",
            template_id=template.id,
            message_type="promotional",
            purpose="monthly-updates",
            status="active",
            variables={},
        )
        session.add(campaign)
        session.flush()
        schedule = CampaignSchedule(
            campaign_id=campaign.id,
            recipient_id=recipient.id,
            rule={"kind": "once"},
            next_execution=datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc),
            timezone="UTC",
        )
        session.add_all([consent, schedule])
        session.commit()
        consent_id = consent.id
        schedule_id = schedule.id
    with factory() as session:
        consent = session.get(Consent, consent_id)
        assert consent is not None
        consent.revoked_at = datetime.now(timezone.utc)
        session.commit()

    dispatched = dispatch_due_schedules(
        factory,
        now=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
        enqueue=enqueued_ids.append,
    )

    assert dispatched == 0
    assert enqueued_ids == []
    with factory() as session:
        assert session.scalar(select(Message)) is None
        schedule = session.get(CampaignSchedule, schedule_id)
    assert schedule is not None
    assert schedule.processed_at is not None


def test_scheduler_retries_broker_failure_without_creating_duplicate_message(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    template_id = create_template(client)
    with factory() as session:
        recipient = Recipient(email="qa@example.com", timezone="UTC")
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    created = client.post(
        "/api/v1/campaigns",
        json={
            "name": "retry-schedule",
            "template_id": template_id,
            "recipient_ids": [recipient_id],
            "variables": {"name": "Ada"},
            "schedule": {
                "kind": "once",
                "send_at": "2026-11-10T12:00:00Z",
                "timezone": "UTC",
            },
        },
    )
    assert created.status_code == 201
    attempts: list[str] = []

    def enqueue(message_id: str) -> None:
        attempts.append(message_id)
        if len(attempts) == 1:
            raise RuntimeError("broker unavailable")

    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        dispatch_due_schedules(factory, now=now, enqueue=enqueue)
    retried = dispatch_due_schedules(factory, now=now, enqueue=enqueue)

    assert retried == 1
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    with factory() as session:
        messages = session.scalars(select(Message)).all()
    assert len(messages) == 1


def test_paused_campaign_schedule_is_not_dispatched(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, enqueued_ids = api_database
    template_id = create_template(client)
    with factory() as session:
        recipient = Recipient(email="qa@example.com", timezone="UTC")
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    created = client.post(
        "/api/v1/campaigns",
        json={
            "name": "paused-campaign",
            "template_id": template_id,
            "recipient_ids": [recipient_id],
            "variables": {"name": "Ada"},
            "schedule": {
                "kind": "once",
                "send_at": "2026-11-10T12:00:00Z",
                "timezone": "UTC",
            },
        },
    )
    assert created.status_code == 201
    with factory() as session:
        campaign = session.get(Campaign, created.json()["id"])
        assert campaign is not None
        campaign.status = "paused"
        session.commit()

    count = dispatch_due_schedules(
        factory,
        now=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
        enqueue=enqueued_ids.append,
    )
    assert count == 0
    assert enqueued_ids == []


def test_campaign_can_pause_resume_and_cancel_pending_messages(
    api_database: tuple[TestClient, sessionmaker[Session], list[str]],
) -> None:
    client, factory, _ = api_database
    template_id = create_template(client)
    with factory() as session:
        recipient = Recipient(email="qa@example.com", timezone="UTC")
        session.add(recipient)
        session.commit()
        recipient_id = recipient.id

    created = client.post(
        "/api/v1/campaigns",
        json={
            "name": "controllable-campaign",
            "template_id": template_id,
            "recipient_ids": [recipient_id],
            "variables": {"name": "Ada"},
            "schedule": {
                "kind": "once",
                "send_at": "2026-11-12T12:00:00Z",
                "timezone": "UTC",
            },
        },
    )
    campaign_id = created.json()["id"]
    with factory() as session:
        schedule = session.scalar(select(CampaignSchedule))
        pending = Message(
            recipient_id=recipient_id,
            template_id=template_id,
            campaign_id=campaign_id,
            idempotency_key="campaign-control-message",
            request_hash="f" * 64,
            status=MessageStatus.QUEUED.value,
            message_type="transactional",
            variables={"name": "Ada"},
        )
        session.add(pending)
        session.commit()
        message_id = pending.id
        schedule_id = schedule.id

    paused = client.post(f"/api/v1/campaigns/{campaign_id}/pause")
    invalid_pause = client.post(f"/api/v1/campaigns/{campaign_id}/pause")
    resumed = client.post(f"/api/v1/campaigns/{campaign_id}/resume")
    cancelled = client.post(f"/api/v1/campaigns/{campaign_id}/cancel")

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert invalid_pause.status_code == 409
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    with factory() as session:
        pending = session.get(Message, message_id)
        schedule = session.get(CampaignSchedule, schedule_id)
    assert pending is not None
    assert pending.status == MessageStatus.CANCELLED.value
    assert schedule is not None
    assert schedule.processed_at is not None
