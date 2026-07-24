from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path

from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_data_service import (
    assemble_customer_data_from_batch_files,
)
from app.services.customer_batch_recognition_service import (
    DOCUMENT_TYPE_LABELS,
    REQUIRED_DOCUMENT_TYPES,
    CustomerBatchRecognitionService,
    format_kit_telegram_message,
    validate_document_kit,
)
from app.services.customer_batch_verification_service import format_kit_for_form
from app.services.customer_file_download_service import CUSTOMER_UPLOAD_EXTENSIONS
from app.services.customer_folder_service import CustomerFolderService
from app.services.file_service import get_file_extension


logger = logging.getLogger(__name__)

BATCH_ORIGIN_TELEGRAM = "telegram"
BATCH_ORIGIN_MINIAPP = "miniapp"

DECLARED_DOCUMENT_TYPES = frozenset(REQUIRED_DOCUMENT_TYPES)

ALLOWED_UPLOAD_MIMES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
        "application/pdf",
    }
)

USER_STATUS_MESSAGES = {
    CustomerUploadBatchStatus.COLLECTING: "Ожидание документов",
    CustomerUploadBatchStatus.RECOGNIZING: "Распознавание документов",
    CustomerUploadBatchStatus.RECOGNIZED: "Проверка комплекта",
    CustomerUploadBatchStatus.CREATING_FOLDER: "Создание папки на Яндекс Диске",
    CustomerUploadBatchStatus.UPLOADING: "Сохранение на Яндекс Диск",
    CustomerUploadBatchStatus.FILES_SAVED: "Документы сохранены",
    CustomerUploadBatchStatus.AWAITING_CONFIRMATION: "Ожидание проверки данных",
    CustomerUploadBatchStatus.CUSTOMER_SAVED: "Клиент сохранён",
    CustomerUploadBatchStatus.ABANDONED: "Добавление отменено",
    CustomerUploadBatchStatus.FAILED: "Ошибка обработки",
}

PROCESSING_INTERRUPTED = "processing_interrupted"
PROCESSING_INTERRUPTED_USER_MESSAGE = (
    "Обработка была прервана перезапуском сервиса. "
    "Запустите распознавание повторно."
)


class MiniAppBatchError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def sniff_extension_and_mime(
    *,
    filename: str | None,
    content_type: str | None,
    content: bytes,
) -> tuple[str, str]:
    extension = get_file_extension(filename or "")
    declared_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not extension and declared_mime:
        guessed = mimetypes.guess_extension(declared_mime) or ""
        extension = guessed.lstrip(".")

    # Magic-byte sniff for common formats.
    if content.startswith(b"%PDF"):
        extension = "pdf"
        mime = "application/pdf"
    elif content.startswith(b"\xff\xd8\xff"):
        extension = "jpg"
        mime = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = "png"
        mime = "image/png"
    elif content.startswith(b"RIFF") and b"WEBP" in content[:16]:
        extension = "webp"
        mime = "image/webp"
    else:
        mime = declared_mime or mimetypes.guess_type(filename or "")[0] or ""
        mime = mime.split(";", 1)[0].strip().lower()
        if extension in {"heic", "heif"}:
            mime = "image/heic"
        elif extension in {"jpg", "jpeg"}:
            mime = "image/jpeg"
        elif extension == "png":
            mime = "image/png"
        elif extension == "webp":
            mime = "image/webp"
        elif extension == "pdf":
            mime = "application/pdf"

    if extension not in CUSTOMER_UPLOAD_EXTENSIONS:
        raise MiniAppBatchError(
            "UNSUPPORTED_FILE_TYPE",
            "Неподдерживаемый формат. Допустимы: jpg, jpeg, png, webp, heic, pdf.",
        )
    if mime and mime not in ALLOWED_UPLOAD_MIMES:
        raise MiniAppBatchError(
            "UNSUPPORTED_FILE_TYPE",
            "Неподдерживаемый формат. Допустимы: jpg, jpeg, png, webp, heic, pdf.",
        )
    return extension, mime or "application/octet-stream"


def serialize_batch_status(
    batch: dict,
    files: list[dict],
    *,
    include_preview: bool = True,
) -> dict:
    status = str(batch.get("status") or "")
    kit = validate_document_kit(files) if files else None
    kit_dict = None
    if kit is not None:
        kit_dict = {
            "is_complete": kit.is_complete,
            "missing_types": list(kit.missing_types),
            "duplicate_types": list(kit.duplicate_types),
            "unknown_file_ids": list(kit.unknown_file_ids),
            "mixed_file_ids": list(kit.mixed_file_ids),
            "failed_file_ids": list(kit.failed_file_ids),
            "warnings": list(kit.warnings),
            "documents_by_type": {
                key: [int(item["id"]) for item in value]
                for key, value in kit.documents_by_type.items()
            },
        }
    preview_fields = None
    warnings: list[str] = []
    if include_preview and status in {
        CustomerUploadBatchStatus.FILES_SAVED,
        CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        CustomerUploadBatchStatus.CUSTOMER_SAVED,
        CustomerUploadBatchStatus.RECOGNIZED,
    }:
        assembled = assemble_customer_data_from_batch_files(
            files,
            kit_warnings=kit.warnings if kit else None,
        )
        preview_fields = {
            key: (value if value else None)
            for key, value in assembled.fields.items()
            if key
            in {
                "last_name",
                "first_name",
                "surname",
                "passport",
                "date_issue",
                "department_code",
                "birth_date",
                "birth_place",
                "registration_address",
                "ipain",
                "tin",
            }
        }
        warnings = list(assembled.warnings)

    file_payload = []
    for file_row in files:
        file_payload.append(
            {
                "id": int(file_row["id"]),
                "declared_document_type": file_row.get("declared_document_type"),
                "detected_document_type": file_row.get("detected_document_type"),
                "declared_document_type_label": DOCUMENT_TYPE_LABELS.get(
                    file_row.get("declared_document_type") or "",
                    file_row.get("declared_document_type"),
                ),
                "detected_document_type_label": DOCUMENT_TYPE_LABELS.get(
                    file_row.get("detected_document_type") or "",
                    file_row.get("detected_document_type"),
                ),
                "original_filename": file_row.get("original_filename"),
                "recognition_status": file_row.get("recognition_status"),
                "file_size": file_row.get("file_size"),
            }
        )

    progress = {
        "files_total": 4,
        "files_uploaded": len(files),
        "files_recognized": sum(
            1 for item in files if item.get("recognition_status") == "success"
        ),
    }

    technical_error = str(batch.get("error_message") or "")
    interrupted = technical_error == PROCESSING_INTERRUPTED
    if interrupted:
        error_code = "PROCESSING_INTERRUPTED"
        error_message = PROCESSING_INTERRUPTED_USER_MESSAGE
        user_message = PROCESSING_INTERRUPTED_USER_MESSAGE
    elif status == CustomerUploadBatchStatus.FAILED:
        error_code = "BATCH_FAILED"
        error_message = "Распознавание временно недоступно"
        user_message = USER_STATUS_MESSAGES.get(status, "Обработка")
    else:
        error_code = None
        error_message = None
        user_message = USER_STATUS_MESSAGES.get(status, "Обработка")

    return {
        "batch_id": int(batch["id"]),
        "status": status,
        "origin": batch.get("origin"),
        "customer_id": batch.get("customer_id"),
        "customer_path": batch.get("customer_path"),
        "user_message": user_message,
        "error_code": error_code,
        "error_message": error_message,
        "progress": progress,
        "files": file_payload,
        "kit": format_kit_for_form(kit_dict, files) if kit_dict else None,
        "kit_message": format_kit_telegram_message(kit) if kit else None,
        "preview": preview_fields,
        "warnings": warnings,
        "can_recognize": (
            status == CustomerUploadBatchStatus.COLLECTING and len(files) >= 4
        ),
        "can_open_form": status
        in {
            CustomerUploadBatchStatus.FILES_SAVED,
            CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        },
    }


async def create_miniapp_batch(
    repository: CustomerUploadBatchRepository,
    *,
    telegram_user_id: int,
    origin_chat_id: int | None,
    force_new: bool = False,
) -> dict:
    active = await repository.list_active_batches_for_user(telegram_user_id)
    miniapp_active = [
        item for item in active if (item.get("origin") or "") == BATCH_ORIGIN_MINIAPP
    ]
    other_active = [
        item for item in active if (item.get("origin") or "") != BATCH_ORIGIN_MINIAPP
    ]

    if not force_new and miniapp_active:
        batch = miniapp_active[0]
        files = await repository.get_batch_files(int(batch["id"]))
        return {
            "created": False,
            "conflict": False,
            "batch": serialize_batch_status(batch, files),
            "other_active": [
                {
                    "batch_id": int(item["id"]),
                    "origin": item.get("origin") or BATCH_ORIGIN_TELEGRAM,
                    "status": item.get("status"),
                }
                for item in other_active
            ],
        }

    if not force_new and other_active:
        return {
            "created": False,
            "conflict": True,
            "message": "У вас уже есть незавершённое добавление клиента.",
            "active": [
                {
                    "batch_id": int(item["id"]),
                    "origin": item.get("origin") or BATCH_ORIGIN_TELEGRAM,
                    "status": item.get("status"),
                }
                for item in other_active
            ],
        }

    if force_new:
        for item in active:
            if item.get("status") == CustomerUploadBatchStatus.COLLECTING:
                await repository.update_batch_status(
                    int(item["id"]),
                    CustomerUploadBatchStatus.ABANDONED,
                )
        active = await repository.list_active_batches_for_user(telegram_user_id)
        miniapp_active = [
            item
            for item in active
            if (item.get("origin") or "") == BATCH_ORIGIN_MINIAPP
        ]
        if miniapp_active:
            batch = miniapp_active[0]
            files = await repository.get_batch_files(int(batch["id"]))
            return {
                "created": False,
                "conflict": False,
                "batch": serialize_batch_status(batch, files),
                "message": "Продолжаем незавершённую обработку документов.",
            }

    chat_id = int(origin_chat_id) if origin_chat_id is not None else int(telegram_user_id)
    batch = await repository.create_batch(
        batch_key=f"miniapp:{telegram_user_id}:{uuid.uuid4().hex}",
        telegram_chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        status=CustomerUploadBatchStatus.COLLECTING,
        origin=BATCH_ORIGIN_MINIAPP,
    )
    return {
        "created": True,
        "conflict": False,
        "batch": serialize_batch_status(batch, []),
    }


async def assert_batch_owner(
    repository: CustomerUploadBatchRepository,
    *,
    batch_id: int,
    telegram_user_id: int,
) -> dict:
    batch = await repository.get_batch_by_id(batch_id)
    if batch is None:
        raise MiniAppBatchError("BATCH_NOT_FOUND", "Пакет документов не найден", status_code=404)
    if int(batch.get("telegram_user_id") or 0) != int(telegram_user_id):
        raise MiniAppBatchError("BATCH_FORBIDDEN", "Нет доступа к этому пакету", status_code=403)
    if batch.get("status") == CustomerUploadBatchStatus.ABANDONED:
        raise MiniAppBatchError("BATCH_ABANDONED", "Добавление клиента отменено", status_code=409)
    return batch


async def upload_batch_file(
    repository: CustomerUploadBatchRepository,
    *,
    batch_id: int,
    telegram_user_id: int,
    declared_document_type: str,
    filename: str | None,
    content_type: str | None,
    content: bytes,
    max_file_bytes: int,
) -> dict:
    if declared_document_type not in DECLARED_DOCUMENT_TYPES:
        raise MiniAppBatchError(
            "VALIDATION_ERROR",
            "Недопустимый тип документа",
        )
    batch = await assert_batch_owner(
        repository,
        batch_id=batch_id,
        telegram_user_id=telegram_user_id,
    )
    if batch.get("status") != CustomerUploadBatchStatus.COLLECTING:
        raise MiniAppBatchError(
            "BATCH_NOT_EDITABLE",
            "В этот пакет больше нельзя добавлять файлы",
            status_code=409,
        )
    if len(content) > max_file_bytes:
        mb = max(1, max_file_bytes // (1024 * 1024))
        raise MiniAppBatchError(
            "FILE_TOO_LARGE",
            f"Файл слишком большой. Максимум {mb} МБ.",
        )
    if not content:
        raise MiniAppBatchError("VALIDATION_ERROR", "Пустой файл")

    extension, mime = sniff_extension_and_mime(
        filename=filename,
        content_type=content_type,
        content=content,
    )
    safe_name = Path(filename or f"{declared_document_type}.{extension}").name

    existing = await repository.get_batch_file_by_declared_type(
        batch_id,
        declared_document_type,
    )
    if existing is not None:
        await repository.delete_batch_file(batch_id, int(existing["id"]))

    file_row = await repository.add_file(
        batch_id=batch_id,
        original_filename=safe_name,
        mime_type=mime,
        file_extension=extension,
        file_size=len(content),
        temporary_content=content,
        declared_document_type=declared_document_type,
    )
    files = await repository.get_batch_files(batch_id)
    refreshed = await repository.get_batch_by_id(batch_id)
    assert refreshed is not None
    return {
        "file": {
            "id": int(file_row["id"]),
            "declared_document_type": declared_document_type,
            "original_filename": safe_name,
            "file_size": len(content),
        },
        "batch": serialize_batch_status(refreshed, files),
    }


async def delete_batch_slot_file(
    repository: CustomerUploadBatchRepository,
    *,
    batch_id: int,
    file_id: int,
    telegram_user_id: int,
) -> dict:
    batch = await assert_batch_owner(
        repository,
        batch_id=batch_id,
        telegram_user_id=telegram_user_id,
    )
    if batch.get("status") != CustomerUploadBatchStatus.COLLECTING:
        raise MiniAppBatchError(
            "BATCH_NOT_EDITABLE",
            "Файл нельзя удалить на этом этапе",
            status_code=409,
        )
    deleted = await repository.delete_batch_file(batch_id, file_id)
    if not deleted:
        raise MiniAppBatchError("FILE_NOT_FOUND", "Файл не найден", status_code=404)
    files = await repository.get_batch_files(batch_id)
    refreshed = await repository.get_batch_by_id(batch_id)
    assert refreshed is not None
    return serialize_batch_status(refreshed, files)


async def run_batch_processing(
    *,
    batch_id: int,
    recognition_service: CustomerBatchRecognitionService,
    folder_service: CustomerFolderService,
) -> None:
    try:
        await recognition_service.process_batch(batch_id)
        batch = await folder_service.repository.get_batch_by_id(batch_id)
        if batch is None:
            return
        if batch.get("status") not in {
            CustomerUploadBatchStatus.RECOGNIZED,
            CustomerUploadBatchStatus.CREATING_FOLDER,
            CustomerUploadBatchStatus.UPLOADING,
            CustomerUploadBatchStatus.FILES_SAVED,
            CustomerUploadBatchStatus.FAILED,
        }:
            return
        if batch.get("status") == CustomerUploadBatchStatus.FAILED:
            return
        await folder_service.ensure_batch_files_saved(batch_id)
    except Exception:
        logger.exception("Mini App batch processing failed batch_id=%s", batch_id)
        try:
            await recognition_service.repository.update_batch_status(
                batch_id,
                CustomerUploadBatchStatus.FAILED,
                error_message="processing_failed",
            )
        except Exception:
            logger.exception(
                "Failed to mark miniapp batch failed batch_id=%s",
                batch_id,
            )


async def maybe_recover_stale_batch(
    repository: CustomerUploadBatchRepository,
    *,
    batch_id: int,
    stale_after_seconds: int,
) -> dict | None:
    """Recover a single stale recognizing batch if the timeout elapsed."""
    return await repository.recover_stale_recognizing_batch(
        batch_id,
        stale_after_seconds=stale_after_seconds,
        origin=BATCH_ORIGIN_MINIAPP,
    )


async def recover_stale_miniapp_batches_on_startup(
    repository: CustomerUploadBatchRepository,
    *,
    stale_after_seconds: int,
) -> list[dict]:
    """Mark interrupted Mini App OCR batches recoverable without starting OCR."""
    recovered = await repository.recover_stale_recognizing_batches(
        stale_after_seconds=stale_after_seconds,
        origin=BATCH_ORIGIN_MINIAPP,
    )
    if recovered:
        logger.warning(
            "Recovered %s stale Mini App recognizing batch(es) after restart",
            len(recovered),
        )
    return recovered

