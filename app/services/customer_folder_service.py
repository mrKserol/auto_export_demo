from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
from pathlib import Path

from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchStatus,
)
from app.services.customer_batch_data_service import (
    extract_fields_from_file_row,
    get_passport_main_identity,
)
from app.yadisk_client import YandexDiskClient


logger = logging.getLogger(__name__)

DOCUMENT_TYPE_BASE_NAMES = {
    "passport_main": "Паспорт",
    "passport_registration": "Прописка",
    "snils": "Снилс",
    "tin": "Инн",
    "unknown": "Неопознанный",
    "mixed": "Смешанный",
}

MAX_FOLDER_SUFFIX = 50


@dataclass
class FolderFinalizeResult:
    batch_id: int
    customer_path: str | None
    uploaded_count: int
    skipped_count: int
    failed_count: int
    status: str
    error_message: str | None = None
    uploaded_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CustomerFolderService:
    repository: CustomerUploadBatchRepository
    yandex_disk_client: YandexDiskClient

    @staticmethod
    def sanitize_folder_component(value: str | None) -> str:
        text = (value or "").strip()
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"[^\w\-]+", "", text, flags=re.UNICODE)
        text = text.strip("._-")
        return text or "unknown"

    @classmethod
    def build_customer_folder_name(
        cls,
        last_name: str,
        passport: str,
    ) -> str:
        surname = cls.sanitize_folder_component(last_name)
        passport_part = cls.sanitize_folder_component(
            re.sub(r"\D+", "", passport or "")
        )
        return f"{surname}_{passport_part}"

    async def create_unique_customer_folder(
        self,
        *,
        last_name: str,
        passport: str,
    ) -> str:
        base_name = self.build_customer_folder_name(last_name, passport)
        for index in range(0, MAX_FOLDER_SUFFIX + 1):
            folder_name = base_name if index == 0 else f"{base_name}_{index}"
            folder_path = self.yandex_disk_client.build_customer_folder_path(
                folder_name
            )
            created = await self.yandex_disk_client.try_create_directory(folder_path)
            if created:
                logger.info(
                    "customer folder created path=%s",
                    folder_path,
                )
                return folder_path
            logger.info(
                "customer folder conflict path=%s trying_next_suffix=%s",
                folder_path,
                index + 1,
            )
        raise RuntimeError(
            f"Unable to create unique customer folder for base={base_name}"
        )

    async def ensure_batch_files_saved(self, batch_id: int) -> FolderFinalizeResult:
        batch = await self.repository.get_batch_by_id(batch_id)
        if batch is None:
            raise LookupError(f"customer_upload_batch id={batch_id} not found")

        if batch["status"] in {
            CustomerUploadBatchStatus.FILES_SAVED,
            CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
            CustomerUploadBatchStatus.CUSTOMER_SAVED,
        } and batch.get("customer_path"):
            return FolderFinalizeResult(
                batch_id=batch_id,
                customer_path=batch.get("customer_path"),
                uploaded_count=0,
                skipped_count=await self.repository.count_batch_files(batch_id),
                failed_count=0,
                status=batch["status"],
            )

        files = await self.repository.get_batch_files(batch_id)
        identity = get_passport_main_identity(files)
        if not identity:
            return FolderFinalizeResult(
                batch_id=batch_id,
                customer_path=batch.get("customer_path"),
                uploaded_count=0,
                skipped_count=0,
                failed_count=0,
                status=batch["status"],
                error_message="passport_main_identity_missing",
            )

        customer_path = batch.get("customer_path")
        if not customer_path:
            await self.repository.update_batch_status(
                batch_id,
                CustomerUploadBatchStatus.CREATING_FOLDER,
            )
            customer_path = await self.create_unique_customer_folder(
                last_name=identity["last_name"],
                passport=identity["passport"],
            )
            await self.repository.set_batch_customer_path(batch_id, customer_path)
        else:
            await self.yandex_disk_client.ensure_directory(customer_path)

        await self.repository.update_batch_status(
            batch_id,
            CustomerUploadBatchStatus.UPLOADING,
        )
        upload_result = await self.upload_batch_files(
            batch_id=batch_id,
            customer_path=customer_path,
        )

        if upload_result.failed_count == 0:
            await self.repository.clear_batch_file_contents(batch_id)
            await self.repository.update_batch_status(
                batch_id,
                CustomerUploadBatchStatus.FILES_SAVED,
            )
            status = CustomerUploadBatchStatus.FILES_SAVED
            error_message = None
        else:
            status = CustomerUploadBatchStatus.UPLOADING
            error_message = (
                f"upload_partial_failure failed={upload_result.failed_count}"
            )
            await self.repository.update_batch_status(
                batch_id,
                status,
                error_message=error_message,
            )

        return FolderFinalizeResult(
            batch_id=batch_id,
            customer_path=customer_path,
            uploaded_count=upload_result.uploaded_count,
            skipped_count=upload_result.skipped_count,
            failed_count=upload_result.failed_count,
            status=status,
            error_message=error_message,
            uploaded_paths=upload_result.uploaded_paths,
        )

    async def upload_batch_files(
        self,
        *,
        batch_id: int,
        customer_path: str,
    ) -> FolderFinalizeResult:
        files = await self.repository.get_batch_files(batch_id)
        folder_name = customer_path.rstrip("/").rsplit("/", 1)[-1]
        planned_names = assign_disk_filenames(files)

        uploaded_count = 0
        skipped_count = 0
        failed_count = 0
        uploaded_paths: list[str] = []

        for file_row in files:
            file_id = int(file_row["id"])
            existing_path = file_row.get("final_yadisk_path")
            if existing_path:
                skipped_count += 1
                continue

            content = file_row.get("temporary_content")
            if content is None:
                failed_count += 1
                logger.info(
                    "customer batch upload skipped missing content "
                    "batch_id=%s file_id=%s",
                    batch_id,
                    file_id,
                )
                continue

            file_name = planned_names[file_id]
            disk_path = self.yandex_disk_client.build_customer_file_path(
                folder_name,
                file_name,
            )
            try:
                if await self.yandex_disk_client.path_exists(disk_path):
                    # avoid overwrite; keep existing remote file
                    await self.repository.set_file_yadisk_path(file_id, disk_path)
                    await self.repository.clear_file_temporary_content(file_id)
                    skipped_count += 1
                    uploaded_paths.append(disk_path)
                    continue

                uploaded_path = await self.yandex_disk_client.upload_bytes(
                    disk_path,
                    bytes(content),
                    overwrite=False,
                )
                await self.repository.set_file_yadisk_path(file_id, uploaded_path)
                await self.repository.clear_file_temporary_content(file_id)
                uploaded_count += 1
                uploaded_paths.append(uploaded_path)
                logger.info(
                    "customer batch file uploaded batch_id=%s file_id=%s "
                    "document_type=%s",
                    batch_id,
                    file_id,
                    file_row.get("detected_document_type"),
                )
            except Exception:
                failed_count += 1
                logger.exception(
                    "customer batch file upload failed batch_id=%s file_id=%s",
                    batch_id,
                    file_id,
                )

        return FolderFinalizeResult(
            batch_id=batch_id,
            customer_path=customer_path,
            uploaded_count=uploaded_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            status=CustomerUploadBatchStatus.UPLOADING,
            uploaded_paths=uploaded_paths,
        )


def assign_disk_filenames(files: list[dict]) -> dict[int, str]:
    type_counts: dict[str, int] = {}
    for file_row in files:
        doc_type = file_row.get("detected_document_type") or "unknown"
        type_counts[doc_type] = type_counts.get(doc_type, 0) + 1

    type_indexes: dict[str, int] = {}
    result: dict[int, str] = {}
    for file_row in files:
        file_id = int(file_row["id"])
        doc_type = file_row.get("detected_document_type") or "unknown"
        base = DOCUMENT_TYPE_BASE_NAMES.get(doc_type, "Неопознанный")
        extension = _normalize_extension(
            file_row.get("file_extension"),
            file_row.get("original_filename"),
        )
        type_indexes[doc_type] = type_indexes.get(doc_type, 0) + 1
        needs_index = type_counts[doc_type] > 1 or doc_type in {"unknown", "mixed"}
        if needs_index:
            name = f"{base}_{type_indexes[doc_type]}{extension}"
        else:
            name = f"{base}{extension}"
        result[file_id] = name
    return result


def _normalize_extension(
    file_extension: str | None,
    original_filename: str | None,
) -> str:
    if file_extension:
        ext = file_extension if file_extension.startswith(".") else f".{file_extension}"
        return ext.lower()
    if original_filename:
        suffix = Path(original_filename).suffix
        if suffix:
            return suffix.lower()
    return ".bin"
