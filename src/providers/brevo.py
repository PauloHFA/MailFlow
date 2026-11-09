from typing import Any

import httpx

from src.config import Settings
from src.providers.base import EmailPayload, ProviderPermanentError
from src.providers.http import HttpEmailProvider, sender_parts


class BrevoEmailProvider(HttpEmailProvider):
    name = "brevo"
    endpoint = "https://api.brevo.com/v3/smtp/email"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(
            settings,
            api_key=settings.brevo_api_key.get_secret_value(),
            transport=transport,
            timeout=timeout,
        )

    def _headers(self, payload: EmailPayload) -> dict[str, str]:
        headers = {
            "api-key": self._api_key,
            "Content-Type": "application/json",
        }
        headers.update(payload.headers)
        return headers

    def _message_body(self, payload: EmailPayload, recipient: str) -> dict[str, Any]:
        sender_email, sender_name = sender_parts(payload.sender)
        sender: dict[str, str] = {"email": sender_email}
        if sender_name is not None:
            sender["name"] = sender_name
        body: dict[str, Any] = {
            "sender": sender,
            "to": [{"email": recipient}],
            "subject": payload.subject,
            "htmlContent": payload.html_body,
            "textContent": payload.text_body,
        }
        if payload.headers:
            body["headers"] = payload.headers
        if payload.attachments:
            body["attachment"] = [
                {"name": item["filename"], "content": item["content"]}
                for item in self._attachments(payload.attachments)
            ]
        return body

    def _message_id(self, response_body: object) -> str:
        if isinstance(response_body, dict) and isinstance(response_body.get("messageId"), str):
            return response_body["messageId"]
        raise ProviderPermanentError("Brevo response is missing its message ID.")
