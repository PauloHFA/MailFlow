import pytest

from src.config import Settings, UnsafeRecipientError, validate_recipient


def test_non_production_rejects_recipients_outside_allowlist() -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        test_recipient_allowlist="qa@example.com",
    )

    with pytest.raises(UnsafeRecipientError):
        validate_recipient(settings, "customer@example.com")


def test_non_production_normalizes_allowlisted_recipient() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="QA@example.com",
    )

    assert validate_recipient(settings, " qa@example.com ") == "qa@example.com"


def test_empty_non_production_allowlist_denies_all_recipients() -> None:
    settings = Settings(_env_file=None, app_env="staging")

    with pytest.raises(UnsafeRecipientError):
        validate_recipient(settings, "qa@example.com")


def test_production_does_not_apply_test_allowlist() -> None:
    settings = Settings(_env_file=None, app_env="production")

    assert validate_recipient(settings, "customer@example.com") == "customer@example.com"
