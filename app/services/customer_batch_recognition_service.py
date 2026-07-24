from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import time

from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchFileRecognitionStatus,
    CustomerUploadBatchStatus,
)
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionService,
)
from app.services.customer_extraction_service import person_names_match
from app.services.validation_service import normalize_passport


logger = logging.getLogger(__name__)

REQUIRED_DOCUMENT_TYPES = (
    "passport_main",
    "passport_registration",
    "snils",
    "tin",
)

DOCUMENT_TYPE_LABELS = {
    "passport_main": "Паспорт",
    "passport_registration": "Прописка",
    "snils": "СНИЛС",
    "tin": "ИНН",
}

PROCESSABLE_BATCH_STATUSES = frozenset(
    {
        CustomerUploadBatchStatus.COLLECTING,
        CustomerUploadBatchStatus.RECOGNIZING,
        CustomerUploadBatchStatus.FAILED,
    }
)

BLOCKED_BATCH_STATUSES = frozenset(
    {
        CustomerUploadBatchStatus.RECOGNIZED,
        CustomerUploadBatchStatus.CREATING_FOLDER,
        CustomerUploadBatchStatus.UPLOADING,
        CustomerUploadBatchStatus.FILES_SAVED,
        CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        CustomerUploadBatchStatus.CUSTOMER_SAVED,
        CustomerUploadBatchStatus.ABANDONED,
    }
)


@dataclass
class KitValidationResult:
    is_complete: bool
    documents_by_type: dict[str, list[dict]]
    missing_types: list[str]
    duplicate_types: list[str]
    unknown_file_ids: list[int]
    mixed_file_ids: list[int]
    failed_file_ids: list[int]
    warnings: list[str] = field(default_factory=list)

    def summary_message(self) -> str | None:
        parts: list[str] = []
        if self.missing_types:
            labels = ", ".join(
                DOCUMENT_TYPE_LABELS.get(item, item) for item in self.missing_types
            )
            parts.append(f"missing={labels}")
        if self.duplicate_types:
            labels = ", ".join(
                DOCUMENT_TYPE_LABELS.get(item, item) for item in self.duplicate_types
            )
            parts.append(f"duplicates={labels}")
        if self.unknown_file_ids:
            parts.append(f"unknown_count={len(self.unknown_file_ids)}")
        if self.mixed_file_ids:
            parts.append(f"mixed_count={len(self.mixed_file_ids)}")
        if self.failed_file_ids:
            parts.append(f"failed_count={len(self.failed_file_ids)}")
        if self.warnings:
            parts.append(f"warnings={len(self.warnings)}")
        if not parts:
            return None
        return "; ".join(parts)


@dataclass
class BatchRecognitionResult:
    batch_id: int
    status: str
    file_count: int
    processed_file_count: int
    skipped_file_count: int
    kit: KitValidationResult
    telegram_message: str
    is_technical_failure: bool = False
    error_message: str | None = None


@dataclass(frozen=True)
class CustomerBatchRecognitionService:
    repository: CustomerUploadBatchRepository
    recognition_service: CustomerDocumentRecognitionService

    async def process_batch(self, batch_id: int) -> BatchRecognitionResult:
        batch = await self.repository.get_batch_by_id(batch_id)
        if batch is None:
            raise LookupError(f"customer_upload_batch id={batch_id} not found")

        status = batch["status"]
        if status == CustomerUploadBatchStatus.ABANDONED:
            kit = _empty_kit()
            return BatchRecognitionResult(
                batch_id=batch_id,
                status=status,
                file_count=0,
                processed_file_count=0,
                skipped_file_count=0,
                kit=kit,
                telegram_message="Загрузка клиента отменена.",
                is_technical_failure=True,
                error_message="batch_abandoned",
            )

        if status in {
            CustomerUploadBatchStatus.RECOGNIZED,
            CustomerUploadBatchStatus.CREATING_FOLDER,
            CustomerUploadBatchStatus.UPLOADING,
            CustomerUploadBatchStatus.FILES_SAVED,
            CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
            CustomerUploadBatchStatus.CUSTOMER_SAVED,
        }:
            files = await self.repository.get_batch_files(batch_id)
            kit = validate_document_kit(files)
            return BatchRecognitionResult(
                batch_id=batch_id,
                status=status,
                file_count=len(files),
                processed_file_count=0,
                skipped_file_count=len(files),
                kit=kit,
                telegram_message=format_kit_telegram_message(kit),
                is_technical_failure=False,
                error_message=batch.get("error_message"),
            )

        if status == CustomerUploadBatchStatus.COLLECTING:
            claimed = await self.repository.claim_batch_for_processing(batch_id)
            if claimed is None:
                batch = await self.repository.get_batch_by_id(batch_id)
                if batch is None:
                    raise LookupError(f"customer_upload_batch id={batch_id} not found")
                if batch["status"] not in PROCESSABLE_BATCH_STATUSES:
                    files = await self.repository.get_batch_files(batch_id)
                    kit = validate_document_kit(files)
                    return BatchRecognitionResult(
                        batch_id=batch_id,
                        status=batch["status"],
                        file_count=len(files),
                        processed_file_count=0,
                        skipped_file_count=len(files),
                        kit=kit,
                        telegram_message=(
                            "Этот пакет уже передан в обработку."
                            if batch["status"] != CustomerUploadBatchStatus.FAILED
                            else format_kit_telegram_message(kit)
                        ),
                        error_message=batch.get("error_message"),
                    )
            else:
                batch = claimed
        elif status == CustomerUploadBatchStatus.FAILED:
            await self.repository.mark_batch_recognizing(batch_id)

        reset_count = await self.repository.reset_interrupted_processing_files(batch_id)
        if reset_count:
            logger.info(
                "customer batch reset interrupted files batch_id=%s count=%s",
                batch_id,
                reset_count,
            )

        all_files = await self.repository.get_batch_files(batch_id)
        pending_files = await self.repository.get_files_for_recognition(batch_id)
        skipped_count = len(all_files) - len(pending_files)
        processed_count = 0

        try:
            for file_row in pending_files:
                await self._recognize_one_file(batch_id=batch_id, file_row=file_row)
                processed_count += 1
        except Exception as exc:
            logger.exception(
                "customer batch recognition crashed batch_id=%s",
                batch_id,
            )
            await self.repository.set_batch_error(
                batch_id,
                f"recognition_crashed: {type(exc).__name__}",
            )
            files = await self.repository.get_batch_files(batch_id)
            kit = validate_document_kit(files)
            return BatchRecognitionResult(
                batch_id=batch_id,
                status=CustomerUploadBatchStatus.FAILED,
                file_count=len(files),
                processed_file_count=processed_count,
                skipped_file_count=skipped_count,
                kit=kit,
                telegram_message=(
                    "Не удалось завершить распознавание документов.\n\n"
                    "Полученные файлы сохранены. Попробуйте запустить обработку повторно."
                ),
                is_technical_failure=True,
                error_message=f"recognition_crashed: {type(exc).__name__}",
            )

        files = await self.repository.get_batch_files(batch_id)
        kit = validate_document_kit(files)
        technical_failure = _is_technical_failure(files, pending_attempted=processed_count > 0)

        if technical_failure:
            error_message = kit.summary_message() or "technical_recognition_failure"
            await self.repository.set_batch_error(batch_id, error_message)
            logger.info(
                "customer batch recognition failed technically batch_id=%s "
                "processed=%s skipped=%s",
                batch_id,
                processed_count,
                skipped_count,
            )
            return BatchRecognitionResult(
                batch_id=batch_id,
                status=CustomerUploadBatchStatus.FAILED,
                file_count=len(files),
                processed_file_count=processed_count,
                skipped_file_count=skipped_count,
                kit=kit,
                telegram_message=(
                    "Не удалось завершить распознавание документов.\n\n"
                    "Полученные файлы сохранены. Попробуйте запустить обработку повторно."
                ),
                is_technical_failure=True,
                error_message=error_message,
            )

        validation_error = None if kit.is_complete else kit.summary_message()
        await self.repository.set_batch_recognized(
            batch_id,
            error_message=validation_error,
        )
        logger.info(
            "customer batch recognized batch_id=%s processed=%s skipped=%s "
            "is_complete=%s missing=%s duplicates=%s unknown=%s mixed=%s failed=%s "
            "warnings=%s",
            batch_id,
            processed_count,
            skipped_count,
            kit.is_complete,
            kit.missing_types,
            kit.duplicate_types,
            len(kit.unknown_file_ids),
            len(kit.mixed_file_ids),
            len(kit.failed_file_ids),
            len(kit.warnings),
        )
        return BatchRecognitionResult(
            batch_id=batch_id,
            status=CustomerUploadBatchStatus.RECOGNIZED,
            file_count=len(files),
            processed_file_count=processed_count,
            skipped_file_count=skipped_count,
            kit=kit,
            telegram_message=format_kit_telegram_message(kit),
            is_technical_failure=False,
            error_message=validation_error,
        )

    async def _recognize_one_file(self, *, batch_id: int, file_row: dict) -> None:
        file_id = int(file_row["id"])
        content = file_row.get("temporary_content")
        if content is None:
            await self.repository.mark_file_recognition_failed(
                file_id,
                "temporary_content is missing",
            )
            logger.info(
                "customer batch file missing content batch_id=%s file_id=%s",
                batch_id,
                file_id,
            )
            return

        await self.repository.mark_file_processing(file_id)
        started = time.monotonic()
        try:
            result = await self.recognition_service.recognize_document(
                content=bytes(content),
                mime_type=file_row.get("mime_type"),
                filename=file_row.get("original_filename"),
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            await self.repository.mark_file_recognition_failed(
                file_id,
                f"{type(exc).__name__}: recognition failed",
            )
            logger.info(
                "customer batch file recognition failed batch_id=%s file_id=%s "
                "elapsed_ms=%s error=%s",
                batch_id,
                file_id,
                elapsed_ms,
                type(exc).__name__,
            )
            return

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if result.error_message and result.document_type == "unknown" and not result.ocr_text:
            await self.repository.mark_file_recognition_failed(
                file_id,
                result.error_message,
                detected_document_type=result.document_type,
            )
            logger.info(
                "customer batch file empty ocr batch_id=%s file_id=%s "
                "document_type=%s confidence=%s elapsed_ms=%s",
                batch_id,
                file_id,
                result.document_type,
                result.confidence,
                elapsed_ms,
            )
            return

        extracted_payload = {
            "document_type": result.document_type,
            "confidence": result.confidence,
            "fields": result.extracted_fields,
            "warnings": result.warnings,
        }
        await self.repository.mark_file_recognized(
            file_id,
            detected_document_type=result.document_type,
            extracted_json=extracted_payload,
        )
        logger.info(
            "customer batch file recognized batch_id=%s file_id=%s "
            "document_type=%s confidence=%s recognition_status=%s elapsed_ms=%s",
            batch_id,
            file_id,
            result.document_type,
            result.confidence,
            CustomerUploadBatchFileRecognitionStatus.SUCCESS,
            elapsed_ms,
        )


def validate_document_kit(files: list[dict]) -> KitValidationResult:
    documents_by_type: dict[str, list[dict]] = defaultdict(list)
    unknown_file_ids: list[int] = []
    mixed_file_ids: list[int] = []
    failed_file_ids: list[int] = []
    warnings: list[str] = []

    for file_row in files:
        file_id = int(file_row["id"])
        status = file_row.get("recognition_status")
        if status == CustomerUploadBatchFileRecognitionStatus.FAILED:
            failed_file_ids.append(file_id)
            continue

        document_type = file_row.get("detected_document_type")
        if not document_type:
            extracted = file_row.get("extracted_json") or {}
            if isinstance(extracted, dict):
                document_type = extracted.get("document_type")

        if document_type == "unknown":
            unknown_file_ids.append(file_id)
            continue
        if document_type == "mixed":
            mixed_file_ids.append(file_id)
            continue
        if document_type in REQUIRED_DOCUMENT_TYPES:
            documents_by_type[document_type].append(file_row)

    missing_types = [
        doc_type
        for doc_type in REQUIRED_DOCUMENT_TYPES
        if not documents_by_type.get(doc_type)
    ]
    duplicate_types = [
        doc_type
        for doc_type in REQUIRED_DOCUMENT_TYPES
        if len(documents_by_type.get(doc_type, [])) > 1
    ]

    warnings.extend(_build_person_match_warnings(documents_by_type))

    is_complete = (
        not missing_types
        and not duplicate_types
        and not unknown_file_ids
        and not mixed_file_ids
        and not failed_file_ids
    )
    return KitValidationResult(
        is_complete=is_complete,
        documents_by_type=dict(documents_by_type),
        missing_types=missing_types,
        duplicate_types=duplicate_types,
        unknown_file_ids=unknown_file_ids,
        mixed_file_ids=mixed_file_ids,
        failed_file_ids=failed_file_ids,
        warnings=warnings,
    )


def format_kit_telegram_message(kit: KitValidationResult) -> str:
    lines = [
        _format_type_status_line(kit, "passport_main"),
        _format_type_status_line(kit, "passport_registration"),
        _format_type_status_line(kit, "snils"),
        _format_type_status_line(kit, "tin"),
    ]

    if kit.is_complete:
        message = (
            "Документы распознаны:\n\n"
            + "\n".join(lines)
            + "\n\nДанные готовы к следующему этапу."
        )
    else:
        message = (
            "Документы распознаны, но комплект требует проверки:\n\n"
            + "\n".join(lines)
        )
        if kit.warnings:
            message += "\n\nПредупреждения:\n" + "\n".join(
                f"• {warning}" for warning in kit.warnings
            )
    return message


def _format_type_status_line(kit: KitValidationResult, document_type: str) -> str:
    label = DOCUMENT_TYPE_LABELS[document_type]
    count = len(kit.documents_by_type.get(document_type, []))
    if document_type in kit.duplicate_types:
        return f"⚠️ {label}: найдено {count} документа"
    if document_type in kit.missing_types:
        return f"❌ {label}: не найден"
    if count == 1:
        return f"✅ {label}"
    return f"❌ {label}: не найден"


def _build_person_match_warnings(
    documents_by_type: dict[str, list[dict]],
) -> list[str]:
    warnings: list[str] = []
    passport_rows = documents_by_type.get("passport_main") or []
    if len(passport_rows) != 1:
        return warnings

    passport_fields = _extract_fields(passport_rows[0])
    if not passport_fields:
        return warnings

    snils_rows = documents_by_type.get("snils") or []
    if len(snils_rows) == 1:
        snils_fields = _extract_fields(snils_rows[0])
        if snils_fields and not person_names_match(passport_fields, snils_fields):
            warnings.append("ФИО паспорта не совпадает с ФИО в СНИЛС")

    tin_rows = documents_by_type.get("tin") or []
    if len(tin_rows) == 1:
        tin_fields = _extract_fields(tin_rows[0])
        if tin_fields and not person_names_match(passport_fields, tin_fields):
            warnings.append("ФИО паспорта не совпадает с ФИО в ИНН")

    registration_rows = documents_by_type.get("passport_registration") or []
    if len(registration_rows) == 1:
        registration_fields = _extract_fields(registration_rows[0])
        passport_number = normalize_passport(passport_fields.get("passport"))
        registration_passport = normalize_passport(registration_fields.get("passport"))
        if (
            passport_number
            and registration_passport
            and passport_number != registration_passport
        ):
            warnings.append(
                "Номер паспорта на странице регистрации не совпадает с основным"
            )
    return warnings


def _extract_fields(file_row: dict) -> dict:
    extracted = file_row.get("extracted_json")
    if not isinstance(extracted, dict):
        return {}
    fields = extracted.get("fields")
    if isinstance(fields, dict):
        return fields
    return {
        key: value
        for key, value in extracted.items()
        if key not in {"document_type", "confidence", "warnings", "fields"}
    }


def _is_technical_failure(files: list[dict], *, pending_attempted: bool) -> bool:
    if not files:
        return True
    if all(
        row.get("recognition_status")
        == CustomerUploadBatchFileRecognitionStatus.FAILED
        for row in files
    ):
        return True
    if pending_attempted and all(
        row.get("temporary_content") is None
        and row.get("recognition_status")
        != CustomerUploadBatchFileRecognitionStatus.SUCCESS
        for row in files
    ):
        return True
    return False


def _empty_kit() -> KitValidationResult:
    return KitValidationResult(
        is_complete=False,
        documents_by_type={},
        missing_types=list(REQUIRED_DOCUMENT_TYPES),
        duplicate_types=[],
        unknown_file_ids=[],
        mixed_file_ids=[],
        failed_file_ids=[],
        warnings=[],
    )
