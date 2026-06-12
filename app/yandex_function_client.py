from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


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

        async with aiohttp.ClientSession() as session:
            async with session.post(self.url, json=payload) as response:
                response.raise_for_status()
                return await response.json()
