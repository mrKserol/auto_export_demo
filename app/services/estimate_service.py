from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.database import Database

BANK_COMMISSION_RATE = Decimal("0.025")
DEFAULT_PRICE_CURRENCY = "CNY"

SPEC_FIELD_LABELS_FOR_ESTIMATE = {
    "price": "бюджет",
    "year": "год",
    "eng_capacity": "объём",
    "eng_type": "тип ДВС",
}


def parse_positive_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    text = value.strip().replace(",", ".")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number <= 0:
        return None
    return number


def calculate_car_age_category(year: int) -> str:
    current_year = datetime.now().year
    age = current_year - year
    if age < 3:
        return "до 3 лет"
    if age <= 5:
        return "от 3 до 5 лет"
    return "старше 5 лет"


def get_missing_specification_fields_for_estimate(specification: dict) -> list[str]:
    missing: list[str] = []
    for field, label in SPEC_FIELD_LABELS_FOR_ESTIMATE.items():
        if not (specification.get(field) or "").strip():
            missing.append(label)
    return missing


def calculate_estimate(
    *,
    specification: dict,
    engine_power: Decimal,
    exchange_rate: Decimal,
) -> dict:
    year_text = (specification.get("year") or "").strip()
    year = int(year_text)
    car_age_category = calculate_car_age_category(year)

    price_abroad = parse_positive_decimal(specification.get("price"))
    if price_abroad is None:
        raise ValueError("Specification price is invalid")

    price_currency = (specification.get("price_currency") or DEFAULT_PRICE_CURRENCY).strip() or DEFAULT_PRICE_CURRENCY
    price_rub = _quantize_money(price_abroad * exchange_rate)
    bank_commission = _quantize_money(price_rub * BANK_COMMISSION_RATE)
    transit_declaration_price = Decimal("0")
    insurance_shipment = Decimal("0")
    custom_clearing = Decimal("0")
    custom_duties = Decimal("0")
    contractor_comission = Decimal("0")
    total_rub = _quantize_money(
        price_rub
        + bank_commission
        + transit_declaration_price
        + insurance_shipment
        + custom_clearing
        + custom_duties
        + contractor_comission
    )

    return {
        "engine_power": _quantize_decimal(engine_power, 2),
        "car_age_category": car_age_category,
        "price_abroad": price_abroad,
        "price_currency": price_currency,
        "exchange_rate": _quantize_decimal(exchange_rate, 6),
        "price_rub": price_rub,
        "bank_commission": bank_commission,
        "transit_declaration_price": transit_declaration_price,
        "insurance_shipment": insurance_shipment,
        "custom_clearing": custom_clearing,
        "custom_duties": custom_duties,
        "contractor_comission": contractor_comission,
        "total_rub": total_rub,
        "brand": specification.get("brand") or "",
        "model": specification.get("model") or "",
        "year": year_text,
    }


async def create_estimate(
    database: Database,
    *,
    customer_id: int,
    specification_id: int,
    specification: dict,
    engine_power: Decimal,
    exchange_rate: Decimal,
) -> dict:
    calculated = calculate_estimate(
        specification=specification,
        engine_power=engine_power,
        exchange_rate=exchange_rate,
    )
    estimate_id = await database.create_estimate(
        customer_id=customer_id,
        specification_id=specification_id,
        data=calculated,
    )
    estimate = await database.get_estimate_by_id(estimate_id)
    if not estimate:
        raise RuntimeError("Failed to load created estimate")
    return estimate


async def get_estimate_by_customer_id(database: Database, customer_id: int) -> dict | None:
    return await database.get_estimate_by_customer_id(customer_id)


async def get_estimate_by_id(database: Database, estimate_id: int) -> dict | None:
    return await database.get_estimate_by_id(estimate_id)


def format_estimate_summary(estimate: dict, specification: dict | None = None) -> str:
    brand = (specification or estimate).get("brand") or estimate.get("brand") or "—"
    model = (specification or estimate).get("model") or estimate.get("model") or "—"
    year = (specification or estimate).get("year") or estimate.get("year") or "—"

    return (
        "Смета создана.\n\n"
        f"Автомобиль: {brand} {model} {year}\n"
        f"Возраст: {estimate.get('car_age_category') or '—'}\n"
        f"Стоимость авто: {_format_amount(estimate.get('price_abroad'))} "
        f"{estimate.get('price_currency') or DEFAULT_PRICE_CURRENCY}\n"
        f"Курс: {_format_amount(estimate.get('exchange_rate'))}\n"
        f"Стоимость авто в RUB: {_format_amount(estimate.get('price_rub'))}\n"
        f"Банковская комиссия 2,5%: {_format_amount(estimate.get('bank_commission'))}\n"
        f"Итого: {_format_amount(estimate.get('total_rub'))} RUB"
    )


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_decimal(value: Decimal, places: int) -> Decimal:
    quantizer = Decimal("1").scaleb(-places)
    return value.quantize(quantizer, rounding=ROUND_HALF_UP)


def _format_amount(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        normalized = value.normalize()
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    return str(value)
