from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from docxtpl import DocxTemplate

from app.database import Database
from app.services.customer_card_service import format_customer_fio

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path("templates/customer_contract_template.docx")
GENERATED_DIR = Path("generated_contracts")

_INVALID_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')


class CustomerNotFoundError(Exception):
    pass


class CustomerSpecificationMissingError(Exception):
    pass


class SpecificationNotFoundError(Exception):
    pass


class TemplateNotFoundError(Exception):
    pass


async def generate_customer_contract_docx(
    customer_id: int,
    database: Database,
) -> str:
    if not TEMPLATE_PATH.is_file():
        raise TemplateNotFoundError(
            "Шаблон договора не найден. Добавьте файл templates/customer_contract_template.docx."
        )

    customer = await database.get_customer_by_id(customer_id)
    if not customer:
        raise CustomerNotFoundError("Клиент не найден.")

    specification_id = customer.get("specification_id")
    if not specification_id:
        raise CustomerSpecificationMissingError(
            "У клиента нет спецификации авто. Сначала добавьте спецификацию."
        )

    specification = await database.get_specification_by_id(int(specification_id))
    if not specification:
        raise SpecificationNotFoundError("Спецификация клиента не найдена.")

    context = _build_context(customer, specification)
    output_path = _build_output_path(customer, specification)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_render_docx, context, output_path)
    return str(output_path)


def build_short_name(
    last_name: str | None,
    first_name: str | None,
    surname: str | None,
) -> str:
    last_name = (last_name or "").strip()
    first_name = (first_name or "").strip()
    surname = (surname or "").strip()

    parts: list[str] = []
    if last_name:
        parts.append(last_name)
    if first_name:
        parts.append(f"{first_name[0].upper()}.")
    if surname:
        parts.append(f"{surname[0].upper()}.")

    return " ".join(parts)


def _build_context(customer: dict, specification: dict) -> dict:
    full_name = format_customer_fio(customer)
    if full_name == "—":
        full_name = ""

    short_name = build_short_name(
        customer.get("last_name"),
        customer.get("first_name"),
        customer.get("surname"),
    )

    return {
        "customer": {
            "id": customer["id"],
            "passport": customer.get("passport") or "",
            "first_name": customer.get("first_name") or "",
            "last_name": customer.get("last_name") or "",
            "surname": customer.get("surname") or "",
            "full_name": full_name,
            "short_name": short_name,
            "by_whom_issued": customer.get("by_whom_issued") or "",
            "date_issue": customer.get("date_issue") or "",
            "department_code": customer.get("department_code") or "",
            "registration_address": customer.get("registration_address") or "",
            "tin": customer.get("tin") or "",
            "ipain": customer.get("ipain") or "",
            "phone": customer.get("phone") or "",
            "email": customer.get("email") or "",
        },
        "specification": {
            "id": specification["id"],
            "brand": specification.get("brand") or "",
            "model": specification.get("model") or "",
            "year": specification.get("year") or "",
            "eng_capacity": specification.get("eng_capacity") or "",
            "eng_type": specification.get("eng_type") or "",
            "drive": specification.get("drive") or "",
            "transmission": specification.get("transmission") or "",
            "color": specification.get("color") or "",
            "complectation": specification.get("complectation") or "",
            "mileage": specification.get("mileage") or "",
            "price": specification.get("price") or "",
        },
    }


def _sanitize_filename_part(value: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("", value)
    return cleaned.replace(" ", "_").strip("_")


def _build_output_filename(customer: dict, specification: dict) -> str:
    parts = [
        "Договор",
        "Клиент",
        str(customer["id"]),
        _sanitize_filename_part(customer.get("last_name") or "БезФамилии"),
        _sanitize_filename_part(specification.get("brand") or "БезМарки"),
        _sanitize_filename_part(specification.get("model") or "БезМодели"),
    ]
    filename = "_".join(parts) + ".docx"
    max_length = 200
    if len(filename) > max_length:
        filename = filename[: max_length - 5].rstrip("_") + ".docx"
    return filename


def _build_output_path(customer: dict, specification: dict) -> Path:
    filename = _build_output_filename(customer, specification)
    return GENERATED_DIR / filename


def _render_docx(context: dict, output_path: Path) -> None:
    doc = DocxTemplate(str(TEMPLATE_PATH))
    doc.render(context)
    doc.save(str(output_path))
