import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.api.dependencies import get_db, get_settings
from src.config import Settings
from src.db.models import Message
from src.providers.webhook import (
    ProviderEvent,
    WebhookPayloadError,
    WebhookSignatureError,
    parse_brevo_webhook,
    parse_generic_webhook,
    parse_resend_webhook,
    verify_resend_webhook_signature,
    verify_webhook_signature,
)
from src.suppression.events import (
    UnknownWebhookMessageError,
    process_provider_event,
)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
MAX_WEBHOOK_BYTES = 1024 * 1024


async def _read_limited_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body exceeds the 1 MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/generic", status_code=status.HTTP_202_ACCEPTED)
async def receive_generic_webhook(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    raw_body = await _read_limited_body(request)
    if len(settings.webhook_signing_secret.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=503,
            detail="Webhook signature verification is not configured.",
        )
    timestamp = request.headers.get("X-MailFlow-Timestamp", "")
    signature = request.headers.get("X-MailFlow-Signature", "")
    try:
        verify_webhook_signature(
            raw_body,
            timestamp=timestamp,
            signature=signature,
            secret=settings.webhook_signing_secret,
            tolerance_seconds=settings.webhook_timestamp_tolerance_seconds,
        )
    except WebhookSignatureError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error

    try:
        event = parse_generic_webhook(raw_body)
    except WebhookPayloadError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    try:
        duplicate = process_provider_event(
            session,
            event,
            provider="generic",
            settings=settings,
        )
    except UnknownWebhookMessageError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"accepted": True, "duplicate": duplicate}


async def _process_native_event(
    request: Request,
    session: Session,
    settings: Settings,
    *,
    provider: str,
) -> dict[str, bool]:
    raw_body = await _read_limited_body(request)
    if provider == "resend":
        secret = settings.resend_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(
                status_code=503,
                detail="Resend webhook verification is not configured.",
            )
        try:
            verify_resend_webhook_signature(
                raw_body,
                message_id=request.headers.get("svix-id", ""),
                timestamp=request.headers.get("svix-timestamp", ""),
                signature=request.headers.get("svix-signature", ""),
                secret=secret,
                tolerance_seconds=settings.webhook_timestamp_tolerance_seconds,
            )
            event = parse_resend_webhook(
                raw_body,
                event_id=request.headers["svix-id"],
            )
        except WebhookSignatureError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except WebhookPayloadError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        message = session.scalar(
            select(Message).where(Message.provider_message_id == event.message_id)
        )
    else:
        expected_token = settings.brevo_webhook_token.get_secret_value()
        supplied_token = request.headers.get("X-MailFlow-Brevo-Token", "")
        if len(expected_token.encode("utf-8")) < 32:
            raise HTTPException(
                status_code=503,
                detail="Brevo webhook authentication is not configured.",
            )
        if not secrets.compare_digest(
            supplied_token.encode("utf-8"),
            expected_token.encode("utf-8"),
        ):
            raise HTTPException(status_code=401, detail="Brevo webhook authentication failed.")
        try:
            event = parse_brevo_webhook(raw_body)
        except WebhookPayloadError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        message = session.scalar(
            select(Message).where(
                or_(
                    Message.id == event.message_id,
                    Message.provider_message_id == event.message_id,
                )
            )
        )

    if message is None:
        raise HTTPException(status_code=404, detail="Webhook message was not found.")
    event = ProviderEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        message_id=message.id,
        occurred_at=event.occurred_at,
    )
    try:
        duplicate = process_provider_event(
            session,
            event,
            provider=provider,
            settings=settings,
        )
    except UnknownWebhookMessageError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"accepted": True, "duplicate": duplicate}


@router.post("/resend", status_code=status.HTTP_202_ACCEPTED)
async def receive_resend_webhook(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    return await _process_native_event(request, session, settings, provider="resend")


@router.post("/brevo", status_code=status.HTTP_202_ACCEPTED)
async def receive_brevo_webhook(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    return await _process_native_event(request, session, settings, provider="brevo")
