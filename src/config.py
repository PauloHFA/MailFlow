from typing import Literal
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
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
    email_provider: Literal["mailpit", "resend", "brevo"] = "mailpit"
    resend_api_key: SecretStr = SecretStr("")
    brevo_api_key: SecretStr = SecretStr("")
    default_from_email: str = "MailFlow <no-reply@mailflow.local>"
    database_url: str = "postgresql+psycopg://mailflow:mailflow@postgres:5432/mailflow"
    redis_url: str = "redis://redis:6379/0"
    provider_rate_per_second: int = Field(default=10, ge=1)
    provider_daily_limit: int = Field(default=10000, ge=1)
    recipient_domain_rate_per_second: int = Field(default=5, ge=1)
    recipient_domain_daily_limit: int = Field(default=5000, ge=1)
    worker_max_attempts: int = Field(default=5, ge=1)
    retry_backoff_base_seconds: int = Field(default=2, ge=1)
    processing_visibility_timeout_seconds: int = Field(default=300, ge=1)
    warmup_enabled: bool = False
    warmup_daily_limit: int = Field(default=500, ge=1)
    warmup_domain_daily_limit: int = Field(default=50, ge=1)
    reputation_bounce_threshold: float = Field(default=0.05, gt=0, le=1)
    reputation_complaint_threshold: float = Field(default=0.001, gt=0, le=1)
    webhook_signing_secret: str = ""
    webhook_timestamp_tolerance_seconds: int = Field(default=300, ge=1)
    resend_webhook_secret: SecretStr = SecretStr("")
    brevo_webhook_token: SecretStr = SecretStr("")
    suppression_hash_secret: SecretStr = SecretStr("")
    unsubscribe_signing_secret: str = ""
    unsubscribe_base_url: str = "http://localhost:8000/api/v1/unsubscribe"
    operations_api_token: str = ""
    daily_report_enabled: bool = False
    report_recipient_email: str = ""
    message_retention_days: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_selected_provider_credentials(self) -> "Settings":
        if self.email_provider == "resend" and not self.resend_api_key.get_secret_value().strip():
            raise ValueError("RESEND_API_KEY is required when EMAIL_PROVIDER=resend.")
        if self.email_provider == "brevo" and not self.brevo_api_key.get_secret_value().strip():
            raise ValueError("BREVO_API_KEY is required when EMAIL_PROVIDER=brevo.")
        return self

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
