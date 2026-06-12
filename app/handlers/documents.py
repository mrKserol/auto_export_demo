from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.database import Database, DocumentMetadata
from app.services.file_service import (
    build_stored_filename,
    download_telegram_document,
    get_file_extension,
    get_original_filename,
    is_supported_file,
)
from app.yadisk_client import YandexDiskClient
from app.yandex_function_client import YandexFunctionClient


router = Router(name="documents")
logger = logging.getLogger(__name__)


@router.message(F.document)
async def handle_document(
    message: Message,
    bot: Bot,
    database: Database,
    yandex_disk_client: YandexDiskClient,
    yandex_function_client: YandexFunctionClient | None,
    enable_processing: bool,
) -> None:
    document = message.document
    if document is None:
        return

    original_filename = get_original_filename(document)
    if not is_supported_file(original_filename):
        await message.reply(
            "Формат файла не поддерживается. Поддерживаются: "
            "pdf, jpg, jpeg, png, webp, heic, docx, xlsx."
        )
        return

    try:
        file_content = await download_telegram_document(bot, document)
    except Exception:
        logger.exception("Failed to download Telegram document")
        await message.reply("Не удалось скачать файл из Telegram.")
        return

    stored_filename = build_stored_filename(message.message_id, original_filename)
    disk_path = yandex_disk_client.build_file_path(
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_name=stored_filename,
    )
    try:
        uploaded_path = await yandex_disk_client.upload_bytes(disk_path, file_content)
    except Exception:
        logger.exception("Failed to upload document to Yandex Disk")
        await message.reply("Не удалось сохранить файл в Yandex Disk.")
        return

    user = message.from_user

    metadata = DocumentMetadata(
        telegram_chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        telegram_user_id=user.id if user else None,
        uploaded_by_name=user.full_name if user else None,
        uploaded_by_username=user.username if user else None,
        original_filename=original_filename,
        stored_filename=stored_filename,
        file_extension=get_file_extension(original_filename),
        mime_type=document.mime_type,
        file_size=document.file_size,
        telegram_file_id=document.file_id,
        yadisk_path=uploaded_path,
        processing_status="stored",
    )
    document_id = await database.insert_document(metadata)
    logger.info("Stored Telegram document %s as row %s", original_filename, document_id)

    if not enable_processing:
        await message.reply("✅ Файл принят и сохранён. Автообработка пока отключена.")
        return

    if yandex_function_client is None:
        await database.mark_processing_error(
            document_id,
            "Processing is enabled but Yandex Function client is not configured",
        )
        await message.reply("Файл сохранён, но автообработка не настроена.")
        return

    try:
        response_payload = await yandex_function_client.process_document(
            document_id=document_id,
            file_path=uploaded_path,
            original_filename=original_filename,
            mime_type=document.mime_type,
            telegram_chat_id=message.chat.id,
            telegram_message_id=message.message_id,
        )
        status = str(response_payload.get("status") or "processed")
        if status not in {"processed", "error"}:
            status = "processed"

        error_message = response_payload.get("error_message")
        await database.save_processing_result(
            document_id=document_id,
            response_payload=response_payload,
            status=status,
            error_message=str(error_message) if error_message else None,
        )
    except Exception as exc:
        logger.exception("Yandex Function processing failed for document %s", document_id)
        await database.mark_processing_error(document_id, str(exc))
        await message.reply("Файл сохранён, но автообработка завершилась ошибкой.")
        return

    if status == "error":
        await message.reply("Файл сохранён, автообработка вернула ошибку.")
        return

    await message.reply("✅ Файл принят, сохранён и обработан.")
