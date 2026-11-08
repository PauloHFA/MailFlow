from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.dependencies import get_db
from src.consent.import_csv import CSVImportError, parse_recipients_csv
from src.db.models import Consent, Recipient, RecipientStatus

router = APIRouter(prefix="/api/v1/recipients", tags=["recipients"])
MAX_CSV_BYTES = 5 * 1024 * 1024


class ConsentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    purpose: str = Field(min_length=1, max_length=120)
    legal_basis: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=255)
    registered_at: datetime | None = None

    @model_validator(mode="after")
    def require_timezone(self) -> "ConsentCreate":
        if self.registered_at is not None and (
            self.registered_at.tzinfo is None or self.registered_at.utcoffset() is None
        ):
            raise ValueError("registered_at must include a timezone.")
        return self

    @field_validator("purpose", "legal_basis", "source")
    @classmethod
    def reject_line_breaks(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Consent fields must not contain line breaks.")
        return value


@router.post("/import", status_code=status.HTTP_200_OK)
async def import_recipients(
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    content = await file.read(MAX_CSV_BYTES + 1)
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV exceeds the 5 MB size limit.")

    existing_emails = set(
        session.scalars(select(Recipient.email)).all()
    )
    try:
        result = parse_recipients_csv(content, existing_emails)
    except CSVImportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    imported_response: list[dict[str, str]] = []
    for imported in result.imported:
        recipient = Recipient(
            email=imported.email,
            name=imported.name,
            timezone=imported.timezone,
            status=RecipientStatus.ACTIVE.value,
        )
        session.add(recipient)
        session.flush()
        imported_response.append({"id": recipient.id, "email": recipient.email})
        if imported.purpose is not None:
            session.add(
                Consent(
                    recipient_id=recipient.id,
                    purpose=imported.purpose,
                    legal_basis=imported.legal_basis,
                    source=imported.consent_source,
                    registered_at=imported.consented_at,
                )
            )

    session.commit()
    rejected = [
        {"row": row.row, "reason": row.reason}
        for row in result.rejected
    ]
    return {
        "imported_count": len(imported_response),
        "duplicate_count": result.duplicates,
        "rejected_count": len(rejected),
        "imported": imported_response,
        "rejected": rejected,
    }


@router.post("/{recipient_id}/consents", status_code=status.HTTP_201_CREATED)
def create_consent(
    recipient_id: str,
    payload: ConsentCreate,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    recipient = session.get(Recipient, recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found.")

    consent = Consent(
        recipient_id=recipient.id,
        purpose=payload.purpose,
        legal_basis=payload.legal_basis,
        source=payload.source,
        registered_at=(payload.registered_at or datetime.now(timezone.utc)).astimezone(
            timezone.utc
        ),
    )
    session.add(consent)
    session.commit()
    session.refresh(consent)
    return {
        "id": consent.id,
        "recipient_id": consent.recipient_id,
        "purpose": consent.purpose,
        "legal_basis": consent.legal_basis,
        "source": consent.source,
        "registered_at": consent.registered_at,
        "revoked_at": consent.revoked_at,
    }


@router.delete("/{recipient_id}/consents/{consent_id}")
def revoke_consent(
    recipient_id: str,
    consent_id: str,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    consent = session.scalar(
        select(Consent).where(
            Consent.id == consent_id,
            Consent.recipient_id == recipient_id,
        )
    )
    if consent is None:
        raise HTTPException(status_code=404, detail="Consent record not found.")
    if consent.revoked_at is None:
        consent.revoked_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(consent)
    return {
        "id": consent.id,
        "recipient_id": consent.recipient_id,
        "purpose": consent.purpose,
        "revoked_at": consent.revoked_at,
    }
