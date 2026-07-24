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
    # Top admin action: open customer edit miniapp
    if is_admin:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Изменить данные клиента",
                    callback_data=f"customer_edit:{customer_id}",
                )
            ]
        )

    if customer.get("specification_id"):
        if has_estimate:
            # arrange in two rows:
            # [Договор] [Смета]
            # [Пересоздать смету] [Удалить смету]
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Распечатать Договор",
                        callback_data=f"contract:generate:{customer_id}",
                    ),
                    InlineKeyboardButton(
                        text="Распечатать Смету",
                        callback_data=f"estimate:file:{customer_id}",
                    ),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Пересоздать смету",
                        callback_data=f"estimate:recreate:{customer_id}",
                    ),
                    InlineKeyboardButton(
                        text="Удалить смету",
                        callback_data=f"estimate:delete:{customer_id}",
                    ),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Создать смету",
                        callback_data=f"estimate:create:{customer_id}",
                    ),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Изменить спецификацию",
                    callback_data=f"customer_edit_spec:{customer_id}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Удалить спецификацию",
                    callback_data=f"customer_delete_spec:{customer_id}",
                ),
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
                    text="Удалить клиента",
                    callback_data=f"customer_delete:{customer_id}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_estimate_actions_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Распечатать Договор",
                    callback_data=f"contract:generate:{customer_id}",
                ),
                InlineKeyboardButton(
                    text="Распечатать Смету",
                    callback_data=f"estimate:file:{customer_id}",
                ),
            ]
        ]
    )


def build_estimate_creation_method_keyboard(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Заполнить форму",
                    callback_data=f"estimate:form:{customer_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📎 Загрузить смету",
                    callback_data=f"estimate:upload:{customer_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"estimate:cancel:{customer_id}",
                )
            ],
        ]
    )


async def customer_has_estimate(customer: dict, database: Database) -> bool:
    specification_id = customer.get("specification_id")
    if not specification_id:
        return False
    estimate = await database.get_estimate_by_specification_id(int(specification_id))
    return bool(estimate)


def format_customer_fio(customer: dict) -> str:
    fio_parts = [
        customer.get("last_name"),
        customer.get("first_name"),
        customer.get("surname"),
    ]
    return " ".join(part for part in fio_parts if part) or "—"


def format_customer_fio_translit(customer: dict) -> str:
    fio_parts = [
        customer.get("last_name_translit"),
        customer.get("first_name_translit"),
        customer.get("surname_translit"),
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
        f"ФИО (лат.): {format_customer_fio_translit(customer)}\n"
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
