from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.repositories.document_intake_repository import DocumentIntakeRepository
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionService,
)
from app.yadisk_client import YandexDiskClient

logger = logging.getLogger(__name__)

CHANNEL_TELEGRAM = "telegram"
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_WECHAT = "wechat"
CHANNEL_MAX = "max"
SUPPORTED_INTAKE_CHANNELS = frozenset(
    {CHANNEL_TELEGRAM, CHANNEL_WHATSAPP, CHANNEL_WECHAT, CHANNEL_MAX}
)

INTAKE_STATUS_COLLECTING = "collecting"
INTAKE_STATUS_REVIEW = "review"
INTAKE_STATUS_CANCELLED = "cancelled"
INTAKE_STATUS_EXPIRED = "expired"
ACTIVE_INTAKE_STATUSES = frozenset({INTAKE_STATUS_COLLECTING})

DOCUMENT_STATUS_RECEIVED = "received"
DOCUMENT_STATUS_RECOGNIZED = "recognized"
DOCUMENT_STATUS_FAILED = "failed"

STORAGE_STATUS_PENDING = "pending"
STORAGE_STATUS_STORED = "stored"
STORAGE_STATUS_FAILED = "failed"

DEFAULT_REQUIRED_DOCUMENT_TYPES = (
    "passport_main",
    "passport_registration",
    "tin",
    "snils",
)
DEFAULT_SESSION_TTL_MINUTES = 60


class DocumentIntakeError(Exception):
    status_code = 500
    code = "DOCUMENT_INTAKE_ERROR"


class IntakeNotFoundError(DocumentIntakeError):
    status_code = 404
    code = "INTAKE_SESSION_NOT_FOUND"


class IntakeConflictError(DocumentIntakeError):
    status_code = 409
    code = "INTAKE_SESSION_NOT_COLLECTING"


class IntakeValidationError(DocumentIntakeError):
    status_code = 422
    code = "INTAKE_VALIDATION_ERROR"


@dataclass(frozen=True)
class IntakeUpload:
    content: bytes
    channel: str
    external_user_id: str
    conversation_id: str
    provider_message_id: str | None
    provider_file_id: str | None
    media_group_id: str | None
    original_name: str | None
    mime_type: str | None
    file_size: int


@dataclass(frozen=True)
class DocumentIntakeService:
    repository: DocumentIntakeRepository
    recognition_service: CustomerDocumentRecognitionService
    yandex_disk_client: YandexDiskClient | None = None
    max_file_bytes: int = 20 * 1024 * 1024
    ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES
    required_document_types: tuple[str, ...] = DEFAULT_REQUIRED_DOCUMENT_TYPES

    async def create_or_get_session(
        self,
        *,
        channel: str,
        external_user_id: str,
        conversation_id: str,
        metadata: dict | None = None,
    ) -> dict:
        self._validate_channel_context(channel, external_user_id, conversation_id)
        active = await self.repository.get_active_session(
            channel=channel,
            external_user_id=external_user_id,
            conversation_id=conversation_id,
        )
        if active is not None:
            expired = await self._expire_if_needed(active)
            if expired is None:
                return self._session_payload(active)

        session = await self.repository.create_session(
            session_id=uuid4(),
            channel=channel,
            external_user_id=external_user_id,
            conversation_id=conversation_id,
            expires_at=self._now() + timedelta(minutes=self.ttl_minutes),
            metadata={} if metadata is None else dict(metadata),
        )
        return self._session_payload(session)

    async def add_document(self, session_id: UUID, upload: IntakeUpload) -> dict:
        session = await self._get_collecting_session(session_id)
        self._validate_channel_context(
            upload.channel,
            upload.external_user_id,
            upload.conversation_id,
        )
        if upload.file_size > self.max_file_bytes:
            raise IntakeValidationError("File is too large")

        content_sha256 = hashlib.sha256(upload.content).hexdigest()
        duplicate = await self.repository.find_duplicate_document(
            session_id=session_id,
            provider_file_id=upload.provider_file_id,
            content_sha256=content_sha256,
        )
        if duplicate is not None:
            if duplicate.get("storage_status") != STORAGE_STATUS_STORED:
                storage_path, storage_status = await self._store_draft_file(
                    session_id,
                    upload,
                )
                if storage_status == STORAGE_STATUS_STORED:
                    updated = await self.repository.update_document_storage(
                        document_id=duplicate["id"],
                        storage_path=storage_path,
                        storage_status=storage_status,
                    )
                    if updated is not None:
                        duplicate = updated
            return self._document_response(
                session,
                duplicate,
                duplicate=True,
            )

        storage_path, storage_status = await self._store_draft_file(session_id, upload)
        status = DOCUMENT_STATUS_RECOGNIZED
        recognition_payload: dict | None = None
        document_type = None
        confidence = None
        warnings: list[str] = []
        try:
            result = await self.recognition_service.recognize_document(
                content=upload.content,
                mime_type=upload.mime_type,
                filename=upload.original_name,
            )
            recognition_payload = _normalize_customer_document_result(result)
            document_type = recognition_payload.get("document_type")
            confidence = recognition_payload.get("confidence")
            warnings = list(recognition_payload.get("warnings") or [])
        except Exception:
            logger.exception("Document intake recognition failed")
            status = DOCUMENT_STATUS_FAILED
            recognition_payload = {
                "error": "Document recognition failed",
            }
            warnings = ["recognition_failed"]

        document = await self.repository.create_document(
            id=uuid4(),
            session_id=session_id,
            status=status,
            channel=upload.channel,
            external_user_id=upload.external_user_id,
            conversation_id=upload.conversation_id,
            provider_message_id=upload.provider_message_id,
            provider_file_id=upload.provider_file_id,
            media_group_id=upload.media_group_id,
            original_name=upload.original_name,
            mime_type=upload.mime_type,
            file_size=upload.file_size,
            content_sha256=content_sha256,
            storage_path=storage_path,
            storage_status=storage_status,
            document_type=document_type,
            recognition_result=recognition_payload,
            confidence=confidence,
            warnings=warnings,
        )
        return self._document_response(session, document, duplicate=False)

    async def get_summary(self, session_id: UUID) -> dict:
        session = await self._get_session_or_404(session_id)
        session = await self._expire_if_needed(session) or session
        return await self._summary(session)

    async def finish(self, session_id: UUID) -> dict:
        session = await self._get_session_or_404(session_id)
        session = await self._expire_if_needed(session) or session
        summary = await self._summary(session)
        reasons = self._finish_reasons(summary)
        summary["ready_for_confirmation"] = not reasons
        summary["reasons"] = reasons

        if session["status"] == INTAKE_STATUS_REVIEW:
            return summary
        if session["status"] != INTAKE_STATUS_COLLECTING:
            raise IntakeConflictError("Intake session cannot be finished")
        if reasons:
            return summary

        session = await self.repository.update_session_status(
            session_id,
            INTAKE_STATUS_REVIEW,
        )
        assert session is not None
        summary = await self._summary(session)
        summary["ready_for_confirmation"] = True
        summary["reasons"] = []
        return summary

    @staticmethod
    def _finish_reasons(summary: dict) -> list[str]:
        reasons = []
        if summary["missing_document_types"]:
            reasons.append("missing_document_types")
        if summary["duplicate_document_types"]:
            reasons.append("duplicate_document_types")
        if summary["failed_document_count"]:
            reasons.append("failed_documents")
        if summary["storage_not_ready_count"]:
            reasons.append("storage_not_ready")
        return reasons

    async def cancel(self, session_id: UUID) -> dict:
        session = await self._get_session_or_404(session_id)
        if session["status"] in {INTAKE_STATUS_CANCELLED, INTAKE_STATUS_EXPIRED}:
            return self._session_payload(session)
        session = await self.repository.update_session_status(
            session_id,
            INTAKE_STATUS_CANCELLED,
        )
        assert session is not None
        return self._session_payload(session)

    async def _get_collecting_session(self, session_id: UUID) -> dict:
        session = await self._get_session_or_404(session_id)
        expired = await self._expire_if_needed(session)
        if expired is not None:
            raise IntakeConflictError("Intake session is expired")
        if session["status"] != INTAKE_STATUS_COLLECTING:
            raise IntakeConflictError("Intake session does not accept documents")
        return session

    async def _get_session_or_404(self, session_id: UUID) -> dict:
        session = await self.repository.get_session(session_id)
        if session is None:
            raise IntakeNotFoundError("Intake session not found")
        return session

    async def _expire_if_needed(self, session: dict) -> dict | None:
        if session["status"] != INTAKE_STATUS_COLLECTING:
            return None
        expires_at = session["expires_at"]
        if expires_at <= self._now():
            return await self.repository.update_session_status(
                session["id"],
                INTAKE_STATUS_EXPIRED,
            )
        return None

    async def _summary(self, session: dict) -> dict:
        documents = await self.repository.list_documents(session["id"])
        recognized_types = [
            document.get("document_type")
            for document in documents
            if document.get("status") == DOCUMENT_STATUS_RECOGNIZED
            and document.get("document_type")
        ]
        duplicates = sorted(
            {
                document_type
                for document_type in recognized_types
                if recognized_types.count(document_type) > 1
            }
        )
        missing = [
            document_type
            for document_type in self.required_document_types
            if document_type not in recognized_types
        ]
        warnings: list[str] = []
        for document in documents:
            for warning in document.get("warnings") or []:
                warnings.append(str(warning))
            if document.get("storage_status") != STORAGE_STATUS_STORED:
                warnings.append("storage_not_ready")

        return {
            **self._session_payload(session),
            "recognized_document_types": recognized_types,
            "missing_document_types": missing,
            "duplicate_document_types": duplicates,
            "failed_document_count": sum(
                1 for document in documents if document.get("status") == DOCUMENT_STATUS_FAILED
            ),
            "storage_not_ready_count": sum(
                1
                for document in documents
                if document.get("storage_status") != STORAGE_STATUS_STORED
            ),
            "warnings": warnings,
            "documents": [self._document_summary(document) for document in documents],
        }

    def _document_response(self, session: dict, document: dict, *, duplicate: bool) -> dict:
        return {
            "ok": True,
            "session_id": str(session["id"]),
            "document_id": str(document["id"]),
            "duplicate": duplicate,
            "status": document["status"],
            "storage_status": document.get("storage_status"),
            "storage_path": document.get("storage_path"),
            "document": document.get("recognition_result") or {},
        }

    def _document_summary(self, document: dict) -> dict:
        return {
            "document_id": str(document["id"]),
            "status": document["status"],
            "channel": document["channel"],
            "external_user_id": document["external_user_id"],
            "conversation_id": document["conversation_id"],
            "provider_message_id": document.get("provider_message_id"),
            "provider_file_id": document.get("provider_file_id"),
            "media_group_id": document.get("media_group_id"),
            "original_name": document.get("original_name"),
            "mime_type": document.get("mime_type"),
            "file_size": document.get("file_size"),
            "content_sha256": document.get("content_sha256"),
            "storage_path": document.get("storage_path"),
            "storage_status": document.get("storage_status"),
            "document_type": document.get("document_type"),
            "confidence": document.get("confidence"),
            "warnings": document.get("warnings") or [],
            "document": document.get("recognition_result") or {},
        }

    def _session_payload(self, session: dict) -> dict:
        return {
            "session_id": str(session["id"]),
            "status": session["status"],
            "expires_at": session["expires_at"].isoformat(),
        }

    async def _store_draft_file(
        self,
        session_id: UUID,
        upload: IntakeUpload,
    ) -> tuple[str | None, str]:
        if self.yandex_disk_client is None:
            return None, STORAGE_STATUS_PENDING
        name = _safe_filename(upload.original_name or "document.bin")
        path = (
            f"{self.yandex_disk_client.base_path}/02_Клиенты/"
            f"00_Черновики/{session_id}/{name}"
        )
        try:
            return (
                await self.yandex_disk_client.upload_bytes(path, upload.content),
                STORAGE_STATUS_STORED,
            )
        except Exception:
            logger.exception("Document intake draft upload failed")
            return None, STORAGE_STATUS_FAILED

    @staticmethod
    def _validate_channel_context(
        channel: str,
        external_user_id: str,
        conversation_id: str,
    ) -> None:
        if channel not in SUPPORTED_INTAKE_CHANNELS:
            raise IntakeValidationError("Unsupported channel")
        if not external_user_id or not conversation_id:
            raise IntakeValidationError("Channel context is incomplete")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


def _normalize_customer_document_result(result) -> dict:
    fields = dict(getattr(result, "extracted_fields", {}) or {})
    warnings: list[str] = []
    for warning in getattr(result, "warnings", []) or []:
        warnings.append(str(warning))
    for warning in fields.get("warnings") or []:
        warnings.append(str(warning))

    return {
        "surname": fields.get("last_name") or fields.get("surname"),
        "first_name": fields.get("first_name"),
        "patronymic": fields.get("surname") or fields.get("patronymic"),
        "passport": fields.get("passport"),
        "date_issue": fields.get("date_issue"),
        "department_code": fields.get("department_code"),
        "birth_date": fields.get("birth_date"),
        "birth_place": fields.get("birth_place"),
        "registration_address": fields.get("registration_address"),
        "snils": fields.get("snils") or fields.get("ipain"),
        "tin": fields.get("tin"),
        "document_type": getattr(result, "document_type", None)
        or fields.get("document_type"),
        "confidence": getattr(result, "confidence", None),
        "warnings": warnings,
    }


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", name).strip("._")
    return cleaned or "document.bin"
