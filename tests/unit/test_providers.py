from email import policy
from email.parser import BytesParser

import pytest

from src.config import Settings, UnsafeRecipientError
from src.providers.base import EmailAttachment, EmailPayload
from src.providers.headers import promotional_unsubscribe_headers
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


def test_mailpit_provider_preserves_valid_one_click_unsubscribe_headers() -> None:
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
            headers={
                "List-Unsubscribe": "<https://mailflow.example/unsubscribe?token=abc>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        )
    )

    assert str(message["List-Unsubscribe"]) == (
        "<https://mailflow.example/unsubscribe?token=abc>"
    )
    assert str(message["List-Unsubscribe-Post"]) == "List-Unsubscribe=One-Click"


def test_mailpit_provider_rejects_newlines_in_additional_headers() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    provider = MailpitSMTPProvider(settings)

    with pytest.raises(ValueError, match="Additional email headers"):
        provider.build_message(
            EmailPayload(
                recipient="qa@example.com",
                sender="MailFlow <no-reply@mailflow.local>",
                subject="Hello",
                html_body="<p>Hello</p>",
                text_body="Hello",
                headers={"List-Unsubscribe": "<https://safe.test>\r\nBcc: bad@example.com"},
            )
        )


def test_promotional_unsubscribe_url_requires_https_in_production() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        unsubscribe_base_url="http://mailflow.example/api/v1/unsubscribe",
    )

    with pytest.raises(ValueError, match="safe absolute HTTP"):
        promotional_unsubscribe_headers("signed-token", settings)


def test_smtp_provider_attaches_only_small_valid_pdfs() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    provider = MailpitSMTPProvider(settings)
    attachment = EmailAttachment(
        filename="report.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.4\nreport\n%%EOF",
    )

    message = provider.build_message(
        EmailPayload(
            recipient="qa@example.com",
            sender="MailFlow <no-reply@mailflow.local>",
            subject="Report",
            html_body="<p>Report</p>",
            text_body="Report",
            attachments=(attachment,),
        )
    )

    parts = list(message.iter_attachments())
    assert len(parts) == 1
    assert parts[0].get_filename() == "report.pdf"
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == attachment.content


def test_smtp_provider_enforces_five_megabyte_total_attachment_limit() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    provider = MailpitSMTPProvider(settings)

    with pytest.raises(ValueError, match="5 MB total limit"):
        provider.build_message(
            EmailPayload(
                recipient="qa@example.com",
                sender="MailFlow <no-reply@mailflow.local>",
                subject="Report",
                html_body="<p>Report</p>",
                text_body="Report",
                attachments=(
                    EmailAttachment(
                        filename="report.pdf",
                        content_type="application/pdf",
                        content=b"%PDF-" + b"x" * (5 * 1024 * 1024),
                    ),
                ),
            )
        )


@pytest.mark.parametrize(
    "filename,content_type,content",
    [
        ("report.txt", "text/plain", b"report"),
        ("../report.pdf", "application/pdf", b"%PDF-1.4\n%%EOF"),
        ("report.pdf", "application/pdf", b"not a pdf"),
    ],
)
def test_smtp_provider_rejects_unsafe_or_unrecognized_attachments(
    filename: str,
    content_type: str,
    content: bytes,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_recipient_allowlist="qa@example.com",
    )
    provider = MailpitSMTPProvider(settings)

    with pytest.raises(ValueError, match="PDF email attachments"):
        provider.build_message(
            EmailPayload(
                recipient="qa@example.com",
                sender="MailFlow <no-reply@mailflow.local>",
                subject="Report",
                html_body="<p>Report</p>",
                text_body="Report",
                attachments=(
                    EmailAttachment(
                        filename=filename,
                        content_type=content_type,
                        content=content,
                    ),
                ),
            )
        )
