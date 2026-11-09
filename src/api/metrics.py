from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.dependencies import get_db, require_operations_access
from src.db.models import Message, MessageEvent

router = APIRouter(
    prefix="/api/v1/metrics",
    tags=["delivery metrics"],
    dependencies=[Depends(require_operations_access)],
)
MAX_METRIC_RANGE = timedelta(days=366)


def _validate_period(
    start_at: datetime | None,
    end_at: datetime | None,
) -> tuple[datetime, datetime]:
    end = end_at or datetime.now(timezone.utc)
    start = start_at or end - timedelta(days=1)
    if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
        raise HTTPException(status_code=422, detail="Metric period must include timezone offsets.")
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end_utc <= start_utc or end_utc - start_utc > MAX_METRIC_RANGE:
        raise HTTPException(
            status_code=422,
            detail="Metric period must be positive and no longer than 366 days.",
        )
    return start_utc, end_utc


@router.get("/delivery")
def get_delivery_metrics(
    start_at: Annotated[datetime | None, Query()] = None,
    end_at: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    start, end = _validate_period(start_at, end_at)
    message_rows = session.execute(
        select(Message.status, func.count(Message.id))
        .where(Message.created_at >= start, Message.created_at < end)
        .group_by(Message.status)
    ).all()
    event_rows = session.execute(
        select(MessageEvent.event_type, func.count(MessageEvent.id))
        .where(MessageEvent.occurred_at >= start, MessageEvent.occurred_at < end)
        .group_by(MessageEvent.event_type)
    ).all()
    return {
        "period": {"start_at": start, "end_at": end},
        "messages": {str(name): int(count) for name, count in message_rows},
        "events": {str(name): int(count) for name, count in event_rows},
    }
