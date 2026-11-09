import hashlib
import hmac

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.db.models import Suppression


def suppression_digest(email: str, secret: str) -> str:
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("SUPPRESSION_HASH_SECRET must contain at least 32 bytes.")
    normalized_email = email.strip().casefold()
    digest = hmac.new(
        secret.encode("utf-8"),
        normalized_email.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def is_suppressed(session: Session, email: str, secret: str) -> bool:
    normalized_email = email.strip().casefold()
    if session.scalar(
        select(Suppression.id).where(Suppression.email == normalized_email)
    ) is not None:
        return True
    if len(secret.encode("utf-8")) < 32:
        has_erased_suppressions = session.scalar(
            select(Suppression.id).where(Suppression.email.like("hmac-sha256:%")).limit(1)
        )
        if has_erased_suppressions is not None:
            raise ValueError(
                "SUPPRESSION_HASH_SECRET is required to enforce erased-recipient suppressions."
            )
        return False
    digest = suppression_digest(normalized_email, secret)
    return session.scalar(
        select(Suppression.id).where(
            or_(
                Suppression.email == normalized_email,
                Suppression.email == digest,
            )
        )
    ) is not None
