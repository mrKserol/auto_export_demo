from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from docxtpl import DocxTemplate  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - environment-dependent dependency
    DocxTemplate = None  # type: ignore[assignment]

from app.database import Database
from app.services.customer_card_service import format_customer_fio

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path("templates/customer_contract_template.docx")
GENERATED_DIR = Path("generated_contracts")

_INVALID_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')

RUSSIAN_MONTHS_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


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
    *,
    estimate: dict | None = None,
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

    if estimate is None:
        estimate = await database.get_estimate_by_customer_id(customer_id)
    if estimate is None and specification_id:
        estimate = await database.get_estimate_by_specification_id(int(specification_id))

    context = _build_context(customer, specification, estimate)
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


def get_contract_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("Europe/Moscow"))
    except Exception:
        return datetime.now()


def build_russian_contract_date(dt: datetime | None = None) -> str:
    if dt is None:
        dt = get_contract_now()

    day = f"{dt.day:02d}"
    month = RUSSIAN_MONTHS_GENITIVE[dt.month]
    year = dt.year

    return f"«{day}» {month} {year} г."


def build_numeric_contract_date(dt: datetime | None = None) -> str:
    if dt is None:
        dt = get_contract_now()

    return dt.strftime("%d.%m.%Y")


def _build_context(customer: dict, specification: dict, estimate: dict | None = None) -> dict:
    full_name = format_customer_fio(customer)
    if full_name == "—":
        full_name = ""

    short_name = build_short_name(
        customer.get("last_name"),
        customer.get("first_name"),
        customer.get("surname"),
    )
    contract_now = get_contract_now()

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
        "contract": {
            "current_date_text": build_russian_contract_date(contract_now),
            "current_date_numeric": build_numeric_contract_date(contract_now),
        },
        "estimates": build_estimates_context(estimate),
    }


def build_estimates_context(estimate: dict | None) -> dict[str, str]:
    if not estimate:
        return {
            "price_currency": "",
            "exchange_rate": "",
            "price_rub": "",
            "price_abroad": "",
            "bank_commission": "",
            "transit_declaration_price": "",
            "insurance_shipment": "",
            "customs_total": "",
            "custom_clearing": "",
            "contractor_comission": "",
            "contractor_comission_prepayment": "",
            "contractor_comission_postpayment": "",
        }

    return {
        "price_currency": estimate.get("price_currency") or "",
        "exchange_rate": format_template_number(estimate.get("exchange_rate")),
        "price_rub": format_template_money(estimate.get("price_rub")),
        "price_abroad": format_template_money(estimate.get("price_abroad")),
        "bank_commission": format_template_money(estimate.get("bank_commission")),
        "transit_declaration_price": format_template_money(
            estimate.get("transit_declaration_price")
        ),
        "insurance_shipment": format_template_money(estimate.get("insurance_shipment")),
        "customs_total": format_template_money(estimate.get("customs_total")),
        "custom_clearing": format_template_money(estimate.get("custom_clearing")),
        "contractor_comission": format_template_money(estimate.get("contractor_comission")),
        "contractor_comission_prepayment": format_template_money(
            estimate.get("contractor_comission_prepayment")
        ),
        "contractor_comission_postpayment": format_template_money(
            estimate.get("contractor_comission_postpayment")
        ),
    }


def format_template_number(value: object) -> str:
    if value is None:
        return ""
    try:
        number = Decimal(str(value))
    except Exception:
        return str(value)

    normalized = number.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_template_money(value: object) -> str:
    text = format_template_number(value)
    if not text:
        return ""
    return text


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
    if DocxTemplate is None:
        raise RuntimeError("DOCX generation requires 'docxtpl' dependency")
    doc = DocxTemplate(str(TEMPLATE_PATH))
    doc.render(context)
    doc.save(str(output_path))
