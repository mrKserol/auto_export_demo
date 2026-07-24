from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiogram import Bot
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
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
from app.web.token_service import TokenError, verify_specification_context_token, verify_estimate_context_token
from app.web.token_service import verify_specification_edit_context_token

logger = logging.getLogger(__name__)

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent / "static"


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


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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


@router.put("/api/specifications")
async def update_specification_api(
    payload: dict[str, Any],
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
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

    return JSONResponse(status_code=200, content={"ok": True, "customer_id": context.customer_id, "specification_id": specification_id, "estimate_reset": estimate_reset, "message": "Спецификация обновлена"})


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
