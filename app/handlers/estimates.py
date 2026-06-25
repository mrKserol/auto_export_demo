from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.database import Database
from app.services.contract_generation_service import (
    CustomerNotFoundError,
    CustomerSpecificationMissingError,
    SpecificationNotFoundError,
    TemplateNotFoundError,
    generate_customer_contract_docx,
)
from app.services.estimate_service import (
    create_estimate,
    format_estimate_summary,
    get_missing_specification_fields_for_estimate,
    parse_non_negative_decimal,
    parse_positive_decimal,
)
from app.states.estimate_states import EstimateStates


router = Router(name="estimates")
logger = logging.getLogger(__name__)

INVALID_NUMBER_REPLY = "Введите число, например 110"
INVALID_NON_NEGATIVE_REPLY = "Введите положительное число или 0."
EXCHANGE_RATE_PROMPT = (
    "Введите курс валюты к рублю. Например: 12.1 для CNY или 0.57 для JPY."
)
INSPECT_TRANSPORT_PROMPT = "Введите стоимость осмотра и транспортировки в КНР"
TRANSIT_DECLARATION_PROMPT = (
    "Введите стоимость приемки в РК и оформление транзитной декларации "
    "в республике Казахстан."
)
INSURANCE_SHIPMENT_PROMPT = "Введите стоимость страхования и доставки до города назначения"
CUSTOM_CLEARING_PROMPT = "Введите стоимость таможенной очистки"
CONTRACTOR_COMISSION_PROMPT = "Введите комиссию исполнителя"
SESSION_EXPIRED_REPLY = "Сессия устарела. Начните создание сметы заново."
CONTRACT_GENERATION_FAILED_REPLY = (
    "Смета создана, но договор не удалось сформировать. "
    "Проверьте шаблон договора или данные клиента."
)


@router.callback_query(F.data.startswith("estimate_contract:create:"))
async def handle_estimate_contract_create_start(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    await _start_estimate_flow(
        callback,
        state,
        database,
        generate_contract_after_estimate=True,
    )


@router.callback_query(F.data.startswith("estimate:create:"))
async def handle_estimate_create_start(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
) -> None:
    await _start_estimate_flow(
        callback,
        state,
        database,
        generate_contract_after_estimate=False,
    )


async def _start_estimate_flow(
    callback: CallbackQuery,
    state: FSMContext,
    database: Database,
    *,
    generate_contract_after_estimate: bool,
) -> None:
    if callback.message is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] == "":
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
        generate_contract_after_estimate=generate_contract_after_estimate,
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
async def handle_estimate_exchange_rate(message: Message, state: FSMContext) -> None:
    exchange_rate = parse_positive_decimal(message.text)
    if exchange_rate is None:
        await message.answer(INVALID_NUMBER_REPLY)
        return

    await state.update_data(exchange_rate=str(exchange_rate))
    await state.set_state(EstimateStates.waiting_inspect_transport_price)
    await message.answer(INSPECT_TRANSPORT_PROMPT)


@router.message(StateFilter(EstimateStates.waiting_inspect_transport_price), F.text)
async def handle_estimate_inspect_transport_price(message: Message, state: FSMContext) -> None:
    inspect_transport_price = parse_non_negative_decimal(message.text)
    if inspect_transport_price is None:
        await message.answer(INVALID_NON_NEGATIVE_REPLY)
        return

    await state.update_data(inspect_transport_price=str(inspect_transport_price))
    await state.set_state(EstimateStates.waiting_transit_declaration_price)
    await message.answer(TRANSIT_DECLARATION_PROMPT)


@router.message(StateFilter(EstimateStates.waiting_transit_declaration_price), F.text)
async def handle_estimate_transit_declaration_price(message: Message, state: FSMContext) -> None:
    transit_declaration_price = parse_non_negative_decimal(message.text)
    if transit_declaration_price is None:
        await message.answer(INVALID_NON_NEGATIVE_REPLY)
        return

    await state.update_data(transit_declaration_price=str(transit_declaration_price))
    await state.set_state(EstimateStates.waiting_insurance_shipment)
    await message.answer(INSURANCE_SHIPMENT_PROMPT)


@router.message(StateFilter(EstimateStates.waiting_insurance_shipment), F.text)
async def handle_estimate_insurance_shipment(message: Message, state: FSMContext) -> None:
    insurance_shipment = parse_non_negative_decimal(message.text)
    if insurance_shipment is None:
        await message.answer(INVALID_NON_NEGATIVE_REPLY)
        return

    await state.update_data(insurance_shipment=str(insurance_shipment))
    await state.set_state(EstimateStates.waiting_custom_clearing)
    await message.answer(CUSTOM_CLEARING_PROMPT)


@router.message(StateFilter(EstimateStates.waiting_custom_clearing), F.text)
async def handle_estimate_custom_clearing(message: Message, state: FSMContext) -> None:
    custom_clearing = parse_non_negative_decimal(message.text)
    if custom_clearing is None:
        await message.answer(INVALID_NON_NEGATIVE_REPLY)
        return

    await state.update_data(custom_clearing=str(custom_clearing))
    await state.set_state(EstimateStates.waiting_contractor_comission)
    await message.answer(CONTRACTOR_COMISSION_PROMPT)


@router.message(StateFilter(EstimateStates.waiting_contractor_comission), F.text)
async def handle_estimate_contractor_comission(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    contractor_comission = parse_non_negative_decimal(message.text)
    if contractor_comission is None:
        await message.answer(INVALID_NON_NEGATIVE_REPLY)
        return

    data = await state.get_data()
    customer_id = data.get("customer_id")
    specification_id = data.get("specification_id")
    generate_contract_after_estimate = bool(data.get("generate_contract_after_estimate"))
    if not customer_id or not specification_id:
        await state.clear()
        await message.answer(SESSION_EXPIRED_REPLY)
        return

    parsed_values = _parse_fsm_estimate_values(data)
    if parsed_values is None:
        await state.clear()
        await message.answer(SESSION_EXPIRED_REPLY)
        return

    (
        engine_power,
        exchange_rate,
        inspect_transport_price,
        transit_declaration_price,
        insurance_shipment,
        custom_clearing,
    ) = parsed_values

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
            inspect_transport_price=inspect_transport_price,
            transit_declaration_price=transit_declaration_price,
            insurance_shipment=insurance_shipment,
            custom_clearing=custom_clearing,
            contractor_comission=contractor_comission,
        )
    except ValueError as error:
        await message.answer(str(error))
        return
    except Exception:
        await message.answer("Не удалось создать смету.")
        return

    await state.clear()

    if generate_contract_after_estimate:
        await _send_estimate_with_contract(
            message,
            customer_id=int(customer_id),
            database=database,
            estimate=estimate,
            specification=specification,
        )
        return

    await message.answer(format_estimate_summary(estimate, specification))


async def _send_estimate_with_contract(
    message: Message,
    *,
    customer_id: int,
    database: Database,
    estimate: dict,
    specification: dict,
) -> None:
    contract_file_path: str | None = None
    try:
        contract_file_path = await generate_customer_contract_docx(
            customer_id,
            database,
            estimate=estimate,
        )
    except (
        CustomerNotFoundError,
        CustomerSpecificationMissingError,
        SpecificationNotFoundError,
        TemplateNotFoundError,
    ) as error:
        logger.exception(
            "Contract generation failed after estimate for customer_id=%s: %s",
            customer_id,
            error,
        )
    except Exception:
        logger.exception(
            "Contract generation failed after estimate for customer_id=%s",
            customer_id,
        )

    summary = format_estimate_summary(estimate, specification)
    if contract_file_path:
        summary = summary.replace("Смета создана.", "Смета и договор сформированы.", 1)

    await message.answer(summary)

    if contract_file_path:
        await message.answer_document(
            FSInputFile(contract_file_path),
            caption="Готово. Договор сформирован.",
        )
        return

    await message.answer(CONTRACT_GENERATION_FAILED_REPLY)


def _parse_fsm_estimate_values(data: dict) -> tuple | None:
    engine_power = parse_positive_decimal(data.get("engine_power"))
    exchange_rate = parse_positive_decimal(data.get("exchange_rate"))
    inspect_transport_price = parse_non_negative_decimal(data.get("inspect_transport_price"))
    transit_declaration_price = parse_non_negative_decimal(data.get("transit_declaration_price"))
    insurance_shipment = parse_non_negative_decimal(data.get("insurance_shipment"))
    custom_clearing = parse_non_negative_decimal(data.get("custom_clearing"))

    if (
        engine_power is None
        or exchange_rate is None
        or inspect_transport_price is None
        or transit_declaration_price is None
        or insurance_shipment is None
        or custom_clearing is None
    ):
        return None

    return (
        engine_power,
        exchange_rate,
        inspect_transport_price,
        transit_declaration_price,
        insurance_shipment,
        custom_clearing,
    )
