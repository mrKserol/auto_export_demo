from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.config import Settings
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import CustomerUploadBatchStatus
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
N8N_UNSUPPORTED_ATTACHMENT_USER_TEXT = (
    "Этот файл пока не поддерживается. Пришлите фото или PDF."
)
SUPPORTED_DOCUMENT_MIME_TYPES = {"application/pdf"}
INTAKE_COMMANDS = ("intake_start", "intake_finish", "intake_cancel")


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    Command(*INTAKE_COMMANDS),
)
async def handle_n8n_intake_command(message: Message, settings: Settings) -> None:
    if not n8n_chat_configured(settings):
        return

    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_command(message)
    except N8NChatError:
        logger.exception("n8n intake command webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    if isinstance(reply, dict) and reply.get("type") == "web_app":
        await message.answer(
            reply["text"],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=reply["button_text"],
                            web_app=WebAppInfo(url=reply["url"]),
                        )
                    ]
                ]
            ),
        )
    else:
        await message.answer(str(reply))


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


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    F.photo,
)
async def handle_n8n_private_photo(
    message: Message,
    settings: Settings,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if not n8n_chat_configured(settings):
        return

    await _skip_if_active_customer_batch(message, customer_upload_batch_repository)

    if not message.photo:
        return

    photo = message.photo[-1]
    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_attachment(
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            name=f"photo_{message.message_id}.jpg",
            mime_type="image/jpeg",
            kind="photo",
            size=photo.file_size,
            chat_id="" if message.chat is None else str(message.chat.id),
            user_id="" if message.from_user is None else str(message.from_user.id),
            message_id="" if message.message_id is None else str(message.message_id),
            media_group_id=message.media_group_id,
            caption=message.caption,
        )
    except N8NChatError:
        logger.exception("n8n attachment webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    await message.answer(reply)


@router.message(
    StateFilter(None),
    F.chat.type == ChatType.PRIVATE,
    F.document,
)
async def handle_n8n_private_document(
    message: Message,
    settings: Settings,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if not n8n_chat_configured(settings):
        return

    await _skip_if_active_customer_batch(message, customer_upload_batch_repository)

    document = message.document
    if document is None:
        return

    mime_type = document.mime_type or "application/octet-stream"
    if not (mime_type.startswith("image/") or mime_type in SUPPORTED_DOCUMENT_MIME_TYPES):
        await message.answer(N8N_UNSUPPORTED_ATTACHMENT_USER_TEXT)
        return

    service = N8NChatService(
        webhook_url=settings.n8n_telegram_webhook_url or "",
        webhook_secret=settings.n8n_webhook_secret or "",
    )
    try:
        reply = await service.send_telegram_attachment(
            file_id=document.file_id,
            file_unique_id=document.file_unique_id,
            name=document.file_name or f"document_{message.message_id}",
            mime_type=mime_type,
            kind="document",
            size=document.file_size,
            chat_id="" if message.chat is None else str(message.chat.id),
            user_id="" if message.from_user is None else str(message.from_user.id),
            message_id="" if message.message_id is None else str(message.message_id),
            media_group_id=message.media_group_id,
            caption=message.caption,
        )
    except N8NChatError:
        logger.exception("n8n attachment webhook failed")
        await message.answer(N8N_UNAVAILABLE_USER_TEXT)
        return

    await message.answer(reply)


async def _skip_if_active_customer_batch(
    message: Message,
    customer_upload_batch_repository: CustomerUploadBatchRepository,
) -> None:
    if message.chat is None or message.from_user is None:
        return

    batch = await customer_upload_batch_repository.get_active_batch(
        message.chat.id,
        message.from_user.id,
    )
    if batch is not None and batch.get("status") == CustomerUploadBatchStatus.COLLECTING:
        raise SkipHandler()
