from __future__ import annotations

from datetime import datetime, timezone
import json

import asyncpg

from app.repositories.customer_upload_batch_statuses import (
    CustomerUploadBatchFileRecognitionStatus,
    CustomerUploadBatchStatus,
)


def _record_to_dict(record: asyncpg.Record | None) -> dict | None:
    if record is None:
        return None
    return dict(record)


def _normalize_file_record(record: asyncpg.Record | None) -> dict | None:
    data = _record_to_dict(record)
    if data is None:
        return None
    extracted = data.get("extracted_json")
    if isinstance(extracted, (bytes, bytearray)):
        extracted = extracted.decode("utf-8")
    if isinstance(extracted, str):
        data["extracted_json"] = json.loads(extracted)
    return data


class CustomerUploadBatchRepository:
    """PostgreSQL repository for batch customer document uploads."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_batch(
        self,
        *,
        batch_key: str,
        telegram_chat_id: int,
        telegram_user_id: int | None = None,
        media_group_id: str | None = None,
        status: str = CustomerUploadBatchStatus.COLLECTING,
    ) -> dict:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO customer_upload_batches (
                    batch_key,
                    telegram_chat_id,
                    telegram_user_id,
                    media_group_id,
                    status
                ) VALUES ($1, $2, $3, $4, $5)
                RETURNING *;
                """,
                batch_key,
                telegram_chat_id,
                telegram_user_id,
                media_group_id,
                status,
            )
            result = _record_to_dict(row)
            assert result is not None
            return result

    async def get_batch_by_id(self, batch_id: int) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM customer_upload_batches WHERE id = $1;",
                batch_id,
            )
            return _record_to_dict(row)

    async def get_batch_by_key(self, batch_key: str) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM customer_upload_batches WHERE batch_key = $1;",
                batch_key,
            )
            return _record_to_dict(row)

    async def get_active_batch(
        self,
        chat_id: int,
        user_id: int | None,
    ) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM customer_upload_batches
                WHERE telegram_chat_id = $1
                  AND telegram_user_id IS NOT DISTINCT FROM $2
                  AND status = ANY($3::text[])
                ORDER BY id DESC
                LIMIT 1;
                """,
                chat_id,
                user_id,
                list(CustomerUploadBatchStatus.ACTIVE),
            )
            return _record_to_dict(row)

    async def get_resumable_batch(
        self,
        chat_id: int,
        user_id: int | None,
    ) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM customer_upload_batches
                WHERE telegram_chat_id = $1
                  AND telegram_user_id IS NOT DISTINCT FROM $2
                  AND status = ANY($3::text[])
                ORDER BY id DESC
                LIMIT 1;
                """,
                chat_id,
                user_id,
                list(CustomerUploadBatchStatus.RECOGNITION_RESUMABLE),
            )
            return _record_to_dict(row)

    async def get_batch_by_media_group(
        self,
        chat_id: int,
        user_id: int | None,
        media_group_id: str,
    ) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM customer_upload_batches
                WHERE telegram_chat_id = $1
                  AND telegram_user_id IS NOT DISTINCT FROM $2
                  AND media_group_id = $3
                ORDER BY id DESC
                LIMIT 1;
                """,
                chat_id,
                user_id,
                media_group_id,
            )
            return _record_to_dict(row)

    async def add_file(
        self,
        *,
        batch_id: int,
        telegram_message_id: int | None = None,
        telegram_file_id: str | None = None,
        original_filename: str | None = None,
        mime_type: str | None = None,
        file_extension: str | None = None,
        file_size: int | None = None,
        temporary_content: bytes | None = None,
    ) -> dict:
        async with self._pool.acquire() as connection:
            if telegram_message_id is not None:
                row = await connection.fetchrow(
                    """
                    INSERT INTO customer_upload_batch_files (
                        batch_id,
                        telegram_message_id,
                        telegram_file_id,
                        original_filename,
                        mime_type,
                        file_extension,
                        file_size,
                        temporary_content
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (batch_id, telegram_message_id) DO NOTHING
                    RETURNING *;
                    """,
                    batch_id,
                    telegram_message_id,
                    telegram_file_id,
                    original_filename,
                    mime_type,
                    file_extension,
                    file_size,
                    temporary_content,
                )
                if row is None:
                    row = await connection.fetchrow(
                        """
                        SELECT *
                        FROM customer_upload_batch_files
                        WHERE batch_id = $1
                          AND telegram_message_id = $2;
                        """,
                        batch_id,
                        telegram_message_id,
                    )
            else:
                row = await connection.fetchrow(
                    """
                    INSERT INTO customer_upload_batch_files (
                        batch_id,
                        telegram_message_id,
                        telegram_file_id,
                        original_filename,
                        mime_type,
                        file_extension,
                        file_size,
                        temporary_content
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING *;
                    """,
                    batch_id,
                    telegram_message_id,
                    telegram_file_id,
                    original_filename,
                    mime_type,
                    file_extension,
                    file_size,
                    temporary_content,
                )

            result = _normalize_file_record(row)
            assert result is not None
            return result

    async def get_batch_files(self, batch_id: int) -> list[dict]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT *
                FROM customer_upload_batch_files
                WHERE batch_id = $1
                ORDER BY id ASC;
                """,
                batch_id,
            )
            return [
                normalized
                for row in rows
                if (normalized := _normalize_file_record(row)) is not None
            ]

    async def count_batch_files(self, batch_id: int) -> int:
        async with self._pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT COUNT(*)::bigint
                FROM customer_upload_batch_files
                WHERE batch_id = $1;
                """,
                batch_id,
            )
            return int(value or 0)

    async def update_batch_status(
        self,
        batch_id: int,
        status: str,
        *,
        error_message: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    error_message = COALESCE($3, error_message),
                    updated_at = $4,
                    processing_started_at = CASE
                        WHEN $2 = $5 AND processing_started_at IS NULL
                            THEN $4
                        ELSE processing_started_at
                    END,
                    completed_at = CASE
                        WHEN $2 = ANY($6::text[]) THEN COALESCE(completed_at, $4)
                        ELSE completed_at
                    END
                WHERE id = $1
                RETURNING *;
                """,
                batch_id,
                status,
                error_message,
                now,
                CustomerUploadBatchStatus.RECOGNIZING,
                list(CustomerUploadBatchStatus.TERMINAL),
            )
            result = _record_to_dict(row)
            if result is None:
                raise LookupError(f"customer_upload_batch id={batch_id} not found")
            return result

    async def set_batch_media_group_id(
        self,
        batch_id: int,
        media_group_id: str,
    ) -> dict:
        """Set media_group_id only when it is currently empty."""
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    media_group_id = COALESCE(media_group_id, $2),
                    updated_at = $3
                WHERE id = $1
                RETURNING *;
                """,
                batch_id,
                media_group_id,
                now,
            )
            result = _record_to_dict(row)
            if result is None:
                raise LookupError(f"customer_upload_batch id={batch_id} not found")
            return result

    async def get_files_for_recognition(self, batch_id: int) -> list[dict]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT *
                FROM customer_upload_batch_files
                WHERE batch_id = $1
                  AND NOT (
                    recognition_status = $2
                    AND extracted_json IS NOT NULL
                  )
                ORDER BY id ASC;
                """,
                batch_id,
                CustomerUploadBatchFileRecognitionStatus.SUCCESS,
            )
            return [
                normalized
                for row in rows
                if (normalized := _normalize_file_record(row)) is not None
            ]

    async def mark_file_processing(self, file_id: int) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batch_files
                SET
                    recognition_status = $2,
                    error_message = NULL,
                    updated_at = $3
                WHERE id = $1
                RETURNING *;
                """,
                file_id,
                CustomerUploadBatchFileRecognitionStatus.PROCESSING,
                now,
            )
            result = _normalize_file_record(row)
            if result is None:
                raise LookupError(
                    f"customer_upload_batch_file id={file_id} not found"
                )
            return result

    async def mark_file_recognized(
        self,
        file_id: int,
        *,
        detected_document_type: str,
        extracted_json: dict,
        recognition_status: str = CustomerUploadBatchFileRecognitionStatus.SUCCESS,
    ) -> dict:
        now = datetime.now(timezone.utc)
        payload = json.dumps(extracted_json, ensure_ascii=False)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batch_files
                SET
                    detected_document_type = $2,
                    extracted_json = $3::jsonb,
                    recognition_status = $4,
                    error_message = NULL,
                    updated_at = $5
                WHERE id = $1
                RETURNING *;
                """,
                file_id,
                detected_document_type,
                payload,
                recognition_status,
                now,
            )
            result = _normalize_file_record(row)
            if result is None:
                raise LookupError(
                    f"customer_upload_batch_file id={file_id} not found"
                )
            return result

    async def mark_file_recognition_failed(
        self,
        file_id: int,
        error_message: str,
        *,
        detected_document_type: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batch_files
                SET
                    recognition_status = $2,
                    error_message = $3,
                    detected_document_type = COALESCE($4, detected_document_type),
                    updated_at = $5
                WHERE id = $1
                RETURNING *;
                """,
                file_id,
                CustomerUploadBatchFileRecognitionStatus.FAILED,
                error_message,
                detected_document_type,
                now,
            )
            result = _normalize_file_record(row)
            if result is None:
                raise LookupError(
                    f"customer_upload_batch_file id={file_id} not found"
                )
            return result

    async def reset_interrupted_processing_files(self, batch_id: int) -> int:
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                """
                UPDATE customer_upload_batch_files
                SET
                    recognition_status = $2,
                    updated_at = NOW()
                WHERE batch_id = $1
                  AND recognition_status = $3;
                """,
                batch_id,
                CustomerUploadBatchFileRecognitionStatus.PENDING,
                CustomerUploadBatchFileRecognitionStatus.PROCESSING,
            )
            try:
                return int(result.split()[-1])
            except (AttributeError, IndexError, ValueError):
                return 0

    async def set_batch_recognized(
        self,
        batch_id: int,
        *,
        error_message: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    error_message = $3,
                    updated_at = $4
                WHERE id = $1
                RETURNING *;
                """,
                batch_id,
                CustomerUploadBatchStatus.RECOGNIZED,
                error_message,
                now,
            )
            result = _record_to_dict(row)
            if result is None:
                raise LookupError(f"customer_upload_batch id={batch_id} not found")
            return result

    async def set_batch_error(
        self,
        batch_id: int,
        error_message: str,
        *,
        status: str = CustomerUploadBatchStatus.FAILED,
    ) -> dict:
        return await self.update_batch_status(
            batch_id,
            status,
            error_message=error_message,
        )

    async def mark_batch_recognizing(self, batch_id: int) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    error_message = NULL,
                    updated_at = $3,
                    processing_started_at = COALESCE(processing_started_at, $3)
                WHERE id = $1
                RETURNING *;
                """,
                batch_id,
                CustomerUploadBatchStatus.RECOGNIZING,
                now,
            )
            result = _record_to_dict(row)
            if result is None:
                raise LookupError(f"customer_upload_batch id={batch_id} not found")
            return result

    async def claim_batch_for_processing(self, batch_id: int) -> dict | None:
        """Atomically move batch from collecting to recognizing.

        Returns None when the batch is missing or already left collecting.
        """
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    updated_at = $3,
                    processing_started_at = COALESCE(processing_started_at, $3)
                WHERE id = $1
                  AND status = $4
                RETURNING *;
                """,
                batch_id,
                CustomerUploadBatchStatus.RECOGNIZING,
                now,
                CustomerUploadBatchStatus.COLLECTING,
            )
            return _record_to_dict(row)

    async def claim_batch_for_file_saving(self, batch_id: int) -> bool:
        """Atomically move batch from recognized to creating_folder.

        Returns True only for the single winner of the race.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = $3
                RETURNING id;
                """,
                batch_id,
                CustomerUploadBatchStatus.CREATING_FOLDER,
                CustomerUploadBatchStatus.RECOGNIZED,
            )
            return row is not None

    async def set_batch_customer_path(
        self,
        batch_id: int,
        customer_path: str,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    customer_path = $2,
                    updated_at = $3
                WHERE id = $1
                RETURNING *;
                """,
                batch_id,
                customer_path,
                now,
            )
            result = _record_to_dict(row)
            if result is None:
                raise LookupError(f"customer_upload_batch id={batch_id} not found")
            return result

    async def update_file_recognition(
        self,
        file_id: int,
        *,
        detected_document_type: str | None = None,
        recognition_status: str | None = None,
        extracted_json: dict | list | None = None,
        error_message: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        payload = (
            json.dumps(extracted_json, ensure_ascii=False)
            if extracted_json is not None
            else None
        )
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batch_files
                SET
                    detected_document_type = COALESCE($2, detected_document_type),
                    recognition_status = COALESCE($3, recognition_status),
                    extracted_json = COALESCE($4::jsonb, extracted_json),
                    error_message = COALESCE($5, error_message),
                    updated_at = $6
                WHERE id = $1
                RETURNING *;
                """,
                file_id,
                detected_document_type,
                recognition_status,
                payload,
                error_message,
                now,
            )
            result = _normalize_file_record(row)
            if result is None:
                raise LookupError(
                    f"customer_upload_batch_file id={file_id} not found"
                )
            return result

    async def set_file_yadisk_path(
        self,
        file_id: int,
        final_yadisk_path: str,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batch_files
                SET
                    final_yadisk_path = $2,
                    updated_at = $3
                WHERE id = $1
                RETURNING *;
                """,
                file_id,
                final_yadisk_path,
                now,
            )
            result = _normalize_file_record(row)
            if result is None:
                raise LookupError(
                    f"customer_upload_batch_file id={file_id} not found"
                )
            return result

    async def clear_file_temporary_content(self, file_id: int) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE customer_upload_batch_files
                SET
                    temporary_content = NULL,
                    updated_at = NOW()
                WHERE id = $1;
                """,
                file_id,
            )

    async def mark_batch_customer_saved(
        self,
        batch_id: int,
        *,
        customer_id: int | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    customer_id = COALESCE($4, customer_id),
                    updated_at = $3,
                    completed_at = COALESCE(completed_at, $3)
                WHERE id = $1
                RETURNING *;
                """,
                batch_id,
                CustomerUploadBatchStatus.CUSTOMER_SAVED,
                now,
                customer_id,
            )
            result = _record_to_dict(row)
            if result is None:
                raise LookupError(f"customer_upload_batch id={batch_id} not found")
            return result

    async def try_mark_batch_customer_saved(
        self,
        batch_id: int,
        *,
        customer_id: int,
    ) -> dict | None:
        """Atomically finish batch only when it is not already customer_saved."""
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batches
                SET
                    status = $2,
                    customer_id = $4,
                    updated_at = $3,
                    completed_at = COALESCE(completed_at, $3),
                    error_message = NULL
                WHERE id = $1
                  AND status <> $2
                RETURNING *;
                """,
                batch_id,
                CustomerUploadBatchStatus.CUSTOMER_SAVED,
                now,
                customer_id,
            )
            return _record_to_dict(row)

    async def get_batch_file(self, file_id: int) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM customer_upload_batch_files
                WHERE id = $1;
                """,
                file_id,
            )
            return _normalize_file_record(row)

    async def get_batch_files_with_paths(self, batch_id: int) -> list[dict]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    batch_id,
                    telegram_message_id,
                    telegram_file_id,
                    original_filename,
                    mime_type,
                    file_extension,
                    file_size,
                    detected_document_type,
                    recognition_status,
                    extracted_json,
                    error_message,
                    final_yadisk_path,
                    created_at,
                    updated_at
                FROM customer_upload_batch_files
                WHERE batch_id = $1
                ORDER BY id ASC;
                """,
                batch_id,
            )
            return [
                normalized
                for row in rows
                if (normalized := _normalize_file_record(row)) is not None
            ]

    async def update_file_document_type(
        self,
        file_id: int,
        *,
        detected_document_type: str,
        extracted_json: dict | list | None = None,
        error_message: str | None = None,
        clear_error_message: bool = False,
    ) -> dict:
        now = datetime.now(timezone.utc)
        payload = (
            json.dumps(extracted_json, ensure_ascii=False)
            if extracted_json is not None
            else None
        )
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customer_upload_batch_files
                SET
                    detected_document_type = $2,
                    recognition_status = $3,
                    extracted_json = COALESCE($4::jsonb, extracted_json),
                    error_message = CASE
                        WHEN $6 THEN NULL
                        ELSE COALESCE($5, error_message)
                    END,
                    updated_at = $7
                WHERE id = $1
                RETURNING *;
                """,
                file_id,
                detected_document_type,
                CustomerUploadBatchFileRecognitionStatus.SUCCESS,
                payload,
                error_message,
                clear_error_message,
                now,
            )
            result = _normalize_file_record(row)
            if result is None:
                raise LookupError(
                    f"customer_upload_batch_file id={file_id} not found"
                )
            return result

    async def recalculate_batch_validation(self, batch_id: int) -> dict:
        from app.services.customer_batch_recognition_service import (
            validate_document_kit,
        )

        files = await self.get_batch_files_with_paths(batch_id)
        kit = validate_document_kit(files)
        return {
            "is_complete": kit.is_complete,
            "missing_types": list(kit.missing_types),
            "duplicate_types": list(kit.duplicate_types),
            "unknown_file_ids": list(kit.unknown_file_ids),
            "mixed_file_ids": list(kit.mixed_file_ids),
            "failed_file_ids": list(kit.failed_file_ids),
            "warnings": list(kit.warnings),
            "documents_by_type": {
                key: [int(row["id"]) for row in rows]
                for key, rows in kit.documents_by_type.items()
            },
        }

    async def mark_batch_awaiting_confirmation(self, batch_id: int) -> dict:
        return await self.update_batch_status(
            batch_id,
            CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
        )

    async def clear_batch_file_contents(self, batch_id: int) -> int:
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                """
                UPDATE customer_upload_batch_files
                SET
                    temporary_content = NULL,
                    updated_at = NOW()
                WHERE batch_id = $1
                  AND temporary_content IS NOT NULL;
                """,
                batch_id,
            )
            # asyncpg returns strings like "UPDATE 3"
            try:
                return int(result.split()[-1])
            except (AttributeError, IndexError, ValueError):
                return 0

    async def mark_batch_failed(
        self,
        batch_id: int,
        error_message: str,
    ) -> dict:
        return await self.update_batch_status(
            batch_id,
            CustomerUploadBatchStatus.FAILED,
            error_message=error_message,
        )
