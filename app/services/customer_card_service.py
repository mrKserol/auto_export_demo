from __future__ import annotations

from app.database import Database


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
