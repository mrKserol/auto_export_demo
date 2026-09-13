from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.services.channel_event import (
    AttachmentKind,
    ChannelEvent,
    build_telegram_attachment_event,
    build_telegram_text_event,
)

logger = logging.getLogger(__name__)

N8N_WEBHOOK_TIMEOUT_SECONDS = 30.0
N8N_WEBHOOK_SECRET_HEADER = "X-N8N-Webhook-Secret"


class N8NChatError(Exception):
    """Raised when the n8n webhook request fails or the response is invalid."""


class N8NChatService:
    def __init__(
        self,
        *,
        webhook_url: str,
        webhook_secret: str,
        timeout_seconds: float = N8N_WEBHOOK_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client

    async def send_telegram_text(
        self,
        *,
        text: str,
        chat_id: str,
        user_id: str,
        message_id: str,
    ) -> str:
        event = build_telegram_text_event(
            text=text,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
        )
        return await self.send_event(event)

    async def send_telegram_attachment(
        self,
        *,
        file_id: str,
        file_unique_id: str,
        name: str,
        mime_type: str,
        kind: AttachmentKind,
        size: int | None,
        chat_id: str,
        user_id: str,
        message_id: str,
        media_group_id: str | None = None,
        caption: str | None = None,
    ) -> str:
        event = build_telegram_attachment_event(
            file_id=file_id,
            file_unique_id=file_unique_id,
            name=name,
            mime_type=mime_type,
            kind=kind,
            size=size,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            media_group_id=media_group_id,
            caption=caption,
        )
        return await self.send_event(event)

    async def send_event(self, event: ChannelEvent) -> str:
        payload = event.to_dict()
        headers = {N8N_WEBHOOK_SECRET_HEADER: self._webhook_secret}

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            close_client = True

        try:
            try:
                response = await client.post(
                    self._webhook_url,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                logger.error("n8n webhook request timed out")
                raise N8NChatError("n8n webhook request timed out") from exc
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "n8n webhook returned HTTP %s",
                    exc.response.status_code,
                )
                raise N8NChatError(
                    f"n8n webhook returned HTTP {exc.response.status_code}"
                ) from exc
            except httpx.RequestError as exc:
                logger.error("n8n webhook request failed: %s", type(exc).__name__)
                raise N8NChatError("n8n webhook request failed") from exc

            try:
                body = response.json()
            except ValueError as exc:
                logger.error("n8n webhook returned invalid JSON")
                raise N8NChatError("n8n webhook returned invalid JSON") from exc

            return extract_n8n_assistant_text(body)
        finally:
            if close_client:
                await client.aclose()


def extract_n8n_assistant_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise N8NChatError("n8n webhook returned unexpected JSON")

    reply = payload.get("reply")
    if isinstance(reply, dict):
        reply_type = reply.get("type")
        text = reply.get("text")
        if reply_type == "text" and isinstance(text, str) and text.strip():
            return text.strip()

    result = payload.get("result")
    if not isinstance(result, dict):
        raise N8NChatError("n8n webhook response is missing result")

    alternatives = result.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise N8NChatError("n8n webhook response is missing alternatives")

    first = alternatives[0]
    if not isinstance(first, dict):
        raise N8NChatError("n8n webhook response is malformed")

    message = first.get("message")
    if not isinstance(message, dict):
        raise N8NChatError("n8n webhook response is missing message")

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        raise N8NChatError("n8n webhook response is missing assistant text")

    return text.strip()


def n8n_chat_configured(settings: Settings) -> bool:
    return bool(settings.n8n_telegram_webhook_url and settings.n8n_webhook_secret)
