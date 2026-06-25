from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database import Database
from app.services.specification_edit_service import format_specification_text


def build_customer_card_keyboard(
    customer: dict,
    *,
    is_admin: bool,
    has_estimate: bool = False,
) -> InlineKeyboardMarkup:
    customer_id = int(customer["id"])
    rows: list[list[InlineKeyboardButton]] = []

    if customer.get("specification_id"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="Сформировать договор",
                    callback_data=f"customer_generate_contract:{customer_id}",
                ),
                InlineKeyboardButton(
                    text="Изменить спецификацию",
                    callback_data=f"customer_edit_spec:{customer_id}",
                ),
            ]
        )
        if has_estimate:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Показать смету",
                        callback_data=f"estimate:show:{customer_id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Создать смету",
                        callback_data=f"estimate:create:{customer_id}",
                    )
                ]
            )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Добавить спецификацию",
                    callback_data=f"customer_add_spec:{customer_id}",
                )
            ]
        )

    if is_admin:
        rows.append(
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
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_create_estimate_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать смету",
                    callback_data=f"estimate:create:{customer_id}",
                )
            ]
        ]
    )


def build_show_estimate_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Показать смету",
                    callback_data=f"estimate:show:{customer_id}",
                )
            ]
        ]
    )


def format_customer_fio(customer: dict) -> str:
    fio_parts = [
        customer.get("last_name"),
        customer.get("first_name"),
        customer.get("surname"),
    ]
    return " ".join(part for part in fio_parts if part) or "—"


async def build_specification_block(
    specification_id: int | None,
    database: Database,
) -> str:
    if not specification_id:
        return "Желаемый автомобиль: не заполнен"

    specification = await database.get_specification_by_id(int(specification_id))
    if not specification:
        return "Желаемый автомобиль: не заполнен"

    return format_specification_text(specification)


async def build_customer_card(customer: dict, database: Database) -> str:
    return (
        f"Клиент #{customer.get('id')}\n\n"
        f"Паспорт: {customer.get('passport') or '—'}\n"
        f"ФИО: {format_customer_fio(customer)}\n"
        f"Дата выдачи: {customer.get('date_issue') or '—'}\n"
        f"Кем выдан: {customer.get('by_whom_issued') or '—'}\n"
        f"Код подразделения: {customer.get('department_code') or '—'}\n"
        f"Адрес регистрации: {customer.get('registration_address') or '—'}\n"
        f"СНИЛС: {customer.get('ipain') or '—'}\n"
        f"ИНН: {customer.get('tin') or '—'}\n"
        f"Телефон: {customer.get('phone') or '—'}\n"
        f"Email: {customer.get('email') or '—'}\n"
        f"Specification ID: {customer.get('specification_id') or '—'}\n\n"
        f"{await build_specification_block(customer.get('specification_id'), database)}"
    )


async def build_specification_created_reply(
    customer: dict,
    database: Database,
) -> str:
    return (
        "✅ Спецификация добавлена\n\n"
        f"Клиент: {format_customer_fio(customer)}\n"
        f"Паспорт: {customer.get('passport') or '—'}\n"
        f"Specification ID: {customer.get('specification_id') or '—'}\n\n"
        f"{await build_specification_block(customer.get('specification_id'), database)}"
    )
