import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.dependencies import get_db, require_operations_access
from src.db.models import Consent, Message, Recipient

router = APIRouter(
    prefix="/api/v1/exports",
    tags=["exports"],
    dependencies=[Depends(require_operations_access)],
)
MAX_EXPORT_ROWS = 50_000
MAX_EXPORT_RANGE = timedelta(days=366)


def _period(
    start_at: datetime | None,
    end_at: datetime | None,
) -> tuple[datetime, datetime]:
    end = end_at or datetime.now(timezone.utc)
    start = start_at or end - timedelta(days=30)
    if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
        raise HTTPException(status_code=422, detail="Export period must include timezone offsets.")
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end_utc <= start_utc or end_utc - start_utc > MAX_EXPORT_RANGE:
        raise HTTPException(
            status_code=422,
            detail="Export period must be positive and no longer than 366 days.",
        )
    return start_utc, end_utc


def _csv_response(filename: str, headers: list[str], rows: list[list[object]]) -> Response:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows([[_csv_value(value) for value in row] for row in rows])
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_value(value: object) -> object:
    if isinstance(value, str) and (
        value.startswith(("=", "+", "-", "@", "\t", "\r"))
        or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return f"'{value}"
    return value


def _enforce_export_size(row_count: int) -> None:
    if row_count > MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail="Export exceeds the 50,000 row limit; narrow the requested period.",
        )


@router.get("/messages.csv")
def export_messages(
    start_at: Annotated[datetime | None, Query()] = None,
    end_at: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db),
) -> Response:
    start, end = _period(start_at, end_at)
    records = session.execute(
        select(
            Message.id,
            Recipient.email,
            Message.message_type,
            Message.status,
            Message.created_at,
            Message.sent_at,
            Message.provider_message_id,
        )
        .join(Recipient, Recipient.id == Message.recipient_id)
        .where(Message.created_at >= start, Message.created_at < end)
        .order_by(Message.created_at, Message.id)
        .limit(MAX_EXPORT_ROWS + 1)
    ).all()
    _enforce_export_size(len(records))
    return _csv_response(
        f"mailflow-messages-{start:%Y%m%d}-{end:%Y%m%d}.csv",
        [
            "message_id",
            "recipient_email",
            "message_type",
            "status",
            "created_at",
            "sent_at",
            "provider_message_id",
        ],
        [
            [
                value.isoformat() if isinstance(value, datetime) else value or ""
                for value in row
            ]
            for row in records
        ],
    )


@router.get("/consents.csv")
def export_consents(
    start_at: Annotated[datetime | None, Query()] = None,
    end_at: Annotated[datetime | None, Query()] = None,
    session: Session = Depends(get_db),
) -> Response:
    start, end = _period(start_at, end_at)
    records = session.execute(
        select(
            Consent.id,
            Recipient.email,
            Consent.purpose,
            Consent.legal_basis,
            Consent.source,
            Consent.registered_at,
            Consent.revoked_at,
        )
        .join(Recipient, Recipient.id == Consent.recipient_id)
        .where(Consent.registered_at >= start, Consent.registered_at < end)
        .order_by(Consent.registered_at, Consent.id)
        .limit(MAX_EXPORT_ROWS + 1)
    ).all()
    _enforce_export_size(len(records))
    return _csv_response(
        f"mailflow-consents-{start:%Y%m%d}-{end:%Y%m%d}.csv",
        [
            "consent_id",
            "recipient_email",
            "purpose",
            "legal_basis",
            "source",
            "registered_at",
            "revoked_at",
        ],
        [
            [
                value.isoformat() if isinstance(value, datetime) else value or ""
                for value in row
            ]
            for row in records
        ],
    )
