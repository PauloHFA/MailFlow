from datetime import datetime, timezone

from celery import Celery

from src.config import Settings, get_settings
from src.db.session import get_session_factory
from src.providers.smtp import MailpitSMTPProvider
from src.queue.rate_limit import redis_client
from src.queue.retries import (
    dispatch_pending_messages as dispatch_pending_message_rows,
    recover_stale_messages,
)
from src.queue.tasks import process_persisted_message

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
        },
        "recover-and-dispatch-pending-messages": {
            "task": "src.worker.dispatch_pending_messages",
            "schedule": 30.0,
        },
    },
)


@celery_app.task(name="src.worker.process_message", ignore_result=True)
def process_message(message_id: str) -> None:
    current_settings = get_settings()
    session_factory = get_session_factory(current_settings.database_url)
    redis = redis_client(current_settings.redis_url)
    try:
        process_persisted_message(
            message_id,
            settings=current_settings,
            session_factory=session_factory,
            redis=redis,
            provider=MailpitSMTPProvider(current_settings),
        )
    finally:
        redis.close()


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


@celery_app.task(name="src.worker.dispatch_pending_messages", ignore_result=True)
def dispatch_pending_and_recover_messages() -> int:
    from src.queue.dispatch import enqueue_message

    current_settings = get_settings()
    session_factory = get_session_factory(current_settings.database_url)
    now = datetime.now(timezone.utc)
    recover_stale_messages(
        session_factory,
        now=now,
        visibility_timeout_seconds=current_settings.processing_visibility_timeout_seconds,
        max_attempts=current_settings.worker_max_attempts,
    )
    return dispatch_pending_message_rows(
        session_factory,
        now=now,
        enqueue=enqueue_message,
    )
