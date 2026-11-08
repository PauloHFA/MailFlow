import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import (
    Campaign,
    CampaignSchedule,
    CampaignStatus,
    Consent,
    Message,
    MessageStatus,
    Recipient,
    RecipientStatus,
)
from src.scheduler.windows import normalize_utc


def _request_hash(
    *,
    recipient_email: str,
    template_id: str,
    variables: dict[str, object],
) -> str:
    request = json.dumps(
        {
            "recipient": recipient_email,
            "template_id": template_id,
            "variables": variables,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(request.encode("utf-8")).hexdigest()


def _has_current_consent(session: Session, campaign: Campaign, recipient: Recipient) -> bool:
    if campaign.message_type != "promotional":
        return True
    if campaign.purpose is None:
        return False
    return (
        session.scalar(
            select(Consent.id).where(
                Consent.recipient_id == recipient.id,
                Consent.purpose == campaign.purpose,
                Consent.registered_at <= datetime.now(timezone.utc),
                Consent.revoked_at.is_(None),
            )
        )
        is not None
    )


def dispatch_due_schedules(
    session_factory: sessionmaker[Session],
    *,
    now: datetime,
    enqueue: Callable[[str], None],
    batch_size: int = 500,
) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    if batch_size < 1:
        raise ValueError("batch_size must be positive.")
    current_time = now.astimezone(timezone.utc)
    dispatched = 0

    with session_factory() as session:
        schedules = session.scalars(
            select(CampaignSchedule)
            .where(
                CampaignSchedule.processed_at.is_(None),
                CampaignSchedule.next_execution <= current_time,
            )
            .order_by(CampaignSchedule.next_execution, CampaignSchedule.id)
            .limit(batch_size)
        ).all()

        for schedule in schedules:
            campaign = session.get(Campaign, schedule.campaign_id)
            recipient = session.get(Recipient, schedule.recipient_id)
            if campaign is None or recipient is None:
                schedule.processed_at = datetime.now(timezone.utc)
                session.commit()
                continue
            if campaign.status != CampaignStatus.ACTIVE.value:
                continue
            if (
                recipient.status != RecipientStatus.ACTIVE.value
                or not _has_current_consent(session, campaign, recipient)
            ):
                schedule.processed_at = datetime.now(timezone.utc)
                session.commit()
                continue

            scheduled_for = normalize_utc(schedule.next_execution)
            variables = dict(campaign.variables)
            if recipient.name:
                variables["name"] = recipient.name
            idempotency_key = (
                f"campaign:{campaign.id}:recipient:{recipient.id}:"
                f"schedule:{schedule.id}"
            )
            message = session.scalar(
                select(Message).where(Message.idempotency_key == idempotency_key)
            )
            if message is None:
                message = Message(
                    recipient_id=recipient.id,
                    template_id=campaign.template_id,
                    idempotency_key=idempotency_key,
                    request_hash=_request_hash(
                        recipient_email=recipient.email,
                        template_id=campaign.template_id,
                        variables=variables,
                    ),
                    status=MessageStatus.QUEUED.value,
                    message_type=campaign.message_type,
                    variables=variables,
                    scheduled_for=scheduled_for,
                )
                session.add(message)
                session.commit()
                session.refresh(message)

            if (
                message.status == MessageStatus.QUEUED.value
                and message.enqueued_at is None
            ):
                enqueue(message.id)
                message.enqueued_at = datetime.now(timezone.utc)
                dispatched += 1

            schedule.processed_at = datetime.now(timezone.utc)
            session.commit()

    return dispatched
