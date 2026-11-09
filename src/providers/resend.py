from typing import Any

import httpx

from src.config import Settings
from src.providers.base import EmailPayload, ProviderPermanentError
from src.providers.http import HttpEmailProvider


class ResendEmailProvider(HttpEmailProvider):
    name = "resend"
    endpoint = "https://api.resend.com/emails"
    supports_idempotency = True

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(
            settings,
            api_key=settings.resend_api_key.get_secret_value(),
            transport=transport,
            timeout=timeout,
        )

    def _message_body(self, payload: EmailPayload, recipient: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "from": payload.sender,
            "to": [recipient],
            "subject": payload.subject,
            "html": payload.html_body,
            "text": payload.text_body,
        }
        if payload.headers:
            body["headers"] = payload.headers
        if payload.attachments:
            body["attachments"] = self._attachments(payload.attachments)
        return body

    def _headers(self, payload: EmailPayload) -> dict[str, str]:
        headers = super()._headers(payload)
        if not payload.idempotency_key:
            raise ValueError("Resend sends require a stable idempotency key.")
        headers["Idempotency-Key"] = payload.idempotency_key
        return headers

    def _message_id(self, response_body: object) -> str:
        if isinstance(response_body, dict) and isinstance(response_body.get("id"), str):
            return response_body["id"]
        raise ProviderPermanentError("Resend response is missing its message ID.")
