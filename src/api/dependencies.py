from collections.abc import Callable, Iterator

from sqlalchemy.orm import Session

from src.config import Settings, get_settings
from src.db.session import session_scope
from src.queue.dispatch import enqueue_message


def get_db() -> Iterator[Session]:
    yield from session_scope()


def get_enqueue_message() -> Callable[[str], None]:
    return enqueue_message


__all__ = ["Settings", "get_db", "get_enqueue_message", "get_settings"]
