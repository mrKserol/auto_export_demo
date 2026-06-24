from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import Database
from app.services.customer_card_service import build_specification_created_reply
from app.services.validation_service import normalize_passport
from app.states.specification_states import SpecificationAddStates


router = Router(name="specifications")


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

    await _finalize_specification(message, state, database)


@router.callback_query(
    StateFilter(SpecificationAddStates.confirm_replace),
    F.data == "spec_replace:yes",
)
async def handle_spec_replace_yes(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    if callback.message is None:
        return
    await _finalize_specification(callback.message, state, database)
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
    await message.answer(await build_specification_created_reply(customer, database))


def _build_replace_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Заменить", callback_data="spec_replace:yes"),
                InlineKeyboardButton(text="Отмена", callback_data="spec_replace:no"),
            ]
        ]
    )
