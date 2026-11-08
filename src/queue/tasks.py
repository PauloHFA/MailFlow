import logging
import smtplib
from datetime import datetime, timedelta, timezone
from typing import Protocol

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from src.config import Settings
from src.db.models import (
    Campaign,
    CampaignStatus,
    EmailTemplate,
    Message,
    MessageEvent,
    MessageStatus,
    Recipient,
    RecipientStatus,
    Suppression,
)
from src.providers.base import EmailPayload, EmailProvider
from src.providers.smtp import MailpitSMTPProvider
from src.queue.rate_limit import RateLimitExceeded, RedisRateLimiter
from src.queue.retries import dead_letter_message, schedule_retry
from src.queue.warmup import handle_reputation_signal, reserve_warmup_capacity
from src.rendering.templates import TemplateValidationError, render_template

logger = logging.getLogger("mailflow.worker")


class RateLimiter(Protocol):
    def reserve(
        self,
        provider: str,
        recipient_domain: str,
        settings: Settings,
        *,
        now: datetime,
    ) -> None: ...


def _permanent_failure(
    session: Session,
    message: Message,
    reason: str,
) -> None:
    message.status = MessageStatus.FAILED.value
    message.error = reason
    message.next_attempt_at = None
    message.processing_started_at = None
    dead_letter_message(session, message, reason)
    session.add(
        MessageEvent(
            message_id=message.id,
            event_type="failed",
            payload={"reason": "permanent_failure"},
        )
    )
    session.commit()


def _transient_failure(error: Exception) -> tuple[bool, str]:
    if isinstance(error, smtplib.SMTPResponseException):
        code = error.smtp_code
        return 400 <= code < 500, f"SMTP provider response {code}."
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [response[0] for response in error.recipients.values()]
        temporary = bool(codes) and all(400 <= code < 500 for code in codes)
        return temporary, "SMTP provider refused recipient."
    if isinstance(
        error,
        (
            OSError,
            TimeoutError,
            ConnectionError,
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
            smtplib.SMTPException,
        ),
    ):
        return True, f"Temporary provider failure ({type(error).__name__})."
    return False, f"Provider rejected message ({type(error).__name__})."


def _defer_message(
    session: Session,
    message: Message,
    *,
    now: datetime,
    retry_after_seconds: int,
    reason: str,
) -> None:
    message.status = MessageStatus.RETRY.value
    message.error = reason
    message.next_attempt_at = now + timedelta(seconds=max(1, retry_after_seconds))
    message.processing_started_at = None
    message.enqueued_at = None
    session.add(
        MessageEvent(
            message_id=message.id,
            event_type="retry_scheduled",
            payload={"reason": reason},
        )
    )
    session.commit()


def process_persisted_message(
    message_id: str,
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    redis: Redis,
    provider: EmailProvider | None = None,
    limiter: RateLimiter | None = None,
    now: datetime | None = None,
) -> None:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    rate_limiter = limiter or RedisRateLimiter(redis)

    with session_factory() as session:
        message = session.get(Message, message_id)
        if message is None:
            raise ValueError(f"Queued message {message_id} was not found.")
        if message.status not in (MessageStatus.QUEUED.value, MessageStatus.RETRY.value):
            return
        if (
            message.status == MessageStatus.RETRY.value
            and message.next_attempt_at is not None
            and message.next_attempt_at > current_time
        ):
            return

        recipient = session.get(Recipient, message.recipient_id)
        template = session.get(EmailTemplate, message.template_id)
        if recipient is None or template is None:
            _permanent_failure(session, message, "Queued message has invalid references.")
            return

        campaign = (
            session.get(Campaign, message.campaign_id)
            if message.campaign_id is not None
            else None
        )
        if campaign is not None and campaign.status in (
            CampaignStatus.PAUSED.value,
            CampaignStatus.CANCELLED.value,
        ):
            if campaign.status == CampaignStatus.CANCELLED.value:
                message.status = MessageStatus.CANCELLED.value
                message.next_attempt_at = None
                message.enqueued_at = None
            else:
                message.status = MessageStatus.QUEUED.value
                message.enqueued_at = None
            message.processing_started_at = None
            session.commit()
            return

        if (
            recipient.status != RecipientStatus.ACTIVE.value
            or session.scalar(select(Suppression.id).where(Suppression.email == recipient.email))
        ):
            _permanent_failure(session, message, "Recipient is suppressed.")
            return

        try:
            rendered = render_template(
                subject=template.subject,
                html_body=template.html_body,
                text_body=template.text_body,
                required_variables=template.required_variables,
                variables=message.variables,
            )
        except TemplateValidationError as error:
            _permanent_failure(session, message, str(error))
            logger.error("message_template_failed", extra={"message_id": message.id})
            return

        domain = recipient.email.rsplit("@", maxsplit=1)[-1].casefold()
        try:
            reserve_warmup_capacity(redis, domain, settings, now=current_time)
            rate_limiter.reserve(
                "mailpit-smtp",
                domain,
                settings,
                now=current_time,
            )
        except RateLimitExceeded as error:
            _defer_message(
                session,
                message,
                now=current_time,
                retry_after_seconds=error.retry_after_seconds,
                reason="rate_limit",
            )
            logger.warning(
                "message_rate_limited",
                extra={
                    "message_id": message.id,
                    "limit_scope": (
                        "provider"
                        if ":provider:" in error.scope
                        else "destination_domain"
                    ),
                },
            )
            return
        except RedisError as error:
            _defer_message(
                session,
                message,
                now=current_time,
                retry_after_seconds=settings.retry_backoff_base_seconds,
                reason="rate_limit_store_unavailable",
            )
            logger.error(
                "message_rate_limit_store_failed",
                extra={"message_id": message.id, "error_type": type(error).__name__},
            )
            return

        claimed = session.execute(
            update(Message)
            .where(
                Message.id == message_id,
                Message.status.in_(
                    (MessageStatus.QUEUED.value, MessageStatus.RETRY.value)
                ),
            )
            .values(
                status=MessageStatus.PROCESSING.value,
                attempts=Message.attempts + 1,
                processing_started_at=current_time,
                next_attempt_at=None,
            )
        )
        if claimed.rowcount != 1:
            session.rollback()
            return
        session.commit()
        session.refresh(message)

        current_provider = provider or MailpitSMTPProvider(settings)
        try:
            result = current_provider.send(
                EmailPayload(
                    recipient=recipient.email,
                    sender=settings.default_from_email,
                    subject=rendered.subject,
                    html_body=rendered.html_body,
                    text_body=rendered.text_body,
                )
            )
        except (OSError, TimeoutError, ConnectionError, smtplib.SMTPException, ValueError) as error:
            transient, reason = _transient_failure(error)
            if transient:
                retrying = schedule_retry(
                    message,
                    now=current_time,
                    base_seconds=settings.retry_backoff_base_seconds,
                    max_attempts=settings.worker_max_attempts,
                    reason=reason,
                    session=session,
                )
                session.commit()
                logger.warning(
                    "message_retry_scheduled" if retrying else "message_dead_lettered",
                    extra={"message_id": message.id, "attempt": message.attempts},
                )
            else:
                _permanent_failure(session, message, reason)
                logger.error("message_provider_rejected", extra={"message_id": message.id})
            return

        message.status = MessageStatus.SENT.value
        message.provider_message_id = result.provider_message_id
        message.sent_at = current_time
        message.next_attempt_at = None
        message.processing_started_at = None
        message.error = None
        session.add(
            MessageEvent(
                message_id=message.id,
                event_type="sent",
                payload={"provider": result.provider},
            )
        )
        session.commit()

    try:
        handle_reputation_signal(
            session_factory,
            redis,
            settings,
            "sent",
            now=current_time,
        )
    except RedisError as error:
        logger.error(
            "message_reputation_counter_failed",
            extra={"message_id": message_id, "error_type": type(error).__name__},
        )
