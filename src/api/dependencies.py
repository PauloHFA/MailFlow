from collections.abc import Callable, Iterator
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from src.config import Settings, get_settings
from src.db.session import session_scope
from src.queue.dispatch import enqueue_message


def get_db() -> Iterator[Session]:
    yield from session_scope()


def get_enqueue_message() -> Callable[[str], None]:
    return enqueue_message


def require_operations_access(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if len(settings.operations_api_token.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=503,
            detail="Operations API access is not configured.",
        )
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not supplied_token
        or not secrets.compare_digest(
            supplied_token.encode("utf-8"),
            settings.operations_api_token.encode("utf-8"),
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="A valid operations bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


__all__ = [
    "Settings",
    "get_db",
    "get_enqueue_message",
    "get_settings",
    "require_operations_access",
]
