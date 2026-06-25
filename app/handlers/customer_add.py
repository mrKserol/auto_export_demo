from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import Database
from app.handlers.customers import is_admin_for_customer_management
from app.services.customer_card_service import (
    build_customer_card,
    build_customer_card_keyboard,
    customer_has_estimate,
    format_customer_fio,
)
from app.services.customer_document_recognition_service import (
    CustomerDocumentRecognitionService,
)
from app.services.customer_extraction_service import format_fio_normalized, person_names_match
from app.services.file_service import (
    build_stored_filename,
    get_original_filename,
    is_supported_file,
)
from app.services.validation_service import (
    normalize_phone,
    normalize_snils,
    normalize_tin,
    validate_email,
    validate_registration_address,
)
from app.states.customer_states import CustomerAddStates
from app.yadisk_client import YandexDiskClient


router = Router(name="customer_add")
logger = logging.getLogger(__name__)

PROCESSING_FILE_MESSAGE = "Подождите, обрабатываю файл…"


@router.message(Command("add_customer"))
async def handle_add_customer(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CustomerAddStates.waiting_passport_main)
    await state.update_data(customer_fields={}, uploaded_files=[])
    await message.answer(
        "Загрузите файл паспорта со страницами 2 и 3 (разворот с фотографией)."
    )


@router.message(StateFilter(CustomerAddStates.waiting_passport_main), F.document | F.photo)
async def handle_passport_main_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
    customer_document_recognition_service: CustomerDocumentRecognitionService,
) -> None:
    await message.answer(PROCESSING_FILE_MESSAGE)

    response_payload = await _upload_customer_file(message, bot, yandex_disk_client)
    if response_payload is None:
        return

    try:
        fields = await customer_document_recognition_service.recognize_passport_main(
            response_payload["content"],
            response_payload["mime_type"],
            filename=response_payload["original_filename"],
        )
    except Exception:
        logger.exception("Passport main recognition failed")
        await message.answer(
            "Не удалось распознать номер паспорта. "
            "Загрузите корректный файл паспорта со страницами 2 и 3."
        )
        return

    passport = fields.get("passport")
    if not passport:
        await message.answer(
            "Не удалось распознать номер паспорта. "
            "Загрузите корректный файл паспорта со страницами 2 и 3."
        )
        return

    await state.update_data(customer_fields=fields)
    await state.set_state(CustomerAddStates.waiting_registration)
    await message.answer(
        "Паспорт распознан.\n\n"
        f"Паспорт: {fields.get('passport') or '—'}\n"
        f"ФИО: {format_customer_fio(fields)}\n"
        f"Кем выдан: {fields.get('by_whom_issued') or '—'}\n"
        f"Дата выдачи: {fields.get('date_issue') or '—'}\n"
        f"Код подразделения: {fields.get('department_code') or '—'}\n\n"
        f"Загрузите файл паспорта со страницей регистрации паспорта {passport}."
    )


@router.message(StateFilter(CustomerAddStates.waiting_registration), F.document | F.photo)
async def handle_registration_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
    customer_document_recognition_service: CustomerDocumentRecognitionService,
) -> None:
    await message.answer(PROCESSING_FILE_MESSAGE)

    data = await state.get_data()
    customer_fields = dict(data.get("customer_fields") or {})
    expected_passport = customer_fields.get("passport")
    if not expected_passport:
        await state.clear()
        await message.answer("Сессия устарела. Начните с /add_customer")
        return

    response_payload = await _upload_customer_file(message, bot, yandex_disk_client)
    if response_payload is None:
        return

    try:
        reg_fields = await customer_document_recognition_service.recognize_passport_registration(
            response_payload["content"],
            response_payload["mime_type"],
            expected_passport=expected_passport,
            filename=response_payload["original_filename"],
        )
    except Exception:
        logger.exception("Registration recognition failed")
        await message.answer(
            "Не удалось обработать файл регистрации.",
            reply_markup=_manual_registration_keyboard(),
        )
        return

    found_passport = reg_fields.get("passport")
    if found_passport and found_passport != expected_passport:
        await message.answer(
            "Номера паспорта не совпадают, загрузите корректный файл.",
            reply_markup=_manual_registration_keyboard(),
        )
        return

    address = reg_fields.get("registration_address")
    if not address:
        await message.answer(
            "Не удалось уверенно распознать адрес регистрации.",
            reply_markup=_manual_registration_keyboard(),
        )
        return

    customer_fields["registration_address"] = address
    await state.update_data(customer_fields=customer_fields)
    await _go_to_snils_step(message, state, customer_fields)


@router.callback_query(
    StateFilter(CustomerAddStates.waiting_registration),
    F.data == "add_customer_manual:registration",
)
async def handle_manual_registration_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.set_state(CustomerAddStates.waiting_manual_registration_address)
    await callback.message.answer("Введите адрес регистрации вручную.")
    await callback.answer()


@router.message(StateFilter(CustomerAddStates.waiting_manual_registration_address), F.text)
async def handle_manual_registration_value(message: Message, state: FSMContext) -> None:
    address = (message.text or "").strip()
    if not validate_registration_address(address):
        await message.answer("Адрес слишком короткий или слишком длинный. Длина 1–500 символов.")
        return

    data = await state.get_data()
    customer_fields = dict(data.get("customer_fields") or {})
    customer_fields["registration_address"] = address
    await state.update_data(customer_fields=customer_fields)
    await _go_to_snils_step(message, state, customer_fields)


@router.message(StateFilter(CustomerAddStates.waiting_snils), F.document | F.photo)
async def handle_snils_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
    customer_document_recognition_service: CustomerDocumentRecognitionService,
) -> None:
    await message.answer(PROCESSING_FILE_MESSAGE)

    data = await state.get_data()
    customer_fields = dict(data.get("customer_fields") or {})
    response_payload = await _upload_customer_file(message, bot, yandex_disk_client)
    if response_payload is None:
        return

    try:
        snils_fields = await customer_document_recognition_service.recognize_snils(
            response_payload["content"],
            response_payload["mime_type"],
            filename=response_payload["original_filename"],
        )
    except Exception:
        logger.exception("SNILS recognition failed")
        await message.answer(
            "Не удалось обработать файл СНИЛС.",
            reply_markup=_manual_snils_keyboard(),
        )
        return

    if not person_names_match(customer_fields, snils_fields):
        logger.warning(
            "SNILS name mismatch: passport_fio=%s, snils_fio=%s",
            format_fio_normalized(customer_fields),
            format_fio_normalized(snils_fields),
        )
        await message.answer(
            "ФИО не совпадает, загрузите соответствующий файл.",
            reply_markup=_manual_snils_keyboard(),
        )
        return

    ipain = snils_fields.get("ipain")
    if not ipain:
        await message.answer(
            "Не удалось распознать номер СНИЛС.",
            reply_markup=_manual_snils_keyboard(),
        )
        return

    customer_fields["ipain"] = ipain
    await state.update_data(customer_fields=customer_fields)
    await _go_to_tin_step(message, state, customer_fields)


@router.callback_query(
    StateFilter(CustomerAddStates.waiting_snils),
    F.data == "add_customer_manual:snils",
)
async def handle_manual_snils_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.set_state(CustomerAddStates.waiting_manual_snils)
    await callback.message.answer("Введите номер СНИЛС в формате 123-456-789 00.")
    await callback.answer()


@router.message(StateFilter(CustomerAddStates.waiting_manual_snils), F.text)
async def handle_manual_snils_value(message: Message, state: FSMContext) -> None:
    ipain = normalize_snils(message.text)
    if not ipain:
        await message.answer("Неверный формат СНИЛС. Пример: 123-456-789 00")
        return

    data = await state.get_data()
    customer_fields = dict(data.get("customer_fields") or {})
    customer_fields["ipain"] = ipain
    await state.update_data(customer_fields=customer_fields)
    await _go_to_tin_step(message, state, customer_fields)


@router.message(StateFilter(CustomerAddStates.waiting_tin), F.document | F.photo)
async def handle_tin_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
    customer_document_recognition_service: CustomerDocumentRecognitionService,
) -> None:
    await message.answer(PROCESSING_FILE_MESSAGE)

    data = await state.get_data()
    customer_fields = dict(data.get("customer_fields") or {})
    response_payload = await _upload_customer_file(message, bot, yandex_disk_client)
    if response_payload is None:
        return

    try:
        tin_fields = await customer_document_recognition_service.recognize_tin(
            response_payload["content"],
            response_payload["mime_type"],
            filename=response_payload["original_filename"],
        )
    except Exception:
        logger.exception("TIN recognition failed")
        await message.answer(
            "Не удалось обработать файл ИНН.",
            reply_markup=_manual_tin_keyboard(),
        )
        return

    if not person_names_match(customer_fields, tin_fields):
        logger.warning(
            "TIN name mismatch: passport_fio=%s, tin_fio=%s",
            format_fio_normalized(customer_fields),
            format_fio_normalized(tin_fields),
        )
        await message.answer(
            "ФИО не совпадает, загрузите соответствующий файл.",
            reply_markup=_manual_tin_keyboard(),
        )
        return

    tin = tin_fields.get("tin")
    if not tin:
        await message.answer(
            "Не удалось распознать ИНН.",
            reply_markup=_manual_tin_keyboard(),
        )
        return

    customer_fields["tin"] = tin
    await state.update_data(customer_fields=customer_fields)
    await state.set_state(CustomerAddStates.waiting_phone)
    await message.answer(
        f"ИНН распознан: {tin}\n\nВведите номер телефона клиента."
    )


@router.callback_query(
    StateFilter(CustomerAddStates.waiting_tin),
    F.data == "add_customer_manual:tin",
)
async def handle_manual_tin_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.set_state(CustomerAddStates.waiting_manual_tin)
    await callback.message.answer("Введите ИНН клиента.")
    await callback.answer()


@router.message(StateFilter(CustomerAddStates.waiting_manual_tin), F.text)
async def handle_manual_tin_value(message: Message, state: FSMContext) -> None:
    tin = normalize_tin(message.text)
    if not tin:
        await message.answer("Неверный формат ИНН.")
        return

    data = await state.get_data()
    customer_fields = dict(data.get("customer_fields") or {})
    customer_fields["tin"] = tin
    await state.update_data(customer_fields=customer_fields)
    await state.set_state(CustomerAddStates.waiting_phone)
    await message.answer("Введите номер телефона клиента.")


@router.message(StateFilter(CustomerAddStates.waiting_phone), F.text)
async def handle_add_customer_phone(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(
            "Неверный формат телефона. Введите номер в формате +79171234567."
        )
        return

    await state.update_data(phone=phone)
    await state.set_state(CustomerAddStates.waiting_email)
    await message.answer("Введите email клиента.")


@router.message(StateFilter(CustomerAddStates.waiting_email), F.text)
async def handle_add_customer_email(
    message: Message,
    state: FSMContext,
    database: Database,
    bot: Bot,
) -> None:
    email = (message.text or "").strip()
    if not validate_email(email):
        await message.answer(
            "Неверный формат email. Введите email в формате name@example.com."
        )
        return

    data = await state.get_data()
    customer_fields = dict(data.get("customer_fields") or {})
    passport = customer_fields.get("passport")
    phone = data.get("phone")
    if not passport or not customer_fields.get("first_name") or not customer_fields.get("last_name"):
        await message.answer("Не хватает обязательных данных. Начните с /add_customer")
        await state.clear()
        return
    if not phone:
        await message.answer("Телефон не указан. Начните с /add_customer")
        await state.clear()
        return

    existing = await database.find_customer_by_passport(passport)
    if existing:
        await state.clear()
        is_admin = await is_admin_for_customer_management(
            bot, message.chat.id, message.from_user.id if message.from_user else 0
        )
        has_estimate = await customer_has_estimate(existing, database)
        await message.answer(
            "Клиент с таким паспортом уже существует.\n\n"
            + await build_customer_card(existing, database),
            reply_markup=build_customer_card_keyboard(
                existing,
                is_admin=is_admin,
                has_estimate=has_estimate,
            ),
        )
        return

    customer_data = {
        **customer_fields,
        "phone": phone,
        "email": email,
        "specification_id": None,
    }
    customer = await database.create_customer(customer_data)
    await state.clear()

    is_admin = await is_admin_for_customer_management(
        bot, message.chat.id, message.from_user.id if message.from_user else 0
    )
    has_estimate = await customer_has_estimate(customer, database)
    await message.answer(
        "✅ Клиент добавлен\n\n" + await build_customer_card(customer, database),
        reply_markup=build_customer_card_keyboard(
            customer,
            is_admin=is_admin,
            has_estimate=has_estimate,
        ),
    )


async def _go_to_snils_step(
    message: Message,
    state: FSMContext,
    customer_fields: dict,
) -> None:
    await state.set_state(CustomerAddStates.waiting_snils)
    await message.answer(
        "Адрес регистрации распознан:\n\n"
        f"{customer_fields.get('registration_address')}\n\n"
        f"Загрузите файл СНИЛС для клиента:\n"
        f"{format_customer_fio(customer_fields)}\n\n"
        f"Паспортные данные:\n"
        f"{customer_fields.get('passport')}, выдан {customer_fields.get('by_whom_issued') or '—'}\n"
        f"Дата выдачи: {customer_fields.get('date_issue') or '—'}, "
        f"код подразделения: {customer_fields.get('department_code') or '—'}"
    )


async def _go_to_tin_step(
    message: Message,
    state: FSMContext,
    customer_fields: dict,
) -> None:
    await state.set_state(CustomerAddStates.waiting_tin)
    await message.answer(
        f"СНИЛС распознан: {customer_fields.get('ipain')}\n\n"
        f"Загрузите файл ИНН для клиента {format_customer_fio(customer_fields)}."
    )


async def _upload_customer_file(
    message: Message,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
) -> dict | None:
    if message.document is not None:
        file_id = message.document.file_id
        original_filename = get_original_filename(message.document)
        mime_type = message.document.mime_type
    elif message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        original_filename = f"{message.message_id}_photo.jpg"
        mime_type = "image/jpeg"
    else:
        return None

    if not is_supported_file(original_filename):
        await message.reply(
            "Формат файла не поддерживается. Поддерживаются: "
            "pdf, jpg, jpeg, png, webp, heic, docx, xlsx."
        )
        return None

    user = message.from_user
    if user is None:
        await message.reply("Не удалось определить пользователя Telegram.")
        return None

    try:
        telegram_file = await bot.get_file(file_id)
        if telegram_file.file_path is None:
            raise RuntimeError("Telegram did not return a file path")
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        file_content = buffer.getvalue()
    except Exception:
        logger.exception("Failed to download customer file")
        await message.reply("Не удалось скачать файл из Telegram.")
        return None

    stored_filename = build_stored_filename(message.message_id, original_filename)
    disk_path = yandex_disk_client.build_customer_intake_file_path(
        telegram_user_id=user.id,
        file_name=stored_filename,
    )
    try:
        uploaded_path = await yandex_disk_client.upload_bytes(disk_path, file_content)
    except Exception:
        logger.exception("Failed to upload customer file to Yandex Disk")
        await message.reply("Не удалось сохранить файл в Yandex Disk.")
        return None

    return {
        "path": uploaded_path,
        "original_filename": original_filename,
        "mime_type": mime_type,
        "content": file_content,
    }


def _manual_registration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ввести вручную",
                    callback_data="add_customer_manual:registration",
                )
            ]
        ]
    )


def _manual_snils_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ввести вручную",
                    callback_data="add_customer_manual:snils",
                )
            ]
        ]
    )


def _manual_tin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ввести вручную",
                    callback_data="add_customer_manual:tin",
                )
            ]
        ]
    )
