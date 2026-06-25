from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import Database
from app.services.customer_card_service import build_customer_card, build_customer_card_keyboard
from app.services.validation_service import (
    normalize_date,
    normalize_department_code,
    normalize_passport,
    normalize_phone,
    normalize_snils,
    normalize_tin,
    validate_email,
    validate_issued_by,
    validate_name,
    validate_registration_address,
)
from app.states.customer_states import CustomerEditStates


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
}


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

    is_admin = await is_admin_for_customer_management(
        bot, message.chat.id, message.from_user.id if message.from_user else 0
    )
    estimate = await database.get_estimate_by_customer_id(int(customer["id"]))
    await message.answer(
        await build_customer_card(customer, database),
        reply_markup=build_customer_card_keyboard(
            customer,
            is_admin=is_admin,
            has_estimate=bool(estimate),
        ),
    )


@router.callback_query(F.data.startswith("customer_delete:"))
async def handle_customer_delete(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    database: Database,
) -> None:
    if callback.message is None or callback.from_user is None:
        return

    if not await is_admin_for_customer_management(
        bot, callback.message.chat.id, callback.from_user.id
    ):
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

    if not await is_admin_for_customer_management(
        bot, callback.message.chat.id, callback.from_user.id
    ):
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


@router.callback_query(F.data.startswith("customer_edit_back:"))
async def handle_customer_edit_back(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    database: Database,
) -> None:
    if callback.message is None or callback.from_user is None:
        return

    if not await is_admin_for_customer_management(
        bot, callback.message.chat.id, callback.from_user.id
    ):
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    customer_id = int(callback.data.split(":", 1)[1])
    customer = await database.get_customer_by_id(customer_id)
    if not customer:
        await callback.message.answer("Клиент не найден")
        await callback.answer()
        return

    await state.set_state(CustomerEditStates.choosing_action)
    await state.update_data(customer_id=customer_id)
    estimate = await database.get_estimate_by_customer_id(customer_id)
    await callback.message.answer(
        await build_customer_card(customer, database),
        reply_markup=build_customer_card_keyboard(
            customer,
            is_admin=await is_admin_for_customer_management(
                bot, callback.message.chat.id, callback.from_user.id
            ),
            has_estimate=bool(estimate),
        ),
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

    if not await is_admin_for_customer_management(
        bot, callback.message.chat.id, callback.from_user.id
    ):
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
    is_admin = await is_admin_for_customer_management(
        bot, message.chat.id, message.from_user.id if message.from_user else 0
    )
    estimate = await database.get_estimate_by_customer_id(int(customer["id"]))
    await message.answer(
        await build_customer_card(customer, database),
        reply_markup=build_customer_card_keyboard(
            customer,
            is_admin=is_admin,
            has_estimate=bool(estimate),
        ),
    )
    await state.set_state(CustomerEditStates.choosing_action)


def _build_customer_field_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=label,
            callback_data=f"customer_edit_field:{customer_id}:{field_name}",
        )
        for field_name, label in FIELD_LABELS.items()
    ]
    buttons.append(
        InlineKeyboardButton(
            text="Изменить спецификацию авто",
            callback_data=f"edit_customer_specification:{customer_id}",
        )
    )
    buttons.append(
        InlineKeyboardButton(
            text="Назад",
            callback_data=f"customer_edit_back:{customer_id}",
        )
    )
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


async def is_admin_for_customer_management(bot: Bot, chat_id: int, user_id: int) -> bool:
    if chat_id > 0:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"administrator", "creator"}
    except Exception:
        logger.exception("Failed to check chat admin status")
        return False


# Backward compatibility for specifications handler imports.
async def _can_edit_customer(bot: Bot, chat_id: int, user_id: int) -> bool:
    return await is_admin_for_customer_management(bot, chat_id, user_id)


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
