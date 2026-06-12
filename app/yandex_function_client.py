from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class YandexFunctionClient:
    url: str

    async def process_document(
        self,
        *,
        document_id: int,
        file_path: str,
        original_filename: str,
        mime_type: str | None,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> dict[str, Any]:
        payload = {
            "document_id": document_id,
            "file_path": file_path,
            "original_filename": original_filename,
            "mime_type": mime_type,
            "telegram_chat_id": telegram_chat_id,
            "telegram_message_id": telegram_message_id,
        }

        logger.info("Calling Yandex Function with payload: %s", payload)

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.url, json=payload) as response:
                text = await response.text()
                logger.info("Yandex Function response status: %s", response.status)

                if response.status >= 400:
                    error_body = _format_response_body(text)
                    logger.error(
                        "Yandex Function returned %s: %s",
                        response.status,
                        error_body,
                    )
                    raise RuntimeError(
                        f"Yandex Function error {response.status}: {error_body}"
                    )

                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Yandex Function returned invalid JSON: {text}"
                    ) from exc


def _format_response_body(text: str) -> str:
    try:
        return json.dumps(json.loads(text), ensure_ascii=False)
    except json.JSONDecodeError:
        return text
