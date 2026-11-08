from email import policy
from email.parser import BytesParser

import pytest

from src.config import Settings, UnsafeRecipientError
from src.providers.base import EmailPayload
from src.providers.smtp import MailpitSMTPProvider


def test_mailpit_provider_builds_html_and_text_alternatives() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    provider = MailpitSMTPProvider(settings)

    message = provider.build_message(
        EmailPayload(
            recipient="qa@example.com",
            sender="MailFlow <no-reply@mailflow.local>",
            subject="Monthly report",
            html_body="<p>Hello</p>",
            text_body="Hello",
        )
    )

    parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())

    assert parsed["To"] == "qa@example.com"
    assert parsed["Subject"] == "Monthly report"
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == "Hello"
    assert parsed.get_body(preferencelist=("html",)).get_content().strip() == "<p>Hello</p>"


def test_mailpit_provider_refuses_non_allowlisted_recipients() -> None:
    settings = Settings(_env_file=None, app_env="development")
    provider = MailpitSMTPProvider(settings)

    with pytest.raises(UnsafeRecipientError):
        provider.build_message(
            EmailPayload(
                recipient="customer@example.com",
                sender="MailFlow <no-reply@mailflow.local>",
                subject="Hello",
                html_body="<p>Hello</p>",
                text_body="Hello",
            )
        )


def test_mailpit_provider_rejects_header_injection() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    provider = MailpitSMTPProvider(settings)

    with pytest.raises(ValueError, match="line break"):
        provider.build_message(
            EmailPayload(
                recipient="qa@example.com",
                sender="MailFlow <no-reply@mailflow.local>",
                subject="Hello\r\nBcc: customer@example.com",
                html_body="<p>Hello</p>",
                text_body="Hello",
            )
        )
