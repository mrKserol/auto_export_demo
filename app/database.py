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

CREATE_CUSTOMERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id BIGSERIAL PRIMARY KEY,
    passport TEXT UNIQUE NOT NULL,
    first_name TEXT,
    last_name TEXT,
    surname TEXT,
    tin TEXT,
    ipain TEXT,
    phone TEXT,
    email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_CARS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cars (
    id BIGSERIAL PRIMARY KEY,
    vin TEXT UNIQUE,
    brand TEXT,
    model TEXT,
    color TEXT,
    category TEXT,
    engine_num TEXT,
    date TEXT,
    eng_capacity TEXT,
    hp TEXT,
    type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_CONTRACTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS contracts (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT REFERENCES customers(id),
    car_id BIGINT REFERENCES cars(id),
    invoice TEXT,
    epts TEXT,
    invoice_date TEXT,
    epts_date TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_SPECIFICATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS specifications (
    id BIGSERIAL PRIMARY KEY,
    brand TEXT,
    model TEXT,
    year TEXT,
    eng_capacity TEXT,
    eng_type TEXT,
    drive TEXT,
    transmission TEXT,
    color TEXT,
    complectation TEXT,
    mileage TEXT,
    price TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_ESTIMATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS estimates (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    specification_id BIGINT NOT NULL REFERENCES specifications(id) ON DELETE CASCADE,
    engine_power NUMERIC(10, 2),
    car_age_category TEXT,
    price_abroad NUMERIC(14, 2),
    price_currency TEXT DEFAULT 'CNY',
    exchange_rate NUMERIC(14, 6),
    price_rub NUMERIC(14, 2),
    bank_commission NUMERIC(14, 2),
    transit_declaration_price NUMERIC(14, 2),
    insurance_shipment NUMERIC(14, 2),
    custom_clearing NUMERIC(14, 2),
    custom_duties NUMERIC(14, 2),
    contractor_comission NUMERIC(14, 2),
    total_rub NUMERIC(14, 2),
    customs_sbor NUMERIC(14, 2) DEFAULT 0,
    customs_tax NUMERIC(14, 2) DEFAULT 0,
    customs_util NUMERIC(14, 2) DEFAULT 0,
    customs_nds NUMERIC(14, 2) DEFAULT 0,
    customs_excise NUMERIC(14, 2) DEFAULT 0,
    customs_total NUMERIC(14, 2) DEFAULT 0,
    customs_total2 NUMERIC(14, 2) DEFAULT 0,
    customs_source TEXT,
    customs_raw_response JSONB,
    customs_error TEXT,
    inspect_transport_price NUMERIC(14, 2) DEFAULT 0,
    contractor_comission_prepayment NUMERIC(14, 2) DEFAULT 0,
    contractor_comission_postpayment NUMERIC(14, 2) DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

ENSURE_ESTIMATES_CUSTOMS_COLUMNS_SQL = [
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_sbor NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_tax NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_util NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_nds NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_excise NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_total NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_total2 NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_source TEXT;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_raw_response JSONB;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customs_error TEXT;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS inspect_transport_price NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS contractor_comission_prepayment NUMERIC(14, 2) DEFAULT 0;",
    "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS contractor_comission_postpayment NUMERIC(14, 2) DEFAULT 0;",
]

ENSURE_CUSTOMERS_EXTRA_FIELDS_SQL = [
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS by_whom_issued TEXT;",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS date_issue TEXT;",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS registration_address TEXT;",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS department_code TEXT;",
    "ALTER TABLE customers ADD COLUMN IF NOT EXISTS specification_id BIGINT;",
]

ENSURE_CUSTOMERS_SPECIFICATION_FK_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'customers_specification_id_fkey'
    ) THEN
        ALTER TABLE customers
        ADD CONSTRAINT customers_specification_id_fkey
        FOREIGN KEY (specification_id)
        REFERENCES specifications(id)
        ON DELETE SET NULL;
    END IF;
END $$;
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
    "CREATE INDEX IF NOT EXISTS idx_customers_passport ON customers(passport);",
    "CREATE INDEX IF NOT EXISTS idx_contracts_customer_id ON contracts(customer_id);",
    "CREATE INDEX IF NOT EXISTS idx_contracts_car_id ON contracts(car_id);",
    "CREATE INDEX IF NOT EXISTS idx_customers_specification_id ON customers(specification_id);",
    "CREATE INDEX IF NOT EXISTS idx_estimates_customer_id ON estimates(customer_id);",
    "CREATE INDEX IF NOT EXISTS idx_estimates_specification_id ON estimates(specification_id);",
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
            await connection.execute(CREATE_CUSTOMERS_TABLE_SQL)
            await connection.execute(CREATE_CARS_TABLE_SQL)
            await connection.execute(CREATE_CONTRACTS_TABLE_SQL)
            await connection.execute(CREATE_SPECIFICATIONS_TABLE_SQL)
            await connection.execute(CREATE_ESTIMATES_TABLE_SQL)
            for statement in ENSURE_CUSTOMERS_EXTRA_FIELDS_SQL:
                await connection.execute(statement)
            for statement in ENSURE_ESTIMATES_CUSTOMS_COLUMNS_SQL:
                await connection.execute(statement)
            await connection.execute(ENSURE_CUSTOMERS_SPECIFICATION_FK_SQL)
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

    async def find_customer_by_passport(self, passport: str) -> dict | None:
        return await self.search_customer_by_passport(passport)

    async def search_customer_by_passport(self, passport: str) -> dict | None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM customers WHERE passport = $1;",
                passport,
            )
            return _record_to_dict(row) if row else None

    async def get_customer_by_id(self, customer_id: int) -> dict | None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM customers WHERE id = $1;",
                customer_id,
            )
            return _record_to_dict(row) if row else None

    async def create_customer(self, data: dict) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO customers (
                    passport,
                    first_name,
                    last_name,
                    surname,
                    tin,
                    ipain,
                    phone,
                    email,
                    by_whom_issued,
                    date_issue,
                    registration_address,
                    department_code,
                    specification_id,
                    created_at,
                    updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $14)
                RETURNING *;
                """,
                data["passport"],
                data.get("first_name"),
                data.get("last_name"),
                data.get("surname"),
                data.get("tin"),
                data.get("ipain"),
                data.get("phone"),
                data.get("email"),
                data.get("by_whom_issued"),
                data.get("date_issue"),
                data.get("registration_address"),
                data.get("department_code"),
                data.get("specification_id"),
                now,
            )
            return _record_to_dict(row)

    _CUSTOMER_UPDATABLE_FIELDS = frozenset(
        {
            "passport",
            "first_name",
            "last_name",
            "surname",
            "tin",
            "ipain",
            "phone",
            "email",
            "by_whom_issued",
            "date_issue",
            "registration_address",
            "department_code",
            "specification_id",
        }
    )

    _SPECIFICATION_UPDATABLE_FIELDS = frozenset(
        {
            "brand",
            "model",
            "year",
            "eng_capacity",
            "eng_type",
            "drive",
            "transmission",
            "color",
            "complectation",
            "mileage",
            "price",
        }
    )

    async def update_customer(
        self,
        customer_id: int,
        field_name: str,
        value: str | int | None,
    ) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")
        if field_name not in self._CUSTOMER_UPDATABLE_FIELDS:
            raise ValueError(f"Field {field_name} is not updatable")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE customers
                SET {field_name} = $2, updated_at = $3
                WHERE id = $1
                RETURNING *;
                """,
                customer_id,
                value,
                datetime.now(timezone.utc),
            )
            return _record_to_dict(row)

    async def count_contracts_for_customer(self, customer_id: int) -> int:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            count = await connection.fetchval(
                "SELECT COUNT(*) FROM contracts WHERE customer_id = $1;",
                customer_id,
            )
            return int(count)

    async def delete_customer_if_no_contracts(self, customer_id: int) -> bool:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                customer = await connection.fetchrow(
                    "SELECT specification_id FROM customers WHERE id = $1;",
                    customer_id,
                )
                if customer is None:
                    return False

                contract_count = await connection.fetchval(
                    "SELECT COUNT(*) FROM contracts WHERE customer_id = $1;",
                    customer_id,
                )
                if int(contract_count) > 0:
                    return False

                result = await connection.execute(
                    "DELETE FROM customers WHERE id = $1;",
                    customer_id,
                )
                if not result.endswith("1"):
                    return False

                specification_id = customer["specification_id"]
                if specification_id is not None:
                    await connection.execute(
                        "DELETE FROM specifications WHERE id = $1;",
                        specification_id,
                    )

                return True

    async def delete_customer_with_related_data(self, customer_id: int) -> bool:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                customer = await connection.fetchrow(
                    "SELECT id, specification_id FROM customers WHERE id = $1;",
                    customer_id,
                )
                if customer is None:
                    return False

                specification_id = customer["specification_id"]

                await connection.execute(
                    "DELETE FROM estimates WHERE customer_id = $1;",
                    customer_id,
                )
                if specification_id is not None:
                    await connection.execute(
                        "DELETE FROM estimates WHERE specification_id = $1;",
                        specification_id,
                    )

                await connection.execute(
                    "UPDATE contracts SET customer_id = NULL WHERE customer_id = $1;",
                    customer_id,
                )

                result = await connection.execute(
                    "DELETE FROM customers WHERE id = $1;",
                    customer_id,
                )
                if not result.endswith("1"):
                    return False

                if specification_id is not None:
                    await connection.execute(
                        "DELETE FROM specifications WHERE id = $1;",
                        specification_id,
                    )

                return True

    async def create_specification(self, data: dict) -> int:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            spec_id = await connection.fetchval(
                """
                INSERT INTO specifications (
                    brand,
                    model,
                    year,
                    eng_capacity,
                    eng_type,
                    drive,
                    transmission,
                    color,
                    complectation,
                    mileage,
                    price,
                    created_at,
                    updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12)
                RETURNING id;
                """,
                data.get("brand"),
                data.get("model"),
                data.get("year"),
                data.get("eng_capacity"),
                data.get("eng_type"),
                data.get("drive"),
                data.get("transmission"),
                data.get("color"),
                data.get("complectation"),
                data.get("mileage"),
                data.get("price"),
                now,
            )
            return int(spec_id)

    async def create_empty_specification(self) -> int:
        return await self.create_specification({})

    async def get_specification_by_id(self, specification_id: int) -> dict | None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM specifications WHERE id = $1;",
                specification_id,
            )
            return _record_to_dict(row) if row else None

    async def update_specification(
        self,
        specification_id: int,
        field_name: str,
        value: str | None,
    ) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")
        if field_name not in self._SPECIFICATION_UPDATABLE_FIELDS:
            raise ValueError(f"Field {field_name} is not updatable")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE specifications
                SET {field_name} = $2, updated_at = $3
                WHERE id = $1
                RETURNING *;
                """,
                specification_id,
                value,
                datetime.now(timezone.utc),
            )
            return _record_to_dict(row)

    async def delete_specification(self, specification_id: int) -> bool:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM specifications WHERE id = $1;",
                specification_id,
            )
            return result.endswith("1")

    async def attach_specification_to_customer(
        self,
        customer_id: int,
        specification_id: int,
    ) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customers
                SET specification_id = $2, updated_at = $3
                WHERE id = $1
                RETURNING *;
                """,
                customer_id,
                specification_id,
                datetime.now(timezone.utc),
            )
            return _record_to_dict(row)

    async def detach_specification_from_customer(self, customer_id: int) -> dict:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE customers
                SET specification_id = NULL, updated_at = $2
                WHERE id = $1
                RETURNING *;
                """,
                customer_id,
                datetime.now(timezone.utc),
            )
            return _record_to_dict(row)


    async def create_estimate(
        self,
        *,
        customer_id: int,
        specification_id: int,
        data: dict,
    ) -> int:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            await self._delete_estimates_by_specification_id(connection, specification_id)
            estimate_id = await connection.fetchval(
                """
                INSERT INTO estimates (
                    customer_id,
                    specification_id,
                    engine_power,
                    car_age_category,
                    price_abroad,
                    price_currency,
                    exchange_rate,
                    price_rub,
                    bank_commission,
                    transit_declaration_price,
                    insurance_shipment,
                    custom_clearing,
                    custom_duties,
                    contractor_comission,
                    total_rub,
                    customs_sbor,
                    customs_tax,
                    customs_util,
                    customs_nds,
                    customs_excise,
                    customs_total,
                    customs_total2,
                    customs_source,
                    customs_raw_response,
                    customs_error,
                    inspect_transport_price,
                    contractor_comission_prepayment,
                    contractor_comission_postpayment,
                    created_at,
                    updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                    $16, $17, $18, $19, $20, $21, $22, $23, $24::jsonb, $25, $26, $27, $28, $29, $29
                )
                RETURNING id;
                """,
                customer_id,
                specification_id,
                data.get("engine_power"),
                data.get("car_age_category"),
                data.get("price_abroad"),
                data.get("price_currency"),
                data.get("exchange_rate"),
                data.get("price_rub"),
                data.get("bank_commission"),
                data.get("transit_declaration_price"),
                data.get("insurance_shipment"),
                data.get("custom_clearing"),
                data.get("custom_duties"),
                data.get("contractor_comission"),
                data.get("total_rub"),
                data.get("customs_sbor"),
                data.get("customs_tax"),
                data.get("customs_util"),
                data.get("customs_nds"),
                data.get("customs_excise"),
                data.get("customs_total"),
                data.get("customs_total2"),
                data.get("customs_source"),
                json.dumps(data.get("customs_raw_response"))
                if data.get("customs_raw_response") is not None
                else None,
                data.get("customs_error"),
                data.get("inspect_transport_price"),
                data.get("contractor_comission_prepayment"),
                data.get("contractor_comission_postpayment"),
                now,
            )
            return int(estimate_id)

    async def get_estimate_by_specification_id(self, specification_id: int) -> dict | None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM estimates
                WHERE specification_id = $1
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                specification_id,
            )
            return _record_to_dict(row) if row else None

    async def delete_estimates_by_specification_id(self, specification_id: int) -> None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            await self._delete_estimates_by_specification_id(connection, specification_id)

    async def _delete_estimates_by_specification_id(
        self,
        connection: asyncpg.Connection,
        specification_id: int,
    ) -> None:
        await connection.execute(
            """
            DELETE FROM estimates
            WHERE specification_id = $1;
            """,
            specification_id,
        )

    async def get_estimate_by_id(self, estimate_id: int) -> dict | None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM estimates WHERE id = $1;",
                estimate_id,
            )
            return _record_to_dict(row) if row else None

    async def get_estimate_by_customer_id(self, customer_id: int) -> dict | None:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM estimates
                WHERE customer_id = $1
                ORDER BY id DESC
                LIMIT 1;
                """,
                customer_id,
            )
            return _record_to_dict(row) if row else None


def _record_to_dict(record: asyncpg.Record | None) -> dict:
    if record is None:
        return {}
    return dict(record)
