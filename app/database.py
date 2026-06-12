from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json

import asyncpg


CREATE_CASES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cases (
    id BIGSERIAL PRIMARY KEY,
    case_key TEXT UNIQUE,
    case_folder_name TEXT,
    case_folder_path TEXT,
    vin TEXT UNIQUE,
    vin_last4 TEXT,
    customer_name TEXT,
    customer_surname TEXT,
    customer_passport TEXT,
    car_brand TEXT,
    car_model TEXT,
    car_year TEXT,
    contract_number TEXT,
    invoice_number TEXT,
    container_number TEXT,
    epts_number TEXT,
    country_from TEXT,
    country_to TEXT,
    route TEXT,
    responsible_manager TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

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
    case_id BIGINT REFERENCES cases(id),
    intake_yadisk_path TEXT,
    current_yadisk_path TEXT,
    document_type TEXT,
    extracted_json JSONB,
    binding_status TEXT DEFAULT 'unbound',
    binding_score INTEGER,
    binding_candidates_json JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);
"""

CREATE_DOCUMENT_FIELDS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS document_fields (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT REFERENCES documents(id),
    case_id BIGINT REFERENCES cases(id),
    field_name TEXT NOT NULL,
    field_value TEXT,
    confidence NUMERIC,
    source_fragment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_CASE_CHECKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS case_checks (
    id BIGSERIAL PRIMARY KEY,
    case_id BIGINT REFERENCES cases(id),
    check_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS case_id BIGINT REFERENCES cases(id);",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS intake_yadisk_path TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS current_yadisk_path TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_type TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS extracted_json JSONB;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS binding_status TEXT DEFAULT 'unbound';",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS binding_score INTEGER;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS binding_candidates_json JSONB;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;",
    "ALTER TABLE documents ALTER COLUMN telegram_file_unique_id DROP NOT NULL;",
    "ALTER TABLE documents ALTER COLUMN file_name DROP NOT NULL;",
    "ALTER TABLE documents ALTER COLUMN yandex_disk_path DROP NOT NULL;",
]

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_documents_case_id ON documents(case_id);",
    "CREATE INDEX IF NOT EXISTS idx_document_fields_case_id ON document_fields(case_id);",
    "CREATE INDEX IF NOT EXISTS idx_document_fields_document_id ON document_fields(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_document_fields_field_name ON document_fields(field_name);",
    "CREATE INDEX IF NOT EXISTS idx_cases_vin ON cases(vin);",
    "CREATE INDEX IF NOT EXISTS idx_cases_contract_number ON cases(contract_number);",
    "CREATE INDEX IF NOT EXISTS idx_cases_invoice_number ON cases(invoice_number);",
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
    intake_yadisk_path,
    current_yadisk_path,
    processing_status,
    binding_status,
    created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12, $12, $13, 'unbound', $14)
RETURNING id;
"""

UPDATE_PROCESSING_RESULT_SQL = """
UPDATE documents
SET
    processing_status = $2,
    function_response_json = $3::jsonb,
    error_message = $4,
    updated_at = $5,
    processed_at = $6,
    extracted_json = $7::jsonb,
    document_type = $8,
    intake_yadisk_path = COALESCE(intake_yadisk_path, yadisk_path),
    current_yadisk_path = COALESCE(current_yadisk_path, yadisk_path)
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
            await connection.execute(CREATE_CASES_TABLE_SQL)
            await connection.execute(CREATE_DOCUMENTS_TABLE_SQL)
            await connection.execute(CREATE_DOCUMENT_FIELDS_TABLE_SQL)
            await connection.execute(CREATE_CASE_CHECKS_TABLE_SQL)
            for statement in ENSURE_DOCUMENTS_COLUMNS_SQL:
                try:
                    await connection.execute(statement)
                except asyncpg.UndefinedColumnError:
                    pass
            for statement in CREATE_INDEXES_SQL:
                await connection.execute(statement)

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
        extracted_data = response_payload.get("extracted_data") or {}
        document_type = extracted_data.get("document_type")
        async with self._pool.acquire() as connection:
            await connection.execute(
                UPDATE_PROCESSING_RESULT_SQL,
                document_id,
                status,
                json.dumps(response_payload, ensure_ascii=False),
                error_message,
                now,
                now,
                json.dumps(extracted_data, ensure_ascii=False),
                str(document_type) if document_type else None,
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

    async def find_candidate_cases(self, fields: dict) -> list[dict]:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT *
                FROM cases
                WHERE ($1::text IS NOT NULL AND vin = $1)
                   OR ($2::text IS NOT NULL AND contract_number = $2)
                   OR ($3::text IS NOT NULL AND invoice_number = $3)
                   OR ($4::text IS NOT NULL AND customer_passport = $4)
                   OR ($5::text IS NOT NULL AND epts_number = $5)
                   OR ($6::text IS NOT NULL AND vin_last4 = $6)
                   OR ($7::text IS NOT NULL AND customer_surname = $7)
                ORDER BY id;
                """,
                fields.get("vin"),
                fields.get("contract_number"),
                fields.get("invoice_number"),
                fields.get("customer_passport"),
                fields.get("epts_number"),
                fields.get("vin_last4"),
                fields.get("customer_surname"),
            )
            return [_record_to_dict(row) for row in rows]

    async def create_case(self, fields: dict) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO cases (
                    vin,
                    vin_last4,
                    customer_name,
                    customer_surname,
                    customer_passport,
                    car_brand,
                    car_model,
                    car_year,
                    contract_number,
                    invoice_number,
                    epts_number,
                    country_from,
                    country_to,
                    route,
                    updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                RETURNING *;
                """,
                fields.get("vin"),
                fields.get("vin_last4"),
                fields.get("customer_name"),
                fields.get("customer_surname"),
                fields.get("customer_passport"),
                fields.get("car_brand"),
                fields.get("car_model"),
                fields.get("car_year"),
                fields.get("contract_number"),
                fields.get("invoice_number"),
                fields.get("epts_number"),
                fields.get("country_from"),
                fields.get("country_to"),
                fields.get("route"),
                datetime.now(timezone.utc),
            )
            return _record_to_dict(row)

    async def update_case_identity(
        self,
        case_id: int,
        *,
        case_key: str,
        folder_name: str,
        folder_path: str,
    ) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE cases
                SET
                    case_key = $2,
                    case_folder_name = $3,
                    case_folder_path = $4,
                    updated_at = $5
                WHERE id = $1
                RETURNING *;
                """,
                case_id,
                case_key,
                folder_name,
                folder_path,
                datetime.now(timezone.utc),
            )
            return _record_to_dict(row)

    async def update_case_from_fields(self, case_id: int, fields: dict) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE cases
                SET
                    vin = COALESCE(vin, $2),
                    vin_last4 = COALESCE(vin_last4, $3),
                    customer_name = COALESCE(customer_name, $4),
                    customer_surname = COALESCE(customer_surname, $5),
                    customer_passport = COALESCE(customer_passport, $6),
                    car_brand = COALESCE(car_brand, $7),
                    car_model = COALESCE(car_model, $8),
                    car_year = COALESCE(car_year, $9),
                    contract_number = COALESCE(contract_number, $10),
                    invoice_number = COALESCE(invoice_number, $11),
                    epts_number = COALESCE(epts_number, $12),
                    country_from = COALESCE(country_from, $13),
                    country_to = COALESCE(country_to, $14),
                    route = COALESCE(route, $15),
                    updated_at = $16
                WHERE id = $1
                RETURNING *;
                """,
                case_id,
                fields.get("vin"),
                fields.get("vin_last4"),
                fields.get("customer_name"),
                fields.get("customer_surname"),
                fields.get("customer_passport"),
                fields.get("car_brand"),
                fields.get("car_model"),
                fields.get("car_year"),
                fields.get("contract_number"),
                fields.get("invoice_number"),
                fields.get("epts_number"),
                fields.get("country_from"),
                fields.get("country_to"),
                fields.get("route"),
                datetime.now(timezone.utc),
            )
            return _record_to_dict(row)

    async def bind_document_to_case(
        self,
        document_id: int,
        case_id: int | None,
        *,
        score: int,
        status: str,
        candidates: list[dict] | None = None,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE documents
                SET
                    case_id = $2,
                    binding_score = $3,
                    binding_status = $4,
                    binding_candidates_json = $5::jsonb,
                    updated_at = $6
                WHERE id = $1;
                """,
                document_id,
                case_id,
                score,
                status,
                json.dumps(candidates or [], ensure_ascii=False),
                datetime.now(timezone.utc),
            )

    async def update_document_paths(
        self,
        document_id: int,
        *,
        current_yadisk_path: str,
        error_message: str | None = None,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE documents
                SET
                    current_yadisk_path = $2,
                    yadisk_path = $2,
                    error_message = COALESCE($3, error_message),
                    updated_at = $4
                WHERE id = $1;
                """,
                document_id,
                current_yadisk_path,
                error_message,
                datetime.now(timezone.utc),
            )

    async def save_document_fields(
        self,
        document_id: int,
        case_id: int | None,
        fields: dict,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        rows = [
            (document_id, case_id, key, str(value))
            for key, value in fields.items()
            if value is not None and value != ""
        ]
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM document_fields WHERE document_id = $1;",
                document_id,
            )
            if rows:
                await connection.executemany(
                    """
                    INSERT INTO document_fields (
                        document_id,
                        case_id,
                        field_name,
                        field_value
                    ) VALUES ($1, $2, $3, $4);
                    """,
                    rows,
                )

    async def save_case_checks(self, case_id: int, checks: list[dict]) -> None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        rows = [
            (
                case_id,
                check["check_code"],
                check["severity"],
                check["status"],
                check["message"],
                json.dumps(check.get("details_json") or {}, ensure_ascii=False),
            )
            for check in checks
        ]
        codes = [check["check_code"] for check in checks]

        async with self._pool.acquire() as connection:
            if codes:
                await connection.execute(
                    "DELETE FROM case_checks WHERE case_id = $1 AND check_code = ANY($2::text[]);",
                    case_id,
                    codes,
                )
            if rows:
                await connection.executemany(
                    """
                    INSERT INTO case_checks (
                        case_id,
                        check_code,
                        severity,
                        status,
                        message,
                        details_json
                    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb);
                    """,
                    rows,
                )

    async def get_case_field_values(self, case_id: int, field_name: str) -> list[str]:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT field_value
                FROM document_fields
                WHERE case_id = $1
                  AND field_name = $2
                  AND field_value IS NOT NULL
                  AND field_value <> ''
                ORDER BY field_value;
                """,
                case_id,
                field_name,
            )
            return [str(row["field_value"]) for row in rows]

    async def get_case_document_types(self, case_id: int) -> list[str]:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT field_value
                FROM document_fields
                WHERE case_id = $1
                  AND field_name = 'document_type'
                  AND field_value IS NOT NULL
                  AND field_value <> ''
                ORDER BY field_value;
                """,
                case_id,
            )
            return [str(row["field_value"]) for row in rows]


def _record_to_dict(record: asyncpg.Record | None) -> dict:
    if record is None:
        return {}
    return dict(record)
