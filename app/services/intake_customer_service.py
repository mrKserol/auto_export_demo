from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from uuid import UUID

from app.services.document_intake_service import DocumentIntakeError, DocumentIntakeService


def normalize_passport(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[\s-]+", "", str(value)).upper()
    return normalized or None


@dataclass(frozen=True)
class IntakeCustomerResult:
    status: str
    session_id: str
    customer: dict | None = None
    message: str | None = None
    actions: tuple[dict[str, Any], ...] = ()


class IntakeCustomerService:
    """Platform-neutral orchestration for the intake -> customer transition."""

    def __init__(self, *, database, repository, intake_service: DocumentIntakeService,
                 customer_folder_service=None, yandex_disk_client=None) -> None:
        self.database = database
        self.repository = repository
        self.intake_service = intake_service
        self.customer_folder_service = customer_folder_service
        self.yandex_disk_client = yandex_disk_client

    async def add_client(self, *, session_id: UUID, channel: str,
                         external_user_id: str, conversation_id: str) -> IntakeCustomerResult:
        await self.intake_service.validate_session_owner(
            session_id, channel=channel, external_user_id=external_user_id,
            conversation_id=conversation_id,
        )
        session = await self.repository.get_session(session_id)
        if session and session.get("customer_id"):
            customer = await self.database.get_customer_by_id(session["customer_id"])
            if session.get("status") == "completed":
                return IntakeCustomerResult("completed", str(session_id), customer=customer)
            summary = await self.intake_service.get_review_summary(session_id)
            return await self._persist(
                session_id, external_user_id,
                dict(summary.get("effective_values") or {}),
                normalize_passport((summary.get("effective_values") or {}).get("passport")) or "",
                int(session["customer_id"]),
            )

        summary = await self.intake_service.get_review_summary(session_id)
        values = dict(summary.get("effective_values") or {})
        passport = normalize_passport(values.get("passport"))
        if not passport:
            return IntakeCustomerResult(
                "missing_passport", str(session_id),
                message="Заполните номер паспорта в ручной коррекции",
            )

        existing = await self._find_customer(passport)
        if existing:
            return IntakeCustomerResult(
                "duplicate", str(session_id), customer=existing,
                message="Клиент с таким паспортом уже существует. Обновить его данными из текущего черновика?",
                actions=(
                    {"action": "replace_client", "label": "Заменить"},
                    {"action": "cancel", "label": "Отмена"},
                ),
            )
        return await self._persist(session_id, external_user_id, values, passport, None)

    async def replace_client(self, *, session_id: UUID, customer_id: int,
                             channel: str, external_user_id: str,
                             conversation_id: str) -> IntakeCustomerResult:
        await self.intake_service.validate_session_owner(
            session_id, channel=channel, external_user_id=external_user_id,
            conversation_id=conversation_id,
        )
        session = await self.repository.get_session(session_id)
        if session and session.get("status") == "completed":
            return IntakeCustomerResult("completed", str(session_id),
                                        customer=await self.database.get_customer_by_id(session["customer_id"]))
        summary = await self.intake_service.get_review_summary(session_id)
        values = dict(summary.get("effective_values") or {})
        passport = normalize_passport(values.get("passport"))
        if not passport:
            return IntakeCustomerResult("missing_passport", str(session_id),
                                        message="Заполните номер паспорта в ручной коррекции")
        return await self._persist(session_id, external_user_id, values, passport, customer_id)

    async def _find_customer(self, passport: str) -> dict | None:
        finder = getattr(self.database, "find_customer_by_normalized_passport", None)
        if finder:
            return await finder(passport)
        for candidate in (passport, f"{passport[:4]} {passport[4:]}" if len(passport) > 4 else passport):
            found = await self.database.find_customer_by_passport(candidate)
            if found:
                return found
        return None

    async def _persist(self, session_id: UUID, external_user_id: str, values: dict,
                       passport: str, existing_id: int | None) -> IntakeCustomerResult:
        data = {
            "passport": values.get("passport") or passport,
            "first_name": values.get("first_name"), "last_name": values.get("patronymic"),
            "surname": values.get("surname"), "first_name_translit": values.get("first_name_translit"),
            "last_name_translit": values.get("last_name_translit"), "surname_translit": values.get("surname_translit"),
            "tin": values.get("tin"), "phone": values.get("phone"), "email": values.get("email"),
            "by_whom_issued": values.get("by_whom_issued"), "date_issue": values.get("date_issue"),
            "registration_address": values.get("registration_address"), "department_code": values.get("department_code"),
        }
        if existing_id is None:
            locked_creator = getattr(self.database, "create_customer_with_passport_lock", None)
            if locked_creator is not None:
                customer, already_exists = await locked_creator(passport, data)
                if already_exists:
                    return IntakeCustomerResult(
                        "duplicate", str(session_id), customer=customer,
                        message="Клиент с таким паспортом уже существует. Обновить его данными из текущего черновика?",
                        actions=({"action": "replace_client", "label": "Заменить"},
                                 {"action": "cancel", "label": "Отмена"}),
                    )
            else:
                customer = await self.database.create_customer(data)
            event = "customer_created_from_intake"
        else:
            customer = await self.database.update_customer_fields(existing_id, data)
            event = "customer_replaced_from_intake"
        customer_id = int(customer["id"])
        try:
            # Persist the chosen final folder before moving files so a retry
            # after a partial failure resumes in the same folder.
            if not customer.get("customer_path") and self.customer_folder_service is not None:
                folder_path = await self.customer_folder_service.create_unique_customer_folder(
                    last_name=customer.get("surname") or "customer", passport=passport,
                )
                customer = await self.database.update_customer(customer_id, "customer_path", folder_path)
            customer_path = await self._finalize_storage(session_id, customer, passport)
        except Exception:
            await self.repository.set_customer(session_id, customer_id, "finalizing_storage")
            await self.repository.link_documents_to_customer(session_id, customer_id)
            raise
        if customer_path and not customer.get("customer_path"):
            customer = await self.database.update_customer(customer_id, "customer_path", customer_path)
        await self.repository.set_customer(session_id, customer_id, "completed")
        await self.repository.link_documents_to_customer(session_id, customer_id)
        await self.repository.add_audit_event(session_id=session_id, event_type=event,
                                              external_user_id=external_user_id)
        return IntakeCustomerResult("replaced" if existing_id is not None else "completed", str(session_id), customer=customer)

    async def _finalize_storage(self, session_id: UUID, customer: dict, passport: str) -> str | None:
        docs = await self.repository.list_documents(session_id)
        if not docs:
            return None
        if any(doc.get("storage_status") != "stored" or not doc.get("storage_path") for doc in docs):
            raise RuntimeError("intake storage is not ready")
        if self.customer_folder_service is None or self.yandex_disk_client is None:
            raise RuntimeError("intake storage service is unavailable")
        path = customer.get("customer_path") or await self.customer_folder_service.create_unique_customer_folder(
            last_name=customer.get("surname") or "customer", passport=passport,
        )
        for doc in docs:
            source = doc.get("storage_path")
            if source and source != path:
                name = source.rstrip("/").rsplit("/", 1)[-1]
                await self.yandex_disk_client.move_resource(f"{source}", f"{path}/{name}", overwrite=False)
                await self.repository.update_document_storage(document_id=doc["id"],
                                                               storage_path=f"{path}/{name}",
                                                               storage_status="stored")
        return path
