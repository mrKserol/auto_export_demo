from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


SPEC_FIELD_LABELS = {
    "brand": "Марка",
    "model": "Модель",
    "year": "Год",
    "eng_capacity": "Объём двигателя в литрах",
    "eng_type": "Тип ДВС",
    "drive": "Привод",
    "transmission": "КПП",
    "color": "Цвет",
    "complectation": "Комплектация",
    "mileage": "Пробег в км.",
    "price": "Бюджет / стоимость",
}

SPEC_EDIT_BUTTON_LABELS = {
    "brand": "Изменить марку",
    "model": "Изменить модель",
    "year": "Изменить год",
    "eng_capacity": "Изменить объём",
    "eng_type": "Изменить тип ДВС",
    "drive": "Изменить привод",
    "transmission": "Изменить КПП",
    "color": "Изменить цвет",
    "complectation": "Изменить комплектацию",
    "mileage": "Изменить пробег",
    "price": "Изменить бюджет",
}

SPEC_FIELD_ORDER = (
    "brand",
    "model",
    "year",
    "eng_capacity",
    "eng_type",
    "drive",
    "transmission",
    "color",
    "complectation",
    "mileage",
    "price",
)


def format_specification_text(specification: dict) -> str:
    return (
        "Желаемый автомобиль:\n"
        f"Марка: {specification.get('brand') or '—'}\n"
        f"Модель: {specification.get('model') or '—'}\n"
        f"Год: {specification.get('year') or '—'}\n"
        f"Объём: {specification.get('eng_capacity') or '—'}\n"
        f"Тип ДВС: {specification.get('eng_type') or '—'}\n"
        f"Привод: {specification.get('drive') or '—'}\n"
        f"КПП: {specification.get('transmission') or '—'}\n"
        f"Цвет: {specification.get('color') or '—'}\n"
        f"Комплектация: {specification.get('complectation') or '—'}\n"
        f"Пробег: {specification.get('mileage') or '—'}\n"
        f"Бюджет: {specification.get('price') or '—'}"
    )


def build_specification_edit_keyboard(
    specification_id: int,
    customer_id: int,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=SPEC_EDIT_BUTTON_LABELS[field_name],
            callback_data=f"edit_spec_field:{specification_id}:{field_name}",
        )
        for field_name in SPEC_FIELD_ORDER
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"spec_edit_back:{customer_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_specification_field_value(
    field_name: str,
    raw_value: str,
) -> tuple[str | None, str | None]:
    from app.services.validation_service import validate_specification_field

    return validate_specification_field(field_name, raw_value)
