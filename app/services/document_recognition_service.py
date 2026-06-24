from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import Message

from app.database import Database, DocumentMetadata
from app.services.case_service import (
    bind_document_to_case,
    build_case_success_reply,
    build_uncertain_binding_reply,
    extract_business_fields,
    find_or_create_case,
    run_basic_case_checks,
    save_document_fields,
)
from app.services.file_service import (
    build_stored_filename,
    download_telegram_document,
    get_file_extension,
    get_original_filename,
    is_supported_file,
)
from app.yadisk_client import YandexDiskClient
from app.yandex_function_client import YandexFunctionClient


logger = logging.getLogger(__name__)


async def process_uploaded_document(
    message: Message,
    bot: Bot,
    database: Database,
    yandex_disk_client: YandexDiskClient,
    yandex_function_client: YandexFunctionClient | None,
    enable_processing: bool,
) -> dict:
    document = message.document
    if document is None:
        return {
            "document_id": None,
            "status": "error",
            "reply_text": "Документ не найден в сообщении.",
            "function_response_json": None,
            "fields": {},
            "case_data": None,
        }

    original_filename = get_original_filename(document)
    if not is_supported_file(original_filename):
        return {
            "document_id": None,
            "status": "error",
            "reply_text": (
                "Формат файла не поддерживается. Поддерживаются: "
                "pdf, jpg, jpeg, png, webp, heic, docx, xlsx."
            ),
            "function_response_json": None,
            "fields": {},
            "case_data": None,
        }

    try:
        file_content = await download_telegram_document(bot, document)
    except Exception:
        logger.exception("Failed to download Telegram document")
        return {
            "document_id": None,
            "status": "error",
            "reply_text": "Не удалось скачать файл из Telegram.",
            "function_response_json": None,
            "fields": {},
            "case_data": None,
        }

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
        return {
            "document_id": None,
            "status": "error",
            "reply_text": "Не удалось сохранить файл в Yandex Disk.",
            "function_response_json": None,
            "fields": {},
            "case_data": None,
        }

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
        return {
            "document_id": document_id,
            "status": "stored",
            "reply_text": "✅ Файл принят и сохранён. Автообработка пока отключена.",
            "function_response_json": None,
            "fields": {},
            "case_data": None,
        }

    if yandex_function_client is None:
        await database.mark_processing_error(
            document_id,
            "Processing is enabled but Yandex Function client is not configured",
        )
        return {
            "document_id": document_id,
            "status": "error",
            "reply_text": "Файл сохранён, но автообработка не настроена.",
            "function_response_json": None,
            "fields": {},
            "case_data": None,
        }

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
        return {
            "document_id": document_id,
            "status": "error",
            "reply_text": "Файл сохранён, но автообработка завершилась ошибкой.",
            "function_response_json": None,
            "fields": {},
            "case_data": None,
        }

    if status == "error":
        return {
            "document_id": document_id,
            "status": "error",
            "reply_text": "Файл сохранён, автообработка вернула ошибку.",
            "function_response_json": response_payload,
            "fields": {},
            "case_data": None,
        }

    fields = extract_business_fields(response_payload)
    binding_result = await find_or_create_case(
        database,
        fields,
        case_folder_path_builder=yandex_disk_client.build_case_folder_path,
    )

    if binding_result["status"] == "needs_manual_bind":
        candidates = binding_result["candidates"]
        await database.bind_document_to_case(
            document_id,
            None,
            score=binding_result["score"],
            status="needs_manual_bind",
            candidates=candidates,
        )
        await save_document_fields(database, document_id, None, fields)
        return {
            "document_id": document_id,
            "status": "needs_manual_bind",
            "reply_text": build_uncertain_binding_reply(
                document_id=document_id,
                original_filename=original_filename,
                fields=fields,
                candidates=candidates,
            ),
            "function_response_json": response_payload,
            "fields": fields,
            "case_data": None,
        }

    case_data = binding_result["case"]
    case_id = int(case_data["id"])
    await bind_document_to_case(
        database,
        document_id,
        case_id,
        score=binding_result["score"],
        status=binding_result["status"],
    )
    await save_document_fields(database, document_id, case_id, fields)
    await run_basic_case_checks(database, case_id)

    case_folder_name = case_data["case_folder_name"]
    case_file_path = yandex_disk_client.build_case_file_path(
        case_folder_name,
        stored_filename,
    )
    try:
        moved_path = await yandex_disk_client.move_resource(uploaded_path, case_file_path)
        await database.update_document_paths(document_id, current_yadisk_path=moved_path)
    except Exception as exc:
        logger.exception("Failed to move document %s to case folder", document_id)
        await database.update_document_paths(
            document_id,
            current_yadisk_path=uploaded_path,
            error_message=f"Yandex Disk move failed: {exc}",
        )

    return {
        "document_id": document_id,
        "status": binding_result["status"],
        "reply_text": build_case_success_reply(
            case_data=case_data,
            fields=fields,
            pages_processed=response_payload.get("pages_processed"),
        ),
        "function_response_json": response_payload,
        "fields": fields,
        "case_data": case_data,
    }
