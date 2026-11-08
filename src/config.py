from typing import Literal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class UnsafeRecipientError(ValueError):
    """Raised when a non-production send targets an unapproved recipient."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    test_recipient_allowlist: str = ""
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    default_from_email: str = "MailFlow <no-reply@mailflow.local>"
    database_url: str = "postgresql+psycopg://mailflow:mailflow@postgres:5432/mailflow"
    redis_url: str = "redis://redis:6379/0"

    @property
    def allowed_test_recipients(self) -> frozenset[str]:
        return frozenset(
            address.strip().casefold()
            for address in self.test_recipient_allowlist.split(",")
            if address.strip()
        )


def validate_recipient(settings: Settings, recipient: str) -> str:
    normalized = recipient.strip().casefold()
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise UnsafeRecipientError("Recipient address is empty or contains line breaks.")

    if settings.app_env != "production" and normalized not in settings.allowed_test_recipients:
        raise UnsafeRecipientError(
            "Recipient is not in TEST_RECIPIENT_ALLOWLIST; non-production sends are blocked."
        )

    return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
