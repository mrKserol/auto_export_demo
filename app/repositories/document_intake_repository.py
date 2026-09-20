from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID, uuid4

import asyncpg


def _record_to_dict(record: asyncpg.Record | None) -> dict | None:
    if record is None:
        return None
    data = dict(record)
    for key in (
        "metadata",
        "recognition_result",
        "warnings",
        "manual_corrections",
        "changed_fields",
    ):
        value = data.get(key)
        if isinstance(value, str):
            data[key] = json.loads(value)
    return data


class DocumentIntakeRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_active_session(
        self,
        *,
        channel: str,
        external_user_id: str,
        conversation_id: str,
    ) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM document_intake_sessions
                WHERE started_channel = $1
                  AND started_external_user_id = $2
                  AND started_conversation_id = $3
                  AND status = 'collecting'
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                channel,
                external_user_id,
                conversation_id,
            )
            return _record_to_dict(row)

    async def create_session(
        self,
        *,
        session_id: UUID,
        channel: str,
        external_user_id: str,
        conversation_id: str,
        expires_at: datetime,
        metadata: dict,
    ) -> dict:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO document_intake_sessions (
                    id,
                    started_channel,
                    started_external_user_id,
                    started_conversation_id,
                    expires_at,
                    metadata
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING *;
                """,
                session_id,
                channel,
                external_user_id,
                conversation_id,
                expires_at,
                json.dumps(metadata, ensure_ascii=False),
            )
            result = _record_to_dict(row)
            assert result is not None
            return result

    async def get_session(self, session_id: UUID) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM document_intake_sessions WHERE id = $1;",
                session_id,
            )
            return _record_to_dict(row)

    async def update_session_status(self, session_id: UUID, status: str) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE document_intake_sessions
                SET status = $2,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *;
                """,
                session_id,
                status,
            )
            return _record_to_dict(row)

    async def update_review_corrections(
        self,
        *,
        session_id: UUID,
        corrections: dict,
        telegram_user_id: int,
        changed_fields: dict,
    ) -> dict:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE document_intake_sessions
                    SET manual_corrections = $2::jsonb, updated_at = NOW()
                    WHERE id = $1;
                    """,
                    session_id,
                    json.dumps(corrections, ensure_ascii=False),
                )
                await connection.execute(
                    """
                    INSERT INTO document_intake_review_audit
                        (id, session_id, telegram_user_id, changed_fields)
                    VALUES ($1, $2, $3, $4::jsonb);
                    """,
                    uuid4(),
                    session_id,
                    str(telegram_user_id),
                    json.dumps(list(changed_fields), ensure_ascii=False),
                )
        session = await self.get_session(session_id)
        return {} if session is None else dict(session.get("manual_corrections") or {})

    async def find_duplicate_document(
        self,
        *,
        session_id: UUID,
        provider_file_id: str | None,
        content_sha256: str,
    ) -> dict | None:
        async with self._pool.acquire() as connection:
            if provider_file_id:
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM document_intake_documents
                    WHERE session_id = $1
                      AND provider_file_id = $2
                    LIMIT 1;
                    """,
                    session_id,
                    provider_file_id,
                )
                if row is not None:
                    return _record_to_dict(row)
            row = await connection.fetchrow(
                """
                SELECT *
                FROM document_intake_documents
                WHERE session_id = $1
                  AND content_sha256 = $2
                LIMIT 1;
                """,
                session_id,
                content_sha256,
            )
            return _record_to_dict(row)

    async def create_document(self, **values) -> dict:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO document_intake_documents (
                    id,
                    session_id,
                    status,
                    channel,
                    external_user_id,
                    conversation_id,
                    provider_message_id,
                    provider_file_id,
                    media_group_id,
                    original_name,
                    mime_type,
                    file_size,
                    content_sha256,
                    storage_path,
                    storage_status,
                    document_type,
                    recognition_result,
                    confidence,
                    warnings
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14, $15, $16, $17::jsonb, $18, $19::jsonb
                )
                RETURNING *;
                """,
                values["id"],
                values["session_id"],
                values["status"],
                values["channel"],
                values["external_user_id"],
                values["conversation_id"],
                values.get("provider_message_id"),
                values.get("provider_file_id"),
                values.get("media_group_id"),
                values.get("original_name"),
                values.get("mime_type"),
                values.get("file_size"),
                values["content_sha256"],
                values.get("storage_path"),
                values.get("storage_status"),
                values.get("document_type"),
                json.dumps(values.get("recognition_result"), ensure_ascii=False),
                values.get("confidence"),
                json.dumps(values.get("warnings") or [], ensure_ascii=False),
            )
            result = _record_to_dict(row)
            assert result is not None
            return result

    async def update_document_storage(
        self,
        *,
        document_id: UUID,
        storage_path: str | None,
        storage_status: str,
    ) -> dict | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE document_intake_documents
                SET storage_path = $2,
                    storage_status = $3,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *;
                """,
                document_id,
                storage_path,
                storage_status,
            )
            return _record_to_dict(row)

    async def list_documents(self, session_id: UUID) -> list[dict]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT *
                FROM document_intake_documents
                WHERE session_id = $1
                ORDER BY created_at, id;
                """,
                session_id,
            )
            return [_record_to_dict(row) for row in rows if row is not None]
