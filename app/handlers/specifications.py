from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import Database
from app.handlers.customers import (
    is_admin_for_customer_management,
)
from app.services.contract_generation_service import (
    CustomerNotFoundError,
    CustomerSpecificationMissingError,
    SpecificationNotFoundError,
    TemplateNotFoundError,
    generate_customer_contract_docx,
)
from app.services.customer_card_service import (
    build_customer_card,
    build_customer_card_keyboard,
)
from app.services.specification_edit_service import (
    SPEC_FIELD_LABELS,
    build_specification_edit_keyboard,
    format_specification_text,
    parse_specification_field_value,
)
from app.services.validation_service import normalize_passport
from app.states.customer_states import CustomerEditStates
from app.states.specification_states import SpecificationAddStates, SpecificationEditStates


router = Router(name="specifications")
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("customer_generate_contract:"))
async def handle_generate_contract(
    callback: CallbackQuery,
    database: Database,
) -> None:
    if callback.message is None:
        return

    customer_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    await callback.message.answer("Формирую договор...")

    try:
        estimate = await database.get_estimate_by_customer_id(customer_id)
        file_path = await generate_customer_contract_docx(
            customer_id,
            database,
            estimate=estimate,
        )
        await callback.message.answer_document(
            FSInputFile(file_path),
            caption="Готово. Договор сформирован.",
        )
    except CustomerNotFoundError:
        await callback.message.answer("Клиент не найден.")
    except CustomerSpecificationMissingError:
        await callback.message.answer(
            "У клиента нет спецификации авто. Сначала добавьте спецификацию."
        )
    except SpecificationNotFoundError:
        await callback.message.answer("Спецификация клиента не найдена.")
    except TemplateNotFoundError as error:
        await callback.message.answer(str(error))
    except Exception as error:
        logger.exception("Contract generation failed for customer_id=%s", customer_id)
        await callback.message.answer(f"Не удалось сформировать договор: {error}")


@router.callback_query(F.data.startswith("customer_add_spec:"))
async def handle_customer_add_spec(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    if callback.message is None:
        return

    customer_id = int(callback.data.split(":", 1)[1])
    customer = await database.get_customer_by_id(customer_id)
    if not customer:
        await callback.message.answer("Клиент не найден")
        await callback.answer()
        return
    if customer.get("specification_id"):
        await callback.message.answer("У клиента уже есть спецификация.")
        await callback.answer()
        return

    await state.update_data(
        customer_id=customer_id,
        existing_specification_id=None,
        specification_fields={},
    )
    await state.set_state(SpecificationAddStates.waiting_brand)
    await callback.message.answer("Марка")
    await callback.answer()


@router.callback_query(F.data.startswith("customer_edit_spec:"))
async def handle_customer_edit_spec_public(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    if callback.message is None:
        return
    customer_id = int(callback.data.split(":", 1)[1])
    await _open_specification_edit_menu(callback, state, database, customer_id)
    await callback.answer()


@router.callback_query(F.data.startswith("edit_customer_specification:"))
async def handle_edit_customer_specification(
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
    await _open_specification_edit_menu(callback, state, database, customer_id)
    await callback.answer()


async def _open_specification_edit_menu(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    customer_id: int,
) -> None:
    if callback.message is None:
        return

    customer = await database.get_customer_by_id(customer_id)
    if not customer:
        await callback.message.answer("Клиент не найден")
        return

    created = False
    if not customer.get("specification_id"):
        specification_id = await database.create_empty_specification()
        customer = await database.attach_specification_to_customer(
            customer_id, specification_id
        )
        created = True

    specification = await database.get_specification_by_id(
        int(customer["specification_id"])
    )
    if specification is None:
        await callback.message.answer("Не удалось загрузить спецификацию")
        return

    await state.set_state(CustomerEditStates.choosing_field)
    await state.update_data(
        customer_id=customer_id,
        specification_id=int(specification["id"]),
    )

    if created:
        await callback.message.answer(
            "У клиента ещё не было спецификации. "
            "Я создал пустую спецификацию, теперь можно заполнить поля."
        )

    await callback.message.answer(
        format_specification_text(specification),
        reply_markup=build_specification_edit_keyboard(
            int(specification["id"]),
            customer_id,
        ),
    )


@router.callback_query(F.data.startswith("spec_edit_back:"))
async def handle_spec_edit_back(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    database: Database,
) -> None:
    if callback.message is None or callback.from_user is None:
        return

    customer_id = int(callback.data.split(":", 1)[1])
    customer = await database.get_customer_by_id(customer_id)
    if not customer:
        await callback.message.answer("Клиент не найден")
        await callback.answer()
        return

    is_admin = await is_admin_for_customer_management(
        bot, callback.message.chat.id, callback.from_user.id
    )
    await state.set_state(CustomerEditStates.choosing_action)
    await state.update_data(customer_id=customer_id)
    await callback.message.answer(
        await build_customer_card(customer, database),
        reply_markup=await build_customer_card_keyboard(
            customer, database, is_admin=is_admin
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_spec_field:"))
async def handle_edit_spec_field(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    if callback.message is None:
        return

    _, specification_id_str, field_name = callback.data.split(":", 2)
    specification_id = int(specification_id_str)

    data = await state.get_data()
    customer_id = data.get("customer_id")
    if not customer_id:
        await callback.message.answer(
            "Сессия редактирования устарела. Начните с /search_edit_customer"
        )
        await callback.answer()
        return

    customer = await database.get_customer_by_id(int(customer_id))
    if not customer or int(customer.get("specification_id") or 0) != specification_id:
        await callback.message.answer("Спецификация не принадлежит этому клиенту")
        await callback.answer()
        return

    if field_name not in SPEC_FIELD_LABELS:
        await callback.answer("Неизвестное поле", show_alert=True)
        return

    await state.set_state(SpecificationEditStates.waiting_new_value)
    await state.update_data(
        customer_id=int(customer_id),
        specification_id=specification_id,
        field_name=field_name,
    )
    await callback.message.answer(
        f"Введите новое значение для поля: {SPEC_FIELD_LABELS[field_name]}"
    )
    await callback.answer()


@router.message(StateFilter(SpecificationEditStates.waiting_new_value), F.text)
async def handle_specification_edit_value(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    data = await state.get_data()
    customer_id = data.get("customer_id")
    specification_id = data.get("specification_id")
    field_name = data.get("field_name")
    if not customer_id or not specification_id or not field_name:
        await message.answer("Сессия редактирования устарела. Начните с /search_edit_customer")
        await state.clear()
        return

    value, error = parse_specification_field_value(field_name, message.text)
    if error:
        await message.answer(error)
        return

    await database.update_specification(int(specification_id), field_name, value)
    specification = await database.get_specification_by_id(int(specification_id))
    if specification is None:
        await message.answer("Не удалось загрузить обновлённую спецификацию")
        await state.clear()
        return

    await state.set_state(CustomerEditStates.choosing_field)
    await state.update_data(
        customer_id=customer_id,
        specification_id=specification_id,
    )
    await message.answer(
        f"✅ Поле обновлено: {SPEC_FIELD_LABELS[field_name]} = {value}"
    )
    await message.answer(
        format_specification_text(specification),
        reply_markup=build_specification_edit_keyboard(
            int(specification_id),
            int(customer_id),
        ),
    )


@router.message(Command("add_specification"))
async def handle_add_specification(message: Message, state: FSMContext) -> None:
    await state.set_state(SpecificationAddStates.waiting_passport)
    await state.update_data(specification_fields={})
    await message.answer("Введите номер паспорта клиента в формате 80 00 000000")


@router.message(StateFilter(SpecificationAddStates.waiting_passport), F.text)
async def handle_specification_passport(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    passport = normalize_passport(message.text)
    if not passport:
        await message.answer("Неверный формат паспорта. Пример: 80 00 000000")
        return

    customer = await database.search_customer_by_passport(passport)
    if not customer:
        await message.answer(
            "Клиент не найден. Сначала добавьте клиента через /add_customer"
        )
        await state.clear()
        return

    await state.update_data(
        customer_id=customer["id"],
        existing_specification_id=customer.get("specification_id"),
    )
    await state.set_state(SpecificationAddStates.waiting_brand)
    await message.answer("Марка")


@router.message(StateFilter(SpecificationAddStates.waiting_brand), F.text)
async def handle_spec_brand(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(message, state, "brand", SpecificationAddStates.waiting_model, "Модель")


@router.message(StateFilter(SpecificationAddStates.waiting_model), F.text)
async def handle_spec_model(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(message, state, "model", SpecificationAddStates.waiting_year, "Год выпуска")


@router.message(StateFilter(SpecificationAddStates.waiting_year), F.text)
async def handle_spec_year(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "year", SpecificationAddStates.waiting_eng_capacity, "Объём двигателя"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_eng_capacity), F.text)
async def handle_spec_eng_capacity(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "eng_capacity", SpecificationAddStates.waiting_eng_type, "Тип ДВС"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_eng_type), F.text)
async def handle_spec_eng_type(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "eng_type", SpecificationAddStates.waiting_drive, "Привод"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_drive), F.text)
async def handle_spec_drive(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "drive", SpecificationAddStates.waiting_transmission, "КПП"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_transmission), F.text)
async def handle_spec_transmission(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "transmission", SpecificationAddStates.waiting_color, "Цвет"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_color), F.text)
async def handle_spec_color(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "color", SpecificationAddStates.waiting_complectation, "Комплектация"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_complectation), F.text)
async def handle_spec_complectation(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "complectation", SpecificationAddStates.waiting_mileage, "Пробег"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_mileage), F.text)
async def handle_spec_mileage(message: Message, state: FSMContext) -> None:
    await _save_spec_field_and_ask_next(
        message, state, "mileage", SpecificationAddStates.waiting_price, "Бюджет / стоимость"
    )


@router.message(StateFilter(SpecificationAddStates.waiting_price), F.text)
async def handle_spec_price(
    message: Message,
    state: FSMContext,
    database: Database,
    bot: Bot,
) -> None:
    await _save_spec_field(message, state, "price")
    data = await state.get_data()
    existing_spec_id = data.get("existing_specification_id")
    if existing_spec_id:
        await state.set_state(SpecificationAddStates.confirm_replace)
        await message.answer(
            f"У клиента уже есть спецификация #{existing_spec_id}. Заменить?",
            reply_markup=_build_replace_keyboard(),
        )
        return

    await _finalize_specification(message, state, database, bot)


@router.callback_query(
    StateFilter(SpecificationAddStates.confirm_replace),
    F.data == "spec_replace:yes",
)
async def handle_spec_replace_yes(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    bot: Bot,
) -> None:
    if callback.message is None:
        return
    await _finalize_specification(callback.message, state, database, bot)
    await callback.answer()


@router.callback_query(
    StateFilter(SpecificationAddStates.confirm_replace),
    F.data == "spec_replace:no",
)
async def handle_spec_replace_no(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is not None:
        await callback.message.answer("Операция отменена.")
    await callback.answer()
    await state.clear()


async def _save_spec_field_and_ask_next(
    message: Message,
    state: FSMContext,
    field_name: str,
    next_state: object,
    next_prompt: str,
) -> None:
    await _save_spec_field(message, state, field_name)
    await state.set_state(next_state)
    await message.answer(next_prompt)


async def _save_spec_field(message: Message, state: FSMContext, field_name: str) -> None:
    data = await state.get_data()
    fields = dict(data.get("specification_fields") or {})
    value = (message.text or "").strip()
    fields[field_name] = value or None
    await state.update_data(specification_fields=fields)


async def _finalize_specification(
    message: Message,
    state: FSMContext,
    database: Database,
    bot: Bot | None = None,
) -> None:
    data = await state.get_data()
    customer_id = data.get("customer_id")
    specification_fields = data.get("specification_fields") or {}
    if not customer_id:
        await message.answer("Сессия устарела. Начните с /add_specification")
        await state.clear()
        return

    specification_id = await database.create_specification(specification_fields)
    customer = await database.attach_specification_to_customer(customer_id, specification_id)
    await state.clear()

    is_admin = False
    if bot is not None and message.from_user is not None:
        is_admin = await is_admin_for_customer_management(
            bot, message.chat.id, message.from_user.id
        )

    await message.answer(
        "✅ Спецификация добавлена\n\n" + await build_customer_card(customer, database),
        reply_markup=await build_customer_card_keyboard(
            customer, database, is_admin=is_admin
        ),
    )


def _build_replace_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Заменить", callback_data="spec_replace:yes"),
                InlineKeyboardButton(text="Отмена", callback_data="spec_replace:no"),
            ]
        ]
    )
