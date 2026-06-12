from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json

import asyncpg


CREATE_DOCUMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    telegram_chat_id BIGINT,
    telegram_message_id BIGINT,
    telegram_user_id BIGINT,
    uploaded_by_name TEXT,
    uploaded_by_username TEXT,
    original_filename TEXT,
    stored_filename TEXT,
    file_extension TEXT,
    mime_type TEXT,
    file_size BIGINT,
    telegram_file_id TEXT,
    yadisk_path TEXT,
    processing_status TEXT,
    function_response_json JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);
"""

ENSURE_DOCUMENTS_COLUMNS_SQL = [
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS uploaded_by_name TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS uploaded_by_username TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS original_filename TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS stored_filename TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_extension TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS mime_type TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_size BIGINT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS telegram_file_id TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS yadisk_path TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_status TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS function_response_json JSONB;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;",
    "ALTER TABLE documents ALTER COLUMN telegram_file_unique_id DROP NOT NULL;",
    "ALTER TABLE documents ALTER COLUMN file_name DROP NOT NULL;",
    "ALTER TABLE documents ALTER COLUMN yandex_disk_path DROP NOT NULL;",
]


INSERT_DOCUMENT_SQL = """
INSERT INTO documents (
    telegram_chat_id,
    telegram_message_id,
    telegram_user_id,
    uploaded_by_name,
    uploaded_by_username,
    original_filename,
    stored_filename,
    file_extension,
    mime_type,
    file_size,
    telegram_file_id,
    yadisk_path,
    processing_status,
    created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
RETURNING id;
"""


UPDATE_PROCESSING_RESULT_SQL = """
UPDATE documents
SET
    processing_status = $2,
    function_response_json = $3::jsonb,
    error_message = $4,
    updated_at = $5,
    processed_at = $6
WHERE id = $1;
"""


UPDATE_PROCESSING_ERROR_SQL = """
UPDATE documents
SET
    processing_status = 'error',
    error_message = $2,
    updated_at = $3,
    processed_at = $3
WHERE id = $1;
"""


@dataclass(frozen=True)
class DocumentMetadata:
    telegram_chat_id: int
    telegram_message_id: int
    telegram_user_id: int | None
    uploaded_by_name: str | None
    uploaded_by_username: str | None
    original_filename: str
    stored_filename: str
    file_extension: str
    mime_type: str | None
    file_size: int | None
    telegram_file_id: str
    yadisk_path: str
    processing_status: str


class Database:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._database_url)
        async with self._pool.acquire() as connection:
            await connection.execute(CREATE_DOCUMENTS_TABLE_SQL)
            for statement in ENSURE_DOCUMENTS_COLUMNS_SQL:
                try:
                    await connection.execute(statement)
                except asyncpg.UndefinedColumnError:
                    pass

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def insert_document(self, metadata: DocumentMetadata) -> int:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            document_id = await connection.fetchval(
                INSERT_DOCUMENT_SQL,
                metadata.telegram_chat_id,
                metadata.telegram_message_id,
                metadata.telegram_user_id,
                metadata.uploaded_by_name,
                metadata.uploaded_by_username,
                metadata.original_filename,
                metadata.stored_filename,
                metadata.file_extension,
                metadata.mime_type,
                metadata.file_size,
                metadata.telegram_file_id,
                metadata.yadisk_path,
                metadata.processing_status,
                datetime.now(timezone.utc),
            )
            return int(document_id)

    async def save_processing_result(
        self,
        document_id: int,
        response_payload: dict,
        status: str = "processed",
        error_message: str | None = None,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            await connection.execute(
                UPDATE_PROCESSING_RESULT_SQL,
                document_id,
                status,
                json.dumps(response_payload, ensure_ascii=False),
                error_message,
                now,
                now,
            )

    async def mark_processing_error(self, document_id: int, error_message: str) -> None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            await connection.execute(
                UPDATE_PROCESSING_ERROR_SQL,
                document_id,
                error_message,
                datetime.now(timezone.utc),
            )
