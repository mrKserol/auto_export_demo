from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import Database
from app.services.customer_card_service import build_customer_card
from app.services.customer_extraction_service import (
    extract_customer_fields_from_response,
    merge_customer_fields,
)
from app.services.file_service import (
    build_stored_filename,
    get_original_filename,
    is_supported_file,
)
from app.services.validation_service import (
    normalize_date,
    normalize_department_code,
    normalize_passport,
    normalize_phone,
    normalize_snils,
    normalize_specification_id,
    normalize_tin,
    validate_email,
    validate_issued_by,
    validate_name,
    validate_registration_address,
)
from app.states.customer_states import CustomerAddStates, CustomerEditStates
from app.yadisk_client import YandexDiskClient
from app.yandex_function_client import YandexFunctionClient


router = Router(name="customers")
logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "passport": "Изменить номер паспорта",
    "first_name": "Изменить Имя",
    "last_name": "Изменить Фамилию",
    "surname": "Изменить Отчество",
    "by_whom_issued": "Изменить кем выдан",
    "date_issue": "Изменить дату выдачи",
    "department_code": "Изменить код подразделения",
    "registration_address": "Изменить адрес регистрации",
    "ipain": "Изменить СНИЛС",
    "tin": "Изменить ИНН",
    "phone": "Изменить Номер телефона",
    "email": "Изменить email",
    "specification_id": "Изменить specification_id",
}


@router.message(Command("add_customer"))
async def handle_add_customer(message: Message, state: FSMContext) -> None:
    await state.set_state(CustomerAddStates.collecting_files)
    await state.update_data(
        uploaded_files=[],
        customer_fields={},
        warnings=[],
    )
    await message.answer(
        "Загрузите файлы паспорта с главной страницей и регистрацией, "
        "СНИЛС, ИНН. Когда загрузите все файлы, отправьте /done_customer_files."
    )


@router.message(
    StateFilter(CustomerAddStates.collecting_files),
    Command("done_customer_files"),
)
async def handle_done_customer_files(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    uploaded_files = data.get("uploaded_files") or []
    if not uploaded_files:
        await message.answer("Сначала загрузите хотя бы один файл.")
        return

    await state.set_state(CustomerAddStates.waiting_phone)
    await message.answer("Введите номер телефона")


@router.message(StateFilter(CustomerAddStates.collecting_files), F.document)
async def handle_customer_document(
    message: Message,
    state: FSMContext,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
    yandex_function_client: YandexFunctionClient | None,
    enable_processing: bool,
) -> None:
    document = message.document
    if document is None:
        return
    await _process_customer_file(
        message=message,
        state=state,
        bot=bot,
        yandex_disk_client=yandex_disk_client,
        yandex_function_client=yandex_function_client,
        enable_processing=enable_processing,
        file_id=document.file_id,
        original_filename=get_original_filename(document),
        mime_type=document.mime_type,
    )


@router.message(StateFilter(CustomerAddStates.collecting_files), F.photo)
async def handle_customer_photo(
    message: Message,
    state: FSMContext,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
    yandex_function_client: YandexFunctionClient | None,
    enable_processing: bool,
) -> None:
    photo = message.photo[-1]
    await _process_customer_file(
        message=message,
        state=state,
        bot=bot,
        yandex_disk_client=yandex_disk_client,
        yandex_function_client=yandex_function_client,
        enable_processing=enable_processing,
        file_id=photo.file_id,
        original_filename=f"{message.message_id}_photo.jpg",
        mime_type="image/jpeg",
    )


@router.message(StateFilter(CustomerAddStates.waiting_phone), F.text)
async def handle_customer_phone(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(
            "Неверный формат телефона. Введите номер в формате +79171234567"
        )
        return

    await state.update_data(phone=phone)
    await state.set_state(CustomerAddStates.waiting_email)
    await message.answer("Введите email")


@router.message(StateFilter(CustomerAddStates.waiting_email), F.text)
async def handle_customer_email(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    email = (message.text or "").strip()
    if not validate_email(email):
        await message.answer("Неверный формат email. Введите адрес вида example@mail.ru")
        return

    data = await state.get_data()
    customer_fields = data.get("customer_fields") or {}
    passport = customer_fields.get("passport")
    if not passport:
        await message.answer(
            "Не удалось определить паспорт из загруженных файлов. "
            "Попробуйте /add_customer заново."
        )
        await state.clear()
        return

    existing = await database.find_customer_by_passport(passport)
    if existing:
        await message.answer("Клиент с таким паспортом уже существует")
        await state.clear()
        return

    customer_data = {
        "passport": passport,
        "first_name": customer_fields.get("first_name"),
        "last_name": customer_fields.get("last_name"),
        "surname": customer_fields.get("surname"),
        "tin": customer_fields.get("tin"),
        "ipain": customer_fields.get("ipain"),
        "by_whom_issued": customer_fields.get("by_whom_issued"),
        "date_issue": customer_fields.get("date_issue"),
        "registration_address": customer_fields.get("registration_address"),
        "department_code": customer_fields.get("department_code"),
        "phone": data.get("phone"),
        "email": email,
        "specification_id": None,
    }
    customer = await database.create_customer(customer_data)
    await state.clear()
    await message.answer(
        "✅ Клиент добавлен\n\n" + await build_customer_card(customer, database)
    )


@router.message(Command("search_edit_customer"))
async def handle_search_edit_customer(message: Message, state: FSMContext) -> None:
    await state.set_state(CustomerEditStates.waiting_passport)
    await message.answer("Введите номер паспорта в формате: 80 00 000000")


@router.message(StateFilter(CustomerEditStates.waiting_passport), F.text)
async def handle_search_passport(
    message: Message,
    state: FSMContext,
    bot: Bot,
    database: Database,
) -> None:
    passport = normalize_passport(message.text)
    if not passport:
        await message.answer("Неверный формат паспорта. Пример: 80 00 000000")
        return

    customer = await database.search_customer_by_passport(passport)
    if not customer:
        await message.answer("Клиент не найден")
        await state.clear()
        return

    await state.set_state(CustomerEditStates.choosing_action)
    await state.update_data(customer_id=customer["id"])

    can_edit = await _can_edit_customer(bot, message.chat.id, message.from_user.id)
    reply_markup = _build_customer_action_keyboard(customer["id"]) if can_edit else None
    await message.answer(await build_customer_card(customer, database), reply_markup=reply_markup)


@router.callback_query(F.data.startswith("customer_delete:"))
async def handle_customer_delete(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    database: Database,
) -> None:
    if callback.message is None or callback.from_user is None:
        return

    can_edit = await _can_edit_customer(
        bot, callback.message.chat.id, callback.from_user.id
    )
    if not can_edit:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    customer_id = int(callback.data.split(":", 1)[1])
    contract_count = await database.count_contracts_for_customer(customer_id)
    if contract_count > 0:
        await callback.message.answer(
            "У клиента есть контракты, операция отклонена, "
            "удалите контракты или свяжитесь с администратором"
        )
        await callback.answer()
        await state.clear()
        return

    deleted = await database.delete_customer_if_no_contracts(customer_id)
    if deleted:
        await callback.message.answer("✅ Клиент удалён")
    else:
        await callback.message.answer("Не удалось удалить клиента")
    await callback.answer()
    await state.clear()


@router.callback_query(F.data.startswith("customer_edit:"))
async def handle_customer_edit_menu(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    if callback.message is None or callback.from_user is None:
        return

    can_edit = await _can_edit_customer(
        bot, callback.message.chat.id, callback.from_user.id
    )
    if not can_edit:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    customer_id = int(callback.data.split(":", 1)[1])
    await state.set_state(CustomerEditStates.choosing_field)
    await state.update_data(customer_id=customer_id)
    await callback.message.answer(
        "Выберите поле для изменения:",
        reply_markup=_build_customer_field_keyboard(customer_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("customer_edit_field:"))
async def handle_customer_edit_field(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    if callback.message is None or callback.from_user is None:
        return

    can_edit = await _can_edit_customer(
        bot, callback.message.chat.id, callback.from_user.id
    )
    if not can_edit:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    _, customer_id_str, field_name = callback.data.split(":", 2)
    await state.set_state(CustomerEditStates.waiting_new_value)
    await state.update_data(customer_id=int(customer_id_str), edit_field=field_name)
    await callback.message.answer("Введите новые данные")
    await callback.answer()


@router.message(StateFilter(CustomerEditStates.waiting_new_value), F.text)
async def handle_customer_new_value(
    message: Message,
    state: FSMContext,
    database: Database,
    bot: Bot,
) -> None:
    data = await state.get_data()
    customer_id = data.get("customer_id")
    field_name = data.get("edit_field")
    if not customer_id or not field_name:
        await message.answer("Сессия редактирования устарела. Начните с /search_edit_customer")
        await state.clear()
        return

    if field_name == "specification_id":
        spec_id = normalize_specification_id(message.text)
        if spec_id is None and (message.text or "").strip().lower() not in {
            "",
            "-",
            "null",
            "none",
        }:
            await message.answer(
                "Неверный specification_id. Введите положительное число, "
                "или «-» для очистки."
            )
            return
        if spec_id is not None:
            specification = await database.get_specification_by_id(spec_id)
            if not specification:
                await message.answer("Спецификация с таким ID не найдена")
                return
        normalized = spec_id
    else:
        normalized, error = _validate_customer_field(field_name, message.text)
        if error:
            await message.answer(error)
            return

    if field_name == "passport":
        existing = await database.find_customer_by_passport(normalized)
        if existing and int(existing["id"]) != int(customer_id):
            await message.answer("Клиент с таким паспортом уже существует")
            return

    customer = await database.update_customer(customer_id, field_name, normalized)
    can_edit = await _can_edit_customer(bot, message.chat.id, message.from_user.id)
    reply_markup = _build_customer_action_keyboard(customer["id"]) if can_edit else None
    await message.answer(await build_customer_card(customer, database), reply_markup=reply_markup)
    await state.set_state(CustomerEditStates.choosing_action)


async def _process_customer_file(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    yandex_disk_client: YandexDiskClient,
    yandex_function_client: YandexFunctionClient | None,
    enable_processing: bool,
    file_id: str,
    original_filename: str,
    mime_type: str | None,
) -> None:
    if not is_supported_file(original_filename):
        await message.reply(
            "Формат файла не поддерживается. Поддерживаются: "
            "pdf, jpg, jpeg, png, webp, heic, docx, xlsx."
        )
        return

    user = message.from_user
    if user is None:
        await message.reply("Не удалось определить пользователя Telegram.")
        return

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
        return

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
        return

    data = await state.get_data()
    uploaded_files = list(data.get("uploaded_files") or [])
    uploaded_files.append(
        {
            "path": uploaded_path,
            "original_filename": original_filename,
            "stored_filename": stored_filename,
        }
    )

    customer_fields = dict(data.get("customer_fields") or {})
    warnings = list(data.get("warnings") or [])
    extraction_note = ""

    if enable_processing and yandex_function_client is not None:
        try:
            response_payload = await yandex_function_client.process_document(
                document_id=0,
                file_path=uploaded_path,
                original_filename=original_filename,
                mime_type=mime_type,
                telegram_chat_id=message.chat.id,
                telegram_message_id=message.message_id,
            )
            extracted = extract_customer_fields_from_response(response_payload)
            customer_fields, new_warnings = merge_customer_fields(customer_fields, extracted)
            warnings.extend(new_warnings)
        except Exception:
            logger.exception("Yandex Function extraction failed for customer file")
            extraction_note = " (извлечение данных не удалось)"
    elif enable_processing:
        extraction_note = " (автообработка не настроена)"

    await state.update_data(
        uploaded_files=uploaded_files,
        customer_fields=customer_fields,
        warnings=warnings,
    )

    found_parts = []
    for label, key in (
        ("паспорт", "passport"),
        ("СНИЛС", "ipain"),
        ("ИНН", "tin"),
        ("дата выдачи", "date_issue"),
        ("код подразделения", "department_code"),
    ):
        value = customer_fields.get(key)
        if value:
            found_parts.append(f"{label} {value}")

    found_text = ", ".join(found_parts) if found_parts else "данные пока не найдены"
    await message.reply(f"Файл принят{extraction_note}. Найдено: {found_text}")


def _build_customer_action_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить данные",
                    callback_data=f"customer_edit:{customer_id}",
                ),
                InlineKeyboardButton(
                    text="Удалить клиента",
                    callback_data=f"customer_delete:{customer_id}",
                ),
            ]
        ]
    )


def _build_customer_field_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=label,
            callback_data=f"customer_edit_field:{customer_id}:{field_name}",
        )
        for field_name, label in FIELD_LABELS.items()
    ]
    rows = [[button] for button in buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _validate_customer_field(field_name: str, raw_value: str) -> tuple[str | int | None, str | None]:
    text = (raw_value or "").strip()
    if field_name == "passport":
        value = normalize_passport(text)
        if not value:
            return None, "Неверный формат паспорта. Пример: 80 00 000000"
        return value, None
    if field_name in {"first_name", "last_name", "surname"}:
        if not validate_name(text):
            return None, "Неверный формат имени. Длина 1–80 символов."
        return text, None
    if field_name == "by_whom_issued":
        if not validate_issued_by(text):
            return None, "Неверный формат. Длина 1–300 символов."
        return text, None
    if field_name == "date_issue":
        value = normalize_date(text)
        if not value:
            return None, "Неверный формат даты. Пример: 01.01.2020"
        return value, None
    if field_name == "department_code":
        value = normalize_department_code(text)
        if not value:
            return None, "Неверный формат кода подразделения. Пример: 000-000"
        return value, None
    if field_name == "registration_address":
        if not validate_registration_address(text):
            return None, "Неверный адрес. Длина 1–500 символов."
        return text, None
    if field_name == "ipain":
        value = normalize_snils(text)
        if not value:
            return None, "Неверный формат СНИЛС. Пример: 123-456-789 00"
        return value, None
    if field_name == "tin":
        value = normalize_tin(text)
        if not value:
            return None, "Неверный формат ИНН. Пример: 027123456789"
        return value, None
    if field_name == "phone":
        value = normalize_phone(text)
        if not value:
            return None, "Неверный формат телефона. Пример: +79171234567"
        return value, None
    if field_name == "email":
        if not validate_email(text):
            return None, "Неверный формат email."
        return text, None
    return None, "Неизвестное поле"


async def _can_edit_customer(bot: Bot, chat_id: int, user_id: int) -> bool:
    # В личном чате с ботом разрешаем редактирование
    if chat_id > 0:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"administrator", "creator"}
    except Exception:
        logger.exception("Failed to check chat admin status")
        return False
