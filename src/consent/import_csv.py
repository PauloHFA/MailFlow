import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from email_validator import EmailNotValidError, validate_email

CONSENT_COLUMNS = {
    "consent_purpose",
    "legal_basis",
    "consent_source",
    "consented_at",
}


@dataclass(frozen=True)
class ImportedRecipient:
    email: str
    name: str | None
    timezone: str | None
    purpose: str | None = None
    legal_basis: str | None = None
    consent_source: str | None = None
    consented_at: datetime | None = None
    row_number: int = 0


@dataclass(frozen=True)
class RejectedRow:
    row: int
    reason: str


@dataclass
class ImportResult:
    imported: list[ImportedRecipient] = field(default_factory=list)
    duplicates: int = 0
    rejected: list[RejectedRow] = field(default_factory=list)


class CSVImportError(ValueError):
    """Raised when an uploaded CSV cannot be interpreted safely."""


def parse_recipients_csv(content: bytes, existing_emails: set[str]) -> ImportResult:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CSVImportError("CSV must be encoded as UTF-8.") from error

    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    if reader.fieldnames is None:
        raise CSVImportError("CSV must include an email column.")

    normalized_headers = [header.strip().casefold() for header in reader.fieldnames]
    if "email" not in normalized_headers:
        raise CSVImportError("CSV must include an email column.")
    if len(set(normalized_headers)) != len(normalized_headers):
        raise CSVImportError("CSV column names must be unique.")

    headers = dict(zip(reader.fieldnames, normalized_headers, strict=True))
    available_consent_columns = CONSENT_COLUMNS.intersection(normalized_headers)
    if available_consent_columns and available_consent_columns != CONSENT_COLUMNS:
        raise CSVImportError(
            "CSV consent columns must include consent_purpose, legal_basis, "
            "consent_source, and consented_at together."
        )

    result = ImportResult()
    seen = {email.casefold() for email in existing_emails}
    for row_number, raw_row in enumerate(reader, start=2):
        if None in raw_row:
            result.rejected.append(RejectedRow(row_number, "Row contains more values than the header."))
            continue
        row = {
            headers[column]: (value.strip() if value is not None else "")
            for column, value in raw_row.items()
        }
        try:
            email = validate_email(
                row.get("email", ""),
                check_deliverability=False,
            ).normalized.casefold()
        except EmailNotValidError:
            result.rejected.append(RejectedRow(row_number, "Invalid email address."))
            continue

        if email in seen:
            result.duplicates += 1
            continue

        recipient_timezone = row.get("timezone") or None
        if recipient_timezone:
            try:
                ZoneInfo(recipient_timezone)
            except ZoneInfoNotFoundError:
                result.rejected.append(RejectedRow(row_number, "Invalid IANA timezone."))
                continue

        consent_values = [row.get(column, "") for column in CONSENT_COLUMNS]
        has_consent = any(consent_values)
        if has_consent and not all(consent_values):
            result.rejected.append(RejectedRow(row_number, "Consent fields must all be provided together."))
            continue

        consented_at: datetime | None = None
        if has_consent:
            try:
                consented_at = datetime.fromisoformat(row["consented_at"].replace("Z", "+00:00"))
            except ValueError:
                result.rejected.append(RejectedRow(row_number, "Invalid consented_at timestamp."))
                continue
            if consented_at.tzinfo is None or consented_at.utcoffset() is None:
                result.rejected.append(RejectedRow(row_number, "consented_at must include a timezone."))
                continue
            consented_at = consented_at.astimezone(timezone.utc)

        result.imported.append(
            ImportedRecipient(
                email=email,
                name=row.get("name") or None,
                timezone=recipient_timezone,
                purpose=row.get("consent_purpose") or None,
                legal_basis=row.get("legal_basis") or None,
                consent_source=row.get("consent_source") or None,
                consented_at=consented_at,
                row_number=row_number,
            )
        )
        seen.add(email)

    return result
