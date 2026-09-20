from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from app import database
from app.services.document_intake_service import (
    DOCUMENT_STATUS_RECOGNIZED,
    INTAKE_STATUS_CANCELLED,
    INTAKE_STATUS_EXPIRED,
    INTAKE_STATUS_REVIEW,
    STORAGE_STATUS_FAILED,
    STORAGE_STATUS_STORED,
    DocumentIntakeService,
    IntakeConflictError,
    IntakeUpload,
)


class FakeDocumentIntakeRepository:
    def __init__(self) -> None:
        self.sessions: dict[UUID, dict] = {}
        self.documents: dict[UUID, list[dict]] = {}

    async def get_active_session(self, *, channel, external_user_id, conversation_id):
        for session in sorted(
            self.sessions.values(),
            key=lambda item: item["created_at"],
            reverse=True,
        ):
            if (
                session["started_channel"] == channel
                and session["started_external_user_id"] == external_user_id
                and session["started_conversation_id"] == conversation_id
                and session["status"] == "collecting"
            ):
                return dict(session)
        return None

    async def create_session(
        self,
        *,
        session_id,
        channel,
        external_user_id,
        conversation_id,
        expires_at,
        metadata,
    ):
        now = datetime.now(timezone.utc)
        session = {
            "id": session_id,
            "status": "collecting",
            "created_at": now,
            "updated_at": now,
            "expires_at": expires_at,
            "started_channel": channel,
            "started_external_user_id": external_user_id,
            "started_conversation_id": conversation_id,
            "metadata": metadata,
        }
        self.sessions[session_id] = session
        self.documents[session_id] = []
        return dict(session)

    async def get_session(self, session_id):
        session = self.sessions.get(session_id)
        return None if session is None else dict(session)

    async def update_session_status(self, session_id, status):
        session = self.sessions.get(session_id)
        if session is None:
            return None
        session["status"] = status
        session["updated_at"] = datetime.now(timezone.utc)
        return dict(session)

    async def find_duplicate_document(
        self,
        *,
        session_id,
        provider_file_id,
        content_sha256,
    ):
        for document in self.documents.get(session_id, []):
            if provider_file_id and document.get("provider_file_id") == provider_file_id:
                return dict(document)
            if document.get("content_sha256") == content_sha256:
                return dict(document)
        return None

    async def create_document(self, **values):
        document = dict(values)
        now = datetime.now(timezone.utc)
        document["created_at"] = now
        document["updated_at"] = now
        self.documents[document["session_id"]].append(document)
        return dict(document)

    async def update_document_storage(
        self,
        *,
        document_id,
        storage_path,
        storage_status,
    ):
        for documents in self.documents.values():
            for document in documents:
                if document["id"] == document_id:
                    document["storage_path"] = storage_path
                    document["storage_status"] = storage_status
                    document["updated_at"] = datetime.now(timezone.utc)
                    return dict(document)
        return None

    async def list_documents(self, session_id):
        return [dict(document) for document in self.documents.get(session_id, [])]


def _upload(
    content: bytes,
    *,
    provider_file_id: str | None = None,
    provider_message_id: str | None = None,
    original_name: str = "doc.jpg",
) -> IntakeUpload:
    return IntakeUpload(
        content=content,
        channel="telegram",
        external_user_id="316257868",
        conversation_id="316257868",
        provider_message_id=provider_message_id,
        provider_file_id=provider_file_id,
        media_group_id="album-1",
        original_name=original_name,
        mime_type="image/jpeg",
        file_size=len(content),
    )


class DocumentIntakeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repository = FakeDocumentIntakeRepository()
        self.recognition = AsyncMock()
        self.disk = AsyncMock()
        self.disk.base_path = "/auto_export_demo"
        self.disk.upload_bytes.side_effect = lambda path, content: path
        self.service = DocumentIntakeService(
            repository=self.repository,
            recognition_service=self.recognition,
            yandex_disk_client=self.disk,
            max_file_bytes=1024 * 1024,
        )

    async def _session_id(self) -> UUID:
        session = await self.service.create_or_get_session(
            channel="telegram",
            external_user_id="316257868",
            conversation_id="316257868",
            metadata={},
        )
        return UUID(session["session_id"])

    async def test_creates_session_and_reuses_active_draft(self) -> None:
        first = await self.service.create_or_get_session(
            channel="telegram",
            external_user_id="316257868",
            conversation_id="316257868",
            metadata={"source": "n8n"},
        )
        second = await self.service.create_or_get_session(
            channel="telegram",
            external_user_id="316257868",
            conversation_id="316257868",
            metadata={},
        )

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["status"], "collecting")

    async def test_adds_document_and_stores_recognition_result(self) -> None:
        session_id = await self._session_id()
        self.recognition.recognize_document.return_value = SimpleNamespace(
            document_type="passport_main",
            confidence=0.91,
            extracted_fields={
                "last_name": "Иванов",
                "first_name": "Иван",
                "surname": "Иванович",
                "passport": "80 06 035956",
            },
            warnings=[],
        )

        response = await self.service.add_document(
            session_id,
            _upload(b"passport", provider_file_id="file-1"),
        )

        self.assertFalse(response["duplicate"])
        self.assertEqual(response["status"], DOCUMENT_STATUS_RECOGNIZED)
        self.assertEqual(response["document"]["document_type"], "passport_main")
        self.assertEqual(response["document"]["surname"], "Иванов")
        self.disk.upload_bytes.assert_awaited_once()
        self.recognition.recognize_document.assert_awaited_once()

    async def test_duplicate_provider_file_does_not_create_second_document(self) -> None:
        session_id = await self._session_id()
        self.recognition.recognize_document.return_value = SimpleNamespace(
            document_type="tin",
            confidence=0.8,
            extracted_fields={"tin": "123456789012"},
            warnings=[],
        )

        first = await self.service.add_document(
            session_id,
            _upload(b"tin", provider_file_id="file-1"),
        )
        second = await self.service.add_document(
            session_id,
            _upload(b"tin", provider_file_id="file-1"),
        )

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.repository.documents[session_id]), 1)
        self.recognition.recognize_document.assert_awaited_once()

    async def test_multiple_documents_share_one_intake_session(self) -> None:
        session_id = await self._session_id()
        self.recognition.recognize_document.side_effect = [
            SimpleNamespace(
                document_type="passport_main",
                confidence=0.9,
                extracted_fields={"last_name": "Иванов"},
                warnings=[],
            ),
            SimpleNamespace(
                document_type="snils",
                confidence=0.8,
                extracted_fields={"ipain": "123-456-789 00"},
                warnings=[],
            ),
        ]

        await self.service.add_document(session_id, _upload(b"one", provider_file_id="1"))
        await self.service.add_document(session_id, _upload(b"two", provider_file_id="2"))
        summary = await self.service.get_summary(session_id)

        self.assertEqual(len(summary["documents"]), 2)
        self.assertIn("passport_main", summary["recognized_document_types"])
        self.assertIn("snils", summary["recognized_document_types"])

    async def test_finish_incomplete_package_returns_not_ready(self) -> None:
        session_id = await self._session_id()
        summary = await self.service.finish(session_id)

        self.assertEqual(summary["status"], "collecting")
        self.assertFalse(summary["ready_for_confirmation"])
        self.assertIn("missing_document_types", summary["reasons"])

    async def test_finish_review_session_is_idempotent(self) -> None:
        session_id = await self._session_id()
        self.recognition.recognize_document.side_effect = [
            SimpleNamespace(document_type=name, confidence=0.9, extracted_fields={}, warnings=[])
            for name in ("passport_main", "passport_registration", "tin", "snils")
        ]
        for index, content in enumerate((b"pm", b"pr", b"tin", b"snils")):
            await self.service.add_document(
                session_id,
                _upload(content, provider_file_id=f"file-{index}"),
            )

        first = await self.service.finish(session_id)
        second = await self.service.finish(session_id)

        self.assertEqual(first, second)
        self.assertEqual(second["status"], INTAKE_STATUS_REVIEW)
        self.assertTrue(second["ready_for_confirmation"])

    async def test_finish_complete_package_returns_ready(self) -> None:
        session_id = await self._session_id()
        self.recognition.recognize_document.side_effect = [
            SimpleNamespace(document_type=name, confidence=0.9, extracted_fields={}, warnings=[])
            for name in ("passport_main", "passport_registration", "tin", "snils")
        ]
        for index, content in enumerate((b"pm", b"pr", b"tin", b"snils")):
            await self.service.add_document(
                session_id,
                _upload(content, provider_file_id=f"file-{index}"),
            )

        summary = await self.service.finish(session_id)

        self.assertTrue(summary["ready_for_confirmation"])
        self.assertEqual(summary["reasons"], [])
        self.assertEqual(summary["status"], INTAKE_STATUS_REVIEW)

    async def test_storage_failure_blocks_confirmation_until_duplicate_retry_succeeds(self) -> None:
        session_id = await self._session_id()
        self.disk.upload_bytes.side_effect = RuntimeError("disk unavailable")
        self.recognition.recognize_document.side_effect = [
            SimpleNamespace(document_type=name, confidence=0.9, extracted_fields={}, warnings=[])
            for name in ("passport_main", "passport_registration", "tin", "snils")
        ]

        for index, content in enumerate((b"pm", b"pr", b"tin", b"snils")):
            await self.service.add_document(
                session_id,
                _upload(content, provider_file_id=f"file-{index}"),
            )

        summary = await self.service.finish(session_id)
        self.assertFalse(summary["ready_for_confirmation"])
        self.assertIn("storage_not_ready", summary["reasons"])
        self.assertEqual(summary["storage_not_ready_count"], 4)
        self.assertTrue(
            all(
                document["storage_status"] == STORAGE_STATUS_FAILED
                for document in summary["documents"]
            )
        )

        self.repository.sessions[session_id]["status"] = "collecting"
        self.disk.upload_bytes.side_effect = lambda path, content: path
        for index, content in enumerate((b"pm", b"pr", b"tin", b"snils")):
            response = await self.service.add_document(
                session_id,
                _upload(content, provider_file_id=f"file-{index}"),
            )
            self.assertTrue(response["duplicate"])

        summary = await self.service.finish(session_id)
        self.assertTrue(summary["ready_for_confirmation"])
        self.assertEqual(summary["storage_not_ready_count"], 0)
        self.assertTrue(
            all(
                document["storage_status"] == STORAGE_STATUS_STORED
                for document in summary["documents"]
            )
        )
        self.assertEqual(self.recognition.recognize_document.await_count, 4)

    async def test_rejects_upload_to_review_cancelled_or_expired_session(self) -> None:
        session_id = await self._session_id()
        await self.service.finish(session_id)
        self.repository.sessions[session_id]["status"] = INTAKE_STATUS_REVIEW
        with self.assertRaises(IntakeConflictError):
            await self.service.add_document(session_id, _upload(b"after-review"))

        cancelled = await self._session_id()
        await self.service.cancel(cancelled)
        with self.assertRaises(IntakeConflictError):
            await self.service.add_document(cancelled, _upload(b"after-cancel"))

        expired = await self._session_id()
        self.repository.sessions[expired]["expires_at"] = datetime.now(
            timezone.utc
        ) - timedelta(minutes=1)
        with self.assertRaises(IntakeConflictError):
            await self.service.add_document(expired, _upload(b"after-expire"))
        self.assertEqual(self.repository.sessions[expired]["status"], INTAKE_STATUS_EXPIRED)

    def test_intake_tables_do_not_use_telegram_specific_fields(self) -> None:
        ddl = "\n".join(
            [
                database.CREATE_DOCUMENT_INTAKE_SESSIONS_TABLE_SQL,
                database.CREATE_DOCUMENT_INTAKE_DOCUMENTS_TABLE_SQL,
            ]
        )
        self.assertNotIn("telegram_", ddl)
        self.assertIn("provider_file_id", ddl)
        self.assertIn("external_user_id", ddl)
        self.assertIn("conversation_id", ddl)


if __name__ == "__main__":
    unittest.main()
