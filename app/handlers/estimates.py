from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database import Database
from app.services.estimate_service import (
    create_estimate,
    format_estimate_summary,
    get_missing_specification_fields_for_estimate,
    parse_positive_decimal,
)
from app.states.estimate_states import EstimateStates


router = Router(name="estimates")

INVALID_NUMBER_REPLY = "Введите число, например 110"
EXCHANGE_RATE_PROMPT = (
    "Введите курс валюты к рублю. Например: 12.1 для CNY или 0.57 для JPY."
)


@router.callback_query(F.data.startswith("estimate:create:"))
async def handle_estimate_create_start(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    if callback.message is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 3 or parts[0] != "estimate" or parts[1] != "create":
        await callback.answer("Некорректная команда сметы", show_alert=True)
        return

    try:
        customer_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный ID клиента", show_alert=True)
        return

    customer = await database.get_customer_by_id(customer_id)
    if not customer:
        await callback.message.answer("Клиент не найден.")
        await callback.answer()
        return

    specification_id = customer.get("specification_id")
    if not specification_id:
        await callback.message.answer(
            "У клиента нет спецификации авто. Сначала добавьте спецификацию."
        )
        await callback.answer()
        return

    specification = await database.get_specification_by_id(int(specification_id))
    if not specification:
        await callback.message.answer("Спецификация клиента не найдена.")
        await callback.answer()
        return

    missing_fields = get_missing_specification_fields_for_estimate(specification)
    if missing_fields:
        await callback.message.answer(
            "Для создания сметы не хватает данных спецификации: "
            + ", ".join(missing_fields)
        )
        await callback.answer()
        return

    await state.clear()
    await state.update_data(
        customer_id=customer_id,
        specification_id=int(specification_id),
    )
    await state.set_state(EstimateStates.waiting_engine_power)
    await callback.message.answer("Введите мощность автомобиля в л.с.")
    await callback.answer()


@router.message(StateFilter(EstimateStates.waiting_engine_power), F.text)
async def handle_estimate_engine_power(message: Message, state: FSMContext) -> None:
    engine_power = parse_positive_decimal(message.text)
    if engine_power is None:
        await message.answer(INVALID_NUMBER_REPLY)
        return

    await state.update_data(engine_power=str(engine_power))
    await state.set_state(EstimateStates.waiting_exchange_rate)
    await message.answer(EXCHANGE_RATE_PROMPT)


@router.message(StateFilter(EstimateStates.waiting_exchange_rate), F.text)
async def handle_estimate_exchange_rate(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    exchange_rate = parse_positive_decimal(message.text)
    if exchange_rate is None:
        await message.answer(INVALID_NUMBER_REPLY)
        return

    data = await state.get_data()
    customer_id = data.get("customer_id")
    specification_id = data.get("specification_id")
    engine_power_text = data.get("engine_power")
    if not customer_id or not specification_id or not engine_power_text:
        await state.clear()
        await message.answer("Сессия устарела. Начните создание сметы заново.")
        return

    engine_power = parse_positive_decimal(engine_power_text)
    if engine_power is None:
        await state.clear()
        await message.answer("Сессия устарела. Начните создание сметы заново.")
        return

    specification = await database.get_specification_by_id(int(specification_id))
    if not specification:
        await state.clear()
        await message.answer("Спецификация клиента не найдена.")
        return

    try:
        estimate = await create_estimate(
            database,
            customer_id=int(customer_id),
            specification_id=int(specification_id),
            specification=specification,
            engine_power=engine_power,
            exchange_rate=exchange_rate,
        )
    except ValueError as error:
        await message.answer(str(error))
        return
    except Exception:
        await message.answer("Не удалось создать смету.")
        return

    await state.clear()
    await message.answer(format_estimate_summary(estimate, specification))
