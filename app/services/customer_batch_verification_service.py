from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path

from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_recognition_service import (
    DOCUMENT_TYPE_LABELS,
    REQUIRED_DOCUMENT_TYPES,
)
from app.services.customer_batch_data_service import extract_fields_from_file_row
from app.services.customer_folder_service import (
    DOCUMENT_TYPE_BASE_NAMES,
    assign_disk_filenames,
    _normalize_extension,
)
from app.yadisk_client import ConflictYandexDiskError, YandexDiskClient


logger = logging.getLogger(__name__)

ALLOWED_MANUAL_DOCUMENT_TYPES = frozenset(
    {
        "passport_main",
        "passport_registration",
        "snils",
        "tin",
        "unknown",
        "mixed",
    }
)

MANUAL_DOCUMENT_TYPE_LABELS = {
    **DOCUMENT_TYPE_LABELS,
    "unknown": "Не определён",
    "mixed": "Смешанный документ",
}

EDITABLE_BATCH_STATUSES = frozenset(
    {
        CustomerUploadBatchStatus.FILES_SAVED,
        CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        CustomerUploadBatchStatus.RECOGNIZED,
    }
)

MAX_RENAME_SUFFIX = 50


@dataclass
class DocumentTypeUpdateResult:
    file: dict
    kit: dict
    warnings: list[str] = field(default_factory=list)
    renamed: bool = False
    move_error: str | None = None
    values: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class CustomerBatchVerificationService:
    repository: CustomerUploadBatchRepository
    yandex_disk_client: YandexDiskClient

    async def update_file_document_type(
        self,
        *,
        batch_id: int,
        file_id: int,
        document_type: str,
    ) -> DocumentTypeUpdateResult:
        if document_type not in ALLOWED_MANUAL_DOCUMENT_TYPES:
            raise ValueError(f"unsupported_document_type:{document_type}")

        batch = await self.repository.get_batch_by_id(batch_id)
        if batch is None:
            raise LookupError(f"customer_upload_batch id={batch_id} not found")
        if batch["status"] == CustomerUploadBatchStatus.CUSTOMER_SAVED:
            raise PermissionError("batch_already_saved")
        if batch["status"] not in EDITABLE_BATCH_STATUSES:
            raise PermissionError(f"batch_not_editable:{batch['status']}")

        file_row = await self.repository.get_batch_file(file_id)
        if file_row is None or int(file_row["batch_id"]) != int(batch_id):
            raise LookupError(
                f"customer_upload_batch_file id={file_id} not in batch={batch_id}"
            )

        extracted = file_row.get("extracted_json")
        if not isinstance(extracted, dict):
            extracted = {}
        else:
            extracted = dict(extracted)
        extracted["document_type"] = document_type
        extracted["manual_type_override"] = True

        updated = await self.repository.update_file_document_type(
            file_id,
            detected_document_type=document_type,
            extracted_json=extracted,
            clear_error_message=True,
        )

        move_error: str | None = None
        renamed = False
        final_path = updated.get("final_yadisk_path")
        if final_path:
            try:
                new_path = await self._rename_disk_file_for_type(
                    batch=batch,
                    file_row=updated,
                    document_type=document_type,
                )
                if new_path != final_path:
                    updated = await self.repository.set_file_yadisk_path(
                        file_id,
                        new_path,
                    )
                    renamed = True
            except Exception as exc:
                move_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "customer batch file rename failed batch_id=%s file_id=%s "
                    "from=%s",
                    batch_id,
                    file_id,
                    final_path,
                )
                await self.repository.update_file_document_type(
                    file_id,
                    detected_document_type=document_type,
                    extracted_json=extracted,
                    error_message=f"yadisk_rename_failed: {move_error}",
                )
                updated = await self.repository.get_batch_file(file_id) or updated

        kit = await self.repository.recalculate_batch_validation(batch_id)
        files = await self.repository.get_batch_files_with_paths(batch_id)
        from app.services.customer_batch_data_service import (
            assemble_customer_data_from_batch_files,
        )

        assembled = assemble_customer_data_from_batch_files(
            files,
            kit_warnings=kit.get("warnings"),
        )
        warnings = list(assembled.warnings)
        warnings.extend(
            build_manual_assignment_warnings(files)
        )
        if move_error:
            warnings.append(
                "Не удалось переименовать файл на Яндекс Диске. "
                "Тип документа сохранён, исходный файл не удалён."
            )

        return DocumentTypeUpdateResult(
            file=serialize_batch_file_for_form(updated),
            kit=format_kit_for_form(kit, files),
            warnings=_unique(warnings),
            renamed=renamed,
            move_error=move_error,
            values={
                "passport": assembled.fields.get("passport"),
                "last_name": assembled.fields.get("last_name"),
                "first_name": assembled.fields.get("first_name"),
                "surname": assembled.fields.get("surname"),
                "date_issue": assembled.fields.get("date_issue"),
                "by_whom_issued": assembled.fields.get("by_whom_issued"),
                "department_code": assembled.fields.get("department_code"),
                "registration_address": assembled.fields.get("registration_address"),
                "ipain": assembled.fields.get("ipain"),
                "tin": assembled.fields.get("tin"),
                "phone": assembled.fields.get("phone"),
                "email": assembled.fields.get("email"),
            },
        )

    async def _rename_disk_file_for_type(
        self,
        *,
        batch: dict,
        file_row: dict,
        document_type: str,
    ) -> str:
        source_path = file_row.get("final_yadisk_path")
        if not source_path:
            raise RuntimeError("final_yadisk_path is missing")

        files = await self.repository.get_batch_files_with_paths(int(batch["id"]))
        planned_names = assign_disk_filenames(
            [
                {
                    **row,
                    "detected_document_type": (
                        document_type
                        if int(row["id"]) == int(file_row["id"])
                        else row.get("detected_document_type")
                    ),
                }
                for row in files
            ]
        )
        planned_name = planned_names[int(file_row["id"])]
        customer_path = batch.get("customer_path") or ""
        folder_name = customer_path.rstrip("/").rsplit("/", 1)[-1]
        if not folder_name:
            raise RuntimeError("customer_path is missing")

        occupied = {
            row.get("final_yadisk_path")
            for row in files
            if int(row["id"]) != int(file_row["id"]) and row.get("final_yadisk_path")
        }

        stem = Path(planned_name).stem
        # Strip trailing _N from planned stem for suffix search base when needed
        base_stem = DOCUMENT_TYPE_BASE_NAMES.get(document_type, stem)
        extension = Path(planned_name).suffix or _normalize_extension(
            file_row.get("file_extension"),
            file_row.get("original_filename"),
        )

        candidates: list[str] = [planned_name]
        for index in range(1, MAX_RENAME_SUFFIX + 1):
            candidates.append(f"{base_stem}_{index}{extension}")

        last_error: Exception | None = None
        for file_name in candidates:
            destination = self.yandex_disk_client.build_customer_file_path(
                folder_name,
                file_name,
            )
            if destination == source_path:
                return source_path
            if destination in occupied:
                continue
            if await self.yandex_disk_client.path_exists(destination):
                continue
            try:
                return await self.yandex_disk_client.move_resource(
                    source_path,
                    destination,
                    overwrite=False,
                )
            except ConflictYandexDiskError as exc:
                last_error = exc
                continue

        if last_error is not None:
            raise last_error
        raise RuntimeError(
            f"Unable to find free Yandex Disk name for file_id={file_row['id']}"
        )


def serialize_batch_file_for_form(file_row: dict) -> dict:
    doc_type = file_row.get("detected_document_type") or "unknown"
    return {
        "id": int(file_row["id"]),
        "original_filename": file_row.get("original_filename"),
        "detected_document_type": doc_type,
        "detected_document_type_label": MANUAL_DOCUMENT_TYPE_LABELS.get(
            doc_type,
            doc_type,
        ),
        "final_yadisk_path": file_row.get("final_yadisk_path"),
        "final_yadisk_name": (
            Path(file_row["final_yadisk_path"]).name
            if file_row.get("final_yadisk_path")
            else None
        ),
        "recognition_status": file_row.get("recognition_status"),
        "error_message": file_row.get("error_message"),
        "manual_type_override": _is_manual_override(file_row),
    }


def format_kit_for_form(kit: dict, files: list[dict] | None = None) -> dict:
    lines = []
    for doc_type in REQUIRED_DOCUMENT_TYPES:
        label = DOCUMENT_TYPE_LABELS[doc_type]
        if doc_type in kit.get("duplicate_types", []):
            count = len(kit.get("documents_by_type", {}).get(doc_type, []))
            lines.append(f"⚠️ {label}: найдено {count} документа")
        elif doc_type in kit.get("missing_types", []):
            lines.append(f"❌ {label}: не найден")
        else:
            lines.append(f"✅ {label}")
    if files:
        lines.extend(build_manual_assignment_warnings(files))
    return {
        **kit,
        "lines": lines,
        "summary": "\n".join(lines),
    }


def build_manual_assignment_warnings(files: list[dict]) -> list[str]:
    warnings: list[str] = []
    for file_row in files:
        if file_row.get("detected_document_type") != "passport_registration":
            continue
        if not _is_manual_override(file_row):
            continue
        fields = extract_fields_from_file_row(file_row)
        address = (fields.get("registration_address") or "").strip()
        if not address:
            warnings.append("Прописка: назначена вручную, адрес не заполнен")
    return _unique(warnings)


def build_save_warnings(
    *,
    fields: dict,
    kit_warnings: list[str] | None = None,
    files: list[dict] | None = None,
) -> list[str]:
    warnings = list(kit_warnings or [])
    if files:
        warnings.extend(build_manual_assignment_warnings(files))
    address = (fields.get("registration_address") or "").strip()
    if not address:
        warning = "Адрес регистрации не заполнен"
        if warning not in warnings:
            warnings.append(warning)
    return _unique(warnings)


def _is_manual_override(file_row: dict) -> bool:
    extracted = file_row.get("extracted_json")
    return isinstance(extracted, dict) and bool(extracted.get("manual_type_override"))


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
