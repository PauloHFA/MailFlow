import base64
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone


class UnsubscribeTokenError(ValueError):
    pass


class UnsubscribeTokenExpired(UnsubscribeTokenError):
    pass


_TOKEN_PART = re.compile(r"^[A-Za-z0-9_-]+$")


def _signing_key(secret: str) -> bytes:
    key = secret.encode("utf-8")
    if len(key) < 32:
        raise UnsubscribeTokenError("Unsubscribe signing secret is not configured securely.")
    return key


def create_unsubscribe_token(
    consent_id: str,
    secret: str,
    *,
    now: datetime | None = None,
    lifetime: timedelta = timedelta(days=365),
) -> str:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    if lifetime <= timedelta(0):
        raise ValueError("lifetime must be positive.")
    expires_at = int((current_time + lifetime).timestamp())
    payload = f"{consent_id}.{expires_at}".encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signing_input = f"v1.{encoded_payload}".encode("ascii")
    signature = hmac.new(_signing_key(secret), signing_input, hashlib.sha256).hexdigest()
    return f"v1.{encoded_payload}.{signature}"


def verify_unsubscribe_token(
    token: str,
    secret: str,
    *,
    now: datetime | None = None,
) -> str:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not _TOKEN_PART.fullmatch(parts[1]):
        raise UnsubscribeTokenError("Unsubscribe token is invalid.")
    try:
        signature = bytes.fromhex(parts[2])
    except ValueError as error:
        raise UnsubscribeTokenError("Unsubscribe token is invalid.") from error
    if len(signature) != hashlib.sha256().digest_size:
        raise UnsubscribeTokenError("Unsubscribe token is invalid.")

    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.new(_signing_key(secret), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise UnsubscribeTokenError("Unsubscribe token is invalid.")

    try:
        padding = "=" * (-len(parts[1]) % 4)
        consent_id, expiry = base64.urlsafe_b64decode(
            parts[1] + padding
        ).decode("utf-8").rsplit(".", maxsplit=1)
        expires_at = int(expiry)
    except (ValueError, UnicodeDecodeError) as error:
        raise UnsubscribeTokenError("Unsubscribe token is invalid.") from error
    if not consent_id or len(consent_id) > 36:
        raise UnsubscribeTokenError("Unsubscribe token is invalid.")

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must include a timezone.")
    if int(current_time.timestamp()) > expires_at:
        raise UnsubscribeTokenExpired("Unsubscribe token has expired.")
    return consent_id
