from __future__ import annotations

from dataclasses import dataclass, field

from app.services.customer_extraction_service import person_names_match
from app.services.name_transliteration_service import apply_name_transliteration
from app.services.validation_service import normalize_passport


PREVIEW_FIELDS = (
    ("last_name", "Фамилия"),
    ("first_name", "Имя"),
    ("surname", "Отчество"),
    ("passport", "Паспорт"),
    ("date_issue", "Дата выдачи"),
    ("department_code", "Код подразделения"),
    ("birth_date", "Дата рождения"),
    ("birth_place", "Место рождения"),
    ("registration_address", "Адрес регистрации"),
    ("ipain", "СНИЛС"),
    ("tin", "ИНН"),
)


@dataclass
class AssembledCustomerData:
    fields: dict[str, str | None]
    warnings: list[str] = field(default_factory=list)
    source_file_ids: dict[str, int | None] = field(default_factory=dict)


def extract_fields_from_file_row(file_row: dict) -> dict:
    extracted = file_row.get("extracted_json")
    if not isinstance(extracted, dict):
        return {}
    fields = extracted.get("fields")
    if isinstance(fields, dict):
        return dict(fields)
    return {
        key: value
        for key, value in extracted.items()
        if key not in {"document_type", "confidence", "warnings", "fields"}
    }


def get_passport_main_identity(files: list[dict]) -> dict[str, str] | None:
    for file_row in files:
        if file_row.get("detected_document_type") != "passport_main":
            continue
        fields = extract_fields_from_file_row(file_row)
        last_name = (fields.get("last_name") or "").strip()
        passport = normalize_passport(fields.get("passport"))
        if last_name and passport:
            return {"last_name": last_name, "passport": passport}
    return None


def assemble_customer_data_from_batch_files(
    files: list[dict],
    *,
    kit_warnings: list[str] | None = None,
) -> AssembledCustomerData:
    by_type: dict[str, dict] = {}
    source_file_ids: dict[str, int | None] = {}
    for file_row in files:
        doc_type = file_row.get("detected_document_type")
        if doc_type in {
            "passport_main",
            "passport_registration",
            "snils",
            "tin",
        } and doc_type not in by_type:
            by_type[doc_type] = extract_fields_from_file_row(file_row)
            source_file_ids[doc_type] = int(file_row["id"])

    passport_fields = by_type.get("passport_main") or {}
    registration_fields = by_type.get("passport_registration") or {}
    snils_fields = by_type.get("snils") or {}
    tin_fields = by_type.get("tin") or {}

    fields: dict[str, str | None] = {
        "passport": passport_fields.get("passport"),
        "last_name": passport_fields.get("last_name"),
        "first_name": passport_fields.get("first_name"),
        "surname": passport_fields.get("surname"),
        "date_issue": passport_fields.get("date_issue"),
        "by_whom_issued": passport_fields.get("by_whom_issued"),
        "department_code": passport_fields.get("department_code"),
        "birth_date": passport_fields.get("birth_date"),
        "birth_place": passport_fields.get("birth_place"),
        "registration_address": registration_fields.get("registration_address"),
        "ipain": snils_fields.get("ipain"),
        "tin": tin_fields.get("tin"),
        "phone": None,
        "email": None,
    }

    warnings = list(kit_warnings or [])
    if passport_fields and snils_fields and not person_names_match(
        passport_fields,
        snils_fields,
    ):
        warning = "ФИО паспорта не совпадает с ФИО в СНИЛС"
        if warning not in warnings:
            warnings.append(warning)
    if passport_fields and tin_fields and not person_names_match(
        passport_fields,
        tin_fields,
    ):
        warning = "ФИО паспорта не совпадает с ФИО в ИНН"
        if warning not in warnings:
            warnings.append(warning)

    passport_number = normalize_passport(passport_fields.get("passport"))
    registration_passport = normalize_passport(registration_fields.get("passport"))
    if (
        passport_number
        and registration_passport
        and passport_number != registration_passport
    ):
        warning = "Номер паспорта на странице регистрации не совпадает с основным"
        if warning not in warnings:
            warnings.append(warning)

    return AssembledCustomerData(
        fields=fields,
        warnings=warnings,
        source_file_ids=source_file_ids,
    )


def format_customer_data_preview(
    data: AssembledCustomerData,
    *,
    customer_path: str | None = None,
    kit_message: str | None = None,
) -> str:
    lines = ["Распознанные данные клиента:", ""]
    for key, label in PREVIEW_FIELDS:
        value = data.fields.get(key)
        display = value if value else "не распознано"
        lines.append(f"{label}: {display}")

    if customer_path:
        lines.extend(["", f"Папка на Яндекс Диске: {customer_path}"])
    if data.warnings:
        lines.extend(["", "Предупреждения:"])
        lines.extend(f"• {warning}" for warning in data.warnings)
    if kit_message:
        lines.extend(["", kit_message])
    return "\n".join(lines)


def customer_fields_for_create(
    data: AssembledCustomerData,
    *,
    customer_path: str | None,
    overrides: dict | None = None,
) -> dict:
    merged = dict(data.fields)
    if overrides:
        for key, value in overrides.items():
            if value is not None and str(value).strip() != "":
                merged[key] = value
    payload = {
        "passport": merged.get("passport"),
        "first_name": merged.get("first_name"),
        "last_name": merged.get("last_name"),
        "surname": merged.get("surname"),
        "tin": merged.get("tin"),
        "ipain": merged.get("ipain"),
        "phone": merged.get("phone"),
        "email": merged.get("email"),
        "by_whom_issued": merged.get("by_whom_issued"),
        "date_issue": merged.get("date_issue"),
        "registration_address": merged.get("registration_address"),
        "department_code": merged.get("department_code"),
        "customer_path": customer_path,
        "specification_id": None,
    }
    return apply_name_transliteration(payload)


@dataclass(frozen=True)
class CustomerBatchDataService:
    def assemble_from_files(
        self,
        files: list[dict],
        *,
        kit_warnings: list[str] | None = None,
    ) -> AssembledCustomerData:
        return assemble_customer_data_from_batch_files(
            files,
            kit_warnings=kit_warnings,
        )
