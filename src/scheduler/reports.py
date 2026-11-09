import logging
from datetime import datetime, timedelta, timezone

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from src.config import Settings, validate_recipient
from src.db.models import Message, MessageEvent
from src.providers.base import EmailAttachment, EmailPayload, EmailProvider
from src.providers.factory import build_email_provider
from src.rendering.pdf import build_delivery_report_pdf

logger = logging.getLogger("mailflow.reports")


def _report_counts(
    session: Session,
    *,
    start_at: datetime,
    end_at: datetime,
) -> tuple[dict[str, int], dict[str, int]]:
    message_rows = session.execute(
        select(Message.status, func.count(Message.id))
        .where(
            Message.created_at >= start_at,
            Message.created_at < end_at,
        )
        .group_by(Message.status)
    ).all()
    event_rows = session.execute(
        select(MessageEvent.event_type, func.count(MessageEvent.id))
        .where(
            MessageEvent.occurred_at >= start_at,
            MessageEvent.occurred_at < end_at,
        )
        .group_by(MessageEvent.event_type)
    ).all()
    return (
        {str(status): int(count) for status, count in message_rows},
        {str(event_type): int(count) for event_type, count in event_rows},
    )


def send_daily_delivery_report(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    now: datetime,
    provider: EmailProvider | None = None,
) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Report time must include a timezone.")
    if not settings.daily_report_enabled:
        logger.info("daily_delivery_report_disabled")
        return False
    if not settings.report_recipient_email:
        raise ValueError("REPORT_RECIPIENT_EMAIL is required when daily reports are enabled.")

    try:
        normalized_recipient = validate_email(
            settings.report_recipient_email,
            check_deliverability=False,
        ).normalized
        normalized_recipient = validate_recipient(settings, normalized_recipient)
    except (EmailNotValidError, ValueError) as error:
        raise ValueError("Configured daily report recipient is invalid or not allowed.") from error

    end_at = now.astimezone(timezone.utc)
    start_at = end_at - timedelta(days=1)
    with session_factory() as session:
        messages, events = _report_counts(
            session,
            start_at=start_at,
            end_at=end_at,
        )

    report = build_delivery_report_pdf(
        start_at=start_at,
        end_at=end_at,
        messages=messages,
        events=events,
    )
    (provider or build_email_provider(settings)).send(
        EmailPayload(
            recipient=normalized_recipient,
            sender=settings.default_from_email,
            subject=f"MailFlow delivery report - {end_at:%Y-%m-%d}",
            html_body="<p>Your scheduled MailFlow delivery report is attached.</p>",
            text_body="Your scheduled MailFlow delivery report is attached.",
            idempotency_key=f"mailflow-report-{end_at:%Y%m%d}",
            attachments=(
                EmailAttachment(
                    filename=f"mailflow-delivery-{end_at:%Y%m%d}.pdf",
                    content_type="application/pdf",
                    content=report,
                ),
            ),
        )
    )
    logger.info(
        "daily_delivery_report_sent",
        extra={"period_end": end_at.isoformat(), "attachment_bytes": len(report)},
    )
    return True
