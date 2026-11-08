from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.dependencies import get_db, get_enqueue_message
from src.api.main import app
from src.config import Settings, get_settings
from src.db.base import Base
from src.db import models  # noqa: F401
from src.db.models import Suppression


@pytest.fixture
def client_and_enqueued_ids() -> Iterator[tuple[TestClient, list[str]]]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    enqueued_ids: list[str] = []

    def override_get_db() -> Iterator[Session]:
        with test_session() as session:
            yield session

    def override_get_settings() -> Settings:
        return Settings(
            _env_file=None,
            app_env="test",
            test_recipient_allowlist="qa@example.com",
        )

    def override_enqueue_message(message_id: str) -> None:
        enqueued_ids.append(message_id)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = override_get_settings
    app.dependency_overrides[get_enqueue_message] = lambda: override_enqueue_message
    try:
        yield TestClient(app), enqueued_ids
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_transactional_template(client: TestClient) -> str:
    response = client.post(
        "/api/v1/templates",
        json={
            "name": "appointment-confirmation",
            "subject": "Appointment for {{ name }}",
            "html_body": "<p>Hello {{ name }}</p>",
            "text_body": "Hello {{ name }}",
            "required_variables": ["name"],
            "message_type": "transactional",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_template_create_preview_and_versioning(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, _ = client_and_enqueued_ids
    template_id = create_transactional_template(client)

    retrieved = client.get(f"/api/v1/templates/{template_id}")
    preview = client.post(
        f"/api/v1/templates/{template_id}/preview",
        json={"variables": {"name": "Ada"}},
    )
    assert retrieved.status_code == 200
    assert retrieved.json()["id"] == template_id
    assert preview.status_code == 200
    assert preview.json()["subject"] == "Appointment for Ada"
    assert preview.json()["html_body"] == "<p>Hello Ada</p>"
    assert preview.json()["text_body"] == "Hello Ada"

    updated = client.post(
        "/api/v1/templates",
        json={
            "name": "appointment-confirmation",
            "subject": "Updated: {{ name }}",
            "html_body": "<p>Updated {{ name }}</p>",
            "text_body": "Updated {{ name }}",
            "required_variables": ["name"],
            "message_type": "transactional",
        },
    )
    assert updated.status_code == 201
    assert updated.json()["version"] == 2


def test_message_is_queued_and_same_idempotency_key_replays_result(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, enqueued_ids = client_and_enqueued_ids
    template_id = create_transactional_template(client)
    request = {
        "recipient": "qa@example.com",
        "template_id": template_id,
        "variables": {"name": "Ada"},
    }

    first = client.post(
        "/api/v1/messages",
        json=request,
        headers={"Idempotency-Key": "request-001"},
    )
    replay = client.post(
        "/api/v1/messages",
        json=request,
        headers={"Idempotency-Key": "request-001"},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["status"] == "queued"
    assert replay.headers["Idempotent-Replay"] == "true"
    assert enqueued_ids == [first.json()["id"]]


def test_message_status_endpoint_returns_current_status_and_events(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, _ = client_and_enqueued_ids
    template_id = create_transactional_template(client)
    created = client.post(
        "/api/v1/messages",
        json={
            "recipient": "qa@example.com",
            "template_id": template_id,
            "variables": {"name": "Ada"},
        },
        headers={"Idempotency-Key": "request-status"},
    )

    response = client.get(f"/api/v1/messages/{created.json()['id']}")

    assert response.status_code == 200
    assert response.json() == {
        "id": created.json()["id"],
        "status": "queued",
        "events": [],
    }


def test_reusing_idempotency_key_with_different_payload_conflicts(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, _ = client_and_enqueued_ids
    template_id = create_transactional_template(client)
    headers = {"Idempotency-Key": "request-002"}

    first = client.post(
        "/api/v1/messages",
        json={
            "recipient": "qa@example.com",
            "template_id": template_id,
            "variables": {"name": "Ada"},
        },
        headers=headers,
    )
    conflicting = client.post(
        "/api/v1/messages",
        json={
            "recipient": "qa@example.com",
            "template_id": template_id,
            "variables": {"name": "Grace"},
        },
        headers=headers,
    )

    assert first.status_code == 202
    assert conflicting.status_code == 409


def test_message_request_rejects_unapproved_recipient(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, enqueued_ids = client_and_enqueued_ids
    template_id = create_transactional_template(client)

    response = client.post(
        "/api/v1/messages",
        json={
            "recipient": "customer@example.com",
            "template_id": template_id,
            "variables": {"name": "Ada"},
        },
        headers={"Idempotency-Key": "request-003"},
    )

    assert response.status_code == 403
    assert enqueued_ids == []


def test_suppressed_recipient_is_blocked_before_queueing(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, enqueued_ids = client_and_enqueued_ids
    template_id = create_transactional_template(client)
    db_generator = app.dependency_overrides[get_db]()
    session = next(db_generator)
    session.add(
        Suppression(
            email="qa@example.com",
            reason="hard_bounce",
            source="test",
        )
    )
    session.commit()
    db_generator.close()

    response = client.post(
        "/api/v1/messages",
        json={
            "recipient": "qa@example.com",
            "template_id": template_id,
            "variables": {"name": "Ada"},
        },
        headers={"Idempotency-Key": "suppressed-recipient"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Recipient is suppressed."
    assert enqueued_ids == []


def test_missing_template_variable_does_not_create_or_enqueue_message(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, enqueued_ids = client_and_enqueued_ids
    template_id = create_transactional_template(client)

    response = client.post(
        "/api/v1/messages",
        json={
            "recipient": "qa@example.com",
            "template_id": template_id,
            "variables": {},
        },
        headers={"Idempotency-Key": "missing-variable"},
    )

    assert response.status_code == 422
    assert "name" in response.json()["detail"]
    assert enqueued_ids == []


def test_retry_with_same_key_requeues_message_after_broker_error(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, _ = client_and_enqueued_ids
    template_id = create_transactional_template(client)
    queued_ids: list[str] = []

    def enqueue(message_id: str) -> None:
        queued_ids.append(message_id)
        if len(queued_ids) == 1:
            raise OperationalError("broker unavailable")

    app.dependency_overrides[get_enqueue_message] = lambda: enqueue
    request = {
        "recipient": "qa@example.com",
        "template_id": template_id,
        "variables": {"name": "Ada"},
    }
    headers = {"Idempotency-Key": "broker-retry"}

    unavailable = client.post("/api/v1/messages", json=request, headers=headers)
    retried = client.post("/api/v1/messages", json=request, headers=headers)

    assert unavailable.status_code == 503
    assert retried.status_code == 202
    assert retried.headers["Idempotent-Replay"] == "true"
    assert queued_ids == [retried.json()["id"], retried.json()["id"]]


def test_message_request_rejects_promotional_template_until_campaign_flow_exists(
    client_and_enqueued_ids: tuple[TestClient, list[str]],
) -> None:
    client, enqueued_ids = client_and_enqueued_ids
    response = client.post(
        "/api/v1/templates",
        json={
            "name": "newsletter",
            "subject": "News",
            "html_body": "<p>News</p>",
            "text_body": "News",
            "required_variables": [],
            "message_type": "promotional",
        },
    )
    assert response.status_code == 201

    send = client.post(
        "/api/v1/messages",
        json={
            "recipient": "qa@example.com",
            "template_id": response.json()["id"],
            "variables": {},
        },
        headers={"Idempotency-Key": "request-004"},
    )

    assert send.status_code == 422
    assert enqueued_ids == []
