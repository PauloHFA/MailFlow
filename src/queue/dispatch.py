from src.worker import celery_app


def enqueue_message(message_id: str) -> None:
    celery_app.send_task("src.worker.process_message", args=[message_id])
