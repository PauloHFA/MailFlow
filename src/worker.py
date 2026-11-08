import logging
import smtplib
from datetime import datetime, timezone

from celery import Celery
from sqlalchemy import select, update

from src.config import Settings, get_settings
from src.db.models import (
    EmailTemplate,
    Message,
    MessageEvent,
    MessageStatus,
    Recipient,
    Suppression,
)
from src.db.session import get_session_factory
from src.providers.base import EmailPayload
from src.providers.smtp import MailpitSMTPProvider
from src.rendering.templates import TemplateValidationError, render_template

settings = Settings()
celery_app = Celery("mailflow", broker=settings.redis_url)
celery_app.conf.update(
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "dispatch-due-campaign-schedules": {
            "task": "src.worker.dispatch_due_schedules",
            "schedule": 60.0,
        }
    },
)

logger = logging.getLogger("mailflow.worker")


@celery_app.task(name="src.worker.process_message", ignore_result=True)
def process_message(message_id: str) -> None:
    current_settings = get_settings()
    session_factory = get_session_factory(current_settings.database_url)
    with session_factory() as session:
        message = session.get(Message, message_id)
        if message is None:
            raise ValueError(f"Queued message {message_id} was not found.")
        if message.status != MessageStatus.QUEUED.value:
            return

        recipient = session.get(Recipient, message.recipient_id)
        template = session.get(EmailTemplate, message.template_id)
        if recipient is None or template is None:
            raise ValueError(f"Queued message {message_id} has invalid references.")

        if recipient.status != "active" or session.scalar(
            select(Suppression.id).where(Suppression.email == recipient.email)
        ):
            message.status = MessageStatus.FAILED.value
            message.error = "Recipient is suppressed."
            session.add(
                MessageEvent(
                    message_id=message.id,
                    event_type="blocked",
                    payload={"reason": "suppressed"},
                )
            )
            session.commit()
            return

        claimed = session.execute(
            update(Message)
            .where(
                Message.id == message_id,
                Message.status == MessageStatus.QUEUED.value,
            )
            .values(
                status=MessageStatus.PROCESSING.value,
                attempts=Message.attempts + 1,
            )
        )
        if claimed.rowcount != 1:
            session.rollback()
            return
        session.commit()

        try:
            rendered = render_template(
                subject=template.subject,
                html_body=template.html_body,
                text_body=template.text_body,
                required_variables=template.required_variables,
                variables=message.variables,
            )
        except TemplateValidationError as error:
            message.status = MessageStatus.FAILED.value
            message.error = str(error)
            session.add(
                MessageEvent(
                    message_id=message.id,
                    event_type="failed",
                    payload={"reason": "template_validation"},
                )
            )
            session.commit()
            logger.error("message_template_failed", extra={"message_id": message.id})
            return

        provider = MailpitSMTPProvider(current_settings)
        try:
            result = provider.send(
                EmailPayload(
                    recipient=recipient.email,
                    sender=current_settings.default_from_email,
                    subject=rendered.subject,
                    html_body=rendered.html_body,
                    text_body=rendered.text_body,
                )
            )
        except (OSError, smtplib.SMTPException, ValueError):
            message.status = MessageStatus.FAILED.value
            message.error = "Email provider rejected or could not accept the message."
            session.add(
                MessageEvent(
                    message_id=message.id,
                    event_type="failed",
                    payload={"reason": "provider_error"},
                )
            )
            session.commit()
            logger.exception("message_provider_failed", extra={"message_id": message.id})
            raise

        message.status = MessageStatus.SENT.value
        message.provider_message_id = result.provider_message_id
        message.sent_at = datetime.now(timezone.utc)
        message.error = None
        session.add(
            MessageEvent(
                message_id=message.id,
                event_type="sent",
                payload={"provider": result.provider},
            )
        )
        session.commit()


@celery_app.task(name="src.worker.dispatch_due_schedules", ignore_result=True)
def dispatch_scheduled_messages() -> int:
    from src.queue.dispatch import enqueue_message
    from src.scheduler.dispatch import dispatch_due_schedules

    current_settings = get_settings()
    session_factory = get_session_factory(current_settings.database_url)
    return dispatch_due_schedules(
        session_factory,
        now=datetime.now(timezone.utc),
        enqueue=enqueue_message,
    )
