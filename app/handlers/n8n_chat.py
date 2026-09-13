from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.types import Message

from app.config import Settings
from app.services.n8n_chat_service import (
    N8NChatError,
    N8NChatService,
    n8n_chat_configured,
)

router = Router(name="n8n_chat")
logger = logging.getLogger(__name__)

N8N_UNAVAILABLE_USER_TEXT = (
    "ИИ-ассистент временно недоступен. Попробуйте позже."
)


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
)
async def handle_n8n_private_text(message: Message, settings: Settings) -> None:
    if not n8n_chat_configured(settings):
        return

    text = (message.text or "").strip()
    if not text:
        return

    chat_id = "" if message.chat is None else str(message.chat.id)
    user_id = "" if message.from_user is None else str(message.from_user.id)
    message_id = "" if message.message_id is None else str(message.message_id)

    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_text(
            text=text,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
        )
    except N8NChatError:
        logger.exception("n8n chat webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    await message.answer(reply)
