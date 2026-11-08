import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from kombu.exceptions import OperationalError
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.dependencies import get_db, get_enqueue_message, get_settings
from src.config import Settings, UnsafeRecipientError, validate_recipient
from src.db.models import (
    EmailTemplate,
    Message,
    MessageStatus,
    MessageType,
    Recipient,
    RecipientStatus,
    Suppression,
    MessageEvent,
)
from src.rendering.templates import TemplateValidationError, render_template

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])
logger = logging.getLogger("mailflow.messages")


class MessageCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    recipient: EmailStr
    template_id: str = Field(min_length=36, max_length=36)
    variables: dict[str, object] = Field(default_factory=dict)


def _request_fingerprint(
    *, recipient: str, template_id: str, variables: dict[str, object]
) -> str:
    canonical = json.dumps(
        {
            "recipient": recipient,
            "template_id": template_id,
            "variables": variables,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message_response(message: Message) -> dict[str, str]:
    return {"id": message.id, "status": message.status}


@router.get("/{message_id}")
def get_message(
    message_id: str,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    message = session.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    events = session.scalars(
        select(MessageEvent)
        .where(MessageEvent.message_id == message_id)
        .order_by(MessageEvent.occurred_at, MessageEvent.id)
    ).all()
    return {
        "id": message.id,
        "status": message.status,
        "events": [
            {"type": event.event_type, "occurred_at": event.occurred_at}
            for event in events
        ],
    }


def _enqueue_pending_message(
    session: Session,
    message: Message,
    enqueue: Callable[[str], None],
) -> None:
    if message.status != MessageStatus.QUEUED.value or message.enqueued_at is not None:
        return
    try:
        enqueue(message.id)
    except OperationalError as error:
        logger.exception("message_enqueue_failed", extra={"message_id": message.id})
        raise HTTPException(
            status_code=503,
            detail="Message was stored but could not be queued. Retry with the same Idempotency-Key.",
        ) from error

    message.enqueued_at = datetime.now(timezone.utc)
    session.commit()


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_message(
    payload: MessageCreate,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
    ],
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    enqueue: Callable[[str], None] = Depends(get_enqueue_message),
) -> dict[str, str]:
    try:
        normalized_recipient = validate_recipient(settings, str(payload.recipient))
        normalized_recipient = validate_email(
            normalized_recipient, check_deliverability=False
        ).normalized
    except UnsafeRecipientError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except EmailNotValidError as error:
        raise HTTPException(status_code=422, detail="Recipient email address is invalid.") from error

    template = session.get(EmailTemplate, payload.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    if template.message_type != MessageType.TRANSACTIONAL.value:
        raise HTTPException(
            status_code=422,
            detail="Promotional templates must be sent through the consent-aware campaign flow.",
        )

    try:
        render_template(
            subject=template.subject,
            html_body=template.html_body,
            text_body=template.text_body,
            required_variables=template.required_variables,
            variables=payload.variables,
        )
    except TemplateValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if session.scalar(
        select(Suppression.id).where(Suppression.email == normalized_recipient)
    ) or session.scalar(
        select(Recipient.id).where(
            Recipient.email == normalized_recipient,
            Recipient.status == RecipientStatus.SUPPRESSED.value,
        )
    ):
        raise HTTPException(status_code=422, detail="Recipient is suppressed.")

    fingerprint = _request_fingerprint(
        recipient=normalized_recipient,
        template_id=template.id,
        variables=payload.variables,
    )
    existing = session.scalar(
        select(Message).where(Message.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.request_hash != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different request.",
            )
        _enqueue_pending_message(session, existing, enqueue)
        response.headers["Idempotent-Replay"] = "true"
        return _message_response(existing)

    recipient = session.scalar(
        select(Recipient).where(Recipient.email == normalized_recipient)
    )
    if recipient is None:
        recipient = Recipient(email=normalized_recipient)
        session.add(recipient)
        session.flush()

    message = Message(
        recipient_id=recipient.id,
        template_id=template.id,
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
        status=MessageStatus.QUEUED.value,
        message_type=MessageType.TRANSACTIONAL.value,
        variables=payload.variables,
    )
    session.add(message)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(Message).where(Message.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        if existing.request_hash != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different request.",
            )
        _enqueue_pending_message(session, existing, enqueue)
        response.headers["Idempotent-Replay"] = "true"
        return _message_response(existing)

    session.refresh(message)
    _enqueue_pending_message(session, message, enqueue)
    return _message_response(message)
