from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import Database
from app.services.customer_card_service import format_customer_fio
from app.services.validation_service import normalize_passport
from app.states.customer_states import CustomerDeleteStates


router = Router(name="customer_delete")

START_MENU_TEXT = (
    "Команды:\n"
    "- /add_customer — добавить клиента по паспорту, СНИЛС, ИНН\n"
    "- /add_specification — добавить желаемый автомобиль клиента\n"
    "- /search_edit_customer — найти или изменить клиента"
)


@router.message(Command("delete_customer"))
async def handle_delete_customer_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CustomerDeleteStates.waiting_passport)
    await message.answer("Введите номер паспорта в формате: 80 00 000000")


@router.message(StateFilter(CustomerDeleteStates.waiting_passport), F.text)
async def handle_delete_customer_passport(
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
        await message.answer("Клиент не найден")
        await state.clear()
        return

    await state.set_state(CustomerDeleteStates.waiting_confirmation)
    await state.update_data(customer_id=int(customer["id"]))
    await message.answer(
        "Найден клиент:\n\n"
        f"ФИО: {format_customer_fio(customer)}\n"
        f"Телефон: {customer.get('phone') or '—'}\n"
        f"Email: {customer.get('email') or '—'}\n\n"
        "Удалить пользователя?",
        reply_markup=_delete_confirmation_keyboard(int(customer["id"])),
    )


@router.callback_query(
    StateFilter(CustomerDeleteStates.waiting_confirmation),
    F.data.startswith("delete_customer:yes:"),
)
async def handle_delete_customer_confirm_yes(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    if callback.message is None:
        return

    try:
        customer_id = int(callback.data.split(":")[-1])
    except (TypeError, ValueError):
        await callback.answer("Некорректная команда", show_alert=True)
        return

    data = await state.get_data()
    expected_customer_id = data.get("customer_id")
    if expected_customer_id is None or int(expected_customer_id) != customer_id:
        await state.clear()
        await callback.message.answer("Сессия устарела. Начните с /delete_customer")
        await callback.answer()
        return

    deleted = await database.delete_customer_with_related_data(customer_id)
    await state.clear()
    if deleted:
        await callback.message.answer("✅ Клиент удалён")
    else:
        await callback.message.answer("Не удалось удалить клиента")
    await callback.answer()


@router.callback_query(
    StateFilter(CustomerDeleteStates.waiting_confirmation),
    F.data.startswith("delete_customer:no:"),
)
async def handle_delete_customer_confirm_no(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        return

    await state.clear()
    await callback.message.answer(START_MENU_TEXT)
    await callback.answer()


def _delete_confirmation_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да",
                    callback_data=f"delete_customer:yes:{customer_id}",
                ),
                InlineKeyboardButton(
                    text="Нет",
                    callback_data=f"delete_customer:no:{customer_id}",
                ),
            ]
        ]
    )
