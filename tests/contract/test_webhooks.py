import base64
import hashlib
import hmac
import json
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
    Consent,
    EmailTemplate,
    Message,
    MessageEvent,
    MessageStatus,
    Recipient,
    Suppression,
)
from src.suppression.unsubscribe import create_unsubscribe_token

WEBHOOK_SECRET = "test-webhook-signing-secret-that-is-long-enough"
RESEND_WEBHOOK_SECRET = "whsec_" + base64.b64encode(
    b"resend-webhook-signing-secret-for-tests"
).decode("ascii")
BREVO_WEBHOOK_TOKEN = "test-brevo-webhook-token-with-32-bytes"


@pytest.fixture
def webhook_client() -> Iterator[tuple[TestClient, sessionmaker[Session], str, str]]:
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
        webhook_signing_secret=WEBHOOK_SECRET,
        resend_webhook_secret=RESEND_WEBHOOK_SECRET,
        brevo_webhook_token=BREVO_WEBHOOK_TOKEN,
        unsubscribe_signing_secret="test-unsubscribe-signing-secret-that-is-long-enough",
        unsubscribe_base_url="https://mailflow.example/api/v1/unsubscribe",
    )
    recipient_id, message_id = _seed_message(factory)
    try:
        yield TestClient(app), factory, recipient_id, message_id
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_message(factory: sessionmaker[Session]) -> tuple[str, str]:
    with factory() as session:
        recipient = Recipient(email="qa@example.com")
        template = EmailTemplate(
            name="webhook",
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
            idempotency_key="webhook-seed",
            request_hash="a" * 64,
            status=MessageStatus.SENT.value,
            message_type="transactional",
            variables={},
            provider_message_id="resend-provider-message",
            sent_at=datetime.now(timezone.utc),
        )
        session.add(message)
        session.commit()
        return recipient.id, message.id


def _signed_headers(body: bytes, timestamp: int | None = None) -> dict[str, str]:
    timestamp = timestamp or int(datetime.now(timezone.utc).timestamp())
    digest = hmac.new(
        WEBHOOK_SECRET.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-MailFlow-Timestamp": str(timestamp),
        "X-MailFlow-Signature": f"sha256={digest}",
    }


def _send_event(
    client: TestClient,
    *,
    event_id: str,
    event_type: str,
    message_id: str,
    occurred_at: datetime | None = None,
) -> object:
    body = json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "message_id": message_id,
            "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
        },
        separators=(",", ":"),
    ).encode()
    return client.post(
        "/api/v1/webhooks/generic",
        content=body,
        headers=_signed_headers(body),
    )


def _resend_headers(body: bytes, event_id: str) -> dict[str, str]:
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    secret = base64.b64decode(RESEND_WEBHOOK_SECRET.removeprefix("whsec_"))
    signed_content = event_id.encode() + b"." + timestamp.encode() + b"." + body
    digest = base64.b64encode(
        hmac.new(secret, signed_content, hashlib.sha256).digest()
    ).decode("ascii")
    return {
        "svix-id": event_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{digest}",
    }


def test_webhook_rejects_bad_and_expired_signatures(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, _, _, message_id = webhook_client
    body = json.dumps(
        {
            "event_id": "bad-signature",
            "event_type": "delivered",
            "message_id": message_id,
        }
    ).encode()

    invalid = client.post(
        "/api/v1/webhooks/generic",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-MailFlow-Timestamp": str(int(datetime.now(timezone.utc).timestamp())),
            "X-MailFlow-Signature": "sha256=" + "0" * 64,
        },
    )
    expired = client.post(
        "/api/v1/webhooks/generic",
        content=body,
        headers=_signed_headers(
            body,
            int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()),
        ),
    )

    assert invalid.status_code == 401
    assert expired.status_code == 401


def test_webhook_rejects_signed_invalid_payloads(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, _, _ = webhook_client
    body = b'{"event_id":"missing-required-fields"}'
    response = client.post(
        "/api/v1/webhooks/generic",
        content=body,
        headers=_signed_headers(body),
    )

    assert response.status_code == 422
    with factory() as session:
        assert session.scalar(select(MessageEvent.id)) is None


def test_webhook_suppresses_hard_bounces_and_deduplicates_provider_events(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, recipient_id, message_id = webhook_client

    first = _send_event(
        client,
        event_id="hard-bounce-001",
        event_type="hard_bounce",
        message_id=message_id,
    )
    replay = _send_event(
        client,
        event_id="hard-bounce-001",
        event_type="hard_bounce",
        message_id=message_id,
    )

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True
    with factory() as session:
        recipient = session.get(Recipient, recipient_id)
        suppressions = session.scalars(select(Suppression)).all()
        events = session.scalars(
            select(MessageEvent).where(MessageEvent.message_id == message_id)
        ).all()
    assert recipient is not None
    assert recipient.status == "suppressed"
    assert len(suppressions) == 1
    assert len(events) == 1


def test_three_consecutive_soft_bounces_suppress_recipient(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, recipient_id, message_id = webhook_client

    for event_id in ("soft-1", "soft-2", "soft-3"):
        response = _send_event(
            client,
            event_id=event_id,
            event_type="soft_bounce",
            message_id=message_id,
        )
        assert response.status_code == 202

    with factory() as session:
        recipient = session.get(Recipient, recipient_id)
        suppressions = session.scalars(select(Suppression)).all()
    assert recipient is not None
    assert recipient.status == "suppressed"
    assert len(suppressions) == 1


def test_resend_webhook_verifies_svix_signature_and_deduplicates(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, recipient_id, _ = webhook_client
    body = json.dumps(
        {
            "type": "email.bounced",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data": {"email_id": "resend-provider-message"},
        },
        separators=(",", ":"),
    ).encode()
    headers = _resend_headers(body, "msg_resend_test_1")

    first = client.post("/api/v1/webhooks/resend", content=body, headers=headers)
    replay = client.post("/api/v1/webhooks/resend", content=body, headers=headers)
    invalid = client.post(
        "/api/v1/webhooks/resend",
        content=body,
        headers={**headers, "svix-signature": "v1," + "0" * 44},
    )

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True
    assert invalid.status_code == 401
    with factory() as session:
        recipient = session.get(Recipient, recipient_id)
        assert recipient is not None
        assert recipient.status == "suppressed"


def test_brevo_webhook_authenticates_native_payload_and_suppresses_unsubscribe(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, recipient_id, message_id = webhook_client
    body = json.dumps(
        {
            "event": "unsubscribed",
            "id": 29,
            "message-id": "brevo-provider-message",
            "ts_event": int(datetime.now(timezone.utc).timestamp()),
            "X-Mailin-custom": message_id,
        },
        separators=(",", ":"),
    ).encode()

    first = client.post(
        "/api/v1/webhooks/brevo",
        content=body,
        headers={"X-MailFlow-Brevo-Token": BREVO_WEBHOOK_TOKEN},
    )
    replay = client.post(
        "/api/v1/webhooks/brevo",
        content=body,
        headers={"X-MailFlow-Brevo-Token": BREVO_WEBHOOK_TOKEN},
    )
    invalid = client.post("/api/v1/webhooks/brevo", content=body)

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True
    assert invalid.status_code == 401
    with factory() as session:
        recipient = session.get(Recipient, recipient_id)
        suppression = session.scalar(select(Suppression))
        assert recipient is not None
        assert recipient.status == "suppressed"
        assert suppression is not None
        assert suppression.reason == "unsubscribed"


def test_delivery_breaks_soft_bounce_streak_and_complaint_is_global_suppression(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, recipient_id, message_id = webhook_client
    same_provider_timestamp = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    for event_id, event_type in (
        ("soft-a", "soft_bounce"),
        ("soft-b", "soft_bounce"),
        ("delivered-a", "delivered"),
        ("soft-c", "soft_bounce"),
        ("soft-d", "soft_bounce"),
    ):
        response = _send_event(
            client,
            event_id=event_id,
            event_type=event_type,
            message_id=message_id,
            occurred_at=same_provider_timestamp,
        )
        assert response.status_code == 202
    with factory() as session:
        recipient = session.get(Recipient, recipient_id)
        assert session.scalar(select(Suppression.id)) is None

    complaint = _send_event(
        client,
        event_id="complaint-a",
        event_type="complaint",
        message_id=message_id,
    )
    assert complaint.status_code == 202
    with factory() as session:
        recipient = session.get(Recipient, recipient_id)
        suppression = session.scalar(select(Suppression))
    assert recipient is not None
    assert recipient.status == "suppressed"
    assert suppression is not None
    assert suppression.reason == "complaint"


def test_unsubscribe_post_revokes_only_the_target_consent_and_get_is_safe(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, recipient_id, _ = webhook_client
    with factory() as session:
        consent = Consent(
            recipient_id=recipient_id,
            purpose="monthly-updates",
            legal_basis="consent",
            source="signup",
        )
        session.add(consent)
        session.commit()
        consent_id = consent.id
        transactional_consent = Consent(
            recipient_id=recipient_id,
            purpose="account-notices",
            legal_basis="contract",
            source="account",
        )
        session.add(transactional_consent)
        session.commit()
        transactional_consent_id = transactional_consent.id

    token = create_unsubscribe_token(
        consent_id,
        "test-unsubscribe-signing-secret-that-is-long-enough",
    )

    preview = client.get("/api/v1/unsubscribe", params={"token": token})
    assert preview.status_code == 200
    with factory() as session:
        assert session.get(Consent, consent_id).revoked_at is None

    body = b"List-Unsubscribe=One-Click"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    unsubscribed = client.post(
        "/api/v1/unsubscribe",
        params={"token": token},
        content=body,
        headers=headers,
    )
    replay = client.post(
        "/api/v1/unsubscribe",
        params={"token": token},
        content=body,
        headers=headers,
    )
    assert unsubscribed.status_code == 200
    assert replay.status_code == 200
    with factory() as session:
        consent = session.get(Consent, consent_id)
        unrelated_consent = session.get(Consent, transactional_consent_id)
        recipient = session.get(Recipient, recipient_id)
    assert consent is not None and consent.revoked_at is not None
    assert unrelated_consent is not None and unrelated_consent.revoked_at is None
    assert recipient is not None and recipient.status == "active"


def test_unsubscribe_rejects_tampered_expired_or_invalid_request_tokens(
    webhook_client: tuple[TestClient, sessionmaker[Session], str, str],
) -> None:
    client, factory, recipient_id, _ = webhook_client
    with factory() as session:
        consent = Consent(
            recipient_id=recipient_id,
            purpose="monthly-updates",
            legal_basis="consent",
            source="signup",
        )
        session.add(consent)
        session.commit()
        consent_id = consent.id

    secret = "test-unsubscribe-signing-secret-that-is-long-enough"
    valid_token = create_unsubscribe_token(consent_id, secret)
    expired_token = create_unsubscribe_token(
        consent_id,
        secret,
        now=datetime.now(timezone.utc) - timedelta(days=3),
        lifetime=timedelta(days=1),
    )
    tampered_token = valid_token[:-1] + ("0" if valid_token[-1] != "0" else "1")

    assert client.get(
        "/api/v1/unsubscribe",
        params={"token": tampered_token},
    ).status_code == 400
    assert client.get(
        "/api/v1/unsubscribe",
        params={"token": expired_token},
    ).status_code == 410
    invalid_form = client.post(
        "/api/v1/unsubscribe",
        params={"token": valid_token},
        content=b"List-Unsubscribe=not-one-click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert invalid_form.status_code == 400
