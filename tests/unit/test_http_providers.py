import base64

import httpx
import pytest

from src.config import Settings
from src.providers.base import (
    EmailAttachment,
    EmailPayload,
    ProviderAmbiguousError,
    ProviderPermanentError,
    ProviderTransientError,
)
from src.providers.brevo import BrevoEmailProvider
from src.providers.factory import build_email_provider, rate_limit_provider_name
from src.providers.resend import ResendEmailProvider


def _settings(**values: object) -> Settings:
    return Settings(
        _env_file=None,
        app_env="staging",
        test_recipient_allowlist="qa@example.com",
        **values,
    )


def _payload(**values: object) -> EmailPayload:
    idempotency_key = values.pop("idempotency_key", "test-message-key")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise TypeError("idempotency_key must be a string or None.")
    return EmailPayload(
        recipient="qa@example.com",
        sender="MailFlow <no-reply@mailflow.example>",
        subject="Verification",
        html_body="<p>HTML</p>",
        text_body="Plain text",
        idempotency_key=idempotency_key,
        **values,
    )


def test_resend_sends_allowlisted_message_with_headers_and_pdf() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "resend-message-1"})

    attachment = EmailAttachment(
        filename="report.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.4\nreport\n%%EOF",
    )
    provider = ResendEmailProvider(
        _settings(resend_api_key="test-resend-key"),
        transport=httpx.MockTransport(handle),
    )

    result = provider.send(
        _payload(
            idempotency_key="message-key-1",
            headers={
                "List-Unsubscribe": "<https://mailflow.example/unsubscribe?token=x>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
            attachments=(attachment,),
        )
    )

    request = captured[0]
    body = __import__("json").loads(request.content)
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == "Bearer test-resend-key"
    assert request.headers["Idempotency-Key"] == "message-key-1"
    assert body["from"] == "MailFlow <no-reply@mailflow.example>"
    assert body["to"] == ["qa@example.com"]
    assert body["text"] == "Plain text"
    assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert body["attachments"] == [
        {
            "filename": "report.pdf",
            "content": base64.b64encode(attachment.content).decode("ascii"),
        }
    ]
    assert result.provider == "resend"
    assert result.provider_message_id == "resend-message-1"


def test_brevo_sends_its_payload_shape_and_api_key_header() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"messageId": "brevo-message-2"})

    provider = BrevoEmailProvider(
        _settings(brevo_api_key="test-brevo-key"),
        transport=httpx.MockTransport(handle),
    )

    result = provider.send(_payload(idempotency_key="message-key-2"))

    request = captured[0]
    body = __import__("json").loads(request.content)
    assert str(request.url) == "https://api.brevo.com/v3/smtp/email"
    assert request.headers["api-key"] == "test-brevo-key"
    assert body["sender"] == {
        "email": "no-reply@mailflow.example",
        "name": "MailFlow",
    }
    assert body["to"] == [{"email": "qa@example.com"}]
    assert body["htmlContent"] == "<p>HTML</p>"
    assert body["textContent"] == "Plain text"
    assert body["headers"]["X-Mailin-custom"] == "message-key-2"
    assert result.provider == "brevo"
    assert result.provider_message_id == "brevo-message-2"


@pytest.mark.parametrize("provider_type,key_name,key", [
    (ResendEmailProvider, "resend_api_key", "resend-key"),
    (BrevoEmailProvider, "brevo_api_key", "brevo-key"),
])
@pytest.mark.parametrize("status_code", [429])
def test_http_providers_classify_retryable_responses(
    provider_type: type[ResendEmailProvider] | type[BrevoEmailProvider],
    key_name: str,
    key: str,
    status_code: int,
) -> None:
    provider = provider_type(
        _settings(**{key_name: key}),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, json={"message": "failure"})
        ),
    )

    with pytest.raises(ProviderTransientError, match=str(status_code)):
        provider.send(_payload())


@pytest.mark.parametrize("status_code", [408, 500, 503])
def test_resend_can_retry_ambiguous_http_statuses_with_idempotency_key(
    status_code: int,
) -> None:
    provider = ResendEmailProvider(
        _settings(resend_api_key="test-resend-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, json={"message": "failure"})
        ),
    )

    with pytest.raises(ProviderTransientError, match=str(status_code)):
        provider.send(_payload(idempotency_key="message-key"))


@pytest.mark.parametrize("status_code", [408, 500, 503])
def test_brevo_stops_automatic_retry_after_ambiguous_http_status(
    status_code: int,
) -> None:
    provider = BrevoEmailProvider(
        _settings(brevo_api_key="test-brevo-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, json={"message": "failure"})
        ),
    )

    with pytest.raises(ProviderAmbiguousError, match="automatic retry was stopped"):
        provider.send(_payload(idempotency_key="message-key"))


def test_brevo_read_timeout_is_not_retried_automatically() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response timed out", request=request)

    provider = BrevoEmailProvider(
        _settings(brevo_api_key="test-brevo-key"),
        transport=httpx.MockTransport(fail),
    )

    with pytest.raises(ProviderAmbiguousError, match="outcome is unknown"):
        provider.send(_payload(idempotency_key="message-key"))


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 307])
def test_http_providers_reject_permanent_and_redirect_responses(status_code: int) -> None:
    provider = ResendEmailProvider(
        _settings(resend_api_key="test-resend-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, headers={"Location": "https://evil.example"})
        ),
    )

    with pytest.raises(ProviderPermanentError, match=str(status_code)):
        provider.send(_payload())


def test_http_providers_reject_missing_message_id_and_header_auth_overrides() -> None:
    provider = ResendEmailProvider(
        _settings(resend_api_key="test-resend-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    with pytest.raises(ProviderPermanentError, match="message ID"):
        provider.send(_payload())

    with pytest.raises(ValueError, match="Additional email headers"):
        provider.send(_payload(headers={"Authorization": "Bearer attacker"}))


def test_http_provider_masks_network_errors_and_respects_test_allowlist() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret test-resend-key for qa@example.com", request=request)

    provider = ResendEmailProvider(
        _settings(resend_api_key="test-resend-key"),
        transport=httpx.MockTransport(fail),
    )
    with pytest.raises(ProviderTransientError) as error:
        provider.send(_payload())
    assert "test-resend-key" not in str(error.value)
    assert "qa@example.com" not in str(error.value)

    with pytest.raises(ValueError, match="TEST_RECIPIENT_ALLOWLIST"):
        provider.send(
            EmailPayload(
                recipient="outside@example.com",
                sender="no-reply@mailflow.example",
                subject="No external send",
                html_body="<p>Test</p>",
                text_body="Test",
            )
        )


def test_provider_factory_selects_provider_and_requires_credentials() -> None:
    settings = _settings()
    assert build_email_provider(settings).__class__.__name__ == "MailpitSMTPProvider"
    assert rate_limit_provider_name(settings) == "mailpit-smtp"

    resend = _settings(email_provider="resend", resend_api_key="test-resend-key")
    assert isinstance(build_email_provider(resend), ResendEmailProvider)
    assert rate_limit_provider_name(resend) == "resend"
    brevo = _settings(email_provider="brevo", brevo_api_key="test-brevo-key")
    assert isinstance(build_email_provider(brevo), BrevoEmailProvider)
    assert rate_limit_provider_name(brevo) == "brevo"

    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        _settings(email_provider="resend")
    with pytest.raises(ValueError, match="BREVO_API_KEY"):
        _settings(email_provider="brevo")


def test_resend_requires_an_idempotency_key() -> None:
    provider = ResendEmailProvider(
        _settings(resend_api_key="test-resend-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "resend-message"})
        ),
    )

    with pytest.raises(ValueError, match="stable idempotency key"):
        provider.send(_payload(idempotency_key=None))
