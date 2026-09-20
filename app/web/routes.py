from __future__ import annotations

import logging
import mimetypes
import os
import tempfile
from io import BytesIO
from pathlib import Path
from secrets import compare_digest
from typing import Any
from urllib.parse import quote
from uuid import UUID

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from pydantic import ValidationError

from app.config import Settings
from app.database import Database
from app.handlers.customers import is_admin_for_customer_management
from app.services.customer_card_service import (
    build_customer_card,
    build_customer_card_keyboard,
    customer_has_estimate,
)
from app.services.specification_service import (
    CustomerNotFoundError,
    SpecificationAlreadyExistsError,
    SpecificationServiceError,
    create_customer_specification,
)
from app.services.estimate_service import create_estimate, format_estimate_summary
from app.web.auth import InitDataError, validate_telegram_init_data
from app.web.schemas import SpecificationFormIn
from app.web.schemas import SpecificationEditContextIn, SpecificationEditFormIn
from app.web.schemas import EstimateFormIn
from app.web.schemas import CustomerEditContextIn, CustomerEditFormIn
from app.web.schemas import CustomerBatchFileDocumentTypeIn
from app.web.token_service import TokenError, verify_specification_context_token, verify_estimate_context_token
from app.web.token_service import verify_specification_edit_context_token, verify_customer_edit_context_token
from app.web.token_service import verify_customer_batch_context_token
from app.repositories.customer_upload_batch_repository import CustomerUploadBatchRepository
from app.repositories.customer_upload_batch_statuses import CustomerUploadBatchStatus
from app.services.customer_batch_data_service import (
    assemble_customer_data_from_batch_files,
    customer_fields_for_create,
)
from app.services.customer_batch_verification_service import (
    CustomerBatchVerificationService,
    build_save_warnings,
    format_kit_for_form,
    serialize_batch_file_for_form,
)
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionService,
)
from app.services.document_intake_service import (
    DocumentIntakeError,
    DocumentIntakeService,
    IntakeUpload,
    build_intake_review_card,
)
from app.services.channel_action_token import create_channel_action_token
from app.services.channel_adapter import TelegramAdapter, review_card_ref_from_metadata
from app.services.miniapp_link_service import (
    build_intake_review_miniapp_url,
    create_intake_review_token,
)
from app.web.auth import TelegramUser
from app.web.token_service import verify_intake_review_context_token
from app.yadisk_client import YandexDiskClient

logger = logging.getLogger(__name__)

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent / "static"


class InternalTelegramFileRequest(BaseModel):
    file_id: str = Field(min_length=1)


class InternalIntakeSessionCreateRequest(BaseModel):
    channel: str = Field(min_length=1)
    external_user_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InternalIntakeReviewLaunchRequest(BaseModel):
    channel: str = Field(min_length=1)
    external_user_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)


class IntakeReviewCorrectionRequest(BaseModel):
    corrections: dict[str, Any] = Field(default_factory=dict)


class IntakeConfirmRequest(BaseModel):
    pass


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if extra:
        payload["error"].update(extra)
    return JSONResponse(status_code=status_code, content=payload)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_bot(request: Request) -> Bot:
    return request.app.state.bot


def _intake_review_keyboard(settings: Settings, *, session_id: UUID, user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    token = create_intake_review_token(
        settings,
        session_id=str(session_id),
        telegram_user_id=user_id,
        origin_chat_id=chat_id,
    )
    url = f"{build_intake_review_miniapp_url(settings, token)}&session_id={session_id}"
    action_token = create_channel_action_token(
        secret=settings.mini_app_token_secret,
        action="intake.add_client",
        session_id=str(session_id),
        channel="telegram",
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ручная коррекция", web_app=WebAppInfo(url=url))],
            [InlineKeyboardButton(text="➕ Добавить клиента", callback_data=f"intake.action:{action_token}")],
        ]
    )


async def _notify_intake_review(
    *,
    request: Request,
    session_id: UUID,
    summary: dict,
    settings: Settings,
    intake_service: DocumentIntakeService,
    user_id: int,
    chat_id: int,
) -> None:
    bot = get_bot(request)
    text = build_intake_review_card(summary)
    keyboard = _intake_review_keyboard(settings, session_id=session_id, user_id=user_id, chat_id=chat_id)
    session = await intake_service.get_review_session(session_id)
    metadata = session.get("metadata") or {}
    card_ref = review_card_ref_from_metadata(metadata)
    try:
        adapter = TelegramAdapter(bot)
        if card_ref is not None and card_ref.conversation_id:
            await adapter.edit_card(
                ref=card_ref,
                text=text,
                keyboard=keyboard,
            )
            return
        ref = await adapter.send_card(
            conversation_id=str(chat_id),
            text=text,
            keyboard=keyboard,
        )
        await intake_service.set_review_card_ref(
            session_id,
            channel=ref.channel,
            conversation_id=ref.conversation_id,
            message_id=ref.message_id,
        )
    except Exception:
        logger.exception("Failed to update intake review message")


def get_yandex_disk_client(request: Request) -> YandexDiskClient | None:
    return getattr(request.app.state, "yandex_disk_client", None)


def get_customer_document_recognition_service(
    request: Request,
) -> CustomerDocumentRecognitionService:
    service = getattr(request.app.state, "customer_document_recognition_service", None)
    if service is None:
        raise RuntimeError("Customer document recognition service is not configured")
    return service


def get_document_intake_service(request: Request) -> DocumentIntakeService:
    service = getattr(request.app.state, "document_intake_service", None)
    if service is None:
        raise RuntimeError("Document intake service is not configured")
    return service


def _safe_telegram_filename(file_path: str | None, file_id: str) -> str:
    if file_path:
        name = Path(file_path).name
        if name:
            return name
    safe_id = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in file_id
    )
    return f"telegram_file_{safe_id or 'unknown'}"


def _authorize_n8n_internal_request(
    settings: Settings,
    provided_secret: str | None,
) -> JSONResponse | None:
    expected_secret = settings.n8n_webhook_secret or ""
    actual_secret = provided_secret or ""
    if not expected_secret or not actual_secret or not compare_digest(
        actual_secret,
        expected_secret,
    ):
        return error_response(401, "UNAUTHORIZED", "Unauthorized")
    return None


def _normalize_customer_document_result(result) -> dict[str, Any]:
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


def _intake_error_response(error: DocumentIntakeError) -> JSONResponse:
    return error_response(error.status_code, error.code, str(error))


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/internal/n8n/telegram-file")
async def download_internal_telegram_file(
    payload: InternalTelegramFileRequest,
    settings: Settings = Depends(get_settings),
    bot: Bot = Depends(get_bot),
    x_n8n_webhook_secret: str | None = Header(
        default=None,
        alias="X-N8N-Webhook-Secret",
    ),
) -> Response:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error

    try:
        file = await bot.get_file(payload.file_id)
        file_path = file.file_path
        if not file_path:
            logger.warning("Telegram returned file without path for n8n download")
            return error_response(
                502,
                "TELEGRAM_FILE_ERROR",
                "Telegram file download failed",
            )

        buffer = BytesIO()
        await bot.download_file(file_path, destination=buffer)
    except Exception:
        logger.exception("Telegram file download failed for n8n")
        return error_response(
            502,
            "TELEGRAM_FILE_ERROR",
            "Telegram file download failed",
        )

    filename = _safe_telegram_filename(file_path, payload.file_id)
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    quoted_filename = quote(filename)
    return Response(
        content=buffer.getvalue(),
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted_filename}"
            ),
        },
    )


@router.post("/internal/n8n/recognize-customer-document")
async def recognize_internal_customer_document(
    file: UploadFile = File(...),
    original_name: str | None = Form(default=None),
    mime_type: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    recognition_service: CustomerDocumentRecognitionService = Depends(
        get_customer_document_recognition_service
    ),
    x_n8n_webhook_secret: str | None = Header(
        default=None,
        alias="X-N8N-Webhook-Secret",
    ),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error

    temporary_path: str | None = None
    try:
        suffix = Path(original_name or file.filename or "").suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
            temporary_path = temporary.name
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                temporary.write(chunk)

        with open(temporary_path, "rb") as temporary:
            content = temporary.read()

        result = await recognition_service.recognize_document(
            content=content,
            mime_type=mime_type or file.content_type,
            filename=original_name or file.filename,
        )
    except Exception:
        logger.exception("Internal n8n customer document recognition failed")
        return error_response(
            502,
            "DOCUMENT_RECOGNITION_FAILED",
            "Document recognition failed",
        )
    finally:
        await file.close()
        if temporary_path:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass

    return JSONResponse(
        content={
            "ok": True,
            "document": _normalize_customer_document_result(result),
        },
    )


@router.post("/internal/n8n/intake-sessions")
async def create_internal_intake_session(
    payload: InternalIntakeSessionCreateRequest,
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
    x_n8n_webhook_secret: str | None = Header(
        default=None,
        alias="X-N8N-Webhook-Secret",
    ),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error
    try:
        result = await intake_service.create_or_get_session(
            channel=payload.channel,
            external_user_id=payload.external_user_id,
            conversation_id=payload.conversation_id,
            metadata=payload.metadata,
        )
    except DocumentIntakeError as error:
        return _intake_error_response(error)
    return JSONResponse(content=result)


@router.post("/internal/n8n/intake-sessions/{session_id}/documents")
async def add_internal_intake_document(
    session_id: UUID,
    file: UploadFile = File(...),
    channel: str = Form(...),
    external_user_id: str = Form(...),
    conversation_id: str = Form(...),
    provider_message_id: str | None = Form(default=None),
    provider_file_id: str | None = Form(default=None),
    media_group_id: str | None = Form(default=None),
    original_name: str | None = Form(default=None),
    mime_type: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
    x_n8n_webhook_secret: str | None = Header(
        default=None,
        alias="X-N8N-Webhook-Secret",
    ),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error

    temporary_path: str | None = None
    try:
        suffix = Path(original_name or file.filename or "").suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
            temporary_path = temporary.name
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                temporary.write(chunk)

        with open(temporary_path, "rb") as temporary:
            content = temporary.read()

        result = await intake_service.add_document(
            session_id,
            IntakeUpload(
                content=content,
                channel=channel,
                external_user_id=external_user_id,
                conversation_id=conversation_id,
                provider_message_id=provider_message_id,
                provider_file_id=provider_file_id,
                media_group_id=media_group_id,
                original_name=original_name or file.filename,
                mime_type=mime_type or file.content_type,
                file_size=len(content),
            ),
        )
    except DocumentIntakeError as error:
        return _intake_error_response(error)
    except Exception:
        logger.exception("Internal n8n intake document upload failed")
        return error_response(500, "INTAKE_DOCUMENT_UPLOAD_FAILED", "Upload failed")
    finally:
        await file.close()
        if temporary_path:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass

    return JSONResponse(content=result)


@router.get("/internal/n8n/intake-sessions/{session_id}")
async def get_internal_intake_session(
    session_id: UUID,
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
    x_n8n_webhook_secret: str | None = Header(
        default=None,
        alias="X-N8N-Webhook-Secret",
    ),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error
    try:
        result = await intake_service.get_summary(session_id)
    except DocumentIntakeError as error:
        return _intake_error_response(error)
    return JSONResponse(content=result)


@router.post("/internal/n8n/intake-sessions/{session_id}/finish")
async def finish_internal_intake_session(
    session_id: UUID,
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
    x_n8n_webhook_secret: str | None = Header(
        default=None,
        alias="X-N8N-Webhook-Secret",
    ),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error
    try:
        result = await intake_service.finish(session_id)
    except DocumentIntakeError as error:
        return _intake_error_response(error)
    return JSONResponse(content=result)


@router.post("/internal/n8n/intake-sessions/{session_id}/review-launch")
async def launch_internal_intake_review(
    session_id: UUID,
    payload: InternalIntakeReviewLaunchRequest,
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
    x_n8n_webhook_secret: str | None = Header(default=None, alias="X-N8N-Webhook-Secret"),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error
    try:
        await intake_service.validate_review_launch(
            session_id,
            channel=payload.channel,
            external_user_id=payload.external_user_id,
            conversation_id=payload.conversation_id,
        )
        telegram_user_id = int(payload.external_user_id)
        origin_chat_id = int(payload.conversation_id)
        token = create_intake_review_token(
            settings,
            session_id=str(session_id),
            telegram_user_id=telegram_user_id,
            origin_chat_id=origin_chat_id,
        )
    except (ValueError, DocumentIntakeError) as error:
        if isinstance(error, DocumentIntakeError):
            return _intake_error_response(error)
        return error_response(422, "INTAKE_VALIDATION_ERROR", "Invalid Telegram context")
    return JSONResponse(
        content={
            "ok": True,
            "session_id": str(session_id),
            "channel": "telegram",
            "launch_url": f"{build_intake_review_miniapp_url(settings, token)}&session_id={session_id}",
            "expires_in": settings.mini_app_token_ttl_seconds,
        }
    )


@router.post("/internal/n8n/intake-sessions/{session_id}/confirm")
async def confirm_internal_intake_session(
    session_id: UUID,
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
    x_n8n_webhook_secret: str | None = Header(default=None, alias="X-N8N-Webhook-Secret"),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error
    try:
        result = await intake_service.confirm(session_id)
    except DocumentIntakeError as error:
        return _intake_error_response(error)
    return JSONResponse(content=result)


@router.post("/internal/n8n/intake-sessions/{session_id}/cancel")
async def cancel_internal_intake_session(
    session_id: UUID,
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
    x_n8n_webhook_secret: str | None = Header(
        default=None,
        alias="X-N8N-Webhook-Secret",
    ),
) -> JSONResponse:
    auth_error = _authorize_n8n_internal_request(settings, x_n8n_webhook_secret)
    if auth_error is not None:
        return auth_error
    try:
        result = await intake_service.cancel(session_id)
    except DocumentIntakeError as error:
        return _intake_error_response(error)
    return JSONResponse(content=result)


@router.get("/miniapp/specification")
async def specification_form_page(request: Request) -> FileResponse:
    token = request.query_params.get("token", "")
    logger.info(
        "Opening specification mini app form token_present=%s",
        bool(token),
    )
    return FileResponse(
        STATIC_DIR / "specification.html",
        media_type="text/html; charset=utf-8",
    )


@router.get("/miniapp/estimate")
async def estimate_form_page(request: Request) -> FileResponse:
    token = request.query_params.get("token", "")
    logger.info(
        "Opening estimate mini app form token_present=%s",
        bool(token),
    )
    return FileResponse(
        STATIC_DIR / "estimate.html",
        media_type="text/html; charset=utf-8",
    )


@router.get("/miniapp/customer")
async def customer_form_page(request: Request) -> FileResponse:
    token = request.query_params.get("token", "")
    logger.info(
        "Opening customer mini app form token_present=%s",
        bool(token),
    )
    return FileResponse(
        STATIC_DIR / "customer.html",
        media_type="text/html; charset=utf-8",
    )


@router.post("/api/specifications")
async def create_specification_api(
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
    bot: Bot = Depends(get_bot),
) -> JSONResponse:
    try:
        form = SpecificationFormIn.model_validate(payload)
    except ValidationError as error:
        logger.info("Specification validation error")
        field_errors: dict[str, str] = {}
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            field_errors[loc or "_form"] = item.get("msg", "Некорректное значение")
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Проверьте заполнение формы",
            extra={"fields": field_errors},
        )

    try:
        telegram_user = validate_telegram_init_data(
            form.telegram_init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        logger.warning(
            "Invalid Telegram initData code=%s",
            error.code,
        )
        status = 401 if error.code.startswith("INVALID") else 401
        if error.code == "EXPIRED_TELEGRAM_INIT_DATA":
            status = 401
        return error_response(status, error.code, error.message)

    try:
        context = verify_specification_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
        logger.info(
            "Specification context token verified customer_id=%s telegram_user_id=%s",
            context.customer_id,
            telegram_user.id,
        )
    except TokenError as error:
        logger.warning(
            "Invalid context token code=%s telegram_user_id=%s",
            error.code,
            telegram_user.id,
        )
        return error_response(401, error.code, error.message)

    try:
        result = await create_customer_specification(
            database,
            customer_id=context.customer_id,
            fields=form.to_database_fields(),
            replace_existing=False,
        )
    except CustomerNotFoundError as error:
        return error_response(404, error.code, error.message)
    except SpecificationAlreadyExistsError as error:
        return error_response(
            409,
            error.code,
            error.message,
            extra={
                "specification_id": error.details.get("specification_id"),
            },
        )
    except SpecificationServiceError as error:
        logger.exception("Specification service error code=%s", error.code)
        return error_response(500, error.code, "Не удалось сохранить спецификацию")
    except Exception:
        logger.exception(
            "Database error while creating specification customer_id=%s",
            context.customer_id,
        )
        return error_response(500, "DATABASE_ERROR", "Не удалось сохранить спецификацию")

    specification = result["specification"]
    customer = result["customer"]
    logger.info(
        "Specification created specification_id=%s customer_id=%s telegram_user_id=%s",
        result["specification_id"],
        context.customer_id,
        telegram_user.id,
    )

    try:
        await _notify_user_about_specification(
            bot=bot,
            database=database,
            telegram_user_id=telegram_user.id,
            customer=customer,
            specification=specification,
        )
    except Exception:
        logger.exception(
            "Telegram notification failed after specification save "
            "specification_id=%s customer_id=%s telegram_user_id=%s",
            result["specification_id"],
            context.customer_id,
            telegram_user.id,
        )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "specification_id": result["specification_id"],
            "customer_id": context.customer_id,
            "message": "Спецификация сохранена",
        },
    )


@router.post("/api/specifications/edit-context")
async def get_specification_edit_context(
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    try:
        form = SpecificationEditContextIn.model_validate(payload)
    except ValidationError as error:
        field_errors = {}
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            field_errors[loc or "_form"] = item.get("msg", "Некорректное значение")
        return error_response(422, "VALIDATION_ERROR", "Проверьте заполнение формы", extra={"fields": field_errors})

    try:
        telegram_user = validate_telegram_init_data(
            form.telegram_init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        return error_response(401, error.code, error.message)

    try:
        context = verify_specification_edit_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
    except TokenError as error:
        return error_response(401, error.code, error.message)

    customer = await database.get_customer_by_id(int(context.customer_id))
    if not customer:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")
    specification_id = customer.get("specification_id")
    if not specification_id:
        return error_response(404, "SPECIFICATION_NOT_FOUND", "У клиента нет спецификации")
    specification = await database.get_specification_by_id(int(specification_id))
    if not specification:
        return error_response(404, "SPECIFICATION_NOT_FOUND", "Спецификация не найдена")

    editable = {
        "brand": specification.get("brand"),
        "model": specification.get("model"),
        "year": specification.get("year"),
        "eng_capacity": specification.get("eng_capacity"),
        "eng_type": specification.get("eng_type"),
        "drive": specification.get("drive"),
        "transmission": specification.get("transmission"),
        "color": specification.get("color"),
        "complectation": specification.get("complectation"),
        "mileage": specification.get("mileage"),
        "price": specification.get("price"),
    }
    has_estimate = bool(await database.get_estimate_by_specification_id(int(specification_id)))

    return JSONResponse(status_code=200, content={"ok": True, "customer_id": context.customer_id, "specification_id": specification_id, "values": editable, "price_currency": specification.get("price_currency") or "CNY", "has_estimate": has_estimate})


@router.post("/api/customers/edit-context")
async def get_customer_edit_context(
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> JSONResponse:
    try:
        form = CustomerEditContextIn.model_validate(payload)
    except ValidationError as error:
        field_errors = {}
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            field_errors[loc or "_form"] = item.get("msg", "Некорректное значение")
        return error_response(422, "VALIDATION_ERROR", "Проверьте заполнение формы", extra={"fields": field_errors})

    try:
        telegram_user = validate_telegram_init_data(
            form.telegram_init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        return error_response(401, error.code, error.message)

    try:
        batch_context = verify_customer_batch_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
    except TokenError:
        batch_context = None

    if batch_context is not None:
        repository = CustomerUploadBatchRepository(database.pool)
        batch = await repository.get_batch_by_id(int(batch_context.batch_id))
        if batch is None:
            return error_response(404, "BATCH_NOT_FOUND", "Пакет документов не найден")

        files = await repository.get_batch_files_with_paths(int(batch_context.batch_id))
        kit_raw = await repository.recalculate_batch_validation(int(batch_context.batch_id))
        kit = format_kit_for_form(kit_raw, files)
        assembled = assemble_customer_data_from_batch_files(
            files,
            kit_warnings=kit_raw.get("warnings"),
        )
        warnings = build_save_warnings(
            fields=assembled.fields,
            kit_warnings=assembled.warnings,
            files=files,
        )
        values = {
            "passport": assembled.fields.get("passport"),
            "last_name": assembled.fields.get("last_name"),
            "first_name": assembled.fields.get("first_name"),
            "surname": assembled.fields.get("surname"),
            "last_name_translit": None,
            "first_name_translit": None,
            "surname_translit": None,
            "date_issue": assembled.fields.get("date_issue"),
            "by_whom_issued": assembled.fields.get("by_whom_issued"),
            "department_code": assembled.fields.get("department_code"),
            "registration_address": assembled.fields.get("registration_address"),
            "ipain": assembled.fields.get("ipain"),
            "tin": assembled.fields.get("tin"),
            "phone": assembled.fields.get("phone"),
            "email": assembled.fields.get("email"),
        }
        already_saved = batch["status"] == CustomerUploadBatchStatus.CUSTOMER_SAVED
        content = {
            "ok": True,
            "mode": "create_from_batch",
            "batch_id": batch_context.batch_id,
            "customer_path": batch.get("customer_path"),
            "customer_id": batch.get("customer_id"),
            "already_saved": already_saved,
            "warnings": warnings,
            "kit": kit,
            "documents": [serialize_batch_file_for_form(row) for row in files],
            "values": values,
            "message": "Клиент уже сохранён" if already_saved else None,
        }
    return JSONResponse(status_code=200, content=content)


def _authenticate_intake_review(
    payload: dict[str, Any],
    settings: Settings,
) -> tuple[Any | None, JSONResponse | None]:
    token = str(payload.get("context_token") or "").strip()
    init_data = str(payload.get("telegram_init_data") or "").strip()
    try:
        user = validate_telegram_init_data(
            init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
        context = verify_intake_review_context_token(
            token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=user.id,
        )
    except (InitDataError, TokenError) as error:
        return None, error_response(401, getattr(error, "code", "UNAUTHORIZED"), str(error))
    if user.id not in settings.miniapp_allowed_telegram_user_ids:
        return None, error_response(403, "MINIAPP_FORBIDDEN", "Нет доступа к Mini App.")
    return context, None


@router.get("/api/intake-sessions/{session_id}/review")
async def get_intake_review(
    session_id: UUID,
    context_token: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
) -> JSONResponse:
    context, auth_error = _authenticate_intake_review(
        {"context_token": context_token, "telegram_init_data": x_telegram_init_data}, settings
    )
    if auth_error is not None:
        return auth_error
    if context.session_id != str(session_id):
        return error_response(403, "SESSION_MISMATCH", "Нет доступа к этой сессии.")
    try:
        return JSONResponse(content={"ok": True, **await intake_service.get_review_summary(session_id)})
    except DocumentIntakeError as error:
        return _intake_error_response(error)


@router.patch("/api/intake-sessions/{session_id}/review")
async def patch_intake_review(
    session_id: UUID,
    payload: dict[str, Any],
    request: Request,
    settings: Settings = Depends(get_settings),
    intake_service: DocumentIntakeService = Depends(get_document_intake_service),
) -> JSONResponse:
    context, auth_error = _authenticate_intake_review(payload, settings)
    if auth_error is not None:
        return auth_error
    if context.session_id != str(session_id):
        return error_response(403, "SESSION_MISMATCH", "Нет доступа к этой сессии.")
    corrections = payload.get("corrections")
    if not isinstance(corrections, dict):
        return error_response(422, "VALIDATION_ERROR", "Некорректные corrections")
    try:
        result = await intake_service.update_review_corrections(
            session_id,
            corrections=corrections,
            telegram_user_id=context.telegram_user_id,
        )
    except DocumentIntakeError as error:
        return _intake_error_response(error)
    await _notify_intake_review(
        request=request,
        session_id=session_id,
        summary=result,
        settings=settings,
        intake_service=intake_service,
        user_id=context.telegram_user_id,
        chat_id=context.origin_chat_id,
    )
    return JSONResponse(
        content={
            "ok": True,
            **result,
            "card_text": build_intake_review_card(result),
            "notification": {"type": "intake.review.updated", "session_id": str(session_id)},
        }
    )

    try:
        context = verify_customer_edit_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
    except TokenError as error:
        return error_response(401, error.code, error.message)

    customer = await database.get_customer_by_id(int(context.customer_id))
    if not customer:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")

    values = {
        "passport": customer.get("passport"),
        "last_name": customer.get("last_name"),
        "first_name": customer.get("first_name"),
        "surname": customer.get("surname"),
        "last_name_translit": customer.get("last_name_translit"),
        "first_name_translit": customer.get("first_name_translit"),
        "surname_translit": customer.get("surname_translit"),
        "date_issue": None if customer.get("date_issue") is None else (customer.get("date_issue").isoformat() if hasattr(customer.get("date_issue"), "isoformat") else str(customer.get("date_issue"))),
        "by_whom_issued": customer.get("by_whom_issued"),
        "department_code": customer.get("department_code"),
        "registration_address": customer.get("registration_address"),
        "ipain": customer.get("ipain"),
        "tin": customer.get("tin"),
        "phone": customer.get("phone"),
        "email": customer.get("email"),
    }

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "mode": "edit",
            "customer_id": context.customer_id,
            "values": values,
        },
    )


@router.patch("/api/customer-batches/{batch_id}/files/{file_id}/document-type")
async def patch_customer_batch_file_document_type(
    batch_id: int,
    file_id: int,
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
    yandex_disk_client: YandexDiskClient | None = Depends(get_yandex_disk_client),
) -> JSONResponse:
    try:
        form = CustomerBatchFileDocumentTypeIn.model_validate(payload)
    except ValidationError as error:
        field_errors = {}
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            field_errors[loc or "_form"] = item.get("msg", "Некорректное значение")
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Проверьте тип документа",
            extra={"fields": field_errors},
        )

    try:
        telegram_user = validate_telegram_init_data(
            form.telegram_init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        return error_response(401, error.code, error.message)

    try:
        batch_context = verify_customer_batch_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
    except TokenError as error:
        return error_response(401, error.code, error.message)

    if int(batch_context.batch_id) != int(batch_id):
        return error_response(
            403,
            "BATCH_FORBIDDEN",
            "Токен не относится к этому пакету документов",
        )

    if yandex_disk_client is None:
        return error_response(
            503,
            "DISK_UNAVAILABLE",
            "Сервис Яндекс Диска недоступен",
        )

    repository = CustomerUploadBatchRepository(database.pool)
    service = CustomerBatchVerificationService(
        repository=repository,
        yandex_disk_client=yandex_disk_client,
    )
    try:
        result = await service.update_file_document_type(
            batch_id=batch_id,
            file_id=file_id,
            document_type=form.document_type,
        )
    except PermissionError as error:
        message = str(error)
        if message == "batch_already_saved":
            return error_response(
                409,
                "BATCH_ALREADY_SAVED",
                "Клиент уже сохранён. Изменение типа документа недоступно.",
            )
        return error_response(
            409,
            "BATCH_NOT_EDITABLE",
            "Пакет документов сейчас нельзя редактировать",
        )
    except LookupError:
        return error_response(
            404,
            "FILE_NOT_FOUND",
            "Файл пакета не найден",
        )
    except ValueError:
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Недопустимый тип документа",
        )
    except Exception:
        logger.exception(
            "Failed to update batch file document type batch_id=%s file_id=%s",
            batch_id,
            file_id,
        )
        return error_response(
            500,
            "DOCUMENT_TYPE_UPDATE_ERROR",
            "Не удалось обновить тип документа",
        )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "batch_id": batch_id,
            "file": result.file,
            "kit": result.kit,
            "warnings": result.warnings,
            "values": result.values,
            "renamed": result.renamed,
            "move_error": result.move_error,
        },
    )


@router.put("/api/customers")
async def update_customer_api(
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
    bot: Bot = Depends(get_bot),
) -> JSONResponse:
    try:
        form = CustomerEditFormIn.model_validate(payload)
    except ValidationError as error:
        field_errors = {}
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            field_errors[loc or "_form"] = item.get("msg", "Некорректное значение")
        return error_response(422, "VALIDATION_ERROR", "Проверьте заполнение формы", extra={"fields": field_errors})

    try:
        telegram_user = validate_telegram_init_data(
            form.telegram_init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        return error_response(401, error.code, error.message)

    try:
        batch_context = verify_customer_batch_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
    except TokenError:
        batch_context = None

    if batch_context is not None:
        repository = CustomerUploadBatchRepository(database.pool)
        batch = await repository.get_batch_by_id(int(batch_context.batch_id))
        if batch is None:
            return error_response(404, "BATCH_NOT_FOUND", "Пакет документов не найден")

        if batch["status"] == CustomerUploadBatchStatus.CUSTOMER_SAVED:
            existing_id = batch.get("customer_id")
            if existing_id:
                existing_customer = await database.get_customer_by_id(int(existing_id))
                if existing_customer:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "ok": True,
                            "mode": "create_from_batch",
                            "already_saved": True,
                            "customer_id": int(existing_id),
                            "batch_id": batch_context.batch_id,
                            "customer_path": batch.get("customer_path"),
                            "message": "Клиент уже сохранён",
                        },
                    )
            return error_response(
                409,
                "BATCH_ALREADY_SAVED",
                "Клиент по этому пакету уже сохранён",
            )

        passport = (form.passport or "").strip()
        if not passport or not (form.last_name or "").strip() or not (form.first_name or "").strip():
            return error_response(
                422,
                "VALIDATION_ERROR",
                "Заполните фамилию, имя и паспорт",
            )

        existing = await database.find_customer_by_passport(passport)
        if existing:
            return error_response(
                409,
                "PASSPORT_ALREADY_EXISTS",
                "Клиент с таким паспортом уже существует",
            )

        files = await repository.get_batch_files_with_paths(int(batch_context.batch_id))
        assembled = assemble_customer_data_from_batch_files(files)
        overrides = form.model_dump(
            exclude={"context_token", "telegram_init_data"},
        )
        if overrides.get("date_issue") is not None:
            date_issue = overrides["date_issue"]
            overrides["date_issue"] = (
                date_issue.isoformat()
                if hasattr(date_issue, "isoformat")
                else str(date_issue)
            )
        # Empty optional strings should clear / stay empty without blocking save.
        for optional_key in (
            "surname",
            "registration_address",
            "ipain",
            "tin",
            "phone",
            "email",
            "by_whom_issued",
            "department_code",
        ):
            if overrides.get(optional_key) is None:
                continue
            if str(overrides[optional_key]).strip() == "":
                overrides[optional_key] = None

        customer_data = customer_fields_for_create(
            assembled,
            customer_path=batch.get("customer_path"),
            overrides=overrides,
        )
        save_warnings = build_save_warnings(
            fields={**assembled.fields, **{k: overrides.get(k) for k in overrides}},
            kit_warnings=assembled.warnings,
            files=files,
        )
        try:
            customer = await database.create_customer(customer_data)
            marked = await repository.try_mark_batch_customer_saved(
                int(batch_context.batch_id),
                customer_id=int(customer["id"]),
            )
            if marked is None:
                # Another request finished the batch first.
                refreshed = await repository.get_batch_by_id(int(batch_context.batch_id))
                existing_id = refreshed.get("customer_id") if refreshed else None
                if existing_id:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "ok": True,
                            "mode": "create_from_batch",
                            "already_saved": True,
                            "customer_id": int(existing_id),
                            "batch_id": batch_context.batch_id,
                            "customer_path": (
                                refreshed.get("customer_path") if refreshed else None
                            ),
                            "message": "Клиент уже сохранён",
                        },
                    )
                await repository.mark_batch_customer_saved(
                    int(batch_context.batch_id),
                    customer_id=int(customer["id"]),
                )
        except Exception:
            logger.exception(
                "Failed to create customer from batch_id=%s",
                batch_context.batch_id,
            )
            return error_response(
                500,
                "CUSTOMER_CREATE_ERROR",
                "Не удалось сохранить клиента",
            )

        customer_path = batch.get("customer_path")
        try:
            notify_chat_id = int(batch_context.origin_chat_id or telegram_user.id)
            has_est = await customer_has_estimate(customer, database)
            path_block = (
                f"\n\nПапка документов:\n{customer_path}"
                if customer_path
                else ""
            )
            await bot.send_message(
                chat_id=notify_chat_id,
                text=(
                    "Клиент сохранён."
                    f"{path_block}\n\n"
                    "✅ Клиент добавлен\n\n"
                    + await build_customer_card(customer, database)
                ),
                reply_markup=build_customer_card_keyboard(
                    customer,
                    is_admin=False,
                    has_estimate=has_est,
                ),
            )
        except Exception:
            logger.exception(
                "Failed to notify after batch customer create batch_id=%s",
                batch_context.batch_id,
            )

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "mode": "create_from_batch",
                "customer_id": customer["id"],
                "batch_id": batch_context.batch_id,
                "customer_path": customer_path,
                "warnings": save_warnings,
                "message": "Клиент сохранён",
            },
        )

    try:
        context = verify_customer_edit_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
    except TokenError as error:
        return error_response(401, error.code, error.message)

    customer = await database.get_customer_by_id(int(context.customer_id))
    if not customer:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")

    # prevent passport collision
    new_passport = form.passport.strip()
    if new_passport and new_passport != (customer.get("passport") or ""):
        existing = await database.find_customer_by_passport(new_passport)
        if existing and int(existing.get("id")) != int(customer["id"]):
            return error_response(409, "PASSPORT_ALREADY_EXISTS", "Клиент с таким паспортом уже существует")

    # collect updatable fields
    fields = {}
    for field in database._CUSTOMER_UPDATABLE_FIELDS:
        if hasattr(form, field):
            val = getattr(form, field)
            # normalize date field to ISO string if date object
            if field == "date_issue" and isinstance(val, (bytes, bytearray)) is False:
                try:
                    from datetime import date, datetime
                    if isinstance(val, date) and not isinstance(val, datetime):
                        val = val.isoformat()
                    elif isinstance(val, datetime):
                        val = val.date().isoformat()
                except Exception:
                    pass
            fields[field] = val

    # detect changes
    changed = []
    to_update = {}
    for k, v in fields.items():
        old = customer.get(k)
        old_s = "" if old is None else str(old)
        new_s = "" if v is None else str(v)
        if old_s != new_s:
            changed.append(k)
            to_update[k] = v

    if to_update:
        try:
            updated = await database.update_customer_fields(int(customer["id"]), to_update)
        except Exception:
            logger.exception("Failed to update customer_id=%s", customer["id"])
            return error_response(500, "CUSTOMER_UPDATE_ERROR", "Не удалось обновить данные клиента")
    else:
        updated = customer

    # notify user
    try:
        telegram_chat_id = int(telegram_user.id)
        await bot.send_message(chat_id=telegram_chat_id, text="✅ Данные клиента обновлены")
        has_est = await customer_has_estimate(updated, database)
        await bot.send_message(chat_id=telegram_chat_id, text=await build_customer_card(updated, database), reply_markup=build_customer_card_keyboard(updated, is_admin=False, has_estimate=has_est))
    except Exception:
        logger.exception("Failed to notify user after customer update")

    return JSONResponse(status_code=200, content={"ok": True, "customer_id": updated["id"], "changed_fields": changed, "message": "Данные клиента сохранены"})


@router.put("/api/specifications")
async def update_specification_api(
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
    bot: Bot = Depends(get_bot),
) -> JSONResponse:
    try:
        form = SpecificationEditFormIn.model_validate(payload)
    except ValidationError as error:
        field_errors = {}
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            field_errors[loc or "_form"] = item.get("msg", "Некорректное значение")
        return error_response(422, "VALIDATION_ERROR", "Проверьте заполнение формы", extra={"fields": field_errors})

    try:
        telegram_user = validate_telegram_init_data(
            form.telegram_init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        return error_response(401, error.code, error.message)

    try:
        context = verify_specification_edit_context_token(
            form.context_token,
            secret=settings.mini_app_token_secret,
            expected_telegram_user_id=telegram_user.id,
        )
    except TokenError as error:
        return error_response(401, error.code, error.message)

    customer = await database.get_customer_by_id(int(context.customer_id))
    if not customer:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")
    specification_id = customer.get("specification_id")
    if not specification_id:
        return error_response(404, "SPECIFICATION_NOT_FOUND", "У клиента нет спецификации")
    specification = await database.get_specification_by_id(int(specification_id))
    if not specification:
        return error_response(404, "SPECIFICATION_NOT_FOUND", "Спецификация не найдена")

    # prepare new fields
    new_fields = form.to_database_fields()
    # remove any keys not in updatable set (to_database_fields returns all)
    updatable = {k: v for k, v in new_fields.items() if k in database._SPECIFICATION_UPDATABLE_FIELDS}

    # detect changes
    changed = {}
    for k, v in updatable.items():
        old = specification.get(k)
        # compare as strings (DB stores as text)
        old_s = "" if old is None else str(old)
        new_s = "" if v is None else str(v)
        if old_s != new_s:
            changed[k] = v

    has_estimate = bool(await database.get_estimate_by_specification_id(int(specification_id)))
    if changed and has_estimate and not getattr(form, "confirm_estimate_reset", False):
        return error_response(409, "ESTIMATE_RECALCULATION_REQUIRED", "После изменения спецификации существующую смету потребуется пересоздать.")

    try:
        if changed and has_estimate and getattr(form, "confirm_estimate_reset", False):
            updated = await database.update_specification_and_reset_estimate(int(specification_id), changed)
            estimate_reset = True
        elif changed:
            updated = await database.update_specification_fields(int(specification_id), changed)
            estimate_reset = False
        else:
            return JSONResponse(status_code=200, content={"ok": True, "customer_id": context.customer_id, "specification_id": specification_id, "estimate_reset": False, "message": "Изменений нет"})
    except Exception:
        logger.exception("Failed to update specification specification_id=%s", specification_id)
        return error_response(500, "SPEC_UPDATE_ERROR", "Не удалось обновить спецификацию")

    # notify user (no blocking)
    try:
        # fetch updated customer
        customer = await database.get_customer_by_id(int(context.customer_id))
        # send Telegram messages via bot is not available here; controller that opened miniapp should handle notifying.
        pass
    except Exception:
        logger.exception("Failed to notify user after spec update")

    # fetch updated data to return to miniapp for user feedback and notify via bot
    try:
        updated_customer = await database.get_customer_by_id(int(context.customer_id))
        updated_spec = await database.get_specification_by_id(int(specification_id))
        from app.services.customer_card_service import build_customer_card
        customer_card = await build_customer_card(updated_customer, database)
        from app.services.specification_edit_service import format_specification_text
        specification_text = format_specification_text(updated_spec) if updated_spec else ""
    except Exception:
        logger.exception("Failed to build user-facing messages after spec update")
        customer_card = ""
        specification_text = ""

    extra_msg = ""
    if estimate_reset:
        extra_msg = "⚠️ Предыдущая смета удалена, так как данные автомобиля изменились. Создайте новую смету."

    # notify user in Telegram about the update
    try:
        telegram_chat_id = int(telegram_user.id)
        notify_lines = []
        notify_lines.append("✅ Спецификация обновлена")
        if extra_msg:
            notify_lines.append(extra_msg)
        if specification_text:
            notify_lines.append("")
            notify_lines.append(specification_text)
        await bot.send_message(chat_id=telegram_chat_id, text="\n".join(notify_lines))
        # send updated customer card with keyboard
        has_est = await customer_has_estimate(updated_customer, database)
        await bot.send_message(
            chat_id=telegram_chat_id,
            text=customer_card or "",
            reply_markup=build_customer_card_keyboard(updated_customer, is_admin=False, has_estimate=has_est),
        )
    except Exception:
        logger.exception("Failed to send Telegram notification after spec update")

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "customer_id": context.customer_id,
            "specification_id": specification_id,
            "estimate_reset": estimate_reset,
            "message": "Спецификация обновлена",
            "customer_card": customer_card,
            "specification_text": specification_text,
            "extra_message": extra_msg,
        },
    )


@router.post("/api/estimates")
async def create_estimate_api(
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
    bot: Bot = Depends(get_bot),
) -> JSONResponse:
    try:
        form = EstimateFormIn.model_validate(payload)
    except ValidationError as error:
        field_errors: dict[str, str] = {}
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()))
            field_errors[loc or "_form"] = item.get("msg", "Некорректное значение")
        return error_response(422, "VALIDATION_ERROR", "Проверьте заполнение формы", extra={"fields": field_errors})

    try:
        telegram_user = validate_telegram_init_data(
            form.telegram_init_data,
            settings.telegram_bot_token,
            settings.telegram_init_data_max_age_seconds,
        )
    except InitDataError as error:
        return error_response(401, error.code, error.message)

    try:
        context = verify_estimate_context_token(form.context_token, secret=settings.mini_app_token_secret, expected_telegram_user_id=telegram_user.id)
    except TokenError as error:
        return error_response(401, error.code, error.message)

    # check customer and specification
    customer = await database.get_customer_by_id(int(context.customer_id))
    if not customer:
        return error_response(404, "CUSTOMER_NOT_FOUND", "Клиент не найден")
    specification_id = customer.get("specification_id")
    if not specification_id:
        return error_response(404, "SPECIFICATION_NOT_FOUND", "У клиента нет спецификации")
    existing_estimate = await database.get_estimate_by_specification_id(int(specification_id))
    if existing_estimate and not getattr(context, "recreate", False):
        return error_response(409, "ESTIMATE_ALREADY_EXISTS", "Смета для клиента уже создана")

    try:
        result = await create_estimate(
            database,
            customer_id=int(context.customer_id),
            specification_id=int(specification_id),
            specification=await database.get_specification_by_id(int(specification_id)),
            engine_power=form.engine_power,
            exchange_rate=form.exchange_rate,
            inspect_transport_price=form.inspect_transport_price,
            bank_commission=form.bank_commission,
            transit_declaration_price=form.transit_declaration_price,
            insurance_shipment=form.insurance_shipment,
            custom_clearing=form.custom_clearing,
            contractor_comission=form.contractor_comission,
        )
    except Exception:
        return error_response(500, "ESTIMATE_SAVE_ERROR", "Не удалось сохранить смету")

    # notify user
    try:
        await bot.send_message(chat_id=telegram_user.id, text=format_estimate_summary(result, await database.get_specification_by_id(int(specification_id))))
        has_est = await customer_has_estimate(customer, database)
        await bot.send_message(chat_id=telegram_user.id, text=await build_customer_card(customer, database), reply_markup=build_customer_card_keyboard(customer, is_admin=False, has_estimate=has_est))
    except Exception:
        pass

    return JSONResponse(status_code=200, content={"ok": True, "estimate_id": result.get("id"), "customer_id": context.customer_id, "message": "Смета сохранена"})


def format_specification_summary(specification: dict) -> str:
    brand = specification.get("brand") or "—"
    model = specification.get("model") or "—"
    year = specification.get("year") or "—"
    eng_capacity = specification.get("eng_capacity") or "—"
    eng_type = specification.get("eng_type") or "—"
    drive = specification.get("drive") or "—"
    transmission = specification.get("transmission") or "—"
    color = specification.get("color") or "—"
    complectation = specification.get("complectation") or "—"
    mileage = specification.get("mileage") or "—"
    price = specification.get("price") or "—"
    return (
        "✅ Спецификация добавлена\n\n"
        f"{brand} {model}, {year}\n"
        f"{eng_capacity} л · {eng_type} · {drive} привод\n"
        f"{transmission} · {color} · {complectation}\n"
        f"Пробег: {mileage} км\n"
        f"Стоимость: {price} CNY"
    )


async def _notify_user_about_specification(
    *,
    bot: Bot,
    database: Database,
    telegram_user_id: int,
    customer: dict,
    specification: dict,
) -> None:
    await bot.send_message(
        chat_id=telegram_user_id,
        text=format_specification_summary(specification),
    )
    is_admin = await is_admin_for_customer_management(
        bot,
        telegram_user_id,
        telegram_user_id,
    )
    has_estimate = await customer_has_estimate(customer, database)
    await bot.send_message(
        chat_id=telegram_user_id,
        text=await build_customer_card(customer, database),
        reply_markup=build_customer_card_keyboard(
            customer,
            is_admin=is_admin,
            has_estimate=has_estimate,
        ),
    )
