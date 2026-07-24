from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import Settings
from app.database import Database
from app.repositories.customer_upload_batch_repository import (
    CustomerUploadBatchRepository,
)
from app.repositories.customer_upload_batch_statuses import CustomerUploadBatchStatus
from app.services.miniapp_batch_api_service import (
    BATCH_ORIGIN_MINIAPP,
    MiniAppBatchError,
    assert_batch_owner,
    create_miniapp_batch,
    delete_batch_slot_file,
    run_batch_processing,
    serialize_batch_status,
    upload_batch_file,
)
from app.services.miniapp_link_service import (
    build_customer_edit_miniapp_url,
    build_specification_miniapp_url,
    create_customer_batch_token,
    create_customer_edit_token,
    create_customer_specification_token,
)
from app.services.miniapp_rate_limit import miniapp_rate_limiter
from app.services.validation_service import normalize_passport
from app.web.auth import InitDataError, TelegramUser, validate_telegram_init_data
from app.web.routes import (
    STATIC_DIR,
    error_response,
    get_database,
    get_settings,
)

logger = logging.getLogger(__name__)

router = APIRouter()

CUSTOMER_CARD_FIELDS = (
    ("last_name", "Фамилия"),
    ("first_name", "Имя"),
    ("surname", "Отчество"),
    ("passport", "Паспорт"),
    ("birth_date", "Дата рождения"),
    ("birth_place", "Место рождения"),
    ("registration_address", "Адрес регистрации"),
    ("ipain", "СНИЛС"),
    ("tin", "ИНН"),
    ("phone", "Телефон"),
    ("email", "Email"),
    ("customer_path", "Путь к папке документов"),
)


class InitDataPayload(BaseModel):
    telegram_init_data: str = Field(min_length=1)
    origin_chat_id: int | None = None

    @field_validator("telegram_init_data", mode="before")
    @classmethod
    def strip_init(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class CreateBatchPayload(InitDataPayload):
    force_new: bool = False


class SearchCustomerPayload(InitDataPayload):
    passport: str = Field(min_length=1)

    @field_validator("passport", mode="before")
    @classmethod
    def strip_passport(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class CustomerLaunchPayload(InitDataPayload):
    customer_id: int


class BatchLaunchPayload(InitDataPayload):
    batch_id: int


def require_miniapp_employee(
    settings: Settings,
    user: TelegramUser,
) -> TelegramUser | JSONResponse:
    """Any allowlisted employee may work with any Arthur AutoExport customer.

    Empty allowlist always denies access. MINIAPP_TEST_MODE does not bypass
    the allowlist (tests must configure explicit user IDs).
    """
    if user.id not in settings.miniapp_allowed_telegram_user_ids:
        return error_response(
            403,
            "MINIAPP_FORBIDDEN",
            "Нет доступа к Mini App. Обратитесь к администратору.",
        )
    return user


def _authenticate(
    settings: Settings,
    init_data: str,
) -> TelegramUser | JSONResponse:
    """Validate Telegram initData, then enforce employee allowlist."""
    try:
        user = validate_telegram_init_data(
            init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        return error_response(401, error.code, error.message)
    return require_miniapp_employee(settings, user)


def _rate_limit(action: str, user_id: int, *, limit: int, window: float) -> JSONResponse | None:
    key = f"{action}:{user_id}"
    if not miniapp_rate_limiter.allow(key, limit=limit, window_seconds=window):
        return error_response(
            429,
            "RATE_LIMITED",
            "Слишком много запросов. Подождите немного и попробуйте снова.",
        )
    return None


def _get_repository(database: Database) -> CustomerUploadBatchRepository:
    return CustomerUploadBatchRepository(database.pool)


def _serialize_customer_card(customer: dict) -> dict:
    fields = []
    for key, label in CUSTOMER_CARD_FIELDS:
        raw = customer.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            value = "не заполнено"
        else:
            value = str(raw)
        fields.append({"key": key, "label": label, "value": value})
    fio_parts = [
        str(customer.get("last_name") or "").strip(),
        str(customer.get("first_name") or "").strip(),
        str(customer.get("surname") or "").strip(),
    ]
    fio = " ".join(part for part in fio_parts if part) or "не заполнено"
    return {
        "customer_id": int(customer["id"]),
        "fio": fio,
        "fields": fields,
        "has_specification": bool(customer.get("specification_id")),
    }


@router.get("/miniapp")
@router.get("/miniapp/")
@router.get("/miniapp/customer/create")
@router.get("/miniapp/customer/search")
async def miniapp_shell() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "miniapp.html",
        media_type="text/html; charset=utf-8",
    )


@router.post("/api/miniapp/bootstrap")
async def miniapp_bootstrap(
    payload: InitDataPayload,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("bootstrap", auth.id, limit=60, window=60)
    if limited:
        return limited
    return JSONResponse(
        {
            "ok": True,
            "max_file_bytes": settings.customer_upload_max_file_bytes,
            "bot_username": settings.telegram_bot_username,
            "telegram_user_id": auth.id,
        }
    )


@router.post("/api/customers/search")
async def search_customers_api(
    payload: SearchCustomerPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("search", auth.id, limit=20, window=60)
    if limited:
        return limited

    digits = "".join(ch for ch in payload.passport if ch.isdigit())
    if len(digits) < 10:
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Введите полный номер паспорта (10 цифр).",
        )
    normalized = normalize_passport(payload.passport)
    if not normalized:
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Некорректный номер паспорта.",
        )

    customer = await database.search_customer_by_passport(normalized)
    if customer is None:
        return JSONResponse(
            {
                "ok": True,
                "status": "not_found",
                "message": "Клиент не найден",
            }
        )
    return JSONResponse(
        {
            "ok": True,
            "status": "found",
            "customer": _serialize_customer_card(customer),
        }
    )


@router.post("/api/miniapp/customer-card")
async def get_customer_card_api(
    payload: CustomerLaunchPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("customer_card", auth.id, limit=40, window=60)
    if limited:
        return limited
    customer = await database.get_customer_by_id(payload.customer_id)
    if customer is None:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")
    return JSONResponse(
        {
            "ok": True,
            "customer": _serialize_customer_card(customer),
        }
    )


@router.post("/api/miniapp/customer-edit-token")
async def create_customer_edit_launch(
    payload: CustomerLaunchPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("edit_token", auth.id, limit=30, window=60)
    if limited:
        return limited

    customer = await database.get_customer_by_id(payload.customer_id)
    if customer is None:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")

    origin_chat_id = payload.origin_chat_id or auth.id
    token = create_customer_edit_token(
        settings,
        customer_id=int(customer["id"]),
        telegram_user_id=auth.id,
        origin_chat_id=origin_chat_id,
    )
    url = f"{build_customer_edit_miniapp_url(settings, token)}&from=miniapp"
    return JSONResponse({"ok": True, "url": url})


@router.post("/api/miniapp/specification-token")
async def create_specification_launch(
    payload: CustomerLaunchPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("spec_token", auth.id, limit=30, window=60)
    if limited:
        return limited

    customer = await database.get_customer_by_id(payload.customer_id)
    if customer is None:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")
    if customer.get("specification_id"):
        return error_response(
            409,
            "SPECIFICATION_EXISTS",
            "У клиента уже есть спецификация.",
        )

    origin_chat_id = payload.origin_chat_id or auth.id
    token = create_customer_specification_token(
        settings,
        customer_id=int(customer["id"]),
        telegram_user_id=auth.id,
        origin_chat_id=origin_chat_id,
    )
    url = f"{build_specification_miniapp_url(settings, token)}&from=miniapp"
    return JSONResponse({"ok": True, "url": url})


@router.post("/api/miniapp/batch-form-token")
async def create_batch_form_launch(
    payload: BatchLaunchPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    repository = _get_repository(database)
    try:
        batch = await assert_batch_owner(
            repository,
            batch_id=payload.batch_id,
            telegram_user_id=auth.id,
        )
    except MiniAppBatchError as error:
        return error_response(error.status_code, error.code, error.message)

    if batch.get("status") not in {
        CustomerUploadBatchStatus.FILES_SAVED,
        CustomerUploadBatchStatus.AWAITING_CONFIRMATION,
    }:
        return error_response(
            409,
            "BATCH_NOT_READY",
            "Форма доступна после сохранения документов.",
        )

    if batch.get("status") != CustomerUploadBatchStatus.AWAITING_CONFIRMATION:
        await repository.mark_batch_awaiting_confirmation(int(batch["id"]))

    origin_chat_id = payload.origin_chat_id or int(batch["telegram_chat_id"])
    token = create_customer_batch_token(
        settings,
        batch_id=int(batch["id"]),
        telegram_user_id=auth.id,
        origin_chat_id=origin_chat_id,
    )
    url = f"{build_customer_edit_miniapp_url(settings, token)}&from=miniapp"
    return JSONResponse({"ok": True, "url": url})


@router.get("/api/customer-batches/active")
async def get_active_miniapp_batch(
    request: Request,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    init_data = (request.headers.get("X-Telegram-Init-Data") or "").strip()
    if not init_data:
        return error_response(
            401,
            "INVALID_TELEGRAM_INIT_DATA",
            "Telegram initData отсутствует",
        )
    auth = _authenticate(settings, init_data)
    if isinstance(auth, JSONResponse):
        return auth
    repository = _get_repository(database)
    batch = await repository.get_active_batch_for_user(
        auth.id,
        origin=BATCH_ORIGIN_MINIAPP,
    )
    if batch is None:
        return JSONResponse({"ok": True, "batch": None})
    files = await repository.get_batch_files(int(batch["id"]))
    return JSONResponse(
        {
            "ok": True,
            "batch": serialize_batch_status(batch, files),
        }
    )


@router.post("/api/customer-batches")
async def create_customer_batch_api(
    payload: CreateBatchPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("create_batch", auth.id, limit=10, window=60)
    if limited:
        return limited
    repository = _get_repository(database)
    result = await create_miniapp_batch(
        repository,
        telegram_user_id=auth.id,
        origin_chat_id=payload.origin_chat_id,
        force_new=payload.force_new,
    )
    return JSONResponse({"ok": True, **result})


@router.post("/api/customer-batches/{batch_id}/files")
async def upload_customer_batch_file_api(
    batch_id: int,
    background_tasks: BackgroundTasks,
    declared_document_type: str = Form(...),
    telegram_init_data: str = Form(...),
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    _ = background_tasks
    auth = _authenticate(settings, telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("upload_file", auth.id, limit=40, window=60)
    if limited:
        return limited

    content = await file.read(settings.customer_upload_max_file_bytes + 1)
    repository = _get_repository(database)
    try:
        result = await upload_batch_file(
            repository,
            batch_id=batch_id,
            telegram_user_id=auth.id,
            declared_document_type=declared_document_type.strip(),
            filename=file.filename,
            content_type=file.content_type,
            content=content,
            max_file_bytes=settings.customer_upload_max_file_bytes,
        )
    except MiniAppBatchError as error:
        return error_response(error.status_code, error.code, error.message)
    return JSONResponse({"ok": True, **result})


@router.delete("/api/customer-batches/{batch_id}/files/{file_id}")
async def delete_customer_batch_file_api(
    batch_id: int,
    file_id: int,
    payload: InitDataPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    repository = _get_repository(database)
    try:
        batch = await delete_batch_slot_file(
            repository,
            batch_id=batch_id,
            file_id=file_id,
            telegram_user_id=auth.id,
        )
    except MiniAppBatchError as error:
        return error_response(error.status_code, error.code, error.message)
    return JSONResponse({"ok": True, "batch": batch})


@router.get("/api/customer-batches/{batch_id}/status")
async def get_customer_batch_status_api(
    batch_id: int,
    request: Request,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    init_data = (request.headers.get("X-Telegram-Init-Data") or "").strip()
    if not init_data:
        return error_response(
            401,
            "INVALID_TELEGRAM_INIT_DATA",
            "Telegram initData отсутствует",
        )
    auth = _authenticate(settings, init_data)
    if isinstance(auth, JSONResponse):
        return auth
    repository = _get_repository(database)
    try:
        batch = await assert_batch_owner(
            repository,
            batch_id=batch_id,
            telegram_user_id=auth.id,
        )
    except MiniAppBatchError as error:
        return error_response(error.status_code, error.code, error.message)
    files = await repository.get_batch_files(batch_id)
    return JSONResponse({"ok": True, "batch": serialize_batch_status(batch, files)})


@router.post("/api/customer-batches/{batch_id}/recognize")
async def recognize_customer_batch_api(
    batch_id: int,
    payload: InitDataPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _rate_limit("recognize", auth.id, limit=5, window=60)
    if limited:
        return limited

    recognition_service = getattr(request.app.state, "customer_batch_recognition_service", None)
    folder_service = getattr(request.app.state, "customer_folder_service", None)
    if recognition_service is None or folder_service is None:
        return error_response(
            503,
            "SERVICE_UNAVAILABLE",
            "Распознавание временно недоступно",
        )

    repository = _get_repository(database)
    try:
        batch = await assert_batch_owner(
            repository,
            batch_id=batch_id,
            telegram_user_id=auth.id,
        )
    except MiniAppBatchError as error:
        return error_response(error.status_code, error.code, error.message)

    if batch.get("status") in {
        CustomerUploadBatchStatus.RECOGNIZING,
        CustomerUploadBatchStatus.CREATING_FOLDER,
        CustomerUploadBatchStatus.UPLOADING,
    }:
        files = await repository.get_batch_files(batch_id)
        return JSONResponse(
            {
                "ok": True,
                "started": False,
                "message": "Обработка уже выполняется",
                "batch": serialize_batch_status(batch, files),
            }
        )

    if batch.get("status") not in {
        CustomerUploadBatchStatus.COLLECTING,
        CustomerUploadBatchStatus.FAILED,
    }:
        return error_response(
            409,
            "BATCH_ALREADY_PROCESSED",
            "Распознавание для этого пакета уже запускалось.",
        )

    files = await repository.get_batch_files(batch_id)
    if len(files) < 4:
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Загрузите все четыре документа перед распознаванием.",
        )

    if batch.get("status") == CustomerUploadBatchStatus.FAILED:
        await repository.update_batch_status(
            batch_id,
            CustomerUploadBatchStatus.COLLECTING,
            error_message=None,
        )

    claimed = await repository.claim_batch_for_processing(batch_id)
    if claimed is None:
        refreshed = await repository.get_batch_by_id(batch_id)
        files = await repository.get_batch_files(batch_id)
        return JSONResponse(
            {
                "ok": True,
                "started": False,
                "message": "Обработка уже выполняется",
                "batch": serialize_batch_status(refreshed or batch, files),
            }
        )

    asyncio.create_task(
        run_batch_processing(
            batch_id=batch_id,
            recognition_service=recognition_service,
            folder_service=folder_service,
        )
    )
    files = await repository.get_batch_files(batch_id)
    refreshed = await repository.get_batch_by_id(batch_id)
    return JSONResponse(
        {
            "ok": True,
            "started": True,
            "batch": serialize_batch_status(refreshed or claimed, files),
        }
    )


@router.post("/api/customer-batches/{batch_id}/cancel")
async def cancel_customer_batch_api(
    batch_id: int,
    payload: InitDataPayload,
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    auth = _authenticate(settings, payload.telegram_init_data)
    if isinstance(auth, JSONResponse):
        return auth
    repository = _get_repository(database)
    try:
        batch = await assert_batch_owner(
            repository,
            batch_id=batch_id,
            telegram_user_id=auth.id,
        )
    except MiniAppBatchError as error:
        return error_response(error.status_code, error.code, error.message)

    if batch.get("status") != CustomerUploadBatchStatus.COLLECTING:
        return error_response(
            409,
            "BATCH_NOT_CANCELLABLE",
            "Этот пакет уже нельзя отменить.",
        )
    await repository.update_batch_status(
        batch_id,
        CustomerUploadBatchStatus.ABANDONED,
    )
    return JSONResponse({"ok": True})
